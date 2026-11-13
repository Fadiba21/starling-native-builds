#!/usr/bin/env python3
"""Fail the workflow if the upstream build did not leave a valid result."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sys


def main() -> int:
    if os.environ.get("BUILD_STEP_OUTCOME") != "success":
        print("Build workflow step did not complete successfully.", file=sys.stderr)
        return 1
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "result")
    try:
        status_path = root / "status.json"
        library = root / "libhorizon_android.so"
        if status_path.is_symlink() or library.is_symlink() or not status_path.is_file() or not library.is_file():
            raise ValueError
        status = json.loads(status_path.read_text(encoding="utf-8"))
        size = library.stat().st_size
        if not isinstance(status, dict) or status.get("state") != "done":
            raise ValueError
        if isinstance(status.get("size"), bool) or status.get("size") != size or size <= 0:
            raise ValueError
        expected = status.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError
        digest = hashlib.sha256()
        with library.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        print("No valid successful native library result was produced.", file=sys.stderr)
        return 1
    print("Build result verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
