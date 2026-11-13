#!/usr/bin/env python3
"""Publish an upstream mod_build_once.py result to the Starling gateway.

The bearer key is read only by this post-build process. Parts are zero-indexed,
1 MiB PUTs; status callbacks use the upstream status JSON shape.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from validate_inputs import validate_ask, validate_gateway

CHUNK_SIZE = 1_048_576
MAX_LIBRARY_SIZE = 256 * 1024 * 1024
HTTP_TIMEOUT = 30
RETRIES = 3
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PublishError(Exception):
    """Sanitized failure message safe to print in public workflow logs."""


def _no_redirects() -> urllib.request.OpenerDirector:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
            return None

    # Do not route the shared credential through ambient proxy environment variables.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def _request(opener: Any, url: str, method: str, token: str, body: bytes, content_type: str) -> None:
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "User-Agent": "starling-native-builds/1",
        },
        method=method,
    )
    for attempt in range(RETRIES):
        try:
            response = opener.open(request, timeout=HTTP_TIMEOUT) if hasattr(opener, "open") else opener(request, timeout=HTTP_TIMEOUT)
            with response:
                status = response.status if hasattr(response, "status") else response.getcode()
                if 200 <= status < 300:
                    return
                if status < 500 or attempt == RETRIES - 1:
                    raise PublishError(f"gateway returned HTTP {status}")
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code < 500 or attempt == RETRIES - 1:
                raise PublishError(f"gateway returned HTTP {code}") from None
        except urllib.error.URLError:
            if attempt == RETRIES - 1:
                raise PublishError("gateway request failed") from None
        except TimeoutError:
            if attempt == RETRIES - 1:
                raise PublishError("gateway request timed out") from None
        if attempt < RETRIES - 1:
            time.sleep(0.25 * (attempt + 1))
    raise PublishError("gateway request failed")


def _status_url(gateway: str, ask: str) -> str:
    return f"{gateway}/v1/internal/job/{ask}/status"


def _post_status(opener: Any, gateway: str, ask: str, token: str, payload: dict[str, Any]) -> None:
    try:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise PublishError("build metadata is not valid JSON") from None
    if len(body) > 64 * 1024:
        raise PublishError("build metadata is too large")
    _request(opener, _status_url(gateway, ask), "POST", token, body, "application/json")


def _safe_error(value: Any, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    clean = " ".join("".join(ch for ch in value if ch >= " " and ch != "\x7f").split())
    return (clean[:1000] or default)


def _read_status(result_dir: pathlib.Path) -> dict[str, Any] | None:
    path = result_dir / "status.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _failed_payload(status: dict[str, Any] | None, build_outcome: str) -> dict[str, Any]:
    if build_outcome != "success":
        error = "Build did not complete successfully."
    elif status and status.get("state") == "failed":
        error = _safe_error(status.get("error"), "The build failed.")
    else:
        error = "Build result was unavailable or invalid."
    settled = status.get("settled") is True if status and status.get("state") == "failed" else False
    return {"state": "failed", "error": error, "settled": settled}


def publish(
    gateway_value: str,
    ask_value: str,
    token: str,
    result_path: str | pathlib.Path,
    build_outcome: str,
    opener: Any | None = None,
) -> bool:
    gateway = validate_gateway(gateway_value)
    ask = validate_ask(ask_value)
    if not token:
        raise PublishError("GATEWAY_UPLOAD_KEY is missing")
    transport = opener if opener is not None else _no_redirects()
    result_dir = pathlib.Path(result_path)
    status = _read_status(result_dir)
    if build_outcome != "success" or not status or status.get("state") != "done":
        payload = _failed_payload(status, build_outcome)
        _post_status(transport, gateway, ask, token, payload)
        print("Callback sent: failed.")
        return False

    library = result_dir / "libhorizon_android.so"
    try:
        if library.is_symlink() or not library.is_file():
            raise PublishError("built library is missing or invalid")
        stat = library.stat()
        if stat.st_size <= 0 or stat.st_size > MAX_LIBRARY_SIZE:
            raise PublishError("built library size is invalid")
        expected_size = status.get("size")
        expected_hash = status.get("sha256")
        metadata = status.get("build")
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size != stat.st_size:
            raise PublishError("build result size does not match metadata")
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            raise PublishError("build result hash metadata is invalid")
        if not isinstance(metadata, dict):
            raise PublishError("build metadata is invalid")
        # Verify bytes from disk before notifying the gateway or transmitting chunks.
        digest = hashlib.sha256()
        with library.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != expected_hash:
            raise PublishError("build result hash does not match metadata")
        parts = (stat.st_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        done_payload = {
            "state": "done",
            "sha256": expected_hash,
            "size": stat.st_size,
            "build": metadata,
            "parts": parts,
            "chunk_size": CHUNK_SIZE,
        }
        # Inform the worker that the successful build is ready for transfer.
        _post_status(transport, gateway, ask, token, {"state": "building"})
        sent_digest = hashlib.sha256()
        sent_size = 0
        with library.open("rb") as source:
            for index in range(parts):
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    raise PublishError("built library changed during upload")
                _request(
                    transport,
                    f"{gateway}/v1/internal/job/{ask}/part/{index}",
                    "PUT",
                    token,
                    chunk,
                    "application/octet-stream",
                )
                sent_digest.update(chunk)
                sent_size += len(chunk)
        if sent_size != stat.st_size or sent_digest.hexdigest() != expected_hash:
            raise PublishError("uploaded bytes did not match build metadata")
        _post_status(transport, gateway, ask, token, done_payload)
        print(f"Callback sent: done ({parts} part(s), {stat.st_size} bytes).")
        return True
    except PublishError as exc:
        # Best-effort failure callback after any partial transfer; do not print
        # exception details from urllib because they may contain request data.
        try:
            _post_status(transport, gateway, ask, token, {"state": "failed", "error": _safe_error(str(exc), "Result upload failed."), "settled": False})
        except Exception:
            pass
        raise
    except (OSError, ValueError, TypeError) as exc:
        try:
            _post_status(transport, gateway, ask, token, {"state": "failed", "error": "Result upload failed.", "settled": False})
        except Exception:
            pass
        raise PublishError("result upload failed") from None


def main() -> int:
    try:
        complete = publish(
            os.environ.get("GATEWAY", ""),
            os.environ.get("ASK", ""),
            os.environ.get("GATEWAY_UPLOAD_KEY", ""),
            os.environ.get("RESULT_DIR", "result"),
            os.environ.get("BUILD_STEP_OUTCOME", ""),
        )
    except (PublishError, ValueError) as exc:
        print(f"Publish warning: {exc}", file=sys.stderr)
        return 1
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
