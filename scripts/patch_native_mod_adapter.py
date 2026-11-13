#!/usr/bin/env python3
"""Install narrow Horizonmatica 0.3.2 target aliases into Starling's adapter."""
from __future__ import annotations

import ast
import pathlib
import sys

ALIASES = {
    "horizon_client::game::play_input::Game<'_>::play_mouse":
        "horizon_client::game::Game<'_>::play_mouse",
    "horizon_client::game::pointer_capture::Game<'_>::pauses_game":
        "horizon_client::game::Game<'_>::pauses_game",
}
CHECKOUT_LINE = 'git -C "$root/source" checkout -q --detach "$commit" || exit 1'
HOOK_LINE = (
    'python3 "${GITHUB_WORKSPACE:?}/scripts/patch_native_mod_adapter.py" '
    '"$root/source/packaging/android/native_mod_adapt.py"'
)


def patch_adapter(path: pathlib.Path) -> bool:
    """Add the exact old-to-current method paths to the adapter's RENAMED map."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "RENAMED" for target in node.targets)
    ]
    if len(assignments) != 1:
        raise RuntimeError(f"Expected one top-level RENAMED assignment in {path}")

    assignment = assignments[0]
    renamed = ast.literal_eval(assignment.value)
    if not isinstance(renamed, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in renamed.items()
    ):
        raise RuntimeError(f"RENAMED in {path} is not a string-to-string map")

    changed = False
    for old, new in ALIASES.items():
        if old in renamed and renamed[old] != new:
            raise RuntimeError(f"Conflicting mapping for {old!r} in {path}")
        if renamed.get(old) != new:
            renamed[old] = new
            changed = True
    if not changed:
        return False

    replacement = ["RENAMED = {\n"]
    replacement.extend(f"    {key!r}: {value!r},\n" for key, value in renamed.items())
    replacement.append("}\n")
    lines = text.splitlines(keepends=True)
    text = "".join(lines[: assignment.lineno - 1] + replacement + lines[assignment.end_lineno :])
    path.write_text(text, encoding="utf-8")
    return True


def install_sandbox_hook(path: pathlib.Path) -> bool:
    """Run the adapter patcher just after each sandbox wrapper checks out source."""
    text = path.read_text(encoding="utf-8")
    if HOOK_LINE in text:
        return False
    count = text.count(CHECKOUT_LINE)
    if count != 1:
        raise RuntimeError(
            f"Expected exactly one source checkout line in {path}, found {count}"
        )
    text = text.replace(CHECKOUT_LINE, f"{CHECKOUT_LINE}\n{HOOK_LINE}", 1)
    path.write_text(text, encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--install-sandbox-hook":
        path = pathlib.Path(argv[2])
        changed = install_sandbox_hook(path)
        print(f"native_mod_adapt: {'installed hook in' if changed else 'hook already present in'} {path}")
        return 0
    if len(argv) == 2:
        path = pathlib.Path(argv[1])
        changed = patch_adapter(path)
        print(f"native_mod_adapt: {'added Horizonmatica targets to' if changed else 'Horizonmatica targets already present in'} {path}")
        return 0
    print(
        "usage: patch_native_mod_adapter.py ADAPTER | "
        "--install-sandbox-hook SANDBOX_SCRIPT",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
