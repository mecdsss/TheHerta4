"""骨骼合并 JSON 原始元数据严格校验测试。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG = "submesh_json_contract_test_pkg"


def _package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


for name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _package(name)

json_utils = types.ModuleType(f"{PKG}.utils.json_utils")
json_utils.JsonUtils = types.SimpleNamespace(LoadFromFile=lambda _path: {})
sys.modules[json_utils.__name__] = json_utils
element = types.ModuleType(f"{PKG}.common.d3d11_element")
element.D3D11Element = object
sys.modules[element.__name__] = element

spec = importlib.util.spec_from_file_location(
    f"{PKG}.common.submesh_json", ROOT / "common" / "submesh_json.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class SubmeshJsonMetadataContractTests(unittest.TestCase):
    def test_clean_complete_map_is_valid(self):
        self.assertTrue(module._merged_skeleton_metadata_valid({
            "VGOffset": 7,
            "VGCount": 2,
            "VGMap": {"0": 7, "1": 8},
            "SkeletonGroup": 1,
        }))

    def test_nonintegral_values_are_rejected_before_int_truncation(self):
        for payload in (
            {"VGCount": 1.5, "VGMap": {"0": 0}},
            {"VGCount": 1, "VGMap": {"0": 1.5}},
            {"VGCount": 1, "VGMap": {"0": 0}, "SkeletonGroup": True},
        ):
            with self.subTest(payload=payload):
                self.assertFalse(module._merged_skeleton_metadata_valid(payload))

    def test_missing_and_duplicate_keys_are_rejected(self):
        self.assertFalse(module._merged_skeleton_metadata_valid({
            "VGCount": 2, "VGMap": {"0": 0},
        }))
        self.assertFalse(module._merged_skeleton_metadata_valid({
            "VGCount": 1, "VGMap": {"0": 0, "00": 1},
        }))


if __name__ == "__main__":
    unittest.main()
