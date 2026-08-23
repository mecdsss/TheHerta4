"""工作空间未使用 IB 文件夹清理（WorkSpaceHelper 侧）单测。

场景：全量提取会在工作空间生成大量无关 IB 子网格文件夹；用户导入后手动清理
（30 个只留 10 个），但文件夹仍在，下次一键导入会再次全部导入。清理按钮以
当前场景对象保留的 (lod_name, bare_name) 身份键为准，删除未保留的文件夹。

覆盖：
- 根目录直铺子网格文件夹的清理；
- 多 LOD：LOD0/LOD1 同名 bare 互不保留（按 LOD 前缀精确匹配）；
- 裸键不保护 LOD 文件夹、LOD 键不保护根目录文件夹；
- 分区目录（含 Config.json）及分区内 LOD 子目录的枚举；
- 别名后缀文件夹（xxx-1-0.Face）按完整名字精确匹配；
- 根目录下 "LOD0.xxx-1-0" 式文件夹名解析；
- 非子网格目录（Config、无横杠目录）永不作清理候选。
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "workspace_ib_cleanup_test_pkg"


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(qualname, path):
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


sys.modules["bpy"] = types.SimpleNamespace(
    types=types.SimpleNamespace(Collection=object),
    context=types.SimpleNamespace(scene=types.SimpleNamespace(collection=object())),
)

for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(_name)

_load_module(f"{PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py")

_collection_utils = types.ModuleType(f"{PKG}.utils.collection_utils")
_collection_utils.CollectionColor = types.SimpleNamespace(Red=0)
_collection_utils.CollectionUtils = types.SimpleNamespace(
    create_new_collection=lambda *args, **kwargs: object(),
)
sys.modules[f"{PKG}.utils.collection_utils"] = _collection_utils


class _GlobalConfig:
    workspace_folder = ""

    @classmethod
    def path_workspace_folder(cls):
        return _GlobalConfig.workspace_folder


_config_module = types.ModuleType(f"{PKG}.common.global_config")
_config_module.GlobalConfig = _GlobalConfig
sys.modules[f"{PKG}.common.global_config"] = _config_module

_workspace = _load_module(f"{PKG}.common.workspace_helper", REPO_ROOT / "common" / "workspace_helper.py")
WorkSpaceHelper = _workspace.WorkSpaceHelper


class WorkspaceIBCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _GlobalConfig.workspace_folder = str(self.root) + os.sep

    def tearDown(self):
        _GlobalConfig.workspace_folder = ""
        self._tmp.cleanup()

    def _make_submesh(self, rel_path: str) -> Path:
        folder = self.root / rel_path
        folder.mkdir(parents=True, exist_ok=True)
        # 子网格文件夹内部的 TYPE_ 内容应随父文件夹一并删除
        (folder / "TYPE_GPU-X").mkdir(parents=True, exist_ok=True)
        return folder

    def _unwanted(self, kept_pairs):
        return WorkSpaceHelper.get_unwanted_submesh_folder_list(kept_pairs)

    def test_root_plain_folders_cleanup(self):
        keep_folder = self._make_submesh("aaaabbbb-100-0")
        del_folder_a = self._make_submesh("ccccdddd-200-0")
        del_folder_b = self._make_submesh("eeeeffff-300-1")

        unwanted = self._unwanted({("", "aaaabbbb-100-0")})

        self.assertEqual(unwanted, [str(del_folder_a), str(del_folder_b)])
        self.assertNotIn(str(keep_folder), unwanted)

    def test_lod_prefix_exact_matching(self):
        self._make_submesh("LOD0/aaaabbbb-100-0")
        self._make_submesh("LOD0/ccccdddd-200-0")
        self._make_submesh("LOD1/aaaabbbb-100-0")

        # 场景只保留 LOD0.aaaabbbb-100-0：
        # LOD0 的另一个子网格、以及 LOD1 的同名子网格都要删除
        unwanted = self._unwanted({("LOD0", "aaaabbbb-100-0")})

        self.assertEqual(
            unwanted,
            [
                str(self.root / "LOD1" / "aaaabbbb-100-0"),
                str(self.root / "LOD0" / "ccccdddd-200-0"),
            ],
        )

    def test_bare_kept_does_not_protect_lod_folder(self):
        self._make_submesh("aaaabbbb-100-0")
        self._make_submesh("LOD0/aaaabbbb-100-0")

        unwanted = self._unwanted({("", "aaaabbbb-100-0")})

        self.assertEqual(unwanted, [str(self.root / "LOD0" / "aaaabbbb-100-0")])

    def test_lod_kept_does_not_protect_root_folder(self):
        self._make_submesh("aaaabbbb-100-0")
        self._make_submesh("LOD0/aaaabbbb-100-0")

        unwanted = self._unwanted({("LOD0", "aaaabbbb-100-0")})

        self.assertEqual(unwanted, [str(self.root / "aaaabbbb-100-0")])

    def test_alias_suffix_folder_matches_full_name(self):
        self._make_submesh("aaaabbbb-100-0.Face")
        del_folder = self._make_submesh("ccccdddd-200-0")

        unwanted = self._unwanted({("", "aaaabbbb-100-0.Face")})

        self.assertEqual(unwanted, [str(del_folder)])

        # 场景键缺别名后缀时不匹配带别名的文件夹
        unwanted_mismatch = self._unwanted({("", "aaaabbbb-100-0")})
        self.assertEqual(
            unwanted_mismatch,
            [str(self.root / "aaaabbbb-100-0.Face"), str(del_folder)],
        )

    def test_partition_folders_with_lod_children(self):
        p1 = self.root / "P1"
        p1.mkdir()
        (p1 / "Config.json").write_text("[]", encoding="utf-8")
        self._make_submesh("P1/aaaabbbb-100-0")

        p2 = self.root / "P2"
        p2.mkdir()
        (p2 / "Config.json").write_text("[]", encoding="utf-8")
        self._make_submesh("P2/LOD0/ccccdddd-200-0")
        del_partition_lod = self._make_submesh("P2/LOD0/eeeeffff-300-0")

        unwanted = self._unwanted(
            {
                ("", "aaaabbbb-100-0"),
                ("LOD0", "ccccdddd-200-0"),
            }
        )

        self.assertEqual(unwanted, [str(del_partition_lod)])

    def test_lod_dotted_folder_name_at_root(self):
        self._make_submesh("LOD0.aaaabbbb-100-0")

        self.assertEqual(self._unwanted({("LOD0", "aaaabbbb-100-0")}), [])
        self.assertEqual(
            self._unwanted({("", "aaaabbbb-100-0")}),
            [str(self.root / "LOD0.aaaabbbb-100-0")],
        )

    def test_lod_case_insensitive_matching(self):
        # Windows 文件系统大小写不敏感：不能用仅大小写不同的两个目录，
        # 这里用 LOD0/LOD1 两个不同目录，保留键传小写 lod0 验证归一化匹配
        self._make_submesh("LOD0/aaaabbbb-100-0")
        del_folder = self._make_submesh("LOD1/ccccdddd-200-0")

        unwanted = self._unwanted({("lod0", "aaaabbbb-100-0")})

        self.assertEqual(unwanted, [str(del_folder)])

    def test_non_submesh_dirs_never_candidates(self):
        self._make_submesh("aaaabbbb-100-0")
        (self.root / "Config" / "Tabs").mkdir(parents=True)
        (self.root / "RandomName").mkdir()

        unwanted = self._unwanted({("", "aaaabbbb-100-0")})

        self.assertEqual(unwanted, [])

    def test_empty_or_missing_workspace(self):
        self.assertEqual(self._unwanted({("", "aaaabbbb-100-0")}), [])

        _GlobalConfig.workspace_folder = str(self.root / "not-exist") + os.sep
        self.assertEqual(self._unwanted({("", "aaaabbbb-100-0")}), [])

    def test_empty_kept_set_deletes_everything(self):
        keep_folder = self._make_submesh("aaaabbbb-100-0")
        del_folder = self._make_submesh("ccccdddd-200-0")

        unwanted = self._unwanted(set())

        self.assertEqual(unwanted, [str(keep_folder), str(del_folder)])

    def test_records_cover_root_lod_and_alias(self):
        self._make_submesh("aaaabbbb-100-0")
        self._make_submesh("LOD1/ccccdddd-200-0.Tail")

        records = WorkSpaceHelper.get_submesh_folder_records()

        self.assertEqual(len(records), 2)
        record_by_path = {record["folder_path"]: record for record in records}
        self.assertEqual(
            record_by_path[str(self.root / "aaaabbbb-100-0")],
            {"folder_path": str(self.root / "aaaabbbb-100-0"), "lod_name": "", "bare_name": "aaaabbbb-100-0"},
        )
        self.assertEqual(
            record_by_path[str(self.root / "LOD1" / "ccccdddd-200-0.Tail")],
            {
                "folder_path": str(self.root / "LOD1" / "ccccdddd-200-0.Tail"),
                "lod_name": "LOD1",
                "bare_name": "ccccdddd-200-0.Tail",
            },
        )


if __name__ == "__main__":
    unittest.main()
