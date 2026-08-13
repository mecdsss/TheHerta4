import importlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PKG = "_bmtp_set_vertex_color_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_install_module(PKG)
_install_module(f"{PKG}.toolkit")
_install_module(f"{PKG}.utils")


class _FakeOpsObject:
    def __init__(self):
        self.mode_calls = []

    def mode_set(self, mode):
        self.mode_calls.append(mode)


_FAKE_OPS = _FakeOpsObject()

_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
    props=types.SimpleNamespace(FloatProperty=lambda **_kwargs: None),
    ops=types.SimpleNamespace(object=_FAKE_OPS),
)


class _FakeLoop:
    def __init__(self, vertex_index):
        self.vertex_index = vertex_index


class _FakePolygon:
    def __init__(self, loop_start, loop_total):
        self.loop_start = loop_start
        self.loop_total = loop_total


class _LoopCollection(list):
    def foreach_get(self, field, target):
        values = np.asarray([getattr(item, field) for item in self], dtype=np.int32)
        target[: len(values)] = values


class _PolygonCollection(list):
    def foreach_get(self, field, target):
        values = np.asarray([getattr(item, field) for item in self], dtype=np.int32)
        target[: len(values)] = values


class _FakeAttributeData:
    def __init__(self, owner):
        self.owner = owner

    def __len__(self):
        return self.owner.count

    def foreach_set(self, field, values):
        array = np.asarray(values, dtype=np.float32).reshape(-1, 4)
        self.owner.count = array.shape[0]
        self.owner.values = array.copy()

    def foreach_get(self, field, target):
        values = self.owner.values.reshape(-1)
        target[: values.size] = values


class _FakeColorAttribute:
    def __init__(self, name, domain="CORNER", data_type="BYTE_COLOR", count=0):
        self.name = name
        self.domain = domain
        self.data_type = data_type
        self.count = count
        self.values = np.zeros((count, 4), dtype=np.float32)
        self.values[:, 3] = 1.0
        self.data = _FakeAttributeData(self)

    def __len__(self):
        return self.count


class _FakeColorAttributes:
    def __init__(self, mesh):
        self.mesh = mesh
        self.items = []
        self.active_color = None

    def __iter__(self):
        return iter(list(self.items))

    def get(self, name):
        for attr in self.items:
            if attr.name == name:
                return attr
        return None

    def new(self, name, type, domain):
        count = len(self.mesh.loops) if domain == "CORNER" else len(self.mesh.vertices)
        attr = _FakeColorAttribute(name=name, domain=domain, data_type=type, count=count)
        self.items.append(attr)
        return attr

    def remove(self, attr):
        if attr in self.items:
            self.items.remove(attr)
        if self.active_color is attr:
            self.active_color = None


class _FakeMesh:
    def __init__(self, faces, color_attributes=None, selected_verts=(), selected_faces=()):
        vertex_indices = sorted({vi for face in faces for vi in face})
        self.vertices = [types.SimpleNamespace(index=i) for i in vertex_indices]
        self.loops = _LoopCollection()
        self.polygons = _PolygonCollection()
        for face in faces:
            start = len(self.loops)
            self.polygons.append(_FakePolygon(start, len(face)))
            for vi in face:
                self.loops.append(_FakeLoop(vi))
        self.color_attributes = _FakeColorAttributes(self)
        for attr in (color_attributes or []):
            count = len(self.loops) if attr.domain == "CORNER" else len(self.vertices)
            attr.count = count
            attr.values = np.zeros((count, 4), dtype=np.float32)
            attr.values[:, 3] = 1.0
            self.color_attributes.items.append(attr)
        self.selected_verts = set(selected_verts)
        self.selected_faces = set(selected_faces)
        self.update_called = 0

    def update(self):
        self.update_called += 1


class _FakeBMVert:
    def __init__(self, index, selected):
        self.index = index
        self.select = selected


class _FakeBMFace:
    def __init__(self, index, selected):
        self.index = index
        self.select = selected


class _FakeSeq:
    def __init__(self, items):
        self.items = list(items)

    def __iter__(self):
        return iter(self.items)

    def ensure_lookup_table(self):
        pass


class _FakeBMesh:
    def __init__(self, mesh):
        self.verts = _FakeSeq(_FakeBMVert(i, i in mesh.selected_verts) for i in range(len(mesh.vertices)))
        self.faces = _FakeSeq(_FakeBMFace(i, i in mesh.selected_faces) for i in range(len(mesh.polygons)))

    def free(self):
        pass


_install_module("bmesh", from_edit_mesh=lambda mesh: _FakeBMesh(mesh))


class _FakeObject:
    def __init__(self, name, mesh):
        self.name = name
        self.type = "MESH"
        self.data = mesh


class _FakeProps:
    def __init__(self, vc_mode="FULL_COLOR"):
        self.vc_mode = vc_mode
        self.vc_attr_name = "COLOR"
        self.vc_attr_domain = "CORNER"
        self.vc_attr_data_type = "BYTE_COLOR"
        self.vc_color = (1.0, 0.0, 0.0, 1.0)


class _FakeContext:
    def __init__(self, selected_objects=None, edit_object=None, vc_mode="FULL_COLOR"):
        self.selected_objects = selected_objects or []
        self.edit_object = edit_object
        self.mode = "EDIT_MESH" if edit_object is not None else "OBJECT"
        self.scene = types.SimpleNamespace(bmtp_props=_FakeProps(vc_mode))


