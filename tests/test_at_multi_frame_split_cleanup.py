import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _FakeMatrix:
    @staticmethod
    def Identity(_size):
        return _FakeMatrix(((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0)))

    def __init__(self, rows=None):
        self.rows = rows or ()

    def __getitem__(self, index):
        return self.rows[index]

    def to_3x3(self):
        return _FakeMatrix3x3(tuple(tuple(float(col) for col in row[:3]) for row in self.rows[:3]))

    def inverted(self):
        rows = self.rows
        diagonal = [float(rows[i][i]) for i in range(3)]
        translation = [float(rows[i][3]) for i in range(3)]
        inv_rows = [
            [1.0 / diagonal[0], 0.0, 0.0, -translation[0] / diagonal[0]],
            [0.0, 1.0 / diagonal[1], 0.0, -translation[1] / diagonal[1]],
            [0.0, 0.0, 1.0 / diagonal[2], -translation[2] / diagonal[2]],
            [0.0, 0.0, 0.0, 1.0],
        ]
        return _FakeMatrix(tuple(tuple(row) for row in inv_rows))

    def __matmul__(self, other):
        result = []
        for row in range(4):
            result_row = []
            for col in range(4):
                result_row.append(sum(float(self.rows[row][k]) * float(other.rows[k][col]) for k in range(4)))
            result.append(tuple(result_row))
        return _FakeMatrix(tuple(result))


class _FakeMatrix3x3:
    def __init__(self, rows):
        self.rows = rows

    def transposed(self):
        return np.asarray(self.rows, dtype=np.float32).T


class _FakeVector(tuple):
    pass


class _FakeObjectCollection:
    def __init__(self, owner):
        self.owner = owner
        self._items = []

    def link(self, obj):
        if obj not in self._items:
            self._items.append(obj)
        if self.owner not in obj.users_collection:
            obj.users_collection.append(self.owner)

    def unlink(self, obj):
        if obj in self._items:
            self._items.remove(obj)
        if self.owner in obj.users_collection:
            obj.users_collection.remove(self.owner)

    def __iter__(self):
        return iter(self._items)

    def __contains__(self, item):
        if isinstance(item, str):
            return any(obj.name == item for obj in self._items)
        return item in self._items


class _FakeChildCollection(list):
    def link(self, coll):
        if coll not in self:
            self.append(coll)


class _FakeCollection:
    def __init__(self, name):
        self.name = name
        self.objects = _FakeObjectCollection(self)
        self.children = _FakeChildCollection()


class _FakeObject:
    def __init__(self, name):
        self.name = name
        self.users_collection = []
        self.hide_viewport = True
        self.hide_render = True
        self.type = "MESH"
        self.modifiers = []
        self.matrix_world = _FakeMatrix.Identity(4)
        self._props = {}

    def hide_set(self, value):
        self.hidden = value

    def __getitem__(self, key):
        return self._props[key]

    def __setitem__(self, key, value):
        self._props[key] = value

    def get(self, key, default=None):
        return self._props.get(key, default)


class _ObjectRegistry(dict):
    def remove(self, obj, do_unlink=True):
        if do_unlink:
            for coll in list(obj.users_collection):
                coll.objects.unlink(obj)
        self.pop(obj.name, None)


class _CollectionRegistry(dict):
    def remove(self, coll):
        self.pop(coll.name, None)


PKG = "_at_multi_frame_split_cleanup_test_pkg"
_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
    data=types.SimpleNamespace(objects=_ObjectRegistry(), collections=_CollectionRegistry()),
)
_install_module("mathutils", Matrix=_FakeMatrix, Vector=_FakeVector)


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "at_multi_frame_split.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.at_multi_frame_split", module_path)
split_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = split_module
spec.loader.exec_module(split_module)


