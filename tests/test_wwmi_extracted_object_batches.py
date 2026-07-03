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


PKG = "_wwmi_extracted_object_batches_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.wwmi", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(f"{PKG}.utils.format_utils", Fatal=RuntimeError)


module_path = Path(__file__).resolve().parents[1] / "ui" / "wwmi" / "extracted_object.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.wwmi.extracted_object", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class _FakeSubmeshJson:
    def __init__(self, *, vertex_limit_vb="vb", cb4_hash="cb4", shape_keys_info=None):
        self.VertexLimitVB = vertex_limit_vb
        self.CB4Hash = cb4_hash
        self.ShapeKeysInfo = shape_keys_info or {}
        self.VertexOffset = 0
        self.VertexCount = 12
        self.VGOffset = 0
        self.VGCount = 0
        self.VGMap = {}
        self.JsonDict = {"IndexOffset": 0, "IndexCount": 18}


class _FakeMetadata:
    def __init__(self, shape_keys_info):
        self.submesh_json = _FakeSubmeshJson(shape_keys_info=shape_keys_info)


class WWMIExtractedObjectBatchTests(unittest.TestCase):
    def test_build_from_submesh_metadata_list_falls_back_to_single_batch(self):
        metadata = _FakeMetadata(
            {
                "offsets_hash": "hash_a",
                "scale_hash": "hash_b",
                "vertex_count": 256,
                "dispatch_y": 8,
                "checksum": 12345,
            }
        )

        extracted = module.ExtractedObjectHelper.build_from_submesh_metadata_list([metadata])

        self.assertEqual(len(extracted.shapekeys.batches), 1)
        self.assertEqual(extracted.shapekeys.batches[0]["vertex_offset"], 0)
        self.assertEqual(extracted.shapekeys.batches[0]["vertex_count"], 256)
        self.assertEqual(extracted.shapekeys.batches[0]["dispatch_y"], 8)
        self.assertEqual(extracted.shapekeys.batches[0]["checksum"], 12345)


if __name__ == "__main__":
    unittest.main()
