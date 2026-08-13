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


PKG = "_shape_key_batch_remove_test_pkg"
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
ATP_OT_BatchRemoveShapeKey = shape_key_ops.ATP_OT_BatchRemoveShapeKey


class _FakeKeyBlock:
    def __init__(self, name):
        self.name = name


class _FakeKeyBlocks(list):
    def get(self, name):
        return next((item for item in self if item.name == name), None)


class _FakeShapeKeys:
    def __init__(self, names):
        self.key_blocks = _FakeKeyBlocks(_FakeKeyBlock(name) for name in names)
        self.reference_key = self.key_blocks[0] if self.key_blocks else None


class _FakeMeshObject:
    def __init__(self, name, key_names):
        self.name = name
        self.type = "MESH"
        self.data = types.SimpleNamespace(shape_keys=_FakeShapeKeys(key_names))
        self.removed = []

    def shape_key_remove(self, key_block):
        self.removed.append(key_block.name)
        was_reference = key_block is self.data.shape_keys.reference_key
        self.data.shape_keys.key_blocks.remove(key_block)
        if was_reference:
            self.data.shape_keys.reference_key = (
                self.data.shape_keys.key_blocks[0] if self.data.shape_keys.key_blocks else None
            )
        if not self.data.shape_keys.key_blocks:
            self.data.shape_keys = None


def _run_remove(obj, name):
    props = types.SimpleNamespace(sk_add_new_name=name)
    context = types.SimpleNamespace(
        selected_objects=[obj],
        active_object=obj,
        scene=types.SimpleNamespace(atp_props=props),
    )
    operator = ATP_OT_BatchRemoveShapeKey()
    reports = []
    operator.report = lambda kinds, message: reports.append((kinds, message))
    result = operator.execute(context)
    return result, reports


class ShapeKeyBatchRemoveTests(unittest.TestCase):
    def test_remove_regular_shape_key(self):
        obj = _FakeMeshObject("MeshA", ["Basis", "Smile", "Blink"])

        result, reports = _run_remove(obj, "Smile")

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(obj.removed, ["Smile"])
        self.assertEqual([kb.name for kb in obj.data.shape_keys.key_blocks], ["Basis", "Blink"])
        self.assertEqual(len(reports), 1)
        self.assertIn("1", reports[0][1])

    def test_remove_basis_with_other_keys_is_skipped(self):
        obj = _FakeMeshObject("MeshB", ["Basis", "Smile", "Blink"])

        result, reports = _run_remove(obj, "Basis")

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(obj.removed, [])
        self.assertEqual([kb.name for kb in obj.data.shape_keys.key_blocks], ["Basis", "Smile", "Blink"])
        self.assertIn("不能单独删除 Basis", reports[0][1])

    def test_remove_basis_lowercase_works_when_only_basis(self):
        obj = _FakeMeshObject("MeshC", ["Basis", "Smile"])

        result, reports = _run_remove(obj, "basis")

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(obj.removed, [])
        self.assertEqual([kb.name for kb in obj.data.shape_keys.key_blocks], ["Basis", "Smile"])

    def test_remove_lowercase_basis_when_only_basis_clears(self):
        obj = _FakeMeshObject("MeshF", ["Basis"])

        result, reports = _run_remove(obj, "basis")

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(obj.removed, ["Basis"])
        self.assertIsNone(obj.data.shape_keys)

    def test_remove_only_basis_clears_shape_keys(self):
        obj = _FakeMeshObject("MeshD", ["Basis"])

        result, reports = _run_remove(obj, "Basis")

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(obj.removed, ["Basis"])
        self.assertIsNone(obj.data.shape_keys)

    def test_remove_missing_key_reports_not_found(self):
        obj = _FakeMeshObject("MeshE", ["Basis", "Smile"])

        result, reports = _run_remove(obj, "Missing")

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(obj.removed, [])
        self.assertEqual(len(reports), 1)
        self.assertIn("未找到", reports[0][1])


if __name__ == "__main__":
    unittest.main()
