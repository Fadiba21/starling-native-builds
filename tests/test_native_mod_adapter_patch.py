import ast
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from native_mod_source_compat import (  # noqa: E402
    ACTION_BUTTON_SHIM,
    ACTION_SHIM,
    OPTION_NAME_VALUE_SHIM,
    patch_source_compat,
)
from patch_native_mod_adapter import (  # noqa: E402
    ALIASES,
    CHECKOUT_LINE,
    HOOK_LINE,
    MAIN_SOURCE_ANCHOR,
    SHIM_CALL,
    SHIM_IMPORT,
    SOURCE_HELPER_RELATIVE,
    SOURCE_REWRITES,
    install_sandbox_hook,
    install_source_helper,
    patch_adapter,
)


ADAPTER_FIXTURE = '''import pathlib
import re
import sys

RENAMED = {"existing::method": "existing::replacement"}
CHANGED = ()

def owners(source):
    return {}

def main():
    source, mods = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    modules = owners(source)
    for manifest in mods.glob("*/horizon-mixin.toml"):
        pass

if __name__ == "__main__":
    main()
'''


class NativeModAdapterPatchTests(unittest.TestCase):
    def test_adds_exact_aliases_rewrites_and_staged_source_call_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = pathlib.Path(directory) / "native_mod_adapt.py"
            adapter.write_text(ADAPTER_FIXTURE, encoding="utf-8")

            self.assertTrue(patch_adapter(adapter))
            first_result = adapter.read_text(encoding="utf-8")
            tree = ast.parse(first_result)
            assignments = {
                node.targets[0].id: ast.literal_eval(node.value)
                for node in tree.body if isinstance(node, ast.Assign)
            }
            renamed = assignments["RENAMED"]
            changed = assignments["CHANGED"]
            self.assertEqual(renamed["existing::method"], "existing::replacement")
            for old, new in ALIASES.items():
                self.assertEqual(renamed[old], new)
            self.assertEqual(len(renamed), 1 + len(ALIASES))
            for rewrite in SOURCE_REWRITES:
                self.assertIn(rewrite, changed)
            self.assertEqual(first_result.count(SHIM_IMPORT), 1)
            self.assertEqual(first_result.count(SHIM_CALL), 1)

            mob_hit = "g.mob_hit().map(|(i, d, _)| (d, g.mobs[i].kind.id.to_string()))"
            for pattern, replacement in SOURCE_REWRITES:
                mob_hit = re.sub(pattern, replacement, mob_hit)
            self.assertIn("map(|(i, d)|", mob_hit)
            self.assertNotIn("d, _)", mob_hit)

            light_call = "dynamic_light::select_iter(sources.drain(..), camera, &mut out);"
            for pattern, replacement in SOURCE_REWRITES:
                light_call = re.sub(pattern, replacement, light_call)
            self.assertEqual(light_call, "dynamic_light::select(&sources, camera, &mut out);")

            self.assertFalse(patch_adapter(adapter))
            self.assertEqual(adapter.read_text(encoding="utf-8"), first_result)

    def test_rejects_a_conflicting_alias(self):
        old, _ = next(iter(ALIASES.items()))
        adapter_source = ADAPTER_FIXTURE.replace(
            '"existing::method": "existing::replacement"',
            f"{old!r}: 'wrong::target'",
        )
        with tempfile.TemporaryDirectory() as directory:
            adapter = pathlib.Path(directory) / "native_mod_adapt.py"
            adapter.write_text(adapter_source, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Conflicting mapping"):
                patch_adapter(adapter)

    def test_rejects_a_conflicting_source_rewrite(self):
        old, _ = SOURCE_REWRITES[0]
        adapter_source = ADAPTER_FIXTURE.replace(
            "CHANGED = ()", f"CHANGED = (({old!r}, 'wrong'),)"
        )
        with tempfile.TemporaryDirectory() as directory:
            adapter = pathlib.Path(directory) / "native_mod_adapt.py"
            adapter.write_text(adapter_source, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Conflicting source rewrite"):
                patch_adapter(adapter)

    def _make_staged_source(self, source):
        widgets = source / "crates/horizon-client-render/src/widgets.rs"
        ui = source / "crates/horizon-client-render/src/ui.rs"
        text = source / "crates/horizon-client-core/src/text.rs"
        remote = source / "crates/horizon-client/src/game/remote.rs"
        for path in (widgets, ui, text, remote):
            path.parent.mkdir(parents=True, exist_ok=True)
        widgets.write_text(
            "/// A labelled widget in GUI pixels (the coordinates screens take input in).\n"
            "#[derive(Clone, Debug)]\npub struct Widget {\n}\n",
            encoding="utf-8",
        )
        ui.write_text(
            "/// Button (AbstractButton): nine-slice sprite plus the centred label.\n"
            "pub fn button(\n}\n",
            encoding="utf-8",
        )
        text.write_text(
            "impl Text {\n    pub fn empty() -> Self {\n    }\n}\n",
            encoding="utf-8",
        )
        remote.write_text(
            '    fn menu_slot(menu: &str, slot: i32) -> Option<usize> {\n',
            encoding="utf-8",
        )
        return widgets, ui, text, remote

    def test_source_compatibility_shims_are_installed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory)
            widgets, ui, text, remote = self._make_staged_source(source)

            self.assertTrue(patch_source_compat(source))
            self.assertIn(ACTION_SHIM, widgets.read_text(encoding="utf-8"))
            self.assertIn(ACTION_BUTTON_SHIM, ui.read_text(encoding="utf-8"))
            self.assertIn(OPTION_NAME_VALUE_SHIM, text.read_text(encoding="utf-8"))
            self.assertIn("pub(super) fn menu_slot(", remote.read_text(encoding="utf-8"))

            snapshots = [path.read_text(encoding="utf-8") for path in (widgets, ui, text, remote)]
            self.assertFalse(patch_source_compat(source))
            self.assertEqual(
                snapshots,
                [path.read_text(encoding="utf-8") for path in (widgets, ui, text, remote)],
            )

    def test_adapter_applies_shims_to_the_staged_source_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            adapter = root / "source/packaging/android/native_mod_adapt.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_text(ADAPTER_FIXTURE, encoding="utf-8")
            staged = root / "staged-archive"
            widgets, ui, text, remote = self._make_staged_source(staged)
            mods = root / "mods"
            mods.mkdir()

            self.assertTrue(patch_adapter(adapter))
            self.assertTrue(install_source_helper(root / "source"))
            self.assertFalse(install_source_helper(root / "source"))
            run = subprocess.run(
                [sys.executable, str(adapter), str(staged), str(mods)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn(ACTION_SHIM, widgets.read_text(encoding="utf-8"))
            self.assertIn(ACTION_BUTTON_SHIM, ui.read_text(encoding="utf-8"))
            self.assertIn(OPTION_NAME_VALUE_SHIM, text.read_text(encoding="utf-8"))
            self.assertIn("pub(super) fn menu_slot(", remote.read_text(encoding="utf-8"))

    def test_installs_sandbox_hook_after_source_checkout_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = pathlib.Path(directory) / "mod_build_sandbox.sh"
            wrapper.write_text(
                "set -euo pipefail\nroot=$1\ncommit=$2\n"
                f"{CHECKOUT_LINE}\nexec bash build.sh\n",
                encoding="utf-8",
            )

            self.assertTrue(install_sandbox_hook(wrapper))
            contents = wrapper.read_text(encoding="utf-8")
            self.assertIn(f"{CHECKOUT_LINE}\n{HOOK_LINE}\n", contents)
            self.assertFalse(install_sandbox_hook(wrapper))
            self.assertEqual(wrapper.read_text(encoding="utf-8"), contents)

    def test_rejects_unrecognized_sandbox_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = pathlib.Path(directory) / "mod_build_sandbox.sh"
            wrapper.write_text("set -euo pipefail\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "source checkout line"):
                install_sandbox_hook(wrapper)


if __name__ == "__main__":
    unittest.main()
