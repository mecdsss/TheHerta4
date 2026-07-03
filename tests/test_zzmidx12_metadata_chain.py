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


PKG = "_zzmidx12_metadata_chain_test_pkg"
for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils", f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    data=types.SimpleNamespace(node_groups=[]),
    context=types.SimpleNamespace(),
)


class _FakeSubmeshJson:
    def __init__(self, _json_path):
        self.JsonDict = {
            "WorkGameType": "ZZMIDX12",
            "VertexLimitVB": "deadbeef",
            "CategoryHash": {},
            "TextureMarkUpInfoList": [],
            "PartName": "Body",
        }
        self.WorkGameType = "ZZMIDX12"
        self.VertexLimitVB = "deadbeef"
        self.CategoryHash = {}
        self.TextureMarkUpInfoList = []
        self.MatchCS = "abcd1234"
        self.MatchUAVBytes = 4096


class _FakeD3D11GameType:
    @staticmethod
    def from_submesh_json_dict(*_args, **_kwargs):
        return "fake_game_type"


_install_module(
    f"{PKG}.utils.format_utils",
    Fatal=RuntimeError,
)
_install_module(
    f"{PKG}.utils.json_utils",
    JsonUtils=types.SimpleNamespace(LoadFromFile=lambda _path: {}),
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(get_datatype_node_info=lambda: []),
)
_install_module(
    f"{PKG}.blueprint.node_datatype",
    reset_datatype_override_log=lambda: None,
    build_override_element_list=lambda *args, **kwargs: None,
)
_install_module(
    f"{PKG}.common.d3d11_gametype",
    D3D11GameType=_FakeD3D11GameType,
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(path_workspace_folder=lambda: ""),
)
_install_module(
    f"{PKG}.common.workspace_helper",
    WorkSpaceHelper=types.SimpleNamespace(
        parse_lod_unique_str=lambda unique_str: ("LOD0", unique_str),
        get_submesh_folder_path=lambda _unique_str: "workspace_dir",
    ),
)
_install_module(
    f"{PKG}.common.submesh_json",
    SubmeshJson=_FakeSubmeshJson,
)


metadata_module_path = Path(__file__).resolve().parents[1] / "common" / "submesh_metadata.py"
metadata_spec = importlib.util.spec_from_file_location(f"{PKG}.common.submesh_metadata", metadata_module_path)
submesh_metadata_module = importlib.util.module_from_spec(metadata_spec)
sys.modules[metadata_spec.name] = submesh_metadata_module
metadata_spec.loader.exec_module(submesh_metadata_module)


class ZZMIDX12MetadataChainTests(unittest.TestCase):
    def test_submesh_metadata_exposes_match_cs_and_match_uav_bytes(self):
        original_checker = submesh_metadata_module.check_and_get_submesh_json_path
        try:
            submesh_metadata_module.check_and_get_submesh_json_path = lambda _unique_str: (True, "", "fake.json")
            metadata = submesh_metadata_module.SubmeshMetadata("LOD0.fake")
        finally:
            submesh_metadata_module.check_and_get_submesh_json_path = original_checker

        self.assertEqual(metadata.match_cs, "abcd1234")
        self.assertEqual(metadata.match_uav_bytes, 4096)


if __name__ == "__main__":
    unittest.main()
