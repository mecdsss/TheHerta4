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


PKG = "_blueprint_enum_selection_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeNodeGroups(list):
    def get(self, name):
        for node_group in self:
            if getattr(node_group, "name", None) == name:
                return node_group
        return None


class _FakeGlobalProperties(dict):
    def __getattr__(self, name):
        if name in self:
            return self[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


_fake_global_properties = _FakeGlobalProperties()
_fake_bpy = types.SimpleNamespace(
    context=types.SimpleNamespace(
        scene=types.SimpleNamespace(global_properties=_fake_global_properties),
    ),
    data=types.SimpleNamespace(node_groups=_FakeNodeGroups(), objects={}),
    types=types.SimpleNamespace(Object=object),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(get_workspace_name=lambda: ""))
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(f"{PKG}.common.m_key", M_Key=types.SimpleNamespace())
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace())


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "export_helper.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.export_helper", module_path)
export_helper = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.export_helper"] = export_helper
spec.loader.exec_module(export_helper)


class BlueprintEnumSelectionTests(unittest.TestCase):
    def setUp(self):
        _fake_global_properties.clear()
        _fake_bpy.data.node_groups[:] = [
            types.SimpleNamespace(name="Alpha", bl_idname="SSMTBlueprintTreeType"),
            types.SimpleNamespace(name="Beta", bl_idname="SSMTBlueprintTreeType"),
        ]

    def test_enum_items_use_stable_numbers(self):
        first_items = export_helper.BlueprintExportHelper.get_blueprint_enum_items()
        second_items = export_helper.BlueprintExportHelper.get_blueprint_enum_items()

        self.assertEqual(first_items, second_items)
        self.assertTrue(all(len(item) == 5 for item in first_items))

    def test_ensure_valid_selection_repairs_saved_numeric_value(self):
        beta_number = next(
            item[4]
            for item in export_helper.BlueprintExportHelper.get_blueprint_enum_items()
            if item[0] == "Beta"
        )
        _fake_global_properties["selected_blueprint_name"] = str(beta_number)

        selected = export_helper.BlueprintExportHelper.ensure_valid_selected_blueprint_name()

        self.assertEqual(selected, "Beta")
        self.assertEqual(_fake_global_properties["selected_blueprint_name"], "Beta")

    def test_ensure_valid_selection_replaces_deleted_blueprint(self):
        _fake_global_properties["selected_blueprint_name"] = "Missing"

        selected = export_helper.BlueprintExportHelper.ensure_valid_selected_blueprint_name()

        self.assertEqual(selected, "Alpha")
        self.assertEqual(_fake_global_properties["selected_blueprint_name"], "Alpha")


if __name__ == "__main__":
    unittest.main()
