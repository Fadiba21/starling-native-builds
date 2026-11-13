#!/usr/bin/env python3
"""Install narrow compatibility aliases and API shims for Starling 0.8.0 builds."""
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

ACTION_SHIM = '''/// Compatibility for native mods using Horizon's former completion button.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Done,
}

'''
ACTION_BUTTON_SHIM = '''/// Compatibility helper for older native-mod settings screens.
/// The screen's own input handler remains responsible for dispatching its action.
#[allow(clippy::too_many_arguments)]
pub fn action_button(
    batch: &mut HudBatch,
    atlas: &HudAtlas,
    _action: crate::widgets::Action,
    x: f32,
    y: f32,
    w: f32,
    h: f32,
    label: &Text,
    active: bool,
    hover: bool,
) {
    button(batch, atlas, x, y, w, h, label, active, hover);
}

'''
OPTION_NAME_VALUE_SHIM = '''    /// Compatibility for native mods using the former option label formatter.
    pub fn option_name_value(name: Self, value: Self) -> Self {
        name.append(": ").append(value)
    }
'''


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


def patch_adapter(path: pathlib.Path) -> bool:
    """Add exact target aliases and source-rewrite rules to upstream's adapter."""
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
    renamed_changed = False
    for old, new in ALIASES.items():
        if old in renamed and renamed[old] != new:
            raise RuntimeError(f"Conflicting mapping for {old!r} in {path}")
        if renamed.get(old) != new:
            renamed[old] = new
            renamed_changed = True
    if renamed_changed:
        replacement = ["RENAMED = {\n"]
        replacement.extend(f"    {key!r}: {value!r},\n" for key, value in renamed.items())
        replacement.append("}\n")
        replacements.append((renamed_assignment, "".join(replacement)))

    changed_changed = False
    for old, new in SOURCE_REWRITES:
        conflicting = [value for key, value in changed if key == old and value != new]
        if conflicting:
            raise RuntimeError(f"Conflicting source rewrite for {old!r} in {path}")
        if (old, new) not in changed:
            changed.append((old, new))
            changed_changed = True
    if changed_changed:
        replacement = ["CHANGED = (\n"]
        replacement.extend(f"    ({old!r}, {new!r}),\n" for old, new in changed)
        replacement.append(")\n")
        replacements.append((changed_assignment, "".join(replacement)))

    if replacements:
        path.write_text(_replace_assignments(text, replacements), encoding="utf-8")
        return True
    return False


def _insert_before(path: pathlib.Path, anchor: str, addition: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if addition.strip() in text:
        return False
    count = text.count(anchor)
    if count != 1:
        raise RuntimeError(f"Expected one insertion anchor in {path}, found {count}")
    path.write_text(text.replace(anchor, addition + anchor, 1), encoding="utf-8")
    return True


def patch_source_compat(source: pathlib.Path) -> bool:
    """Restore small, behavior-preserving APIs needed by audited native mods."""
    changed = False
    widgets = source / "crates/horizon-client-render/src/widgets.rs"
    changed |= _insert_before(
        widgets,
        "/// A labelled widget in GUI pixels (the coordinates screens take input in).\n#[derive(Clone, Debug)]\npub struct Widget {",
        ACTION_SHIM,
    )

    ui = source / "crates/horizon-client-render/src/ui.rs"
    changed |= _insert_before(
        ui,
        "/// Button (AbstractButton): nine-slice sprite plus the centred label.\npub fn button(",
        ACTION_BUTTON_SHIM,
    )

    text = source / "crates/horizon-client-core/src/text.rs"
    changed |= _insert_before(text, "    pub fn empty() -> Self {", OPTION_NAME_VALUE_SHIM)

    remote = source / "crates/horizon-client/src/game/remote.rs"
    remote_text = remote.read_text(encoding="utf-8")
    old = "    fn menu_slot(menu: &str, slot: i32) -> Option<usize> {"
    new = "    pub(super) fn menu_slot(menu: &str, slot: i32) -> Option<usize> {"
    if new not in remote_text:
        count = remote_text.count(old)
        if count != 1:
            raise RuntimeError(f"Expected one menu_slot method in {remote}, found {count}")
        remote.write_text(remote_text.replace(old, new, 1), encoding="utf-8")
        changed = True
    return changed


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
        source_changed = patch_source_compat(path.parents[2])
        print(
            "native_mod_adapt: compatibility patches "
            f"{'updated' if adapter_changed or source_changed else 'already present'} "
            f"for {path.parents[2]}"
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
