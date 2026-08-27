"""EFMI 骨骼去重（EFMIBoneMapBuilder.build_vg_maps）分层判据单测。

判据（权重扩散检测）：
- 矩阵 maxdiff >= match_tolerance（1e-3）：永不合并——几何接近无权推翻矩阵不一致
  （投票制曾把矩阵完全不同的骨骼因质心/包围盒/扩散球接近而误并，实测"测试"工作空间
  08-10 dump 产生 42 组矩阵不可兼容的误并，已废止）；
- 矩阵 bitwise 完全相同（diff == 0）：有扩散采样时仍需权重场一致；无采样时兼容直接合并；
- 0 < diff < match_tolerance：优先比较接触位置的权重扩散场（覆盖率≥30%、平均
  原始权重误差≤0.20，弱权重点至少保留 25% 的评估影响）；无扩散采样时才回退到
  加权质心距离 < centroid_tolerance（0.02）；
- 两层表面可通过局部 PCA 法向做层间投影，不要求两个物体共享顶点或拓扑连接；
- 缺签名时近似矩阵不合并（保守；漏并无害、误并有害）；
- 同部件内绝不合并；跨组并入走权重扩散连通图判定（合并后不得出现孤立断点）。
"""

import importlib.util
import sys
import tempfile
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


def _diff_sig(points, weights, centroid=None):
    """带权重扩散采样的最小签名。

    points/weights 表示绑定姿态空间中该组的正权重采样；真实导入路径
    由 Position.buf + Blend.buf 生成同样的字段。
    """
    points = numpy.asarray(points, dtype=numpy.float32)
    weights = numpy.asarray(weights, dtype=numpy.float32)
    if centroid is None:
        centroid = numpy.average(points, axis=0, weights=weights)
    c = numpy.asarray(centroid, dtype=numpy.float32)
    return {
        "centroid": c,
        "bbox_min": points.min(axis=0),
        "bbox_max": points.max(axis=0),
        "vertex_count": len(points),
        "spread": float(numpy.std(points - c)),
        "weight_total": float(weights.sum()),
        "diffusion_points": points,
        "diffusion_weights": weights,
    }


def _entry(bones, sigs=None, weighted=None):
    arr = numpy.array(bones, dtype=numpy.float32).reshape(-1, 12)
    n = len(arr)
    if weighted is None:
        weighted = numpy.ones(n, dtype=numpy.int64)
    return (arr, n, weighted, sigs or {})


class BlendIndicesSignedLayoutTests(unittest.TestCase):
    def test_import_layout_accepts_signed_r8_and_r16(self):
        self.assertEqual(
            EFMIBoneMapBuilder._blend_index_layout_entry("R8G8_UINT"),
            ("u1", 2),
        )
        self.assertEqual(
            EFMIBoneMapBuilder._blend_index_layout_entry("R8G8B8A8_SINT"),
            ("i1", 4),
        )
        self.assertEqual(
            EFMIBoneMapBuilder._blend_index_layout_entry("R16G16B16A16_SINT"),
            ("i2", 4),
        )
        self.assertEqual(
            _efmi.EFMISkeletonMergeHelper._blend_indices_layout("R16_SINT"),
            ("i2", 1),
        )

    def test_all_negative_signed_indices_are_invalid_channels(self):
        indices = numpy.array(
            [[0, 1, 0xFFFFFFFF, 0xFFFFFFFE]], dtype=numpy.uint32
        )
        weights = numpy.ones((1, 4), dtype=numpy.float32)
        mask = EFMIBoneMapBuilder.valid_blend_channels(
            indices,
            {"np_type": "i2"},
            weights,
        )
        self.assertEqual(mask.tolist(), [[True, True, False, False]])


