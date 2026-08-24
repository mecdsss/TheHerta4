import importlib.util
import sys
import types
import unittest
from pathlib import Path


_ORIGINAL_GLOBAL_MODULES = {
    name: sys.modules.get(name)
    for name in ("bpy", "bmesh", "numpy")
}


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_bmtp_mesh_face_topology_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeCoord:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def copy(self):
        return _FakeCoord(self.x, self.y, self.z)

    def __add__(self, other):
        return _FakeCoord(self.x + other.x, self.y + other.y, self.z + other.z)

    def __iadd__(self, other):
        self.x += other.x
        self.y += other.y
        self.z += other.z
        return self

    def __mul__(self, scalar):
        return _FakeCoord(self.x * scalar, self.y * scalar, self.z * scalar)

    __rmul__ = __mul__

    def __truediv__(self, scalar):
        return _FakeCoord(self.x / scalar, self.y / scalar, self.z / scalar)

    def __getitem__(self, index):
        return (self.x, self.y, self.z)[index]

    def as_tuple(self):
        return (self.x, self.y, self.z)


class _FakeUVSlot:
    def __init__(self, uv):
        self.uv = uv


class _FakeLoop:
    def __init__(self, vert, edge, uv):
        self.vert = vert
        self.edge = edge
        self._uv_slot = _FakeUVSlot(uv)
        self.link_loop_next = None

    def __getitem__(self, _uv_layer):
        return self._uv_slot


class _FakeVert:
    def __init__(self, name, co=None, selected=True):
        self.name = name
        self.co = co if co is not None else _FakeCoord(0.0, 0.0, 0.0)
        self.select = selected
        self.link_edges = []
        self.link_faces = []

    def __repr__(self):
        return self.name


class _FakeVector:
    def __init__(self, x=0.0, y=0.0, z=1.0):
        self.x = x
        self.y = y
        self.z = z

    def normalized(self):
        return self

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z


class _FakeEdge:
    def __init__(self, name, verts, length=1.0):
        self.name = name
        self.verts = tuple(verts)
        self.link_faces = []
        self._length = length

    def calc_length(self):
        return self._length

    def __repr__(self):
        return self.name


class _FakePolygon:
    def __init__(self, vertices):
        self.vertices = tuple(vertices)


class _FakeFace:
    def __init__(self, name, verts, edges, uvs, selected=True, material_index=0, smooth=True):
        self.name = name
        self.verts = list(verts)
        self.edges = list(edges)
        self.select = selected
        self.material_index = material_index
        self.smooth = smooth
        self.normal = _FakeVector()
        self.loops = []
        for index, vert in enumerate(self.verts):
            edge = self.edges[index]
            loop = _FakeLoop(vert, edge, uvs[index])
            self.loops.append(loop)
        for index, loop in enumerate(self.loops):
            loop.link_loop_next = self.loops[(index + 1) % len(self.loops)]
        for edge in self.edges:
            if self not in edge.link_faces:
                edge.link_faces.append(self)
            for vert in edge.verts:
                if edge not in vert.link_edges:
                    vert.link_edges.append(edge)
        for vert in self.verts:
            if self not in vert.link_faces:
                vert.link_faces.append(self)

    def __repr__(self):
        return self.name


class _FakeFaceCollection(list):
    def ensure_lookup_table(self):
        return None


class _FakeVertCollection(list):
    def ensure_lookup_table(self):
        return None


