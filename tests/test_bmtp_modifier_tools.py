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


PKG = "_bmtp_modifier_tools_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeMesh:
    def __init__(self, name="Mesh", users=1):
        self.name = name
        self.users = users
        self.shape_keys = None
        self.copy_count = 0

    def copy(self):
        self.copy_count += 1
        return _FakeMesh(name=f"{self.name}_Copy", users=1)


class _FakeModifier:
    def __init__(self, name, modifier_type):
        self.name = name
        self.type = modifier_type


class _FakeModifierList(list):
    def get(self, name):
        for item in self:
            if item.name == name:
                return item
        return None


class _FakeObject:
    def __init__(self, name, mesh, modifiers=None):
        self.name = name
        self.type = "MESH"
        self.data = mesh
        self.modifiers = _FakeModifierList(modifiers or [])
        self.constraints = []
        self.mode = "OBJECT"


_modifier_apply_calls = []
_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object),
    ops=types.SimpleNamespace(
        object=types.SimpleNamespace(
            mode_set=lambda **_kwargs: None,
            modifier_apply=lambda **kwargs: _modifier_apply_calls.append(kwargs),
            modifier_apply_as_shapekey=lambda **kwargs: _modifier_apply_calls.append(kwargs),
            shape_key_remove=lambda **_kwargs: None,
        ),
        constraint=types.SimpleNamespace(apply=lambda **_kwargs: None),
    ),
    data=types.SimpleNamespace(objects={}),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.toolkit.bmtp_shape_key_utils", BMTP_ShapeKeyUtils=types.SimpleNamespace(apply_modifiers_for_object_with_shape_keys=lambda *_args, **_kwargs: (True, "")))


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "bmtp_modifier_tools.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.bmtp_modifier_tools", module_path)
bmtp_modifier_tools = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bmtp_modifier_tools
spec.loader.exec_module(bmtp_modifier_tools)


class BMTPModifierToolsTests(unittest.TestCase):
    def setUp(self):
        _modifier_apply_calls.clear()
        _fake_bpy.data.objects.clear()

    def test_ensure_single_user_mesh_data_copies_shared_mesh(self):
        mesh = _FakeMesh(users=2)
        obj = _FakeObject("Body", mesh)

        changed = bmtp_modifier_tools._ensure_single_user_mesh_data(obj)

        self.assertTrue(changed)
        self.assertIsNot(obj.data, mesh)
        self.assertEqual(mesh.copy_count, 1)
        self.assertEqual(obj.data.users, 1)

    def test_apply_modifiers_by_name_makes_shared_mesh_single_user_before_apply(self):
        shared_mesh = _FakeMesh(users=2)
        obj = _FakeObject("Body", shared_mesh, modifiers=[_FakeModifier("Decimate", "DECIMATE")])
        _fake_bpy.data.objects[obj.name] = obj
        context = types.SimpleNamespace(
            scene=types.SimpleNamespace(bmtp_props=types.SimpleNamespace(mod_apply_names="Decimate")),
            selected_objects=[obj],
            view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=None)),
        )
        operator = bmtp_modifier_tools.BMTP_OT_ApplyModifiersByName()
        reports = []
        operator.report = lambda level, message: reports.append((level, message))

        result = operator.execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(shared_mesh.copy_count, 1)
        self.assertEqual(len(_modifier_apply_calls), 1)
        self.assertEqual(_modifier_apply_calls[0]["modifier"], "Decimate")
        self.assertTrue(any("成功应用 1 个修改器" in str(message) for _level, message in reports))

    def test_apply_modifiers_by_name_does_not_copy_shared_mesh_when_no_modifier_matches(self):
        shared_mesh = _FakeMesh(users=2)
        obj = _FakeObject("Body", shared_mesh, modifiers=[_FakeModifier("Bevel", "BEVEL")])
        _fake_bpy.data.objects[obj.name] = obj
        context = types.SimpleNamespace(
            scene=types.SimpleNamespace(bmtp_props=types.SimpleNamespace(mod_apply_names="Decimate")),
            selected_objects=[obj],
            view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=None)),
        )
        operator = bmtp_modifier_tools.BMTP_OT_ApplyModifiersByName()
        operator.report = lambda *_args, **_kwargs: None

        result = operator.execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(shared_mesh.copy_count, 0)
        self.assertEqual(len(_modifier_apply_calls), 0)
        self.assertIs(obj.data, shared_mesh)


if __name__ == "__main__":
    unittest.main()