class DedupGateTests(unittest.TestCase):
    def setUp(self):
        # 显式打开开关，锚定算法行为本身（关闭路径另有回归用例）。
        self._old_dedup_enabled = _efmi._DEDUP_ENABLED
        _efmi._DEDUP_ENABLED = True

    def tearDown(self):
        _efmi._DEDUP_ENABLED = self._old_dedup_enabled

    def test_cross_lod_raw_correspondence_protects_distinct_groups(self):
        """LOD0/LOD1 先按原始矩阵+质心对应，再阻止一侧过度去重。"""
        same = _bone(0.0)
        target_distinct = _bone(0.80)
        lod0 = {
            "LOD0.a": _entry(
                [same],
                {0: _sig((0.0, 0.0, 0.0))},
            ),
            "LOD0.b": _entry(
                [same],
                {0: _sig((1.0, 0.0, 0.0))},
            ),
        }
        lod1 = {
            "LOD1.x": _entry(
                [same],
                {0: _sig((0.01, 0.0, 0.0))},
            ),
            "LOD1.y": _entry(
                [target_distinct],
                {0: _sig((1.01, 0.0, 0.0))},
            ),
        }
        correspondence = EFMIBoneMapBuilder.build_cross_lod_correspondence(
            {"LOD0": lod0, "LOD1": lod1},
        )
        protected0 = correspondence["protected_pairs"]["LOD0"]
        protected1 = correspondence["protected_pairs"]["LOD1"]
        self.assertIn(
            (("LOD0.a", 0), ("LOD0.b", 0)),
            protected0,
        )
        self.assertEqual(protected1, set())

        maps0, _ = EFMIBoneMapBuilder.build_vg_maps(
            lod0,
            protected_pairs=protected0,
        )
        maps1, _ = EFMIBoneMapBuilder.build_vg_maps(
            lod1,
            protected_pairs=protected1,
        )
        self.assertNotEqual(maps0["LOD0.a"][0], maps0["LOD0.b"][0])
        self.assertNotEqual(maps1["LOD1.x"][0], maps1["LOD1.y"][0])

    def test_cross_lod_missing_raw_candidate_is_reported_not_fabricated(self):
        """目标 LOD 真缺原始组时记录缺口，不能伪造带权重的候选。"""
        lod0 = {
            "LOD0.a": _entry([_bone(0.0)], {0: _sig((0.0, 0.0, 0.0))}),
            "LOD0.b": _entry([_bone(0.1)], {0: _sig((1.0, 0.0, 0.0))}),
        }
        lod1 = {
            "LOD1.x": _entry([_bone(0.0)], {0: _sig((0.0, 0.0, 0.0))}),
        }
        correspondence = EFMIBoneMapBuilder.build_cross_lod_correspondence(
            {"LOD0": lod0, "LOD1": lod1},
        )
        self.assertEqual(correspondence["counts"], {"LOD0": 2, "LOD1": 1})
        self.assertEqual(len(correspondence["unmatched_reference"]), 1)
        self.assertEqual(correspondence["unmatched_reference"][0]["unique_str"], "LOD0.b")

    def test_cross_lod_pairs_components_by_geometry_before_local_groups(self):
        """部件先按点云一对一配对，局部组不能跨到另一个部件。"""
        bone = _bone(0.0)
        def point_sig(x):
            points = [[x, 0.0, 0.0], [x + 0.05, 0.0, 0.0]]
            return _diff_sig(points, [1.0, 0.8], centroid=[x + 0.02, 0.0, 0.0])

        lod0 = {
            "LOD0.part_a": _entry([bone], {0: point_sig(0.0)}),
            "LOD0.part_b": _entry([bone], {0: point_sig(10.0)}),
        }
        # 故意让 LOD1 的部件名顺序和 LOD0 不同，必须按几何配成 a->x、b->y。
        lod1 = {
            "LOD1.part_x": _entry([bone], {0: point_sig(10.0)}),
            "LOD1.part_y": _entry([bone], {0: point_sig(0.0)}),
        }
        correspondence = EFMIBoneMapBuilder.build_cross_lod_correspondence(
            {"LOD0": lod0, "LOD1": lod1},
        )
        part_pairs = {
            (row["reference_unique_str"], row["target_unique_str"])
            for row in correspondence["part_matches"]
        }
        self.assertEqual(part_pairs, {
            ("LOD0.part_a", "LOD1.part_y"),
            ("LOD0.part_b", "LOD1.part_x"),
        })
        self.assertEqual({
            (row["reference_unique_str"], row["target_unique_str"])
            for row in correspondence["matches"]
        }, part_pairs)
        self.assertTrue(all(
            row["reference_component"] == row["reference_unique_str"]
            and row["target_component"] == row["target_unique_str"]
            and row["component_score"] >= 0.0
            for row in correspondence["matches"]
        ))

    def test_cross_lod_keeps_geometry_match_when_pose_matrix_diff_is_160(self):
        """跨 LOD 姿态差异约 1.60 时仍须建立原始候选对应。

        同一 LOD 的去重仍以 1e-3 矩阵硬门控为准；这里只验证跨 LOD
        的对应层不能因两个捕获姿态的矩阵差异，把几何上明确对应的骨骼
        当成缺口。真实 EFMI 工作空间中的 LOD0/LOD1 目标正是这个量级。
        """
        reference_bone = _bone(0.0)
        target_bone = list(reference_bone)
        target_bone[0] = -0.60  # max(abs(diff)) == 1.60
        lod0 = {
            "LOD0.reference": _entry(
                [reference_bone],
                {0: _sig((0.0, 0.0, 0.0))},
            ),
        }
        lod1 = {
            "LOD1.target": _entry(
                [target_bone],
                {0: _sig((0.004, 0.0, 0.0))},
            ),
        }
        correspondence = EFMIBoneMapBuilder.build_cross_lod_correspondence(
            {"LOD0": lod0, "LOD1": lod1},
        )
        self.assertEqual(len(correspondence["matches"]), 1)
        row = correspondence["matches"][0]
        self.assertEqual(row["reference_local_vg_id"], 0)
        self.assertEqual(row["target_local_vg_id"], 0)
        self.assertAlmostEqual(row["matrix_diff"], 1.60, places=6)

    def test_each_lod_dedups_independently_into_own_slot_space(self):
        """完全独立编号（用户拍板）：每 LOD 用自己的 dump 独立去重，槽位空间互不相交。

        LOD1 不再借用 LOD0 的参考组 id（串扰根因：参考号在 LOD1 池中命中别的
        部件段/空洞），因此即便矩阵姿态差异很大的对应，两侧各自去重、各自编号。
        """
        reference_bone = _bone(0.0)
        target_bone = list(reference_bone)
        target_bone[0] = -0.60  # max(abs(diff)) == 1.60（跨 LOD 对应允许但不再影响编号）
        lod0 = {
            "LOD0.a": _entry(
                [reference_bone],
                {0: _sig((0.0, 0.0, 0.0))},
            ),
            "LOD0.b": _entry(
                [reference_bone],
                {0: _sig((0.01, 0.0, 0.0))},
            ),
        }
        lod1 = {
            "LOD1.x": _entry(
                [target_bone],
                {0: _sig((0.004, 0.0, 0.0))},
            ),
            "LOD1.y": _entry(
                [target_bone],
                {0: _sig((0.014, 0.0, 0.0))},
            ),
        }
        # 每 LOD 独立 build_vg_maps（LOD1 不再投影到参考编号）
        maps0, offsets0 = EFMIBoneMapBuilder.build_vg_maps(lod0)
        maps1, offsets1 = EFMIBoneMapBuilder.build_vg_maps(lod1)
        # LOD0: a、b 骨骼 bitwise 相同 -> 合并组 0
        self.assertEqual(maps0["LOD0.a"][0], maps0["LOD0.b"][0])
        self.assertEqual(maps0["LOD0.a"][0], 0)
        # LOD1: x、y 骨骼 bitwise 相同 -> 合并组 0（独立编号，不与 LOD0 共享）
        self.assertEqual(maps1["LOD1.x"][0], maps1["LOD1.y"][0])
        self.assertEqual(maps1["LOD1.x"][0], 0)
        # 两 LOD 的编号空间各自从 0 起步 —— 由调用方分段平移保证全局唯一
        self.assertEqual(offsets0, {"LOD0.a": 0, "LOD0.b": 1})
        self.assertEqual(offsets1, {"LOD1.x": 0, "LOD1.y": 1})

    def test_lod1_keeps_own_partition_even_with_matching_geometry(self):
        """LOD1 即使几何与 LOD0 完全一致，也只用自身点云去重，不复用 LOD0 分组。"""
        same = _bone(0.0)
        near = _bone(0.0)
        near[0] += 1e-5
        lod0 = {
            "LOD0.a": _entry([same], {0: _sig((0.0, 0.0, 0.0))}),
            "LOD0.b": _entry([same], {0: _sig((0.01, 0.0, 0.0))}),
        }
        lod1 = {
            "LOD1.x": _entry([near], {0: _sig((0.0, 0.0, 0.0))}),
            "LOD1.y": _entry([near], {0: _sig((0.01, 0.0, 0.0))}),
        }
        maps0, offsets0 = EFMIBoneMapBuilder.build_vg_maps(lod0)
        maps1, offsets1 = EFMIBoneMapBuilder.build_vg_maps(lod1)
        self.assertEqual(maps0["LOD0.a"][0], maps0["LOD0.b"][0])
        self.assertEqual(maps1["LOD1.x"][0], maps1["LOD1.y"][0])
        self.assertEqual(maps1["LOD1.x"][0], 0)
        self.assertEqual(offsets0, {"LOD0.a": 0, "LOD0.b": 1})
        self.assertEqual(offsets1, {"LOD1.x": 0, "LOD1.y": 1})

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

    def test_matrix_hard_gate_wins_over_matching_diffusion(self):
        """不同骨骼矩阵即使接触权重场相同，也不能被扩散证据推翻。"""
        points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
        sig_a = _diff_sig(points, [1.0, 0.5])
        sig_b = _diff_sig([[p[0] + 0.01, p[1], p[2]] for p in points], [1.0, 0.5])
        submesh = {
            "aa_part": _entry([_bone(0.0)], {0: sig_a}),
            "bb_part": _entry([_bone(0.2)], {0: sig_b}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])

    def test_exact_matrix_merges_without_signatures(self):
        """bitwise 完全相同的跨部件骨骼直接合并（缺签名也合并）。"""
        submesh = {
            "aa_part": _entry([_bone(0.1, 0.2, 0.3)]),
            "bb_part": _entry([_bone(0.1, 0.2, 0.3)]),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])

    def test_exact_matrix_with_conflicting_diffusion_stays_split(self):
        """矩阵相同但已有扩散证据冲突时，不能让 bitwise 直接合并掩盖冲突。"""
        points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
        sig_a = _diff_sig(points, [1.0, 0.5, 0.1])
        sig_b = _diff_sig([[p[0] + 0.01, p[1], p[2]] for p in points], [0.1, 0.5, 1.0])
        submesh = {
            "aa_part": _entry([_bone(0.1)], {0: sig_a}),
            "bb_part": _entry([_bone(0.1)], {0: sig_b}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])

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

    def test_continuity_graph_allows_a_bridge_without_direct_pair(self):
        """连续扩散图：C 只需通过 A 连接到 {A,B}，不要求和 B 直接相交。"""
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
        # C 与 A 近似+质心近，但与 B 质心远；A 是扩散桥，整组仍然连续。
        self.assertEqual(vg_maps["cc_part"][0], vg_maps["aa_part"][0])

    def test_zero_bone_keeps_an_independent_mapping_slot(self):
        """全零骨骼不参与去重，但必须保留独立槽位以满足导入/导出满映射契约。"""
        submesh = {
            "aa_part": _entry([_bone(0.1), [0.0] * 12]),
            "bb_part": _entry([[0.0] * 12, _bone(0.1)]),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertEqual(set(vg_maps["aa_part"]), {0, 1})
        self.assertEqual(set(vg_maps["bb_part"]), {0, 1})
        # 全零骨骼不能因为矩阵 bitwise 相同而跨部件互相合并。
        self.assertNotEqual(vg_maps["aa_part"][1], vg_maps["bb_part"][0])
        # 非零骨骼 bitwise 相同 -> 合并
        self.assertEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][1])

    def test_weight_diffusion_merges_contacting_parts_with_distant_centroids(self):
        """平面/散落物体：接触位置扩散权重一致时，整体质心远也应合并。"""
        plane_points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
        object_points = [[0.04, 0.0, 0.0], [1.04, 0.0, 0.0], [2.04, 0.0, 0.0]]
        sig_a = _diff_sig(plane_points, [1.0, 0.5, 0.1], centroid=[0.0, 0.0, 0.0])
        sig_b = _diff_sig(object_points, [1.0, 0.5, 0.1], centroid=[0.3, 0.0, 0.0])
        bone_b = _bone(0.1)
        bone_b[0] += 1e-5
        submesh = {
            "plane": _entry([_bone(0.1)], {0: sig_a}),
            "scattered_object": _entry([bone_b], {0: sig_b}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertEqual(vg_maps["plane"][0], vg_maps["scattered_object"][0])

    def test_weight_diffusion_rejects_mismatched_contact_weights(self):
        """接触位置权重反向时，即使矩阵/几何接近也不能合并。"""
        points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
        sig_a = _diff_sig(points, [1.0, 0.5, 0.1])
        sig_b = _diff_sig([[p[0] + 0.01, p[1], p[2]] for p in points], [0.1, 0.5, 1.0])
        bone_b = _bone(0.1)
        bone_b[0] += 1e-5
        submesh = {
            "aa_part": _entry([_bone(0.1)], {0: sig_a}),
            "bb_part": _entry([bone_b], {0: sig_b}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])

    def test_weight_diffusion_does_not_ignore_weak_weight_tail(self):
        """强中心点一致、两侧弱点冲突时，弱点不能被权重总量淹没。"""
        points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
        sig_a = _diff_sig(points, [1.0, 0.01, 0.01])
        sig_b = _diff_sig([[p[0] + 0.01, p[1], p[2]] for p in points], [1.0, 0.9, 0.9])
        bone_b = _bone(0.1)
        bone_b[0] += 1e-5
        submesh = {
            "aa_part": _entry([_bone(0.1)], {0: sig_a}),
            "bb_part": _entry([bone_b], {0: sig_b}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["aa_part"][0], vg_maps["bb_part"][0])

    def test_weight_diffusion_identical_weak_tail_still_merges(self):
        """弱权重保底只改变评估影响，不能把相同的原始弱权重抬高后制造误差。"""
        points = [[float(index) * 0.1, 0.0, 0.0] for index in range(33)]
        weights = [1.0] + [0.01] * 32
        sig_a = _diff_sig(points, weights)
        sig_b = _diff_sig(
            [[point[0] + 0.01, point[1], point[2]] for point in points],
            weights,
        )
        self.assertTrue(EFMIBoneMapBuilder.weight_diffusion_similarity(sig_a, sig_b))

    def test_weight_diffusion_allows_density_mismatch_on_continuous_patch(self):
        """连续接触区允许不同网格密度；远端点不能迫使局部投影变成一对一。"""
        dense_patch = [[float(index) * 0.01, 0.0, 0.0] for index in range(10)]
        sparse_with_remote_tail = (
            [[0.02, 0.0, 0.0], [0.07, 0.0, 0.0]]
            + [[1.0 + float(index) * 0.1, 0.0, 0.0] for index in range(8)]
        )
        self.assertTrue(EFMIBoneMapBuilder.weight_diffusion_similarity(
            _diff_sig(dense_patch, [1.0] * len(dense_patch)),
            _diff_sig(sparse_with_remote_tail, [1.0] * len(sparse_with_remote_tail)),
        ))

    def test_weight_diffusion_density_mismatch_still_checks_original_weights(self):
        """允许多对一几何投影不等于忽略权重场冲突。"""
        dense_patch = [[float(index) * 0.01, 0.0, 0.0] for index in range(10)]
        sparse_with_remote_tail = (
            [[0.02, 0.0, 0.0], [0.07, 0.0, 0.0]]
            + [[1.0 + float(index) * 0.1, 0.0, 0.0] for index in range(8)]
        )
        self.assertFalse(EFMIBoneMapBuilder.weight_diffusion_similarity(
            _diff_sig(dense_patch, [1.0] * len(dense_patch)),
            _diff_sig(sparse_with_remote_tail, [0.1] * len(sparse_with_remote_tail)),
        ))

    def test_nearest_diffusion_points_is_globally_exact(self):
        """相邻网格 cell 有较远点时，不能漏掉隔两个 cell 但实际更近的点。"""
        source = numpy.asarray([[0.049, 0.0, 0.0]], dtype=numpy.float32)
        target = numpy.asarray([
            [0.099, 0.099, 0.0],
            [0.100, 0.0, 0.0],
        ], dtype=numpy.float32)
        distances, nearest = EFMIBoneMapBuilder._nearest_diffusion_points(source, target)
        self.assertEqual(int(nearest[0]), 1)
        self.assertAlmostEqual(float(distances[0]), 0.051, places=5)

    def test_weight_diffusion_matches_parallel_layers_without_shared_vertices(self):
        """大腿/丝袜：平行表面权重一致，但法向间隔超过点距阈值也应合并。"""
        base = [[x, y, 0.0] for x in (0.0, 0.2, 0.4) for y in (0.0, 0.2, 0.4)]
        tights = [[x, y, 0.08] for x, y, _ in base]
        weights = [1.0, 0.8, 0.6, 0.8, 0.6, 0.4, 0.6, 0.4, 0.2]
        sig_a = _diff_sig(base, weights)
        sig_b = _diff_sig(tights, weights)
        bone_b = _bone(0.1)
        bone_b[0] += 1e-5
        submesh = {
            "thigh": _entry([_bone(0.1)], {0: sig_a}),
            "tights": _entry([bone_b], {0: sig_b}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertEqual(vg_maps["thigh"][0], vg_maps["tights"][0])

    def test_weight_diffusion_searches_past_a_closer_wrong_layer(self):
        """多层裙摆：最近层法向错误时，仍应找到稍远但同层级的扩散对应。"""
        base = [[x, y, 0.0] for x in (0.0, 0.2, 0.4) for y in (0.0, 0.2, 0.4)]
        weights = [1.0, 0.8, 0.6, 0.8, 0.6, 0.4, 0.6, 0.4, 0.2]
        # 每个源点附近放四个更近、但法向垂直的错误层采样；正确的平行层
        # 在 z=0.08。旧实现先取三维最近点再验法向，因而永远看不到正确层。
        wrong_layer = [
            [x + dx, y, 0.01]
            for x, y, _ in base
            for dx in (-0.015, -0.005, 0.005, 0.015)
        ]
        correct_layer = [[x, y, 0.08] for x, y, _ in base]
        sig_a = _diff_sig(base, weights)
        sig_b = _diff_sig(
            wrong_layer + correct_layer,
            [1.0] * len(wrong_layer) + weights,
        )
        sig_a["diffusion_normals"] = numpy.tile(
            numpy.asarray([0.0, 0.0, 1.0], dtype=numpy.float32), (len(base), 1)
        )
        sig_b["diffusion_normals"] = numpy.concatenate((
            numpy.tile(
                numpy.asarray([1.0, 0.0, 0.0], dtype=numpy.float32),
                (len(wrong_layer), 1),
            ),
            numpy.tile(
                numpy.asarray([0.0, 0.0, 1.0], dtype=numpy.float32),
                (len(correct_layer), 1),
            ),
        ))

        self.assertTrue(EFMIBoneMapBuilder.weight_diffusion_similarity(sig_a, sig_b))

    def test_ambiguous_component_candidates_keep_only_best_diffusion_match(self):
        """A:1 同时命中 B:8/B:9 时，只合并扩散相似度更高的那个。"""
        base = [[x, y, 0.0] for x in (0.0, 0.2, 0.4) for y in (0.0, 0.2, 0.4)]
        weights = [1.0, 0.8, 0.6, 0.8, 0.6, 0.4, 0.6, 0.4, 0.2]
        farther = [[x, y, 0.12] for x, y, _ in base]
        closer = [[x, y, 0.04] for x, y, _ in base]
        normal = numpy.tile(
            numpy.asarray([0.0, 0.0, 1.0], dtype=numpy.float32), (len(base), 1)
        )
        sig_a = _diff_sig(base, weights)
        sig_farther = _diff_sig(farther, weights)
        sig_closer = _diff_sig(closer, weights)
        for signature in (sig_a, sig_farther, sig_closer):
            signature["diffusion_normals"] = normal.copy()

        submesh = {
            "A_part": _entry([_bone(0.1)], {0: sig_a}),
            # local 0 先被遍历，但 local 1 的扩散距离明显更相似。
            "B_part": _entry(
                [_bone(0.1), _bone(0.1)],
                {0: sig_farther, 1: sig_closer},
            ),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)

        self.assertNotEqual(vg_maps["B_part"][0], vg_maps["B_part"][1])
        self.assertNotEqual(vg_maps["A_part"][0], vg_maps["B_part"][0])
        self.assertEqual(vg_maps["A_part"][0], vg_maps["B_part"][1])

    def test_ambiguous_candidates_prioritize_matrix_before_diffusion(self):
        """矩阵更接近的候选即使扩散更远，也不能被覆盖层抢走。"""
        base = [[x, y, 0.0] for x in (0.0, 0.2, 0.4) for y in (0.0, 0.2, 0.4)]
        weights = [1.0, 0.8, 0.6, 0.8, 0.6, 0.4, 0.6, 0.4, 0.2]
        exact_matrix_layer = [[x, y, 0.14] for x, y, _ in base]
        near_matrix_layer = [[x, y, 0.04] for x, y, _ in base]
        normal = numpy.tile(
            numpy.asarray([0.0, 0.0, 1.0], dtype=numpy.float32), (len(base), 1)
        )
        sig_a = _diff_sig(base, weights)
        sig_exact = _diff_sig(exact_matrix_layer, weights)
        sig_near = _diff_sig(near_matrix_layer, weights)
        for signature in (sig_a, sig_exact, sig_near):
            signature["diffusion_normals"] = normal.copy()

        near_matrix_bone = _bone(0.1)
        near_matrix_bone[0] += 2.5e-5
        submesh = {
            "A_part": _entry([_bone(0.1)], {0: sig_a}),
            "B_part": _entry(
                [_bone(0.1), near_matrix_bone],
                {0: sig_exact, 1: sig_near},
            ),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)

        self.assertEqual(vg_maps["A_part"][0], vg_maps["B_part"][0])
        self.assertNotEqual(vg_maps["A_part"][0], vg_maps["B_part"][1])

    def test_global_edge_order_keeps_matrix_best_bridge(self):
        """全局并入时也先处理矩阵更好的桥，避免链式浮层占用目标槽位。"""
        base = [[x, y, 0.0] for x in (0.0, 0.2, 0.4) for y in (0.0, 0.2, 0.4)]
        near = [[x, y, 0.02] for x, y, _ in base]
        far = [[x, y, 0.14] for x, y, _ in base]
        weights = [1.0, 0.8, 0.6, 0.8, 0.6, 0.4, 0.6, 0.4, 0.2]
        normal = numpy.tile(
            numpy.asarray([0.0, 0.0, 1.0], dtype=numpy.float32), (len(base), 1)
        )
        sig_base = _diff_sig(base, weights)
        sig_near = _diff_sig(near, weights)
        sig_far = _diff_sig(far, weights)
        for signature in (sig_base, sig_near, sig_far):
            signature["diffusion_normals"] = normal.copy()

        exact = _bone(0.1)
        offset = _bone(0.1)
        offset[0] += 1e-4
        submesh = {
            # A:0→B:0 是真实矩阵相同的边，但扩散距离比链式桥更远；
            # A:1→C:0→B:0 是扩散更近、矩阵略差的覆盖层链。
            "A_part": _entry([exact, offset], {0: sig_base, 1: sig_near}),
            "B_part": _entry([exact], {0: sig_far}),
            "C_part": _entry([offset], {0: sig_near}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)

        self.assertEqual(vg_maps["A_part"][0], vg_maps["B_part"][0])
        self.assertNotEqual(vg_maps["A_part"][1], vg_maps["B_part"][0])

    def test_weight_diffusion_uses_strict_contact_when_one_surface_normal_is_missing(self):
        """单侧 PCA 失败时不能把体积间隔当作跨层表面投影。"""
        base = [[x, y, 0.0] for x in (0.0, 0.2, 0.4) for y in (0.0, 0.2, 0.4)]
        offset = [[x, y, 0.10] for x, y, _ in base]
        weights = [1.0, 0.8, 0.6, 0.8, 0.6, 0.4, 0.6, 0.4, 0.2]
        sig_a = _diff_sig(base, weights)
        sig_b = _diff_sig(offset, weights)
        sig_a["diffusion_normals"] = numpy.full((len(base), 3), numpy.nan, dtype=numpy.float32)
        sig_b["diffusion_normals"] = numpy.tile(
            numpy.asarray([0.0, 0.0, 1.0], dtype=numpy.float32), (len(offset), 1)
        )
        self.assertFalse(EFMIBoneMapBuilder.weight_diffusion_similarity(sig_a, sig_b))

    def test_weight_diffusion_caps_parallel_layer_gap_at_point_fifteen(self):
        """可靠双侧法向也不能放行超过 0.15 的层间距。"""
        base = [[x, y, 0.0] for x in (0.0, 0.2, 0.4) for y in (0.0, 0.2, 0.4)]
        offset = [[x, y, 0.16] for x, y, _ in base]
        weights = [1.0, 0.8, 0.6, 0.8, 0.6, 0.4, 0.6, 0.4, 0.2]
        sig_a = _diff_sig(base, weights)
        sig_b = _diff_sig(offset, weights)
        normals = numpy.tile(numpy.asarray([0.0, 0.0, 1.0], dtype=numpy.float32), (len(base), 1))
        sig_a["diffusion_normals"] = normals
        sig_b["diffusion_normals"] = normals.copy()
        self.assertFalse(EFMIBoneMapBuilder.weight_diffusion_similarity(sig_a, sig_b))

    def test_weight_diffusion_rejects_perpendicular_surfaces(self):
        """相邻但法向垂直的表面不能借层间投影规则合并。"""
        source = [[x, y, 0.0] for x in (0.0, 0.2, 0.4) for y in (0.0, 0.2, 0.4)]
        target = [[0.08, y, z] for y in (0.0, 0.2, 0.4) for z in (0.0, 0.2, 0.4)]
        weights = [1.0, 0.8, 0.6, 0.8, 0.6, 0.4, 0.6, 0.4, 0.2]
        sig_a = _diff_sig(source, weights)
        sig_b = _diff_sig(target, weights)
        bone_b = _bone(0.1)
        bone_b[0] += 1e-5
        submesh = {
            "surface_a": _entry([_bone(0.1)], {0: sig_a}),
            "surface_b": _entry([bone_b], {0: sig_b}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["surface_a"][0], vg_maps["surface_b"][0])

    def test_weight_diffusion_rejects_misaligned_groove_surfaces(self):
        """凹槽式错位叠层（约 40°）不是平行表面，不能借层间投影合并。"""
        source = [[x, y, 0.0] for x in (0.0, 0.05, 0.10) for y in (0.0, 0.05, 0.10)]
        angle = numpy.deg2rad(60.0)
        target = [
            [x, y, 0.08 + numpy.tan(angle) * (x - 0.05)]
            for x, y, _ in source
        ]
        weights = [1.0, 0.8, 0.6, 0.8, 0.6, 0.4, 0.6, 0.4, 0.2]
        sig_a = _diff_sig(source, weights)
        sig_b = _diff_sig(target, weights)
        # 没有拓扑面信息时，显式带入局部 PCA 法向，验证错位折面不会
        # 被“体积距离”单独放行；真实凹槽底的连续桥接仍可由相邻组连接。
        sig_a["diffusion_normals"] = numpy.tile(
            numpy.asarray([0.0, 0.0, 1.0], dtype=numpy.float32), (len(source), 1)
        )
        target_normal = numpy.asarray(
            [-numpy.tan(angle), 0.0, 1.0], dtype=numpy.float32
        )
        target_normal /= numpy.linalg.norm(target_normal)
        sig_b["diffusion_normals"] = numpy.tile(target_normal, (len(target), 1))
        bone_b = _bone(0.1)
        bone_b[0] += 1e-5
        submesh = {
            "groove_a": _entry([_bone(0.1)], {0: sig_a}),
            "groove_b": _entry([bone_b], {0: sig_b}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertNotEqual(vg_maps["groove_a"][0], vg_maps["groove_b"][0])

    def test_weight_diffusion_rejects_many_to_one_nearest_matches(self):
        """凹槽边缘不能让多个源点复用同一个最近目标点后单向通过。"""
        source = [[0.0, 0.0, 0.0], [0.04, 0.0, 0.0], [0.08, 0.0, 0.0], [0.12, 0.0, 0.0]]
        target = [[0.06, 0.0, 0.03]]
        self.assertFalse(
            EFMIBoneMapBuilder.weight_diffusion_similarity(
                _diff_sig(source, [1.0, 1.0, 1.0, 1.0]),
                _diff_sig(target, [1.0]),
            )
        )

    def test_post_merge_continuity_rejects_isolated_member(self):
        """同矩阵不等于同一连续权重场：没有扩散桥的成员必须保持独立。"""
        bridge_a = _diff_sig(
            [[0.0, 0.0, 0.0], [0.2, 0.0, 0.0], [0.4, 0.0, 0.0]],
            [1.0, 0.8, 0.6],
        )
        bridge_b = _diff_sig(
            [[0.03, 0.0, 0.0], [0.23, 0.0, 0.0], [0.43, 0.0, 0.0]],
            [1.0, 0.8, 0.6],
        )
        isolated = _diff_sig(
            [[2.0, 2.0, 2.0], [2.2, 2.0, 2.0], [2.4, 2.0, 2.0]],
            [0.1, 0.08, 0.06],
        )
        near_bone = _bone(0.1)
        near_bone[0] += 1e-5
        submesh = {
            "aa_plane": _entry([_bone(0.1)], {0: bridge_a}),
            "bb_groove": _entry([near_bone], {0: bridge_b}),
            "cc_isolated": _entry([near_bone], {0: isolated}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertEqual(vg_maps["aa_plane"][0], vg_maps["bb_groove"][0])
        self.assertNotEqual(vg_maps["cc_isolated"][0], vg_maps["aa_plane"][0])

    def test_plane_and_multiple_offset_groove_bottoms_merge_through_local_bridges(self):
        """平面与多个错位凹槽底不相连，但各自的局部扩散场连续时应合并。"""
        plane = [[x, y, 0.0] for x in (0.0, 0.2, 0.4, 0.6, 0.8)
                 for y in (0.0, 0.2, 0.4, 0.6, 0.8)]
        plane_weights = [1.0] * len(plane)

        def groove(x_values, y_values, depth):
            return [[x, y, depth + 0.04 * (x - x_values[0])] for x in x_values for y in y_values]

        groove_a = groove((0.0, 0.2, 0.4), (0.0, 0.2, 0.4), -0.12)
        groove_b = groove((0.4, 0.6, 0.8), (0.4, 0.6, 0.8), -0.16)
        weights_a = [1.0] * len(groove_a)
        weights_b = [1.0] * len(groove_b)
        near_bone = _bone(0.1)
        near_bone[0] += 1e-5
        submesh = {
            "plane": _entry([_bone(0.1)], {0: _diff_sig(plane, plane_weights)}),
            "groove_bottom_a": _entry([near_bone], {0: _diff_sig(groove_a, weights_a)}),
            "groove_bottom_b": _entry([near_bone], {0: _diff_sig(groove_b, weights_b)}),
        }
        vg_maps, _ = EFMIBoneMapBuilder.build_vg_maps(submesh)
        self.assertEqual(vg_maps["plane"][0], vg_maps["groove_bottom_a"][0])
        self.assertEqual(vg_maps["plane"][0], vg_maps["groove_bottom_b"][0])

    def test_compute_driven_signatures_exports_diffusion_samples(self):
        """Position/Blend 原始 buffer 能生成扩散点和对应原始权重。"""
        positions = numpy.asarray(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            dtype=numpy.float32,
        )
        indices = numpy.asarray(
            [[0, 0xFFFF, 0xFFFF, 0xFFFF],
             [0, 0xFFFF, 0xFFFF, 0xFFFF],
             [1, 0xFFFF, 0xFFFF, 0xFFFF]],
            dtype=numpy.uint32,
        )
        weights = numpy.asarray(
            [[1.0, 0.0, 0.0, 0.0],
             [0.5, 0.0, 0.0, 0.0],
             [1.0, 0.0, 0.0, 0.0]],
            dtype=numpy.float32,
        )
        layout = {
            "CategoryBufferList": [
                {"D3D11ElementList": [{
                    "Category": "Position", "SemanticName": "POSITION", "ByteWidth": 12,
                }]},
                {"D3D11ElementList": [
                    {"Category": "Blend", "SemanticName": "BLENDINDICES",
                     "Format": "R32G32B32A32_UINT", "ByteWidth": 16},
                    {"Category": "Blend", "SemanticName": "BLENDWEIGHTS",
                     "Format": "R32G32B32A32_FLOAT", "ByteWidth": 16},
                ]},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            position_path = Path(temp_dir) / "part-Position.buf"
            blend_path = Path(temp_dir) / "part-Blend.buf"
            positions.tofile(position_path)
            # 前四列要按 uint32 解释，后四列保持 float32。
            blend_bytes = numpy.empty((len(indices), 32), dtype=numpy.uint8)
            blend_bytes[:, :16] = indices.view(numpy.uint8).reshape(len(indices), 16)
            blend_bytes[:, 16:] = weights.view(numpy.uint8).reshape(len(indices), 16)
            blend_bytes.tofile(blend_path)
            signatures = EFMIBoneMapBuilder.compute_driven_signatures(
                str(position_path), str(blend_path), layout,
            )
        self.assertIn(0, signatures)
        self.assertIn(1, signatures)
        self.assertEqual(len(signatures[0]["diffusion_points"]), 2)
        self.assertTrue(numpy.allclose(signatures[0]["diffusion_weights"], [1.0, 0.5]))

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


class LodProjectionTests(unittest.TestCase):
    """v10（撤销 v9 共享槽位投影）——每 LOD 完全独立 + 分段平移。

    核心保证：非基准 LOD 槽位段与基准 LOD 段**不相交**（LOD0: 0..max0，
    LOD1: base 起），跨 LOD 零共享；v9 投影（LOD1 顶点组映射到 LOD0 槽位 +
    运行时 full→lod BlendRemap）实测爆炸（同帧共用槽位 + 每 component 每帧
    只导入一次骨骼 → LOD1 网格读到 LOD0 矩阵）。
    """

    def test_each_lod_dedups_independently_and_segments_do_not_overlap(self):
        """LOD0 合并（aa.0/bb.0 同槽）；LOD1 用自己的 dump 独立去重 + 整段平移。"""
        shared = _bone(0.0)
        lod0 = {
            "LOD0.aa": _entry([shared, _bone(1.0)], {0: _sig((0.0, 0.0, 0.0))}),
            "LOD0.bb": _entry([shared, _bone(2.0)], {0: _sig((0.01, 0.0, 0.0))}),
        }
        lod1 = {
            "LOD1.xx": _entry([_bone(3.0), _bone(3.1)], {0: _sig((0.0, 0.0, 0.0))}),
        }
        maps, offsets, base = EFMIBoneMapBuilder.build_independent_lod_maps(
            {"LOD0": lod0, "LOD1": lod1}, "LOD0", {}
        )
        # LOD0 分区：aa.0 与 bb.0 同槽（0）；aa.1 -> 1；bb.1 -> 3（自身独立去重不变）
        self.assertEqual(maps["LOD0"]["LOD0.aa"][0], maps["LOD0"]["LOD0.bb"][0])
        self.assertEqual(maps["LOD0"]["LOD0.aa"], {0: 0, 1: 1})
        # 基准段大小 = aa(2) + bb(2) = 4（LOD0 用例槽位 0..3）；不做值域重排。
        self.assertEqual(base, 4)
        # LOD1 用自己 dump 独立去重（3.0/3.1 互不相同 -> 各自槽位），再整体平移 +4：
        # LOD1 全部槽位 ∈ [4, 6)，与 LOD0 段 [0,4) 不相交。
        self.assertEqual(maps["LOD1"]["LOD1.xx"], {0: 4, 1: 5})
        self.assertEqual(offsets["LOD1"]["LOD1.xx"], 4)
        # 两域不相交
        lod0_slots = {v for m in maps["LOD0"].values() for v in m.values()}
        lod1_slots = {v for m in maps["LOD1"].values() for v in m.values()}
        self.assertTrue(lod0_slots.isdisjoint(lod1_slots))

    def test_unmatched_target_local_identity_not_borrowing_baseline(self):
        """LOD1 独有组也是自己槽位段内的恒等映射，绝不借用 LOD0 的编号。"""
        lod0 = {
            "LOD0.aa": _entry([_bone(0.0), _bone(1.0)], {0: _sig((0.0, 0.0, 0.0))}),
        }
        lod1 = {
            "LOD1.xx": _entry([_bone(3.0), _bone(3.1), _bone(3.2)], {}),
        }
        maps, offsets, _base = EFMIBoneMapBuilder.build_independent_lod_maps(
            {"LOD0": lod0, "LOD1": lod1}, "LOD0", {}
        )
        # LOD0 槽位 0..1；LOD1 三个组独立 -> 2,3,4（本 LOD 段内恒等）
        self.assertEqual(maps["LOD0"]["LOD0.aa"], {0: 0, 1: 1})
        self.assertEqual(maps["LOD1"]["LOD1.xx"], {0: 2, 1: 3, 2: 4})
        self.assertEqual(offsets["LOD1"]["LOD1.xx"], 2)

    def test_unmatched_part_gets_own_segment_after_baseline(self):
        """整体未匹配的 LOD1 部件也使用自己段内的恒等映射（base 起）。"""
        lod0 = {
            "LOD0.aa": _entry([_bone(0.0), _bone(1.0)], {}),
        }
        lod1 = {
            "LOD1.zz": _entry([_bone(5.0), _bone(6.0)], {}),
        }
        maps, offsets, base = EFMIBoneMapBuilder.build_independent_lod_maps(
            {"LOD0": lod0, "LOD1": lod1}, "LOD0", {}
        )
        self.assertEqual(base, 2)
        self.assertEqual(offsets["LOD1"]["LOD1.zz"], 2)
        self.assertEqual(maps["LOD1"]["LOD1.zz"], {0: 2, 1: 3})

    # ------------------------------------------------------------------
    # v11（修复 v10 缺陷）：L0 约束 L1 去重（镜像强制 + 标签断边）；
    # v13 撤销值域压缩重排（v12 实测游戏内乱掉）——两侧只做平移，不做重排
    # ------------------------------------------------------------------

    def test_constrained_merge_mirrors_reference_groups(self):
        """v11 镜像强制：L0 合并组 ⇒ L1 对应组必合并（即便 L1 自己的矩阵互不相同）。

        LOD0 的 aa.0/bb.0 矩阵相同 → 合并到槽位 0；四个 L1 候选一一对应这个
        L0 合并组（L1 细分：一个 L0 组 → 七个目标组的同构场景），L1 自己的
        矩阵硬门控会把它们拆成 4 个槽位（v10 缺陷：两边去重不一致）。v11 以
        L0 最终合并组为标签，强制全部落到该组目标侧最小槽位，值域从 base 起。
        """
        lod0 = {
            "LOD0.aa": _entry(
                [_bone(0.0), _bone(1.0)],
                {0: _sig((0.0, 0.0, 0.0))},
                weighted=numpy.array([5, 1]),
            ),
            "LOD0.bb": _entry(
                [_bone(0.0), _bone(2.0)],
                {0: _sig((0.01, 0.0, 0.0))},
                weighted=numpy.array([3, 1]),
            ),
        }
        lod1 = {
            "LOD1.xx": _entry(
                [_bone(3.0), _bone(3.05)],
                {0: _sig((0.0, 0.0, 0.0))},
            ),
            "LOD1.yy": _entry(
                [_bone(4.0), _bone(4.01)],
                {0: _sig((0.01, 0.0, 0.0))},
            ),
        }
        correspondence = {
            "reference_lod": "LOD0",
            "matches": [
                {  # L1 细分：两个候选对应 L0 的 aa.0（同一合并组）
                    "reference_lod": "LOD0", "target_lod": "LOD1",
                    "reference_unique_str": "LOD0.aa", "reference_local_vg_id": 0,
                    "target_unique_str": "LOD1.xx", "target_local_vg_id": 0,
                },
                {
                    "reference_lod": "LOD0", "target_lod": "LOD1",
                    "reference_unique_str": "LOD0.aa", "reference_local_vg_id": 0,
                    "target_unique_str": "LOD1.xx", "target_local_vg_id": 1,
                },
                {
                    "reference_lod": "LOD0", "target_lod": "LOD1",
                    "reference_unique_str": "LOD0.bb", "reference_local_vg_id": 0,
                    "target_unique_str": "LOD1.yy", "target_local_vg_id": 0,
                },
                {
                    "reference_lod": "LOD0", "target_lod": "LOD1",
                    "reference_unique_str": "LOD0.bb", "reference_local_vg_id": 0,
                    "target_unique_str": "LOD1.yy", "target_local_vg_id": 1,
                },
            ],
        }
        maps, offsets, base = EFMIBoneMapBuilder.build_independent_lod_maps(
            {"LOD0": lod0, "LOD1": lod1}, "LOD0", {},
            correspondence=correspondence,
        )
        # L0：aa.0/bb.0 同槽 0（canonical = 加权数更大的 aa.0）；aa.1 -> 1；bb.1 -> 3
        self.assertEqual(maps["LOD0"]["LOD0.aa"][0], maps["LOD0"]["LOD0.bb"][0])
        self.assertEqual(base, 4)
        # L1：四个候选（3.0/3.05/4.0/4.01 互不相同，仅凭 L1 自己绝不合并）
        # 被镜像强制到同一槽位（组内最小槽 0），平移 +4 后 = 4。
        self.assertEqual(maps["LOD1"]["LOD1.xx"], {0: 4, 1: 4})
        self.assertEqual(maps["LOD1"]["LOD1.yy"], {0: 4, 1: 4})
        slots1 = {v for m in maps["LOD1"].values() for v in m.values()}
        self.assertEqual(slots1, {4}, "L1 镜像组应为单个槽位 4（段内最小槽平移，不做重排）")
        lod0_slots = {v for m in maps["LOD0"].values() for v in m.values()}
        self.assertTrue(lod0_slots.isdisjoint(slots1))

    def test_constrained_labels_block_cross_reference_group_merge(self):
        """v11 标签断边：L1 候选对应**不同** L0 组时，即使矩阵完全相同也不合并。

        这里两个 L1 子网格的骨骼矩阵 bitwise 相同（无扩散签名时 v10 会直接
        合并），但它们分别对应 L0 的 aa.0 / bb.0——L0 没合并，L1 绝不能合并。
        """
        lod0 = {
            "LOD0.aa": _entry([_bone(0.0)], {}),
            "LOD0.bb": _entry([_bone(5.0)], {}),
        }
        lod1 = {
            "LOD1.xx": _entry([_bone(2.0)], {}),
            "LOD1.yy": _entry([_bone(2.0)], {}),
        }
        correspondence = {
            "reference_lod": "LOD0",
            "matches": [
                {
                    "reference_lod": "LOD0", "target_lod": "LOD1",
                    "reference_unique_str": "LOD0.aa", "reference_local_vg_id": 0,
                    "target_unique_str": "LOD1.xx", "target_local_vg_id": 0,
                },
                {
                    "reference_lod": "LOD0", "target_lod": "LOD1",
                    "reference_unique_str": "LOD0.bb", "reference_local_vg_id": 0,
                    "target_unique_str": "LOD1.yy", "target_local_vg_id": 0,
                },
            ],
        }
        maps, offsets, base = EFMIBoneMapBuilder.build_independent_lod_maps(
            {"LOD0": lod0, "LOD1": lod1}, "LOD0", {},
            correspondence=correspondence,
        )
        self.assertEqual(base, 2)
        # 无标签时这两组会合并；有标签后必须分属两个槽位（L0 未合并 ⇒ L1 不合并）。
        self.assertNotEqual(
            maps["LOD1"]["LOD1.xx"][0], maps["LOD1"]["LOD1.yy"][0],
            "不同 L0 组的 L1 候选不得合并（标签断边）",
        )
        self.assertEqual(maps["LOD1"]["LOD1.xx"], {0: 2})
        self.assertEqual(maps["LOD1"]["LOD1.yy"], {0: 3})

    def test_merged_away_first_local_keeps_canonical_slot_no_renumber(self):
        """v13 撤销值域重排：L1 首个子网格 local 0 并入他人 canonical 后**不再压缩**。

        v12 曾把该情形重排为 base 起密集（无洞）——实测游戏内乱掉。撤销后：
        canonical 槽位（权重数更大者）保持不变，值 = canonical 槽位置 + 平移；
        段内可能留下未引用槽位（空洞），但值域始终 ⊆ 本 LOD 声明段空间，
        与 LOD0 域 [0, base) 不相交。
        """
        lod0 = {
            "LOD0.aa": _entry([_bone(1.0), _bone(2.0)], {}),
        }
        lod1 = {
            "LOD1.xx": _entry([_bone(3.0)], {}, weighted=numpy.array([1])),
            "LOD1.yy": _entry([_bone(3.0)], {}, weighted=numpy.array([9])),
        }
        maps, offsets, base = EFMIBoneMapBuilder.build_independent_lod_maps(
            {"LOD0": lod0, "LOD1": lod1}, "LOD0", {}
        )
        self.assertEqual(base, 2)
        # xx.0 与 yy.0 矩阵相同 → 合并；canonical = 加权数更大的 yy.0
        # （去重前 global_id = 1），平移 +2 后 = 3（不做重排，0 号槽位成空洞但
        # 值域 [3,4) ⊂ [2,4) 声明段空间内，与 LOD0 值域 [0,2) 不相交）。
        self.assertEqual(maps["LOD1"]["LOD1.xx"], {0: 3})
        self.assertEqual(maps["LOD1"]["LOD1.yy"], {0: 3})
        self.assertEqual(offsets["LOD1"]["LOD1.xx"], 2)
        self.assertEqual(offsets["LOD1"]["LOD1.yy"], 3)

    def test_unmatched_target_local_keeps_own_slot_after_baseline(self):
        """v11：L0 无对应的 L1 候选不打标签——镜像强制只作用于有对应的候选。

        LOD1.xx 两个候选对应同一 L0 合并组（强制同槽）；LOD1.yy 两个候选
        无对应，保持自身去重槽位，绝不借用镜像组槽位；值域仍从 base 起密集。
        """
        lod0 = {
            "LOD0.aa": _entry(
                [_bone(0.0), _bone(1.0)],
                {0: _sig((0.0, 0.0, 0.0))},
                weighted=numpy.array([5, 1]),
            ),
            "LOD0.bb": _entry(
                [_bone(0.0), _bone(2.0)],
                {0: _sig((0.01, 0.0, 0.0))},
                weighted=numpy.array([3, 1]),
            ),
        }
        lod1 = {
            "LOD1.xx": _entry(
                [_bone(3.0), _bone(3.05)],
                {0: _sig((0.0, 0.0, 0.0))},
            ),
            "LOD1.yy": _entry(
                [_bone(4.0), _bone(4.01)],
                {0: _sig((0.01, 0.0, 0.0))},
            ),
        }
        correspondence = {
            "reference_lod": "LOD0",
            "matches": [
                {  # 只有 xx.0/xx.1 有对应（同一个 L0 合并组）；yy 全是无对应
                    "reference_lod": "LOD0", "target_lod": "LOD1",
                    "reference_unique_str": "LOD0.aa", "reference_local_vg_id": 0,
                    "target_unique_str": "LOD1.xx", "target_local_vg_id": 0,
                },
                {
                    "reference_lod": "LOD0", "target_lod": "LOD1",
                    "reference_unique_str": "LOD0.bb", "reference_local_vg_id": 0,
                    "target_unique_str": "LOD1.xx", "target_local_vg_id": 1,
                },
            ],
        }
        maps, offsets, base = EFMIBoneMapBuilder.build_independent_lod_maps(
            {"LOD0": lod0, "LOD1": lod1}, "LOD0", {},
            correspondence=correspondence,
        )
        self.assertEqual(base, 4)
        # xx 两个候选对应同一 L0 合并组 → 强制同槽（组内最小槽 0，平移 +4 = 4）；
        # yy 无标签 → 各自槽位（+4 后 6/7）。**不做值域重排**（v13）：槽位 5
        # 无候选引用（空洞由 canonical 借位产生，属正常；值域 ⊆ L1 声明段空间）。
        self.assertEqual(maps["LOD1"]["LOD1.xx"], {0: 4, 1: 4})
        self.assertNotEqual(
            maps["LOD1"]["LOD1.yy"][0], maps["LOD1"]["LOD1.yy"][1],
            "无标签候选保持自身去重槽位，不并入镜像组",
        )
        slots1 = {v for m in maps["LOD1"].values() for v in m.values()}
        self.assertEqual(slots1, {4, 6, 7}, "L1 值域 = 镜像槽 4 + 无标签槽 6/7（不重排，保留空洞）")
        lod0_slots = {v for m in maps["LOD0"].values() for v in m.values()}
        self.assertTrue(lod0_slots.isdisjoint(slots1))


if __name__ == "__main__":
    unittest.main()
