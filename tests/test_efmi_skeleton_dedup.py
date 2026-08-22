"""EFMI 骨骼去重（EFMIBoneMapBuilder.build_vg_maps）分层判据单测。

判据（2026-08-24 定案，矩阵硬门控 + 质心确认）：
- 矩阵 maxdiff >= match_tolerance（1e-3）：永不合并——几何接近无权推翻矩阵不一致
  （投票制曾把矩阵完全不同的骨骼因质心/包围盒/扩散球接近而误并，实测"测试"工作空间
  08-10 dump 产生 42 组矩阵不可兼容的误并，已废止）；
- 矩阵 bitwise 完全相同（diff == 0）：直接合并（无需签名）；
- 0 < diff < match_tolerance：需加权质心距离 < centroid_tolerance（0.02）才合并
  （手指两节：矩阵近似但质心远离 -> 拆开；539/493：矩阵近似且质心重合 -> 合并）；
- 缺签名时近似矩阵不合并（保守；漏并无害、误并有害）；
- 同部件内绝不合并；跨组并入走完全图判定（须与组内所有成员两两通过）。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "efmi_dedup_test_pkg"


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

EFMIBoneMapBuilder = _efmi.EFMIBoneMapBuilder


def _bone(tx=0.0, ty=0.0, tz=0.0):
    """12 floats 的 4x3 骨骼矩阵（单位旋转 + 平移）。"""
    return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, tx, ty, tz]


def _sig(centroid, spread=0.05, half=0.05):
    c = numpy.array(centroid, dtype=numpy.float32)
    h = numpy.array([half, half, half], dtype=numpy.float32)
    return {
        "centroid": c,
        "bbox_min": c - h,
        "bbox_max": c + h,
        "vertex_count": 10,
        "spread": spread,
        "weight_total": 10.0,
    }


def _entry(bones, sigs=None, weighted=None):
    arr = numpy.array(bones, dtype=numpy.float32).reshape(-1, 12)
    n = len(arr)
    if weighted is None:
        weighted = numpy.ones(n, dtype=numpy.int64)
    return (arr, n, weighted, sigs or {})


class DedupGateTests(unittest.TestCase):
    def test_matrix_mismatch_never_merges_despite_geometry(self):
        """回归（误并核心）：矩阵完全不同（diff=0.2）+ 几何三维度全贴近 -> 绝不合并。

        旧投票制下质心/扩散球/包围盒全过 = 3 票 >= 2 会误并；矩阵必须是硬门控。
        """
        submesh = {
            # 排序后 aa 在前：offset 0；bb 在后：offset 1
            "aa_part": _entry([_bone(0.0)], {0: _sig((0.0, 0.0, 0.0), spread=0.5, half=0.5)}),
            "bb_part": _entry([_bone(0.2)], {0: _sig((0.0, 0.0, 0.0), spread=0.5, half=0.5)}),
        }
        vg_maps, vg_offsets = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])
        self.assertEqual(vg_maps["aa_part"][0], 0)
        self.assertEqual(vg_maps["bb_part"][0], 1)

    def test_exact_matrix_merges_without_signatures(self):
        """bitwise 完全相同的跨部件骨骼直接合并（缺签名也合并）。"""
        submesh = {
            "aa_part": _entry([_bone(0.1, 0.2, 0.3)]),
            "bb_part": _entry([_bone(0.1, 0.2, 0.3)]),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])

    def test_close_matrix_and_close_centroid_merges(self):
        """539/493 形态：矩阵差 1e-5（<1e-3）且质心距 0.005（<0.02）-> 合并。"""
        bone_b = _bone(0.1)
        bone_b[0] += 1e-5  # 矩阵单元素微差
        submesh = {
            "aa_part": _entry([_bone(0.1)], {0: _sig((0.0, 0.0, 0.0))}),
            "bb_part": _entry([bone_b], {0: _sig((0.005, 0.0, 0.0))}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])

    def test_close_matrix_but_distant_centroid_not_merges(self):
        """手指两节形态：矩阵差 1e-5 但质心距 0.03（>0.02）-> 不合并。"""
        bone_b = _bone(0.1)
        bone_b[0] += 1e-5
        submesh = {
            "aa_part": _entry([_bone(0.1)], {0: _sig((0.0, 0.0, 0.0))}),
            "bb_part": _entry([bone_b], {0: _sig((0.03, 0.0, 0.0))}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])

    def test_close_matrix_missing_signature_not_merges(self):
        """缺签名时近似矩阵保守不合并（漏并无害、误并有害）。"""
        bone_b = _bone(0.1)
        bone_b[0] += 1e-5
        submesh = {
            "aa_part": _entry([_bone(0.1)]),
            "bb_part": _entry([bone_b]),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])

    def test_same_part_never_merges(self):
        """同部件两根 bitwise 完全相同的骨骼也各占各的槽位。"""
        submesh = {
            "aa_part": _entry([_bone(0.1), _bone(0.1)]),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["aa_part"][0], vg_maps["aa_part"][1])
        self.assertEqual(vg_maps["aa_part"][0], 0)
        self.assertEqual(vg_maps["aa_part"][1], 1)

    def test_complete_graph_blocks_partial_match(self):
        """完全图判定：C 与 A 通过但与 B 不通过 -> C 不得并入 {A,B} 组。"""
        bone_c = _bone(0.1)
        bone_c[0] += 1e-5  # 与 A/B 矩阵近似
        submesh = {
            "aa_part": _entry([_bone(0.1)], {0: _sig((0.0, 0.0, 0.0))}),
            # B 与 A bitwise 相同（必并）；B 的质心离 C 远（0.03）
            "bb_part": _entry([_bone(0.1)], {0: _sig((0.03, 0.0, 0.0))}),
            "cc_part": _entry([bone_c], {0: _sig((0.005, 0.0, 0.0))}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        # A 与 B 合并
        self.assertEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])
        # C 与 A 近似+质心近，但与 B 质心远 -> 完全图拦截，不入组
        self.assertNotEqual(vg_maps["cc_part"][0], vg_maps["aa_part"][0])

    def test_zero_bone_skipped(self):
        """全零骨骼不参与去重也不进 vg_map（原有行为锚定）。"""
        submesh = {
            "aa_part": _entry([_bone(0.1), [0.0] * 12]),
            "bb_part": _entry([[0.0] * 12, _bone(0.1)]),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotIn(1, vg_maps["aa_part"])
        self.assertNotIn(0, vg_maps["bb_part"])
        # 非零骨骼 bitwise 相同 -> 合并
        self.assertEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][1])

    def test_dedup_disabled_identity_mapping(self):
        """总开关关闭时退化为恒等映射（local -> vg_offset + local），完全相同的骨骼也不并。"""
        submesh = {
            "aa_part": _entry([_bone(0.1), _bone(0.2)]),
            "bb_part": _entry([_bone(0.1), _bone(0.2)]),
        }
        old = _efmi._DEDUP_ENABLED
        _efmi._DEDUP_ENABLED = False
        try:
            vg_maps, vg_offsets = EFMIBoneMapBuilder.build_vg_maps(submesh)
        finally:
            _efmi._DEDUP_ENABLED = old
        self.assertEqual(vg_offsets, {"aa_part": 0, "bb_part": 2})
        self.assertEqual(vg_maps["aa_part"], {0: 0, 1: 1})
        self.assertEqual(vg_maps["bb_part"], {0: 2, 1: 3})


if __name__ == "__main__":
    unittest.main()
