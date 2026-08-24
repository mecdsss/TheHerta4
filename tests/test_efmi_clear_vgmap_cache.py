"""EFMI 骨骼合并 VGMap 缓存清理（EFMISkeletonMergeHelper.clear_vgmap_cache）单测。

背景：去重策略变更后，旧策略写回子网格 json 的 VGMap 会由算法版本自动失效；
clear_vgmap_cache 仍可删除这些键，强制下次导入重新生成。
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "efmi_clear_cache_test_pkg"


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


for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(_name)
_load_module(f"{PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py")
_efmi = _load_module(f"{PKG}.common.efmi_skeleton", REPO_ROOT / "common" / "efmi_skeleton.py")

EFMISkeletonMergeHelper = _efmi.EFMISkeletonMergeHelper


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ClearVgmapCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        # 子网格 json（含完整三键 + 分组键 + 其他无关键）
        self.submesh_a = self.root / "aaaabbbb-100-0" / "TYPE_GPU-EFMI" / "aaaabbbb-100-0.json"
        _write_json(self.submesh_a, {
            "DrawIB": "aaaabbbb",
            "VGMap": {"0": 0, "1": 5},
            "VGOffset": 0,
            "VGCount": 2,
            "VGMapAlgorithmVersion": 3,
            "VGMapDedupEnabled": True,
            "SkeletonGroup": 1,
            "BoneMatrixFileName": "aaaabbbb-100-0-BoneMatrix.buf",
        })

        # LOD 子目录里的子网格 json（只写了 VGMap 的半成品）
        self.submesh_b = (
            self.root / "LOD1" / "ccccdddd-200-0" / "TYPE_GPU-EFMI" / "ccccdddd-200-0.json"
        )
        _write_json(self.submesh_b, {"DrawIB": "ccccdddd", "VGMap": {"0": 0}})

        # 无 VGMap 的普通 json：不应被改动
        self.plain_json = self.root / "dddddddd-50-0" / "TYPE_GPU-EFMI" / "dddddddd-50-0.json"
        _write_json(self.plain_json, {"DrawIB": "dddddddd"})

        # Config 目录下的 json：即使含 VGMap 键也必须跳过
        self.config_json = self.root / "Config" / "Tabs" / "ws-tab-test.json"
        _write_json(self.config_json, {"VGMap": {"0": 0}, "frameAnalysisFolderPath": "x"})

        # 损坏的 json：静默跳过
        self.broken_json = self.root / "eeeeeeee-1-0" / "TYPE_GPU-EFMI" / "eeeeeeee-1-0.json"
        self.broken_json.parent.mkdir(parents=True, exist_ok=True)
        self.broken_json.write_text("{ not json", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_clears_only_submesh_vgmap_keys(self):
        cleaned, scanned = EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.root))

        self.assertEqual(cleaned, 2)
        self.assertGreaterEqual(scanned, 4)

        data_a = _read_json(self.submesh_a)
        self.assertNotIn("VGMap", data_a)
        self.assertNotIn("VGOffset", data_a)
        self.assertNotIn("VGCount", data_a)
        self.assertNotIn("VGMapAlgorithmVersion", data_a)
        self.assertNotIn("VGMapDedupEnabled", data_a)
        # ZZMI 分组版的 SkeletonGroup 一并清除（EFMI json 无此键，幂等无副作用）
        self.assertNotIn("SkeletonGroup", data_a)
        # 无关键保留（BoneMatrixFileName 指向原始骨骼池拷贝，与去重策略无关，不删）
        self.assertEqual(data_a["DrawIB"], "aaaabbbb")
        self.assertEqual(data_a["BoneMatrixFileName"], "aaaabbbb-100-0-BoneMatrix.buf")

        data_b = _read_json(self.submesh_b)
        self.assertNotIn("VGMap", data_b)
        self.assertEqual(data_b["DrawIB"], "ccccdddd")

        # 无 VGMap 的 json 原样不动
        self.assertEqual(_read_json(self.plain_json), {"DrawIB": "dddddddd"})

        # Config 目录被跳过，含 VGMap 键也不动
        self.assertEqual(
            _read_json(self.config_json),
            {"VGMap": {"0": 0}, "frameAnalysisFolderPath": "x"},
        )

    def test_second_run_is_noop(self):
        cleaned1, _ = EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.root))
        cleaned2, _ = EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.root))
        self.assertEqual(cleaned1, 2)
        self.assertEqual(cleaned2, 0)

    def test_invalid_workspace_returns_zero(self):
        self.assertEqual(
            EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.root / "不存在的目录")),
            (0, 0),
        )
        self.assertEqual(EFMISkeletonMergeHelper.clear_vgmap_cache(""), (0, 0))


if __name__ == "__main__":
    unittest.main()
