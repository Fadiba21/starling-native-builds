#!/usr/bin/env python3
"""Validate workflow_dispatch values without interpolating them into shell code."""
from __future__ import annotations

import ipaddress
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse

ASK_RE = re.compile(r"^[a-f0-9]{32}$")
DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
TOOLS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def validate_ask(ask: str) -> str:
    if not isinstance(ask, str) or not ASK_RE.fullmatch(ask):
        raise ValueError("ask must be exactly 32 lowercase hexadecimal characters")
    return ask


def validate_gateway(value: str) -> str:
    """Accept an HTTPS origin only; callers append fixed API paths themselves."""
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) <= 32 or c == "\\" for c in value):
        raise ValueError("gateway must be an HTTPS origin")
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("gateway must be an HTTPS origin") from exc
    if parsed.scheme.lower() != "https" or not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("gateway must be an HTTPS origin without credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("gateway must not contain a path, query, or fragment")
    if port not in (None, 443):
        raise ValueError("gateway must use HTTPS port 443")
    # Reject IP literals, localhost, percent-escaped authorities, and malformed DNS names.
    if "%" in parsed.netloc:
        raise ValueError("gateway host must be a DNS hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("gateway host must be a DNS hostname, not an IP address")
    if "." not in host or host.lower() == "localhost" or len(host) > 253:
        raise ValueError("gateway host must be a public-style DNS hostname")
    labels = host.split(".")
    if not labels or any(not DNS_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("gateway host is not a valid DNS hostname")
    # Require a canonical authority so weird userinfo/port encodings cannot pass parsing ambiguously.
    canonical_host = host.lower()
    expected_authority = canonical_host if port is None else f"{canonical_host}:443"
    if parsed.netloc.lower() != expected_authority:
        raise ValueError("gateway authority is not canonical")
    return f"https://{canonical_host}"


def validate_tools(tools: str) -> str:
    if not isinstance(tools, str) or not TOOLS_RE.fullmatch(tools):
        raise ValueError("tools must be a valid upstream git branch name")
    checked = subprocess.run(
        ["git", "check-ref-format", "--branch", tools],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if checked.returncode != 0:
        raise ValueError("tools must be a valid upstream git branch name")
    return tools


def validate_route(env: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    return {
        "ASK": validate_ask(source.get("ASK", "")),
        "GATEWAY": validate_gateway(source.get("GATEWAY", "")),
    }


def validate_inputs(env: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    route = validate_route(source)
    ask = route["ASK"]
    gateway = route["GATEWAY"]
    version = source.get("VERSION", "")
    try:
        versions = json.loads(pathlib.Path("versions.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("versions.json could not be read") from exc
    if not isinstance(versions, dict) or version not in versions:
        raise ValueError("version must exist in versions.json")
    commit = versions[version]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("version commit is malformed")
    tools = validate_tools(source.get("TOOLS", ""))
    return {"ASK": ask, "GATEWAY": gateway, "VERSION": version, "TOOLS": tools}


def main() -> int:
    try:
        if sys.argv[1:] == ["--route-only"]:
            validate_route()
        elif not sys.argv[1:]:
            validate_inputs()
        else:
            raise ValueError("unsupported validation mode")
    except ValueError as exc:
        print(f"Invalid workflow input: {exc}", file=sys.stderr)
        return 2
    print("Workflow inputs validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
