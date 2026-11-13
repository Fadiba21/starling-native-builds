#!/usr/bin/env python3
"""Install narrow compatibility aliases and staged-source shims for Starling 0.8.0 builds."""
from __future__ import annotations

import ast
import pathlib
import sys

from native_mod_source_compat import patch_source_compat

ALIASES = {
    "horizon_client::game::play_input::Game<'_>::play_mouse":
        "horizon_client::game::Game<'_>::play_mouse",
    "horizon_client::game::pointer_capture::Game<'_>::pauses_game":
        "horizon_client::game::Game<'_>::pauses_game",
}

# These source rewrites are deliberately limited to API changes whose replacement
# was verified against the pinned Starling 0.8.0 source.
SOURCE_REWRITES = (
    (
        r"(\.mob_hit\(\)\.map\(\|\(\s*\w+\s*,\s*\w+)\s*,\s*_\s*\)\|",
        r"\1)|",
    ),
    (
        r"dynamic_light::select_iter\(sources\.drain\(\.\.\),",
        "dynamic_light::select(&sources,",
    ),
)

CHECKOUT_LINE = 'git -C "$root/source" checkout -q --detach "$commit" || exit 1'
HOOK_LINE = (
    'python3 "${GITHUB_WORKSPACE:?}/scripts/patch_native_mod_adapter.py" '
    '"$root/source/packaging/android/native_mod_adapt.py"'
)
IMPORT_ANCHOR = "import sys\n"
SHIM_IMPORT = "from native_mod_source_compat import patch_source_compat\n"
MAIN_SOURCE_ANCHOR = (
    "    source, mods = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
)
SHIM_CALL = "    patch_source_compat(source)\n"
SOURCE_HELPER_RELATIVE = pathlib.Path("packaging/android/native_mod_source_compat.py")


def _top_level_assignment(tree: ast.Module, name: str, path: pathlib.Path) -> ast.Assign:
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]
    if len(assignments) != 1:
        raise RuntimeError(f"Expected one top-level {name} assignment in {path}")
    return assignments[0]


def _replace_assignments(
    text: str,
    replacements: list[tuple[ast.Assign, str]],
) -> str:
    lines = text.splitlines(keepends=True)
    for node, replacement in sorted(replacements, key=lambda item: item[0].lineno, reverse=True):
        text = "".join(
            lines[: node.lineno - 1]
            + [replacement]
            + lines[node.end_lineno :]
        )
        lines = text.splitlines(keepends=True)
    return text


def _insert_after(text: str, anchor: str, addition: str, path: pathlib.Path) -> tuple[str, bool]:
    if addition in text:
        return text, False
    count = text.count(anchor)
    if count != 1:
        raise RuntimeError(f"Expected one insertion anchor in {path}, found {count}")
    return text.replace(anchor, anchor + addition, 1), True


def patch_adapter(path: pathlib.Path) -> bool:
    """Add exact target aliases, mod-source rewrites, and staged-source shims."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    renamed_assignment = _top_level_assignment(tree, "RENAMED", path)
    renamed = ast.literal_eval(renamed_assignment.value)
    if not isinstance(renamed, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in renamed.items()
    ):
        raise RuntimeError(f"RENAMED in {path} is not a string-to-string map")

    changed_assignment = _top_level_assignment(tree, "CHANGED", path)
    changed = ast.literal_eval(changed_assignment.value)
    if not isinstance(changed, (tuple, list)) or not all(
        isinstance(entry, (tuple, list))
        and len(entry) == 2
        and all(isinstance(part, str) for part in entry)
        for entry in changed
    ):
        raise RuntimeError(f"CHANGED in {path} is not a sequence of string pairs")
    changed = [tuple(entry) for entry in changed]

    replacements: list[tuple[ast.Assign, str]] = []
    for old, new in ALIASES.items():
        if old in renamed and renamed[old] != new:
            raise RuntimeError(f"Conflicting mapping for {old!r} in {path}")
        renamed[old] = new
    replacement = ["RENAMED = {\n"]
    replacement.extend(f"    {key!r}: {value!r},\n" for key, value in renamed.items())
    replacement.append("}\n")
    if ast.literal_eval(renamed_assignment.value) != renamed:
        replacements.append((renamed_assignment, "".join(replacement)))

    for old, new in SOURCE_REWRITES:
        conflicting = [value for key, value in changed if key == old and value != new]
        if conflicting:
            raise RuntimeError(f"Conflicting source rewrite for {old!r} in {path}")
        if (old, new) not in changed:
            changed.append((old, new))
    replacement = ["CHANGED = (\n"]
    replacement.extend(f"    ({old!r}, {new!r}),\n" for old, new in changed)
    replacement.append(")\n")
    original_changed = [tuple(item) for item in ast.literal_eval(changed_assignment.value)]
    if original_changed != changed:
        replacements.append((changed_assignment, "".join(replacement)))

    patched = _replace_assignments(text, replacements) if replacements else text
    patched, _ = _insert_after(patched, IMPORT_ANCHOR, SHIM_IMPORT, path)
    patched, _ = _insert_after(patched, MAIN_SOURCE_ANCHOR, SHIM_CALL, path)
    ast.parse(patched, filename=str(path))
    if patched != text:
        path.write_text(patched, encoding="utf-8")
        return True
    return False


def install_source_helper(source_root: pathlib.Path) -> bool:
    """Copy the trusted shim module beside the adapter used inside the sandbox."""
    helper = pathlib.Path(__file__).with_name("native_mod_source_compat.py")
    target = pathlib.Path(source_root) / SOURCE_HELPER_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = helper.read_bytes()
    if target.exists() and target.read_bytes() == payload:
        return False
    target.write_bytes(payload)
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
        adapter_changed = patch_adapter(path)
        helper_changed = install_source_helper(path.parents[2])
        details = []
        if adapter_changed:
            details.append("adapter updated")
        if helper_changed:
            details.append("staged-source shim helper installed")
        print(
            "native_mod_adapt: "
            + (", ".join(details) if details else "compatibility patches already present")
            + f" for {path.parents[2]}"
        )
        return 0
    print(
        "usage: patch_native_mod_adapter.py ADAPTER | "
        "--install-sandbox-hook SANDBOX_SCRIPT",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