class _FakeBMesh:
    def __init__(self, faces=None, uv_layer=None):
        self.faces = _FakeFaceCollection(faces or [])
        self.verts = _FakeVertCollection(self._collect_verts(self.faces))
        self.from_mesh_calls = []
        self.to_mesh_calls = []
        self.freed = False
        self.loops = types.SimpleNamespace(layers=types.SimpleNamespace(uv=types.SimpleNamespace(active=uv_layer)))

    @staticmethod
    def _collect_verts(faces):
        verts = []
        for face in faces or []:
            for vert in getattr(face, "verts", []) or []:
                if vert not in verts:
                    verts.append(vert)
        return verts

    def from_mesh(self, mesh):
        self.from_mesh_calls.append(mesh)
        self.faces = _FakeFaceCollection(getattr(mesh, "faces", []))
        self.verts = _FakeVertCollection(self._collect_verts(self.faces))
        uv_layer = getattr(mesh, "uv_layer", object())
        self.loops = types.SimpleNamespace(layers=types.SimpleNamespace(uv=types.SimpleNamespace(active=uv_layer)))

    def to_mesh(self, mesh):
        self.to_mesh_calls.append(mesh)

    def free(self):
        self.freed = True


class _FakeMesh:
    def __init__(self, faces, uv_layer=None, shape_keys=None, name="Mesh"):
        self.faces = faces
        self.uv_layer = uv_layer if uv_layer is not None else object()
        self.updated = False
        self.shape_keys = shape_keys
        self.name = name
        self.users = 0
        self.vertices = []
        self.polygons = []
        self.last_pydata = None

    def update(self):
        self.updated = True

    def copy(self):
        copied = _FakeMesh(self.faces, uv_layer=self.uv_layer, shape_keys=self.shape_keys, name=self.name + "_Copy")
        copied.vertices = list(self.vertices)
        copied.polygons = list(self.polygons)
        return copied


class _FakeModifier:
    def __init__(self, name, modifier_type):
        self.name = name
        self.type = modifier_type
        self.subdivision_type = None
        self.levels = 0
        self.render_levels = 0
        self.quality = 0
        self.use_limit_surface = False


class _FakeModifierStack(list):
    def __init__(self):
        super().__init__()
        self.created = []
        self.removed = []

    def new(self, name, type):
        modifier = _FakeModifier(name, type)
        self.append(modifier)
        self.created.append(modifier)
        return modifier

    def remove(self, modifier):
        self.removed.append(modifier)
        if modifier in self:
            super().remove(modifier)


class _FakeEvaluatedObject:
    def __init__(self, mesh):
        self._mesh = mesh

    def to_mesh(self):
        return self._mesh


class _FakeObject:
    def __init__(self, name, faces, uv_layer=None, shape_keys=None, baked_mesh=None):
        self.name = name
        self.type = "MESH"
        self.data = _FakeMesh(faces, uv_layer=uv_layer, shape_keys=shape_keys, name=f"{name}_Mesh")
        self.modifiers = _FakeModifierStack()
        self._baked_mesh = baked_mesh
        self.matrix_world = None
        self.parent = None

    def evaluated_get(self, _depsgraph):
        return _FakeEvaluatedObject(self._baked_mesh or self.data)


class _FakeMeshDataFactory:
    def __init__(self):
        self.created_from_object = []
        self.removed = []

    def new_from_object(self, evaluated_obj, **_kwargs):
        mesh = evaluated_obj.to_mesh()
        self.created_from_object.append(mesh)
        return mesh

    def remove(self, mesh):
        self.removed.append(mesh)


class _BMeshOpsRecorder:
    def __init__(self):
        self.dissolve_calls = []
        self.triangulate_calls = []
        self.created = []

    def new(self):
        bm = _FakeBMesh()
        self.created.append(bm)
        return bm

    def from_edit_mesh(self, mesh):
        return _FakeBMesh(getattr(mesh, "faces", []), uv_layer=getattr(mesh, "uv_layer", object()))

    def update_edit_mesh(self, *_args, **_kwargs):
        return None

    class _Ops:
        def __init__(self, outer):
            self._outer = outer

        def dissolve_edges(self, bm, edges, **kwargs):
            self._outer.dissolve_calls.append(
                {
                    "bm": bm,
                    "edges": list(edges),
                    "kwargs": dict(kwargs),
                }
            )
            return {"edges": edges}

        def triangulate(self, bm, faces, **kwargs):
            self._outer.triangulate_calls.append(
                {
                    "bm": bm,
                    "faces": list(faces),
                    "kwargs": dict(kwargs),
                }
            )
            return {"faces": faces}

    @property
    def ops(self):
        return self._Ops(self)