module_path = REPO_ROOT / "toolkit" / "bmtp_mesh_tools.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.bmtp_mesh_tools", module_path)
bmtp_mesh_tools = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bmtp_mesh_tools
sys.modules[f"{PKG}.utils.color_attribute_utils"] = importlib.import_module("utils.color_attribute_utils")
sys.modules[f"{PKG}.utils.vertex_color_utils"] = importlib.import_module("utils.vertex_color_utils")
spec.loader.exec_module(bmtp_mesh_tools)


def _color_attr(mesh, name):
    return mesh.color_attributes.get(name)


class BMTP_SetVertexColorTests(unittest.TestCase):
    def setUp(self):
        self.reports = []
        self.operator = bmtp_mesh_tools.BMTP_OT_SetVertexColor()
        self.operator.report = lambda level, msg: self.reports.append((level, msg))
        _FAKE_OPS.mode_calls.clear()

    def _execute(self, context):
        return self.operator.execute(context)

    def test_object_full_color_clears_and_rewrites_existing_target(self):
        old = _FakeColorAttribute("OldA", domain="CORNER", data_type="BYTE_COLOR")
        target = _FakeColorAttribute("COLOR", domain="CORNER", data_type="BYTE_COLOR")
        mesh = _FakeMesh([(0, 1, 2, 3)], color_attributes=[old, target])
        context = _FakeContext(selected_objects=[_FakeObject("Body", mesh)])

        result = self._execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([a.name for a in mesh.color_attributes.items], ["COLOR"])
        self.assertIs(mesh.color_attributes.active_color, _color_attr(mesh, "COLOR"))
        np.testing.assert_allclose(_color_attr(mesh, "COLOR").values, [[1, 0, 0, 1]] * 4)

    def test_object_alpha_only_preserves_other_attributes(self):
        old = _FakeColorAttribute("OldA", domain="CORNER", data_type="BYTE_COLOR")
        mesh = _FakeMesh([(0, 1, 2, 3)], color_attributes=[old])
        context = _FakeContext(selected_objects=[_FakeObject("Body", mesh)], vc_mode="ALPHA_ONLY")
        context.scene.bmtp_props.vc_color = (0.2, 0.3, 0.4, 0.75)

        result = self._execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([a.name for a in mesh.color_attributes.items], ["OldA", "COLOR"])
        self.assertEqual(old.values.tolist(), [[0, 0, 0, 1]] * 4)
        np.testing.assert_allclose(_color_attr(mesh, "COLOR").values[:, 3], 0.75)
        np.testing.assert_allclose(_color_attr(mesh, "COLOR").values[:, :3], 0.0)

    def test_edit_full_color_paints_only_selected_face(self):
        mesh = _FakeMesh([(0, 1, 2, 3), (4, 5, 6, 7)], selected_faces={1})
        obj = _FakeObject("Body", mesh)
        context = _FakeContext(edit_object=obj)

        result = self._execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(_FAKE_OPS.mode_calls, ["OBJECT", "EDIT"])
        values = _color_attr(mesh, "COLOR").values
        np.testing.assert_allclose(values[0:4], [[0, 0, 0, 1]] * 4)
        np.testing.assert_allclose(values[4:8], [[1, 0, 0, 1]] * 4)

    def test_edit_full_color_clears_other_attributes(self):
        old = _FakeColorAttribute("OldA", domain="CORNER", data_type="BYTE_COLOR")
        mesh = _FakeMesh([(0, 1, 2, 3)], color_attributes=[old], selected_faces={0})
        obj = _FakeObject("Body", mesh)
        context = _FakeContext(edit_object=obj)

        result = self._execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([a.name for a in mesh.color_attributes.items], ["COLOR"])

    def test_edit_alpha_only_preserves_other_attributes(self):
        old = _FakeColorAttribute("OldA", domain="CORNER", data_type="BYTE_COLOR")
        mesh = _FakeMesh([(0, 1, 2, 3), (4, 5, 6, 7)], color_attributes=[old], selected_faces={1})
        obj = _FakeObject("Body", mesh)
        context = _FakeContext(edit_object=obj, vc_mode="ALPHA_ONLY")
        context.scene.bmtp_props.vc_color = (0.1, 0.2, 0.3, 0.6)

        result = self._execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([a.name for a in mesh.color_attributes.items], ["OldA", "COLOR"])
        self.assertEqual(old.values.tolist(), [[0, 0, 0, 1]] * 8)
        values = _color_attr(mesh, "COLOR").values
        np.testing.assert_allclose(values[0:4, 3], 1.0)
        np.testing.assert_allclose(values[4:8, 3], 0.6)

    def test_object_full_color_float_stores_linear(self):
        mesh = _FakeMesh([(0, 1, 2, 3)])
        context = _FakeContext(selected_objects=[_FakeObject("Body", mesh)])
        context.scene.bmtp_props.vc_attr_data_type = "FLOAT_COLOR"
        context.scene.bmtp_props.vc_color = (0.5, 0.0, 0.0, 1.0)

        result = self._execute(context)

        self.assertEqual(result, {"FINISHED"})
        from utils.vertex_color_utils import convert_color_srgb_to_linear
        expected = convert_color_srgb_to_linear(np.array([0.5, 0.0, 0.0, 1.0], dtype=np.float32))
        np.testing.assert_allclose(_color_attr(mesh, "COLOR").values, [expected] * 4)

    def test_no_selection_cancels(self):
        context = _FakeContext(selected_objects=[])

        result = self._execute(context)

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(self.reports, [({"ERROR"}, "请选择至少一个网格物体")])


if __name__ == "__main__":
    unittest.main()

