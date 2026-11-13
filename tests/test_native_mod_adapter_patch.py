import ast
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from patch_native_mod_adapter import (  # noqa: E402
    ALIASES,
    CHECKOUT_LINE,
    HOOK_LINE,
    install_sandbox_hook,
    patch_adapter,
)


class NativeModAdapterPatchTests(unittest.TestCase):
    def test_adds_only_the_exact_aliases_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = pathlib.Path(directory) / "native_mod_adapt.py"
            adapter.write_text(
                'RENAMED = {"existing::method": "existing::replacement"}\n'
                "def adapt(text):\n    return text\n",
                encoding="utf-8",
            )

            self.assertTrue(patch_adapter(adapter))
            first_result = adapter.read_text(encoding="utf-8")
            renamed = ast.literal_eval(ast.parse(first_result).body[0].value)
            self.assertEqual(renamed["existing::method"], "existing::replacement")
            for old, new in ALIASES.items():
                self.assertEqual(renamed[old], new)
            self.assertEqual(len(renamed), 1 + len(ALIASES))

            self.assertFalse(patch_adapter(adapter))
            self.assertEqual(adapter.read_text(encoding="utf-8"), first_result)

    def test_rejects_a_conflicting_alias(self):
        old, _ = next(iter(ALIASES.items()))
        with tempfile.TemporaryDirectory() as directory:
            adapter = pathlib.Path(directory) / "native_mod_adapt.py"
            adapter.write_text(f"RENAMED = {{{old!r}: 'wrong::target'}}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Conflicting mapping"):
                patch_adapter(adapter)

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