_mesh_factory = _FakeMeshDataFactory()
_object_factory = types.SimpleNamespace(
    created=[],
    removed=[],
)
def _objects_new(name, mesh):
    obj = _FakeObject(name, [], baked_mesh=None)
    obj.data = mesh
    obj.modifiers = _FakeModifierStack()
    obj._baked_mesh = mesh
    _object_factory.created.append(obj)
    return obj
def _objects_remove(obj, do_unlink=True):
    _object_factory.removed.append((obj, do_unlink))
_bmesh_recorder = _BMeshOpsRecorder()
_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object),
    context=types.SimpleNamespace(mode="OBJECT"),
    data=types.SimpleNamespace(
        meshes=_mesh_factory,
        objects=types.SimpleNamespace(new=_objects_new, remove=_objects_remove),
    ),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("numpy", asarray=lambda value, dtype=None: value, float32=float)
_install_module(
    "bmesh",
    new=_bmesh_recorder.new,
    from_edit_mesh=_bmesh_recorder.from_edit_mesh,
    update_edit_mesh=_bmesh_recorder.update_edit_mesh,
    ops=_bmesh_recorder.ops,
)
_install_module(
    f"{PKG}.utils.color_attribute_utils",
    read_color_attribute_data=lambda *args, **kwargs: None,
    write_color_attribute_data=lambda *args, **kwargs: None,
)
_install_module(
    f"{PKG}.utils.vertex_color_utils",
    build_vertex_color_payload=lambda *args, **kwargs: None,
    convert_color_srgb_to_linear=lambda color: color,
    ensure_color_attribute=lambda *args, **kwargs: None,
)


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "bmtp_mesh_tools.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.bmtp_mesh_tools", module_path)
bmtp_mesh_tools = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bmtp_mesh_tools
spec.loader.exec_module(bmtp_mesh_tools)

for _module_name, _original_module in _ORIGINAL_GLOBAL_MODULES.items():
    if _original_module is None:
        sys.modules.pop(_module_name, None)
    else:
        sys.modules[_module_name] = _original_module


def _build_triangle_pair(uvs_a, uvs_b, bridge_length=1.0, material_index=0, smooth=True):
    v0 = _FakeVert("v0")
    v1 = _FakeVert("v1")
    v2 = _FakeVert("v2")
    v3 = _FakeVert("v3")
    shared = _FakeEdge("shared", (v0, v2), length=bridge_length)
    edge_a0 = _FakeEdge("a0", (v0, v1))
    edge_a1 = _FakeEdge("a1", (v1, v2))
    edge_b0 = _FakeEdge("b0", (v2, v3))
    edge_b1 = _FakeEdge("b1", (v3, v0))
    face_a = _FakeFace("A", (v0, v1, v2), (edge_a0, edge_a1, shared), uvs_a, material_index=material_index, smooth=smooth)
    face_b = _FakeFace("B", (v2, v3, v0), (edge_b0, edge_b1, shared), uvs_b, material_index=material_index, smooth=smooth)
    return face_a, face_b, shared