class SplitCleanupTests(unittest.TestCase):
    def setUp(self):
        fresh_data = types.SimpleNamespace(
            objects=_ObjectRegistry(),
            collections=_CollectionRegistry(),
        )
        split_module.bpy.data = fresh_data

    def test_cleanup_temp_collection_preserves_start_object_when_requested(self):
        operator = split_module.ATP_OT_SplitFramesToShapeKeyMulti()

        original_coll = _FakeCollection("Original")
        temp_coll = _FakeCollection("Temp_Split_Test")
        sub_coll = _FakeCollection("Temp_Split_Test_Frames")
        temp_coll.children.link(sub_coll)

        original_obj = _FakeObject("OriginalObj")
        start_obj = _FakeObject("StartBase")
        temp_obj = _FakeObject("TempFrame")

        original_obj.users_collection.append(original_coll)
        sub_coll.objects.link(start_obj)
        sub_coll.objects.link(temp_obj)

        bpy_module = split_module.bpy
        bpy_module.data.objects[start_obj.name] = start_obj
        bpy_module.data.objects[temp_obj.name] = temp_obj
        bpy_module.data.collections[temp_coll.name] = temp_coll
        bpy_module.data.collections[sub_coll.name] = sub_coll

        context = types.SimpleNamespace(scene=types.SimpleNamespace(collection=original_coll))

        operator.cleanup_temp_collection(
            context,
            temp_coll,
            start_obj,
            original_obj,
            preserve_start_obj=True,
        )

        self.assertIn(start_obj.name, bpy_module.data.objects)
        self.assertNotIn(temp_obj.name, bpy_module.data.objects)
        self.assertNotIn(temp_coll.name, bpy_module.data.collections)
        self.assertNotIn(sub_coll.name, bpy_module.data.collections)
        self.assertIn(start_obj, list(original_coll.objects))

    def test_cleanup_temp_collection_can_preserve_base_object_for_copy_back_flow(self):
        operator = split_module.ATP_OT_SplitFramesToShapeKeyMulti()

        original_coll = _FakeCollection("Original")
        temp_coll = _FakeCollection("Temp_Split_Test")
        sub_coll = _FakeCollection("Temp_Split_Test_Frames")
        temp_coll.children.link(sub_coll)

        original_obj = _FakeObject("OriginalObj")
        start_obj = _FakeObject("StartBase")
        start_obj["atp_base_frame"] = True
        temp_obj = _FakeObject("TempFrame")

        original_obj.users_collection.append(original_coll)
        sub_coll.objects.link(start_obj)
        sub_coll.objects.link(temp_obj)

        bpy_module = split_module.bpy
        bpy_module.data.objects[start_obj.name] = start_obj
        bpy_module.data.objects[temp_obj.name] = temp_obj
        bpy_module.data.collections[temp_coll.name] = temp_coll
        bpy_module.data.collections[sub_coll.name] = sub_coll

        context = types.SimpleNamespace(scene=types.SimpleNamespace(collection=original_coll))

        operator.cleanup_temp_collection(
            context,
            temp_coll,
            start_obj,
            original_obj,
            preserve_start_obj=True,
        )

        self.assertIn(start_obj.name, bpy_module.data.objects)
        self.assertNotIn(temp_obj.name, bpy_module.data.objects)
        self.assertIn(start_obj, list(original_coll.objects))

    def test_copy_shape_keys_between_objects_transforms_source_coords_into_target_local_space(self):
        operator = split_module.ATP_OT_SplitFramesToShapeKeyMulti()

        class _FakeShapeKeyPoint:
            def __init__(self, coords):
                self.co = np.asarray(coords, dtype=np.float32)

        class _FakeShapeKeyData:
            def __init__(self, coords):
                self._coords = np.asarray(coords, dtype=np.float32)

            def foreach_get(self, _attr, output):
                output[:] = self._coords.reshape(-1)

            def foreach_set(self, _attr, values):
                self._coords = np.asarray(values, dtype=np.float32).reshape(-1, 3)

            def __len__(self):
                return len(self._coords)

        class _FakeKeyBlock:
            def __init__(self, name, coords):
                self.name = name
                self.data = _FakeShapeKeyData(coords)
                self.value = 0.25
                self.slider_min = -1.0
                self.slider_max = 2.0
                self.mute = True

        class _FakeKeyBlocks(list):
            def get(self, name):
                for item in self:
                    if item.name == name:
                        return item
                return None

        class _FakeShapeKeys:
            def __init__(self, basis_coords, key_name, key_coords):
                self.key_blocks = _FakeKeyBlocks([
                    _FakeKeyBlock("Basis", basis_coords),
                    _FakeKeyBlock(key_name, key_coords),
                ])
                self.reference_key = self.key_blocks[0]

        class _FakeVertices:
            def __init__(self, coords):
                self._coords = np.asarray(coords, dtype=np.float32)

            def foreach_get(self, _attr, output):
                output[:] = self._coords.reshape(-1)

            def __len__(self):
                return len(self._coords)

        class _FakeMesh:
            def __init__(self, coords, shape_keys=None):
                self.vertices = _FakeVertices(coords)
                self.shape_keys = shape_keys
                self.updated = False

            def update(self):
                self.updated = True

        class _FakeTargetObject(_FakeObject):
            def __init__(self, name, coords, matrix_rows):
                super().__init__(name)
                self.data = _FakeMesh(coords)
                self.matrix_world = _FakeMatrix(matrix_rows)

            def evaluated_get(self, _depsgraph):
                return self

            def shape_key_add(self, name, from_mix=False):
                if self.data.shape_keys is None:
                    basis_coords = np.asarray(self.data.vertices._coords, dtype=np.float32)
                    self.data.shape_keys = types.SimpleNamespace(
                        key_blocks=_FakeKeyBlocks([_FakeKeyBlock(name, basis_coords)]),
                        reference_key=None,
                    )
                    self.data.shape_keys.reference_key = self.data.shape_keys.key_blocks[0]
                    return self.data.shape_keys.reference_key
                new_block = _FakeKeyBlock(name, np.asarray(self.data.vertices._coords, dtype=np.float32))
                self.data.shape_keys.key_blocks.append(new_block)
                return new_block

        source_basis = np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32)
        source_key = np.asarray([[2.0, 0.0, 0.0]], dtype=np.float32)
        source_obj = _FakeObject("Source")
        source_obj.data = _FakeMesh(source_basis, _FakeShapeKeys(source_basis, "Smile", source_key))
        source_obj.matrix_world = _FakeMatrix(((2.0, 0.0, 0.0, 3.0), (0.0, 2.0, 0.0, 0.0), (0.0, 0.0, 2.0, 0.0), (0.0, 0.0, 0.0, 1.0)))

        target_obj = _FakeTargetObject(
            "Target",
            np.asarray([[0.5, 0.0, 0.0]], dtype=np.float32),
            ((4.0, 0.0, 0.0, 1.0), (0.0, 4.0, 0.0, 0.0), (0.0, 0.0, 4.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
        )

        context = types.SimpleNamespace(
            evaluated_depsgraph_get=lambda: object(),
        )

        self.assertTrue(operator.copy_shape_keys_between_objects(context, source_obj, target_obj))

        smile_key = target_obj.data.shape_keys.key_blocks.get("Smile")
        self.assertIsNotNone(smile_key)
        np.testing.assert_allclose(smile_key.data._coords, np.asarray([[1.5, 0.0, 0.0]], dtype=np.float32))
        self.assertEqual(smile_key.value, 0.25)
        self.assertEqual(smile_key.slider_min, -1.0)
        self.assertEqual(smile_key.slider_max, 2.0)
        self.assertTrue(smile_key.mute)


if __name__ == "__main__":
    unittest.main()
