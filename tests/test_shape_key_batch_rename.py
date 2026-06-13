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


PKG = "_shape_key_batch_rename_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
)
_install_module(f"{PKG}.utils.shapekey_rebase_utils", rebase_shape_key_coordinates=lambda **_kwargs: {})
_install_module(f"{PKG}.toolkit.at_shape_key_control", refresh_shape_key_list=lambda *_args, **_kwargs: None)


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shape_key_ops = _load_module("toolkit.at_shape_key_operations", "toolkit/at_shape_key_operations.py")
ATP_OT_BatchRenameShapeKey = shape_key_ops.ATP_OT_BatchRenameShapeKey
_build_shape_key_rename_plan = shape_key_ops._build_shape_key_rename_plan


class _FakeKeyBlock:
    def __init__(self, name):
        self.name = name


class _FakeKeyBlocks(list):
    def get(self, name):
        return next((item for item in self if item.name == name), None)


class _FakeShapeKeys:
    def __init__(self, names):
        self.key_blocks = _FakeKeyBlocks(_FakeKeyBlock(name) for name in names)


class _FakeMeshObject:
    def __init__(self, name, key_names):
        self.name = name
        self.type = "MESH"
        self.data = types.SimpleNamespace(shape_keys=_FakeShapeKeys(key_names))


class ShapeKeyBatchRenameTests(unittest.TestCase):
    def test_rename_plan_uses_substring_replace(self):
        key_blocks = _FakeKeyBlocks([
            _FakeKeyBlock("Face_A"),
            _FakeKeyBlock("Face_Blink"),
            _FakeKeyBlock("Jaw_Open"),
        ])

        plan = _build_shape_key_rename_plan(key_blocks, "Face_", "Head_")

        self.assertEqual(
            [
                (current, bool(temp_name), replaced, conflict)
                for _key, current, temp_name, replaced, conflict in plan
            ],
            [
                ("Face_A", True, "Head_A", False),
                ("Face_Blink", True, "Head_Blink", False),
            ],
        )

    def test_batch_rename_replaces_all_matching_name_segments(self):
        obj = _FakeMeshObject("MeshA", ["Face_A", "Face_B", "Jaw_Open"])
        props = types.SimpleNamespace(sk_rename_old_name="Face_", sk_rename_new_name="Head_")
        context = types.SimpleNamespace(
            selected_objects=[obj],
            active_object=obj,
            scene=types.SimpleNamespace(atp_props=props),
        )

        operator = ATP_OT_BatchRenameShapeKey()
        reports = []
        operator.report = lambda kinds, message: reports.append((kinds, message))

        result = operator.execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(
            [key_block.name for key_block in obj.data.shape_keys.key_blocks],
            ["Head_A", "Head_B", "Jaw_Open"],
        )
        self.assertEqual(len(reports), 1)
        self.assertIn("2", reports[0][1])

    def test_batch_rename_skips_conflicts_after_replace(self):
        obj = _FakeMeshObject("MeshB", ["Face_A", "Head_A", "Face_B"])
        props = types.SimpleNamespace(sk_rename_old_name="Face_", sk_rename_new_name="Head_")
        context = types.SimpleNamespace(
            selected_objects=[obj],
            active_object=obj,
            scene=types.SimpleNamespace(atp_props=props),
        )

        operator = ATP_OT_BatchRenameShapeKey()
        reports = []
        operator.report = lambda kinds, message: reports.append((kinds, message))

        result = operator.execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(
            [key_block.name for key_block in obj.data.shape_keys.key_blocks],
            ["Face_A", "Head_A", "Head_B"],
        )
        self.assertEqual(len(reports), 2)
        self.assertIn("1", reports[-1][1])

    def test_batch_rename_uses_two_step_plan_for_chain_replacements(self):
        obj = _FakeMeshObject("MeshC", ["A", "AA", "B"])
        props = types.SimpleNamespace(sk_rename_old_name="A", sk_rename_new_name="AA")
        context = types.SimpleNamespace(
            selected_objects=[obj],
            active_object=obj,
            scene=types.SimpleNamespace(atp_props=props),
        )

        operator = ATP_OT_BatchRenameShapeKey()
        operator.report = lambda *_args, **_kwargs: None

        result = operator.execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(
            [key_block.name for key_block in obj.data.shape_keys.key_blocks],
            ["AA", "AAAA", "B"],
        )


if __name__ == "__main__":
    unittest.main()
