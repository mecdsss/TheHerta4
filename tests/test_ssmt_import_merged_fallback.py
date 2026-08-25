"""SSMT 导入级合并 VGMap 覆盖契约测试（纯 stub，不依赖 Blender）。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "ssmt_import_fallback_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module(
    "bpy",
    types=types.SimpleNamespace(Collection=object),
)
_install_module(f"{PKG}.common.d3d11_element", D3D11Element=object)

_global_properties = types.SimpleNamespace(import_merged_vgmap=lambda: True)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=_global_properties,
)
_install_module(
    f"{PKG}.common.import_scene_settings",
    apply_import_render_environment=lambda: None,
)

_mesh_calls = []


def _create_mesh_object(**kwargs):
    _mesh_calls.append(kwargs)
    return None


_install_module(
    f"{PKG}.common.mesh_create_helper",
    MeshCreateHelper=types.SimpleNamespace(create_mesh_object=_create_mesh_object),
)


class _FakeSubmeshJson:
    def __init__(self, json_file_path):
        self.FileName = "part.json"
        self.GamePreset = "EFMI"
        self.WorkGameType = "GPU_TEST"
        self.VGMap = {"0": 7}
        self.VGCount = 1
        self.VGOffset = 7
        self.JsonFilePath = json_file_path
        self.LocalBoundingBoxMin = []
        self.LocalBoundingBoxMax = []
        self.VertexCompressionParams = {}
        self.VertexOffset = 0
        self.VertexCount = 0
        self.JsonDict = {}


_install_module(
    f"{PKG}.common.submesh_json",
    SubmeshJson=_FakeSubmeshJson,
    SubmeshCategoryBuffer=object,
)
_install_module(
    f"{PKG}.utils.format_utils",
    Fatal=RuntimeError,
    FormatUtils=object,
)

module_path = REPO_ROOT / "common" / "ssmt_import_helper.py"
spec = importlib.util.spec_from_file_location(
    f"{PKG}.common.ssmt_import_helper", module_path
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

SSMTImportHelper = module.SSMTImportHelper


class SSMTImportMergedFallbackTests(unittest.TestCase):
    def setUp(self):
        _mesh_calls.clear()
        self._old_parse_category = SSMTImportHelper.parse_category_buffers
        self._old_parse_index = SSMTImportHelper.parse_index_buffer
        SSMTImportHelper.parse_category_buffers = staticmethod(
            lambda _json: ([], {}, 0, {})
        )
        SSMTImportHelper.parse_index_buffer = staticmethod(
            lambda _json: ([], 0, 0)
        )

    def tearDown(self):
        SSMTImportHelper.parse_category_buffers = self._old_parse_category
        SSMTImportHelper.parse_index_buffer = self._old_parse_index

    def test_failed_batch_override_ignores_stale_json_vgmap(self):
        SSMTImportHelper.create_mesh_from_json(
            "part.json", use_merged_vgmap=False
        )
        self.assertIsNone(_mesh_calls[-1]["wwmi_vg_map"])

    def test_successful_batch_override_uses_generated_vgmap(self):
        SSMTImportHelper.create_mesh_from_json(
            "part.json", use_merged_vgmap=True
        )
        self.assertEqual(_mesh_calls[-1]["wwmi_vg_map"], {"0": 7})

    def test_unspecified_override_preserves_global_option(self):
        SSMTImportHelper.create_mesh_from_json("part.json")
        self.assertEqual(_mesh_calls[-1]["wwmi_vg_map"], {"0": 7})


if __name__ == "__main__":
    unittest.main()