class BMTPMeshFaceTopologyTests(unittest.TestCase):
    def setUp(self):
        _bmesh_recorder.dissolve_calls.clear()
        _bmesh_recorder.triangulate_calls.clear()
        _bmesh_recorder.created.clear()
        _mesh_factory.created_from_object.clear()
        _mesh_factory.removed.clear()
        _object_factory.created.clear()
        _object_factory.removed.clear()

    def test_collect_uv_islands_splits_faces_on_uv_boundary(self):
        face_a, face_b, _shared = _build_triangle_pair(
            uvs_a=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
            uvs_b=((1.1, 1.0), (0.0, 1.0), (0.0, 0.0)),
        )
        bm = _FakeBMesh([face_a, face_b], uv_layer=object())

        islands = bmtp_mesh_tools._collect_uv_islands([face_a, face_b], bmtp_mesh_tools._get_active_uv_layer(bm))

        self.assertEqual(len(islands), 2)

    def test_convert_tris_to_quads_merges_only_same_uv_island_pairs(self):
        face_a, face_b, shared_ok = _build_triangle_pair(
            uvs_a=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
            uvs_b=((1.0, 1.0), (0.0, 1.0), (0.0, 0.0)),
            bridge_length=2.0,
        )
        face_c, face_d, shared_blocked = _build_triangle_pair(
            uvs_a=((2.0, 0.0), (3.0, 0.0), (3.0, 1.0)),
            uvs_b=((3.2, 1.0), (2.0, 1.0), (2.0, 0.0)),
            bridge_length=3.0,
        )
        bm = _FakeBMesh([face_a, face_b, face_c, face_d], uv_layer=object())

        merged_pairs = bmtp_mesh_tools._convert_tris_to_quads_in_bmesh(bm, selected_only=False)

        self.assertEqual(merged_pairs, 1)
        self.assertEqual(len(_bmesh_recorder.dissolve_calls), 1)
        self.assertEqual(_bmesh_recorder.dissolve_calls[0]["edges"], [shared_ok])
        self.assertNotIn(shared_blocked, _bmesh_recorder.dissolve_calls[0]["edges"])

    def test_triangulate_targets_only_quads_and_ngons(self):
        tri_face = _FakeFace(
            "tri",
            (_FakeVert("t0"), _FakeVert("t1"), _FakeVert("t2")),
            (_FakeEdge("te0", (_FakeVert("t0"), _FakeVert("t1"))), _FakeEdge("te1", (_FakeVert("t1"), _FakeVert("t2"))), _FakeEdge("te2", (_FakeVert("t2"), _FakeVert("t0")))),
            ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        )
        quad_face = types.SimpleNamespace(verts=[1, 2, 3, 4], select=True)
        ngon_face = types.SimpleNamespace(verts=[1, 2, 3, 4, 5], select=True)
        bm = _FakeBMesh([tri_face, quad_face, ngon_face], uv_layer=object())

        affected = bmtp_mesh_tools._triangulate_faces_in_bmesh(bm, selected_only=False)

        self.assertEqual(affected, 2)
        self.assertEqual(len(_bmesh_recorder.triangulate_calls), 1)
        self.assertEqual(len(_bmesh_recorder.triangulate_calls[0]["faces"]), 2)

    def test_run_face_converter_in_object_mode_updates_only_objects_with_targets(self):
        face_a, face_b, _shared = _build_triangle_pair(
            uvs_a=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
            uvs_b=((1.0, 1.0), (0.0, 1.0), (0.0, 0.0)),
        )
        context = types.SimpleNamespace(
            mode="OBJECT",
            selected_objects=[
                _FakeObject("Body", [face_a, face_b]),
                _FakeObject("Hair", [types.SimpleNamespace(verts=[1, 2, 3, 4], select=True)]),
            ],
        )

        processed_objects, affected_faces = bmtp_mesh_tools._run_face_converter(
            context,
            bmtp_mesh_tools._convert_tris_to_quads_in_bmesh,
        )

        self.assertEqual(processed_objects, 1)
        self.assertEqual(affected_faces, 1)
        self.assertEqual(len(_bmesh_recorder.created), 2)
        self.assertTrue(context.selected_objects[0].data.updated)
        self.assertFalse(context.selected_objects[1].data.updated)

    def test_operator_reports_cancelled_when_no_target_faces_exist(self):
        context = types.SimpleNamespace(
            mode="OBJECT",
            selected_objects=[_FakeObject("Body", [types.SimpleNamespace(verts=[1, 2, 3, 4], select=True)])],
        )
        operator = bmtp_mesh_tools.BMTP_OT_TrisToQuadsPreserveUV()
        reports = []
        operator.report = lambda level, message: reports.append((level, message))

        result = operator.execute(context)

        self.assertEqual(result, {"CANCELLED"})
        self.assertTrue(any("No triangle pairs were merged" in str(message) for _level, message in reports))

    def test_bake_limit_surface_uses_temp_subsurf_and_replaces_mesh(self):
        baked_mesh = types.SimpleNamespace(
            name="BakedMesh",
            vertices=[types.SimpleNamespace(co=_FakeCoord(0.0, 0.0, 0.0)), types.SimpleNamespace(co=_FakeCoord(1.0, 0.0, 0.0))],
            polygons=[_FakePolygon((0, 1, 1))],
            update=lambda: None,
            users=0,
        )
        body = _FakeObject("Body", [], baked_mesh=baked_mesh)
        original_new_from_object = _mesh_factory.new_from_object
        _mesh_factory.new_from_object = lambda evaluated_obj, **_kwargs: baked_mesh
        context = types.SimpleNamespace(evaluated_depsgraph_get=lambda: object())

        try:
            success, vertex_count, polygon_count, error = bmtp_mesh_tools._bake_limit_surface_from_temp_subsurf(context, body, levels=1)
        finally:
            _mesh_factory.new_from_object = original_new_from_object

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(vertex_count, 2)
        self.assertEqual(polygon_count, 1)
        self.assertEqual(len(_object_factory.created), 1)
        self.assertEqual(len(_object_factory.created[0].modifiers.created), 1)
        self.assertTrue(_object_factory.created[0].modifiers.created[0].use_limit_surface)
        self.assertEqual(body.data, baked_mesh)
        self.assertEqual(len(_object_factory.removed), 1)

    def test_apply_limit_surface_bakes_object_without_existing_subsurf_modifier(self):
        baked_mesh = types.SimpleNamespace(
            name="BakedMesh",
            vertices=[types.SimpleNamespace(co=_FakeCoord(0.0, 0.0, 0.0)), types.SimpleNamespace(co=_FakeCoord(1.0, 0.0, 0.0))],
            polygons=[_FakePolygon((0, 1, 1))],
            update=lambda: None,
            users=0,
        )
        body = _FakeObject("Body", [], baked_mesh=baked_mesh)
        original_new_from_object = _mesh_factory.new_from_object
        _mesh_factory.new_from_object = lambda evaluated_obj, **_kwargs: baked_mesh
        context = types.SimpleNamespace(
            mode="OBJECT",
            selected_objects=[body],
            evaluated_depsgraph_get=lambda: object(),
        )
        operator = bmtp_mesh_tools.BMTP_OT_EnableSubdivisionLimitSurface()
        reports = []
        operator.report = lambda level, message: reports.append((level, message))

        try:
            result = operator.execute(context)
        finally:
            _mesh_factory.new_from_object = original_new_from_object

        self.assertEqual(result, {"FINISHED"})
        self.assertTrue(any("Baked limit surface on 1 object(s)" in str(message) for _level, message in reports))
        self.assertEqual(len(_object_factory.removed), 1)

    def test_apply_limit_surface_skips_shape_key_objects(self):
        shape_keys = types.SimpleNamespace(key_blocks=[object()])
        body = _FakeObject("Body", [], shape_keys=shape_keys)
        context = types.SimpleNamespace(
            mode="OBJECT",
            selected_objects=[body],
            evaluated_depsgraph_get=lambda: object(),
        )
        operator = bmtp_mesh_tools.BMTP_OT_EnableSubdivisionLimitSurface()
        reports = []
        operator.report = lambda level, message: reports.append((level, message))

        result = operator.execute(context)

        self.assertEqual(result, {"CANCELLED"})
        self.assertTrue(any("shape-key objects were skipped" in str(message) for _level, message in reports))


if __name__ == "__main__":
    unittest.main()
