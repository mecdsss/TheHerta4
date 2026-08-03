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


class _FakeVector:
    def __init__(self, values):
        self.x, self.y, self.z = (float(value) for value in values)

    @property
    def length_squared(self):
        return self.x * self.x + self.y * self.y + self.z * self.z

    def normalize(self):
        length = self.length_squared ** 0.5
        self.x /= length
        self.y /= length
        self.z /= length


class _ReflectionMatrix:
    @classmethod
    def Scale(cls, _factor, _dimensions, _axis):
        return cls()

    def inverted_safe(self):
        return self

    def transposed(self):
        return self

    def to_3x3(self):
        return self

    def __matmul__(self, vector):
        return _FakeVector((-vector.x, vector.y, vector.z))


class _FakeVertexCollection:
    def __init__(self, normals):
        self.normals = [tuple(normal) for normal in normals]

    def __len__(self):
        return len(self.normals)

    def foreach_get(self, field_name, output):
        if field_name != "normal":
            raise AssertionError(field_name)
        output[:] = [component for normal in self.normals for component in normal]


class _FakeLoop:
    def __init__(self, vertex_index, normal):
        self.vertex_index = vertex_index
        self.normal = tuple(normal)


class _FakeLoopCollection(list):
    def foreach_get(self, field_name, output):
        if field_name == "normal":
            values = [component for loop in self for component in loop.normal]
        elif field_name == "vertex_index":
            values = [loop.vertex_index for loop in self]
        else:
            raise AssertionError(field_name)
        output[:] = values


class _FakePolygon:
    def __init__(self, loop_indices):
        self.loop_indices = tuple(loop_indices)


class _FakeMesh:
    def __init__(self):
        self.vertices = _FakeVertexCollection([(0.0, 0.0, 1.0)] * 4)
        self.loops = _FakeLoopCollection(
            [
                _FakeLoop(0, (1.0, 0.0, 0.0)),
                _FakeLoop(1, (0.0, 1.0, 0.0)),
                _FakeLoop(2, (0.0, 0.0, 1.0)),
                _FakeLoop(0, (-1.0, 0.0, 0.0)),
                _FakeLoop(2, (0.0, 0.0, -1.0)),
                _FakeLoop(3, (0.0, -1.0, 0.0)),
            ]
        )
        self.polygons = [_FakePolygon(range(0, 3)), _FakePolygon(range(3, 6))]
        self.shape_keys = None
        self.written_normals = None

    def transform(self, _matrix, shape_keys=False):
        return None

    def reverse_face_loops(self):
        for polygon in self.polygons:
            indices = list(polygon.loop_indices)
            source_vertex_indices = [self.loops[index].vertex_index for index in indices]
            reversed_vertex_indices = source_vertex_indices[:1] + source_vertex_indices[:0:-1]
            for loop_index, vertex_index in zip(indices, reversed_vertex_indices):
                self.loops[loop_index].vertex_index = vertex_index
                self.loops[loop_index].normal = (0.0, 0.0, 0.0)

    def calc_normals(self):
        return None

    def calc_normals_split(self):
        return None

    def normals_split_custom_set_from_vertices(self, normals):
        self.written_normals = [tuple(normals[loop.vertex_index]) for loop in self.loops]

    def normals_split_custom_set(self, normals):
        self.written_normals = [tuple(normal) for normal in normals]

    def update(self):
        return None


class _FakeBMesh:
    def __init__(self):
        self.faces = [object(), object()]
        self.mesh = None
        self.reversed = False

    def from_mesh(self, mesh):
        self.mesh = mesh

    def to_mesh(self, mesh):
        if self.reversed:
            mesh.reverse_face_loops()

    def free(self):
        return None


class _FakeBMeshOps:
    @staticmethod
    def reverse_faces(bmesh, faces):
        bmesh.reversed = bool(faces)


PKG = "_non_mirror_workflow_normals_test_pkg"
for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module(
    "bpy",
    types=types.SimpleNamespace(Object=object, Mesh=object),
)
_install_module(
    "bmesh",
    new=_FakeBMesh,
    ops=_FakeBMeshOps,
)
_install_module(
    "mathutils",
    Matrix=_ReflectionMatrix,
    Vector=_FakeVector,
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    ),
)

module_path = Path(__file__).resolve().parents[1] / "common" / "non_mirror_workflow.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.common.non_mirror_workflow", module_path)
non_mirror_workflow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = non_mirror_workflow
spec.loader.exec_module(non_mirror_workflow)

NonMirrorWorkflowHelper = non_mirror_workflow.NonMirrorWorkflowHelper


class NonMirrorWorkflowNormalTests(unittest.TestCase):
    def test_mirror_preserves_distinct_corner_normals_across_a_sharp_edge(self):
        mesh = _FakeMesh()
        obj = types.SimpleNamespace(name="SharpEdge", type="MESH", data=mesh)

        NonMirrorWorkflowHelper._mirror_apply_and_flip(obj)

        self.assertEqual(
            mesh.written_normals,
            [
                (-1.0, 0.0, 0.0),
                (0.0, 0.0, 1.0),
                (0.0, 1.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, -1.0, 0.0),
                (0.0, 0.0, -1.0),
            ],
        )


if __name__ == "__main__":
    unittest.main()
