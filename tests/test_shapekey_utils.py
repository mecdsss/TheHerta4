import importlib.util
import sys
import types
import unittest
from pathlib import Path


sys.modules.setdefault("TheHerta4", types.ModuleType("TheHerta4"))
sys.modules.setdefault("TheHerta4.utils", types.ModuleType("TheHerta4.utils"))
sys.modules["TheHerta4.utils"].__path__ = []

if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace()

if "mathutils" not in sys.modules:
    sys.modules["mathutils"] = types.SimpleNamespace(Matrix=object)
elif not hasattr(sys.modules["mathutils"], "Matrix"):
    setattr(sys.modules["mathutils"], "Matrix", object)

if "TheHerta4.utils.timer_utils" not in sys.modules:
    sys.modules["TheHerta4.utils.timer_utils"] = types.SimpleNamespace(
        TimerUtils=types.SimpleNamespace(End=lambda *_args, **_kwargs: None)
    )


module_path = Path(__file__).resolve().parents[1] / "utils" / "shapekey_utils.py"
spec = importlib.util.spec_from_file_location("TheHerta4.utils.shapekey_utils", module_path)
shapekey_utils_module = importlib.util.module_from_spec(spec)
sys.modules["TheHerta4.utils.shapekey_utils"] = shapekey_utils_module
spec.loader.exec_module(shapekey_utils_module)
ShapeKeyUtils = shapekey_utils_module.ShapeKeyUtils


class _FakeOpsObject:
    def __init__(self, context):
        self._context = context
        self.mode_set_calls = []
        self.transform_apply_calls = []

    def mode_set(self, mode):
        active_object = getattr(getattr(self._context, "view_layer", None), "objects", types.SimpleNamespace(active=None)).active
        if active_object is not None:
            active_object.mode = mode
        self.mode_set_calls.append(mode)

    def transform_apply(self, **kwargs):
        self.transform_apply_calls.append(kwargs)


class _FakeShapeKey:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class _FakeModifier:
    def __init__(self, name, show_viewport=True):
        self.name = name
        self.show_viewport = show_viewport


class _FakeEvaluatedVertexCollection:
    def __init__(self, coords):
        self._coords = [list(co) for co in coords]

    def __len__(self):
        return len(self._coords)

    def foreach_get(self, attr, target):
        if attr != "co":
            raise AssertionError(attr)
        flat = []
        for coord in self._coords:
            flat.extend(coord)
        for index, value in enumerate(flat):
            target[index] = value

    def foreach_set(self, attr, values):
        if attr != "co":
            raise AssertionError(attr)
        flat = list(values)
        for index in range(len(self._coords)):
            start = index * 3
            self._coords[index] = flat[start:start + 3]


class _FakeMeshVertex:
    def __init__(self, co):
        self.co = list(co)


class _FakeMeshVertexCollection(list):
    def foreach_set(self, attr, values):
        if attr != "co":
            raise AssertionError(attr)
        flat = list(values)
        for index, vertex in enumerate(self):
            start = index * 3
            vertex.co = flat[start:start + 3]

    def foreach_get(self, attr, target):
        if attr != "co":
            raise AssertionError(attr)
        flat = []
        for vertex in self:
            flat.extend(vertex.co)
        for index, value in enumerate(flat):
            target[index] = value


class _FakeMesh:
    def __init__(self, coords):
        self.vertices = _FakeMeshVertexCollection(_FakeMeshVertex(co) for co in coords)
        self.update_called = False

    def update(self):
        self.update_called = True


class _FakeEvaluatedMesh:
    def __init__(self, coords):
        self.vertices = _FakeEvaluatedVertexCollection(coords)


class _FakeEvaluatedObject:
    def __init__(self, coords):
        self._mesh = _FakeEvaluatedMesh(coords)
        self.cleared = False

    def to_mesh(self):
        return self._mesh

    def to_mesh_clear(self):
        self.cleared = True


