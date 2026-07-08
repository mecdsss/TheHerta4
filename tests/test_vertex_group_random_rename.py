import importlib.util
import random
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


PKG = "_vertex_group_random_rename_test_pkg"
for package_name in (PKG, f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
)
_install_module("mathutils", Vector=object)
_install_module(f"{PKG}.utils.format_utils", Fatal=RuntimeError)


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vertexgroup_utils_module = _load_module("utils.vertexgroup_utils", "utils/vertexgroup_utils.py")
VertexGroupUtils = vertexgroup_utils_module.VertexGroupUtils


class _FakeVertexGroup:
    def __init__(self, name):
        self.name = name


class _FakeObject:
    def __init__(self, name, group_names, obj_type="MESH"):
        self.name = name
        self.type = obj_type
        self.vertex_groups = [_FakeVertexGroup(group_name) for group_name in group_names]


class VertexGroupRandomRenameTests(unittest.TestCase):
    def test_rename_numeric_vertex_groups_to_random_english_only_changes_numeric_groups(self):
        random.seed(1234)
        obj = _FakeObject("Body", ["0", "1", "Spine", "Head"])

        processed_objects, renamed_count = VertexGroupUtils.rename_numeric_vertex_groups_to_random_english([obj])

        self.assertEqual(processed_objects, 1)
        self.assertEqual(renamed_count, 2)
        renamed_names = [vg.name for vg in obj.vertex_groups]
        self.assertEqual(renamed_names[2:], ["Spine", "Head"])
        self.assertTrue(renamed_names[0].isalpha())
        self.assertTrue(renamed_names[1].isalpha())
        self.assertNotEqual(renamed_names[0], renamed_names[1])

    def test_rename_numeric_vertex_groups_supports_multiple_objects(self):
        random.seed(42)
        obj_a = _FakeObject("A", ["0", "Bone"])
        obj_b = _FakeObject("B", ["1", "2"])

        processed_objects, renamed_count = VertexGroupUtils.rename_numeric_vertex_groups_to_random_english([obj_a, obj_b])

        self.assertEqual(processed_objects, 2)
        self.assertEqual(renamed_count, 3)
        self.assertTrue(obj_a.vertex_groups[0].name.isalpha())
        self.assertEqual(obj_a.vertex_groups[1].name, "Bone")
        self.assertTrue(obj_b.vertex_groups[0].name.isalpha())
        self.assertTrue(obj_b.vertex_groups[1].name.isalpha())
        self.assertNotEqual(obj_b.vertex_groups[0].name, obj_b.vertex_groups[1].name)

    def test_rename_numeric_vertex_groups_skips_non_mesh_and_objects_without_numeric_groups(self):
        random.seed(7)
        obj_mesh = _FakeObject("Mesh", ["Arm", "Leg"])
        obj_curve = _FakeObject("Curve", ["0"], obj_type="CURVE")

        processed_objects, renamed_count = VertexGroupUtils.rename_numeric_vertex_groups_to_random_english([obj_mesh, obj_curve])

        self.assertEqual(processed_objects, 0)
        self.assertEqual(renamed_count, 0)
        self.assertEqual([vg.name for vg in obj_mesh.vertex_groups], ["Arm", "Leg"])
        self.assertEqual([vg.name for vg in obj_curve.vertex_groups], ["0"])


if __name__ == "__main__":
    unittest.main()
