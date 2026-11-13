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

PAINT_CACHE_SHIM = '''/// Compatibility cache for older native-mod debug overlays.
/// Cached geometry is reused only while its layout is unchanged and for at most 250 ms.
#[doc(hidden)]
pub struct PaintCache {
    stored_at: Option<std::time::Instant>,
    layout: Option<([u32; 2], u32, u64)>,
    vertices: Vec<crate::hud::HudVertex>,
}

impl Default for PaintCache {
    fn default() -> Self {
        Self {
            stored_at: None,
            layout: None,
            vertices: Vec::new(),
        }
    }
}

impl PaintCache {
    pub fn fresh(
        &self,
        now: std::time::Instant,
        layout: ([u32; 2], u32, u64),
    ) -> Option<&[crate::hud::HudVertex]> {
        if self.layout? != layout {
            return None;
        }
        if now.checked_duration_since(self.stored_at?)? > std::time::Duration::from_millis(250) {
            return None;
        }
        Some(&self.vertices)
    }

    pub fn store(
        &mut self,
        now: std::time::Instant,
        layout: ([u32; 2], u32, u64),
        vertices: &[crate::hud::HudVertex],
    ) {
        self.stored_at = Some(now);
        self.layout = Some(layout);
        self.vertices.clear();
        self.vertices.extend_from_slice(vertices);
    }
}

'''

SCREEN_BLUR_SHIM = '''    /// Compatibility for legacy mods; Starling 0.8.0 has no local screen-blur API.
    /// The screen continues rendering, but without the optional blur effect.
    pub(super) fn native_apply_screen_blur(&self) {}

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


def _ensure_before(
    path: pathlib.Path,
    anchor: str,
    addition: str,
    declaration: str,
) -> bool:
    text = path.read_text(encoding="utf-8")
    if declaration in text:
        if addition.strip() in text:
            return False
        raise RuntimeError(f"Conflicting compatibility declaration in {path}: {declaration}")
    return _insert_before(path, anchor, addition)


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

    debug_overlay = source / "crates/horizon-client/src/debug_overlay.rs"
    changed |= _ensure_before(
        debug_overlay,
        "/// What the enabled entries reported (`DebugScreenDisplayer`).",
        PAINT_CACHE_SHIM,
        "pub struct PaintCache",
    )

    native_hooks = source / "crates/horizon-client/src/game/native_hooks.rs"
    changed |= _ensure_before(
        native_hooks,
        "    pub(super) fn native_compute_fov_modifier(\n",
        SCREEN_BLUR_SHIM,
        "pub(super) fn native_apply_screen_blur(",
    )
    return changed