class _FakeMeshObject:
    def __init__(self, name, basis_coords, evaluated_coords):
        self.name = name
        self.type = "MESH"
        self.data = _FakeMesh(basis_coords)
        self._evaluated_object = _FakeEvaluatedObject(evaluated_coords)
        self.mode = "OBJECT"
        self.selected = False
        self.matrix_basis = _FakeMatrix()
        self.modifiers = []

    def evaluated_get(self, _depsgraph):
        return self._evaluated_object

    def select_set(self, value):
        self.selected = bool(value)

    def update_from_editmode(self):
        return None


class _FakeQuaternion:
    w = 1.0
    x = 0.0
    y = 0.0
    z = 0.0

    def to_matrix(self):
        return _FakeMatrix3()


class _FakeVector:
    x = 1.0
    y = 1.0
    z = 1.0


class _FakeMatrix3:
    def to_4x4(self):
        return _FakeMatrix()


class _FakeMatrix:
    def __iter__(self):
        import numpy as _np
        return iter(_np.eye(4).tolist())

    def copy(self):
        return self

    @property
    def translation(self):
        return (0.0, 0.0, 0.0)

    @translation.setter
    def translation(self, _value):
        return None

    def decompose(self):
        return ((0.0, 0.0, 0.0), _FakeQuaternion(), _FakeVector())

    def __array__(self, dtype=None, copy=None):
        import numpy as _np
        matrix = _np.eye(4, dtype=dtype if dtype is not None else float)
        return matrix.copy() if copy is True else matrix


