import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_ui_prefix_quick_ops_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(
        PropertyGroup=object,
        Operator=object,
        Scene=object,
    ),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(node_groups=[]),
)
_install_module("bpy", **_fake_bpy.__dict__)


class _FakeObjectPrefixHelper:
    @staticmethod
    def normalize_prefix(prefix: str) -> str:
        return str(prefix or "").strip()

    @staticmethod
    def split_name_and_prefix(object_name: str, prefix: str = "", separator: str = "."):
        token = f"{prefix}{separator}"
        if prefix and object_name.startswith(token):
            return prefix, separator, object_name[len(token):]
        return "", separator, object_name

    @staticmethod
    def parse_prefix_parts(prefix: str) -> dict:
        return {"bare_unique_str": str(prefix or "").strip()}

    @staticmethod
    def get_node_prefix_info(_node):
        raise AssertionError("resolve_display_name should not scan node data during draw")

    @staticmethod
    def build_virtual_object_name_for_node(_node):
        raise AssertionError("resolve_display_name should not build node display names during draw")

    @staticmethod
    def extract_prefix_info(_name: str):
        raise AssertionError("resolve_display_name should not scan scene objects during draw")


_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=_FakeObjectPrefixHelper)


module_path = Path(__file__).resolve().parents[1] / "ui" / "ui_prefix_quick_ops.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.ui_prefix_quick_ops", module_path)
ui_prefix_quick_ops = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.ui.ui_prefix_quick_ops"] = ui_prefix_quick_ops
spec.loader.exec_module(ui_prefix_quick_ops)


class PrefixQuickOpsTests(unittest.TestCase):
    def test_resolve_display_name_returns_cached_name_without_context_scan(self):
        item = types.SimpleNamespace(prefix="abc-1-0", display_name="abc-1-0.Body")
        context = types.SimpleNamespace(scene=types.SimpleNamespace(objects=[]))

        resolved = ui_prefix_quick_ops.PrefixQuickOpsHelper.resolve_display_name(context, item)

        self.assertEqual(resolved, "abc-1-0.Body")

    def test_resolve_display_name_falls_back_to_prefix_when_display_name_missing(self):
        item = types.SimpleNamespace(prefix="abc-1-0", display_name="")
        context = types.SimpleNamespace(scene=types.SimpleNamespace(objects=[]))

        resolved = ui_prefix_quick_ops.PrefixQuickOpsHelper.resolve_display_name(context, item)

        self.assertEqual(resolved, "abc-1-0")

    def test_refresh_operator_uses_selection_scan(self):
        calls = []
        original_rebuild_from_selection = ui_prefix_quick_ops.PrefixQuickOpsHelper.rebuild_from_selection
        original_rebuild_from_scene = ui_prefix_quick_ops.PrefixQuickOpsHelper.rebuild_from_scene
        try:
            ui_prefix_quick_ops.PrefixQuickOpsHelper.rebuild_from_selection = classmethod(
                lambda cls, context: calls.append(("selection", context)) or 2
            )
            ui_prefix_quick_ops.PrefixQuickOpsHelper.rebuild_from_scene = classmethod(
                lambda cls, context: calls.append(("scene", context)) or 9
            )
            reports = []
            operator = ui_prefix_quick_ops.SSMT_OT_PrefixQuickRefresh()
            operator.report = lambda kinds, message: reports.append((kinds, message))
            context = types.SimpleNamespace(selected_objects=[object()], scene=types.SimpleNamespace())

            result = operator.execute(context)

            self.assertEqual(result, {"FINISHED"})
            self.assertEqual(calls[0][0], "selection")
            self.assertEqual(len(calls), 1)
            self.assertTrue(reports)
        finally:
            ui_prefix_quick_ops.PrefixQuickOpsHelper.rebuild_from_selection = original_rebuild_from_selection
            ui_prefix_quick_ops.PrefixQuickOpsHelper.rebuild_from_scene = original_rebuild_from_scene


if __name__ == "__main__":
    unittest.main()
