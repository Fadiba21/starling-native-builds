#!/usr/bin/env python3
"""Narrow source-API compatibility shims for the pinned Starling 0.8.0 build."""
from __future__ import annotations

import pathlib

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
    """Patch the staged source tree that native_build.sh actually compiles."""
    source = pathlib.Path(source)
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
