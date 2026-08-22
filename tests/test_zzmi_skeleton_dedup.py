"""ZZMI 骨骼去重（ZZMIBoneMapBuilder.build_vg_maps）刚性部件质心门控单测。

判据（2026-08-24 用户拍板）：
- 跨部件仍严格 bitwise（字节级）判等，无浮点容差；
- 但只要命中对任一方是"单骨骼刚性部件"（palette 仅 1 根骨骼，单权重物体），
  追加加权质心距离确认：距离 >= rigid_centroid_tolerance 则拆开各占各槽
  （单骨骼部件的"骨骼"就是整个物体的锚点，质心 = 物体位置指纹；
  抓帧瞬间重合的不同锚点骨会被 bitwise 误并——头顶件/前额件/后脑发饰同矩阵案例）；
- 刚性部件误拆零代价（各自 attach 各自写同一矩阵，运行时内容恒等），误并有害；
- 双方都是多骨骼部件时不加质心门控（整块 palette 的多骨骼同时逐位相同
  不可能是巧合；且真共享的驱动区域质心可相距甚远——b20f90ea/a23aa8a3 实测 0.25）；
- 门控触发时任一方缺签名 -> 拆开（保守方向）；
- 同部件内部绝不去重（原有行为不变）。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_dedup_test_pkg"


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
_load_module(f"{PKG}.common.efmi_skeleton", REPO_ROOT / "common" / "efmi_skeleton.py")
_zzmi = _load_module(f"{PKG}.common.zzmi_skeleton", REPO_ROOT / "common" / "zzmi_skeleton.py")

ZZMIBoneMapBuilder = _zzmi.ZZMIBoneMapBuilder


def _bone(tx=0.0, ty=0.0, tz=0.0):
    return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, tx, ty, tz]


def _palette(*bones):
    return numpy.array(list(bones), dtype=numpy.float32).reshape(-1, 12)


def _sig(x, y, z):
    c = numpy.array([x, y, z], dtype=numpy.float32)
    return {
        "centroid": c,
        "bbox_min": c - 0.01,
        "bbox_max": c + 0.01,
        "vertex_count": 10,
        "spread": 0.01,
        "weight_total": 10.0,
    }


class RigidCentroidGateTests(unittest.TestCase):
    def test_rigid_pair_far_centroids_split(self):
        """误并复现：两个单权重物体 bitwise 同矩阵但质心相距 0.3 -> 拆开。"""
        palettes = {
            "aa_rigid": _palette(_bone(0.1)),
            "bb_rigid": _palette(_bone(0.1)),
        }
        sigs = {"aa_rigid": {0: _sig(0, 0, 0)}, "bb_rigid": {0: _sig(0.3, 0, 0)}}
        vg_maps, _, _ = ZZMIBoneMapBuilder.build_vg_maps(palettes, sigs)
        self.assertNotEqual(vg_maps["aa_rigid"][0], vg_maps["bb_rigid"][0])

    def test_rigid_pair_close_centroids_merge(self):
        """同位置的两个刚性件（同一挂件的拆分子件）-> 合并。"""
        palettes = {
            "aa_rigid": _palette(_bone(0.1)),
            "bb_rigid": _palette(_bone(0.1)),
        }
        sigs = {"aa_rigid": {0: _sig(0, 0, 0)}, "bb_rigid": {0: _sig(0.01, 0, 0)}}
        vg_maps, _, _ = ZZMIBoneMapBuilder.build_vg_maps(palettes, sigs)
        self.assertEqual(vg_maps["aa_rigid"][0], vg_maps["bb_rigid"][0])

    def test_rigid_vs_multi_far_centroid_split(self):
        """刚性件命中多骨骼部件的某根骨：质心远离 -> 拆开（如头顶件误并后脑发饰骨）。"""
        palettes = {
            "aa_rigid": _palette(_bone(0.1)),
            "bb_multi": _palette(_bone(0.1), _bone(0.5)),
        }
        sigs = {
            "aa_rigid": {0: _sig(0, 0, 1.6)},
            "bb_multi": {0: _sig(0, 0.3, 1.6), 1: _sig(0, 0.3, 1.5)},
        }
        vg_maps, _, _ = ZZMIBoneMapBuilder.build_vg_maps(palettes, sigs)
        self.assertNotEqual(vg_maps["aa_rigid"][0], vg_maps["bb_multi"][0])

    def test_rigid_vs_multi_close_centroid_merge(self):
        """刚性件命中多骨骼部件的某根骨：质心贴合 -> 合并（挂饰真共享）。"""
        palettes = {
            "aa_rigid": _palette(_bone(0.1)),
            "bb_multi": _palette(_bone(0.1), _bone(0.5)),
        }
        sigs = {
            "aa_rigid": {0: _sig(0, 0, 1.6)},
            "bb_multi": {0: _sig(0.01, 0, 1.6), 1: _sig(0, 0.3, 1.5)},
        }
        vg_maps, _, _ = ZZMIBoneMapBuilder.build_vg_maps(palettes, sigs)
        self.assertEqual(vg_maps["aa_rigid"][0], vg_maps["bb_multi"][0])

    def test_multi_vs_multi_never_gated(self):
        """双方都是多骨骼部件：bitwise 命中即合并，不看质心（身体 13 根真共享形态）。"""
        palettes = {
            "aa_multi": _palette(_bone(0.1), _bone(0.2)),
            "bb_multi": _palette(_bone(0.1), _bone(0.3)),
        }
        sigs = {
            "aa_multi": {0: _sig(0, 0, 0), 1: _sig(0, 0, 0)},
            "bb_multi": {0: _sig(0.5, 0.5, 0.5), 1: _sig(0, 0, 0)},
        }
        vg_maps, _, _ = ZZMIBoneMapBuilder.build_vg_maps(palettes, sigs)
        self.assertEqual(vg_maps["aa_multi"][0], vg_maps["bb_multi"][0])
        # 矩阵不同的骨不并
        self.assertNotEqual(vg_maps["aa_multi"][1], vg_maps["bb_multi"][1])

    def test_rigid_missing_signature_split(self):
        """门控触发时任一方缺签名 -> 保守拆开。"""
        palettes = {
            "aa_rigid": _palette(_bone(0.1)),
            "bb_rigid": _palette(_bone(0.1)),
            "cc_rigid": _palette(_bone(0.1)),
        }
        # bb 有签名、cc 无签名；aa 有签名
        sigs = {"aa_rigid": {0: _sig(0, 0, 0)}, "bb_rigid": {0: _sig(0.01, 0, 0)}}
        vg_maps, _, _ = ZZMIBoneMapBuilder.build_vg_maps(palettes, sigs)
        self.assertEqual(vg_maps["aa_rigid"][0], vg_maps["bb_rigid"][0])
        self.assertNotEqual(vg_maps["aa_rigid"][0], vg_maps["cc_rigid"][0])

    def test_no_signatures_param_keeps_legacy_behavior(self):
        """不传签名（旧调用方式）-> 退化为纯 bitwise，刚性件也照并（向后兼容）。"""
        palettes = {
            "aa_rigid": _palette(_bone(0.1)),
            "bb_rigid": _palette(_bone(0.1)),
        }
        vg_maps, _, _ = ZZMIBoneMapBuilder.build_vg_maps(palettes)
        self.assertEqual(vg_maps["aa_rigid"][0], vg_maps["bb_rigid"][0])

    def test_three_rigid_parts_partial_chain(self):
        """三个刚性件：A(0)、B(距A 0.04)、C(距A 0.08/距B 0.04)。
        B 并入 A；C 对既有 owner（A）质心过远 -> 独立成槽（不链式漂移）。"""
        palettes = {
            "aa_rigid": _palette(_bone(0.1)),
            "bb_rigid": _palette(_bone(0.1)),
            "cc_rigid": _palette(_bone(0.1)),
        }
        sigs = {
            "aa_rigid": {0: _sig(0, 0, 0)},
            "bb_rigid": {0: _sig(0.04, 0, 0)},
            "cc_rigid": {0: _sig(0.08, 0, 0)},
        }
        vg_maps, _, _ = ZZMIBoneMapBuilder.build_vg_maps(palettes, sigs)
        self.assertEqual(vg_maps["aa_rigid"][0], vg_maps["bb_rigid"][0])
        self.assertNotEqual(vg_maps["aa_rigid"][0], vg_maps["cc_rigid"][0])

    def test_same_part_never_merged(self):
        """同部件内部绝不去重（原有行为锚定）。"""
        palettes = {"aa_multi": _palette(_bone(0.1), _bone(0.1))}
        vg_maps, _, _ = ZZMIBoneMapBuilder.build_vg_maps(palettes, {})
        self.assertNotEqual(vg_maps["aa_multi"][0], vg_maps["aa_multi"][1])


if __name__ == "__main__":
    unittest.main()
