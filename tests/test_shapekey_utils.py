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


class _FakeShapeKey:
    def __init__(self, name, value):
        self.name = name
        self.value = value


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


if __name__ == "__main__":
    unittest.main()