class ShapeKeyUtilsTests(unittest.TestCase):
    def test_reset_shapekey_values_defaults_to_resetting_all_non_basis_keys(self):
        key_blocks = [
            _FakeShapeKey("Basis", 0.0),
            _FakeShapeKey("Smile", 1.0),
            _FakeShapeKey("Blink", 0.5),
        ]
        obj = types.SimpleNamespace(
            data=types.SimpleNamespace(
                shape_keys=types.SimpleNamespace(key_blocks=key_blocks)
            )
        )

        ShapeKeyUtils.reset_shapekey_values(obj)

        self.assertEqual(key_blocks[0].value, 0.0)
        self.assertEqual(key_blocks[1].value, 0.0)
        self.assertEqual(key_blocks[2].value, 0.0)

    def test_reset_shapekey_values_preserves_current_key_when_target_list_is_given(self):
        key_blocks = [
            _FakeShapeKey("Basis", 0.0),
            _FakeShapeKey("Smile", 1.0),
            _FakeShapeKey("Blink", 0.5),
            _FakeShapeKey("Other", 0.75),
        ]
        obj = types.SimpleNamespace(
            data=types.SimpleNamespace(
                shape_keys=types.SimpleNamespace(key_blocks=key_blocks)
            )
        )

        ShapeKeyUtils.reset_shapekey_values(
            obj,
            configured_shapekey_names={"Smile", "Blink"},
            current_shapekey_name="Smile",
        )

        self.assertEqual(key_blocks[1].value, 1.0)
        self.assertEqual(key_blocks[2].value, 0.0)
        self.assertEqual(key_blocks[3].value, 0.75)

    def test_bake_current_shape_key_mix_to_mesh_reads_evaluated_mesh_instead_of_raw_vertices(self):
        shapekey_utils_module.bpy.context = types.SimpleNamespace(
            evaluated_depsgraph_get=lambda: object(),
            view_layer=types.SimpleNamespace(update=lambda: None),
        )
        obj = _FakeMeshObject(
            name="Mesh",
            basis_coords=[(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)],
            evaluated_coords=[(2.0, 2.0, 2.0), (3.0, 3.0, 3.0)],
        )
        basis_data = _FakeEvaluatedVertexCollection([(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)])
        obj.data.shape_keys = types.SimpleNamespace(
            key_blocks=[
                types.SimpleNamespace(name="Basis", data=basis_data),
                types.SimpleNamespace(name="Smile"),
            ]
        )
        visible_modifier = _FakeModifier("Armature", show_viewport=True)
        hidden_modifier = _FakeModifier("Hidden", show_viewport=False)
        obj.modifiers = [visible_modifier, hidden_modifier]

        result = ShapeKeyUtils.bake_current_shape_key_mix_to_mesh(obj, "测试烘焙")

        self.assertTrue(result)
        self.assertEqual(
            [tuple(vertex.co) for vertex in obj.data.vertices],
            [(2.0, 2.0, 2.0), (3.0, 3.0, 3.0)],
        )
        self.assertTrue(obj.data.update_called)
        self.assertTrue(obj._evaluated_object.cleared)
        self.assertEqual(
            [tuple(coord) for coord in basis_data._coords],
            [(2.0, 2.0, 2.0), (3.0, 3.0, 3.0)],
        )
        self.assertTrue(visible_modifier.show_viewport)
        self.assertFalse(hidden_modifier.show_viewport)

    def test_has_exportable_shape_keys_excludes_basis_name(self):
        basis_obj = types.SimpleNamespace(
            data=types.SimpleNamespace(
                shape_keys=types.SimpleNamespace(
                    key_blocks=[
                        types.SimpleNamespace(name="Basis"),
                    ]
                )
            )
        )
        smile_obj = types.SimpleNamespace(
            data=types.SimpleNamespace(
                shape_keys=types.SimpleNamespace(
                    key_blocks=[
                        types.SimpleNamespace(name="Basis"),
                        types.SimpleNamespace(name="Smile"),
                    ]
                )
            )
        )

        self.assertEqual(ShapeKeyUtils.count_exportable_shape_keys(basis_obj), 0)
        self.assertFalse(ShapeKeyUtils.has_exportable_shape_keys(basis_obj))
        self.assertEqual(ShapeKeyUtils.count_exportable_shape_keys(smile_obj), 1)
        self.assertTrue(ShapeKeyUtils.has_exportable_shape_keys(smile_obj))

    def test_transform_apply_preserve_shape_keys_switches_to_object_mode_and_restores_original_mode(self):
        context = types.SimpleNamespace(
            selected_objects=[],
            evaluated_depsgraph_get=lambda: object(),
        )
        context.view_layer = types.SimpleNamespace(objects=types.SimpleNamespace(active=None), update=lambda: None)
        ops_object = _FakeOpsObject(context)
        shapekey_utils_module.bpy.context = context
        shapekey_utils_module.bpy.ops = types.SimpleNamespace(object=ops_object)
        shapekey_utils_module.bpy.data = types.SimpleNamespace(objects={"Mesh": object(), "Other": object()})

        active_obj = _FakeMeshObject(
            name="Other",
            basis_coords=[(0.0, 0.0, 0.0)],
            evaluated_coords=[(0.0, 0.0, 0.0)],
        )
        active_obj.mode = "EDIT"
        target_obj = _FakeMeshObject(
            name="Mesh",
            basis_coords=[(0.0, 0.0, 0.0)],
            evaluated_coords=[(1.0, 1.0, 1.0)],
        )
        target_obj.data.shape_keys = types.SimpleNamespace(
            key_blocks=[types.SimpleNamespace(name="Basis", data=_FakeEvaluatedVertexCollection([(0.0, 0.0, 0.0)]))]
        )
        context.view_layer.objects.active = active_obj
        context.selected_objects[:] = [active_obj]

        ShapeKeyUtils.transform_apply_preserve_shape_keys(target_obj, location=True, rotation=True, scale=True)

        self.assertGreaterEqual(len(ops_object.mode_set_calls), 2)
        self.assertEqual(ops_object.mode_set_calls[0], "OBJECT")
        self.assertEqual(ops_object.mode_set_calls[-1], "EDIT")
        self.assertEqual(active_obj.mode, "EDIT")
        self.assertTrue(ops_object.transform_apply_calls)


if __name__ == "__main__":
    unittest.main()
