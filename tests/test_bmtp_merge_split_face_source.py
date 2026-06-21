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


PKG = "_bmtp_merge_split_face_source_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeAttributeDataItem:
    def __init__(self):
        self.value = -1


class _FakeAttribute:
    def __init__(self, name, data_type, domain, size):
        self.name = name
        self.data_type = data_type
        self.domain = domain
        self.data = [_FakeAttributeDataItem() for _ in range(size)]


class _FakeAttributes(dict):
    def new(self, name, type, domain):
        size = self._size_getter(domain)
        attribute = _FakeAttribute(name, type, domain, size)
        self[name] = attribute
        return attribute

    def remove(self, attribute):
        self.pop(attribute.name, None)

    def __init__(self, size_getter):
        super().__init__()
        self._size_getter = size_getter


class _FakeMesh:
    def __init__(self, polygon_count):
        self.polygons = [object() for _ in range(polygon_count)]
        self.attributes = _FakeAttributes(lambda domain: polygon_count if domain == "FACE" else 0)
        self.updated = False
        self.vertices = []

    def update(self):
        self.updated = True


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object, PropertyGroup=object),
    props=types.SimpleNamespace(BoolProperty=lambda **_kwargs: None, CollectionProperty=lambda **_kwargs: None),
    context=types.SimpleNamespace(mode="OBJECT"),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    "bpy.props",
    BoolProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
)
_install_module("bmesh", from_edit_mesh=lambda _mesh: None, update_edit_mesh=lambda *_args, **_kwargs: None)
_install_module("mathutils", Vector=tuple)
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.collection_utils", CollectionUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace(remove_unused_vertex_groups=lambda _obj: None))
_install_module(f"{PKG}.utils.shapekey_utils", ShapeKeyUtils=types.SimpleNamespace(bake_current_shape_key_mix_to_mesh=lambda *_args, **_kwargs: None))
_install_module(f"{PKG}.utils.algorithm_utils", AlgorithmUtils=types.SimpleNamespace())


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "model_operators.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.model_operators", module_path)
model_operators = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model_operators
spec.loader.exec_module(model_operators)
_fake_bpy_module = sys.modules["bpy"]


class BMTPMergeSplitFaceSourceTests(unittest.TestCase):
    def test_write_face_source_id_creates_face_int_attribute(self):
        mesh = _FakeMesh(polygon_count=3)
        obj = types.SimpleNamespace(type="MESH", data=mesh)

        result = model_operators._write_face_source_id(obj, 7)

        self.assertTrue(result)
        attribute = mesh.attributes[model_operators.MERGE_SPLIT_FACE_SOURCE_ATTR]
        self.assertEqual(attribute.domain, "FACE")
        self.assertEqual(attribute.data_type, "INT")
        self.assertEqual([item.value for item in attribute.data], [7, 7, 7])
        self.assertTrue(mesh.updated)

    def test_read_face_source_ids_returns_written_values(self):
        mesh = _FakeMesh(polygon_count=4)
        obj = types.SimpleNamespace(type="MESH", data=mesh)
        model_operators._write_face_source_id(obj, 2)
        attribute = mesh.attributes[model_operators.MERGE_SPLIT_FACE_SOURCE_ATTR]
        attribute.data[1].value = 5

        values = model_operators._read_face_source_ids(mesh)

        self.assertEqual(values, [2, 5, 2, 2])

    def test_clear_internal_merge_split_groups_removes_face_source_attribute(self):
        mesh = _FakeMesh(polygon_count=2)
        obj = types.SimpleNamespace(type="MESH", data=mesh)
        model_operators._write_face_source_id(obj, 1)

        model_operators._clear_internal_merge_split_groups(obj)

        self.assertNotIn(model_operators.MERGE_SPLIT_FACE_SOURCE_ATTR, mesh.attributes)

    def test_collect_recorded_split_entries_uses_marker_source_id_when_list_has_gaps(self):
        items = [
            types.SimpleNamespace(
                object_name="MissingMesh",
                marker_group_name="",
                face_start=0,
                face_count=0,
                vertex_count=0,
            ),
            types.SimpleNamespace(
                object_name="Body",
                marker_group_name=f"{model_operators.MERGE_SPLIT_FACE_SOURCE_ATTR}_Body_0",
                face_start=0,
                face_count=12,
                vertex_count=24,
            ),
            types.SimpleNamespace(
                object_name="Hair",
                marker_group_name=f"{model_operators.MERGE_SPLIT_FACE_SOURCE_ATTR}_Hair_1",
                face_start=12,
                face_count=8,
                vertex_count=16,
            ),
        ]

        entries = model_operators._collect_recorded_split_entries(items)

        self.assertEqual(
            entries,
            [
                (0, "Body", 0, 12, f"{model_operators.MERGE_SPLIT_FACE_SOURCE_ATTR}_Body_0"),
                (1, "Hair", 12, 8, f"{model_operators.MERGE_SPLIT_FACE_SOURCE_ATTR}_Hair_1"),
            ],
        )

    def test_clear_merge_split_item_record_resets_stale_ranges(self):
        item = types.SimpleNamespace(
            marker_group_name="stale_marker",
            face_start=5,
            face_count=7,
            vertex_count=9,
        )

        model_operators._clear_merge_split_item_record(item)

        self.assertEqual(item.marker_group_name, "")
        self.assertEqual(item.face_start, 0)
        self.assertEqual(item.face_count, 0)
        self.assertEqual(item.vertex_count, 0)

    def test_merge_operator_preserves_existing_records_when_not_enough_valid_targets(self):
        stale_item = types.SimpleNamespace(
            object_name="MissingMesh",
            marker_group_name="keep_me",
            face_start=11,
            face_count=22,
            vertex_count=33,
        )
        valid_obj = types.SimpleNamespace(type="MESH", data=_FakeMesh(polygon_count=3))
        valid_item = types.SimpleNamespace(
            object_name="Body",
            marker_group_name="keep_me_too",
            face_start=1,
            face_count=2,
            vertex_count=3,
        )

        _fake_bpy.data = types.SimpleNamespace(objects={"Body": valid_obj})
        _fake_bpy_module.data = _fake_bpy.data
        props = types.SimpleNamespace(
            merge_split_items=[stale_item, valid_item],
            merge_split_target_name="MergedObject",
        )
        context = types.SimpleNamespace(scene=types.SimpleNamespace(bmtp_props=props))

        operator = model_operators.BMTP_MergeObjectsByRecordedRanges()
        reports = []
        operator.report = lambda level, message: reports.append((level, message))

        result = operator.execute(context)

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(stale_item.marker_group_name, "keep_me")
        self.assertEqual(stale_item.face_start, 11)
        self.assertEqual(stale_item.face_count, 22)
        self.assertEqual(stale_item.vertex_count, 33)
        self.assertEqual(valid_item.marker_group_name, "keep_me_too")
        self.assertEqual(valid_item.face_start, 1)
        self.assertEqual(valid_item.face_count, 2)
        self.assertEqual(valid_item.vertex_count, 3)
        self.assertTrue(any("至少需要" in str(message) for _level, message in reports))


if __name__ == "__main__":
    unittest.main()
