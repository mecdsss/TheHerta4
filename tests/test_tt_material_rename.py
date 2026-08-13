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


PKG = "_tt_material_rename_test_pkg"
_install_module(PKG)
_install_module(f"{PKG}.toolkit")

_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
    props=types.SimpleNamespace(StringProperty=lambda **_kwargs: None),
)

module_path = Path(__file__).resolve().parents[1] / "toolkit" / "tt_material_tools.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.tt_material_tools", module_path)
tt_material_tools = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tt_material_tools
spec.loader.exec_module(tt_material_tools)


class _FakeMaterial:
    def __init__(self, name):
        self.name = name


class _FakeSlot:
    def __init__(self, material):
        self.material = material


class _FakeObject:
    def __init__(self, name, materials=None):
        self.name = name
        self.type = "MESH"
        self.material_slots = [_FakeSlot(m) for m in (materials or [])]


class _FakeProps:
    def __init__(self, search, replace):
        self.mat_rename_search = search
        self.mat_rename_replace = replace


class _FakeContext:
    def __init__(self, selected_objects, search, replace):
        self.selected_objects = selected_objects
        self.scene = types.SimpleNamespace(texture_tools_props=_FakeProps(search, replace))


class TTMaterialRenameTests(unittest.TestCase):
    def setUp(self):
        self.reports = []
        self.operator = tt_material_tools.TT_OT_rename_materials_by_fragment()
        self.operator.report = lambda level, msg: self.reports.append((level, msg))

    def test_operator_registered_in_list(self):
        self.assertIn(
            tt_material_tools.TT_OT_rename_materials_by_fragment,
            tt_material_tools.tt_material_tools_list,
        )

    def test_replace_fragment(self):
        mats = [_FakeMaterial("Body_Main"), _FakeMaterial("Body_Main_Extra")]
        obj = _FakeObject("Body", mats)
        context = _FakeContext([obj], search="Main", replace="Sub")

        result = self.operator.execute(context)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual([m.name for m in mats], ["Body_Sub", "Body_Sub_Extra"])
        self.assertEqual(self.reports, [({'INFO'}, "已重命名 2 个材质球")])

    def test_replace_all_occurrences(self):
        mat = _FakeMaterial("A_B_A")
        obj = _FakeObject("Body", [mat])
        context = _FakeContext([obj], search="A", replace="X")

        self.operator.execute(context)

        self.assertEqual(mat.name, "X_B_X")

    def test_delete_fragment_when_replace_empty(self):
        mat = _FakeMaterial("TmpBody")
        obj = _FakeObject("Body", [mat])
        context = _FakeContext([obj], search="Tmp", replace="")

        self.operator.execute(context)

        self.assertEqual(mat.name, "Body")

    def test_shared_material_renamed_only_once(self):
        shared = _FakeMaterial("Body_Main")
        obj_a = _FakeObject("A", [shared])
        obj_b = _FakeObject("B", [shared])
        context = _FakeContext([obj_a, obj_b], search="Main", replace="Sub")

        result = self.operator.execute(context)

        self.assertEqual(shared.name, "Body_Sub")
        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(self.reports, [({'INFO'}, "已重命名 1 个材质球")])

    def test_missing_search_cancels(self):
        obj = _FakeObject("Body", [_FakeMaterial("Body")])
        context = _FakeContext([obj], search="", replace="Sub")

        result = self.operator.execute(context)

        self.assertEqual(result, {'CANCELLED'})
        self.assertEqual(self.reports, [({'ERROR'}, "请先填写要查找的片段")])

    def test_no_match_reports_info(self):
        obj = _FakeObject("Body", [_FakeMaterial("Body")])
        context = _FakeContext([obj], search="Missing", replace="Sub")

        result = self.operator.execute(context)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(self.reports, [({'INFO'}, "没有找到名称中包含查找片段的材质球")])

    def test_object_without_material_slots_skipped(self):
        obj = _FakeObject("Empty")
        obj.material_slots = []
        context = _FakeContext([obj], search="Main", replace="Sub")

        result = self.operator.execute(context)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(self.reports, [({'INFO'}, "没有找到名称中包含查找片段的材质球")])


if __name__ == "__main__":
    unittest.main()
