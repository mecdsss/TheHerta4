# -*- coding: utf-8 -*-
"""高斯权重球核心数学（toolkit/gb_core.py）的纯 numpy 单元测试。

不依赖 bpy，可直接在普通 Python 环境下用 pytest 运行。
"""

import importlib.util
import unittest
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_gb_core():
    """按路径直接加载 toolkit/gb_core.py，绕过 toolkit/__init__.py 的 bpy 依赖。"""
    module_path = _REPO_ROOT / "toolkit" / "gb_core.py"
    spec = importlib.util.spec_from_file_location("toolkit_gb_core", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gb_core = _load_gb_core()


def _identity():
    return np.eye(4, dtype=np.float64)


def _matrix(loc=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
    """构造仅含平移 + 逐轴缩放的 4x4 矩阵（无旋转，对角缩放）。"""
    m = np.eye(4, dtype=np.float64)
    m[0, 0], m[1, 1], m[2, 2] = scale
    m[0, 3], m[1, 3], m[2, 3] = loc
    return m


class GaussianFieldTests(unittest.TestCase):
    def test_center_weight_equals_strength(self):
        verts = np.array([[0.0, 0.0, 0.0]])
        w = gb_core.gaussian_field(verts, _identity(), strength=0.8, falloff_k=4.6)
        self.assertAlmostEqual(float(w[0]), 0.8, places=6)

    def test_zero_outside_ball_surface(self):
        # d >= 1（球面之外）必须严格为 0
        verts = np.array([[1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.0, 1.5, 0.0]])
        w = gb_core.gaussian_field(verts, _identity(), strength=1.0, falloff_k=4.6)
        np.testing.assert_array_equal(w, np.zeros(3))

    def test_monotonic_decay_with_distance(self):
        dists = np.linspace(0.0, 0.95, 20)
        verts = np.stack([dists, np.zeros_like(dists), np.zeros_like(dists)], axis=1)
        w = gb_core.gaussian_field(verts, _identity(), strength=1.0, falloff_k=4.6)
        diffs = np.diff(w)
        self.assertTrue(np.all(diffs <= 1e-12), f"权重应随距离单调不增, diffs={diffs}")

    def test_larger_falloff_shrinks_midrange_weight(self):
        verts = np.array([[0.5, 0.0, 0.0]])
        w_soft = gb_core.gaussian_field(verts, _identity(), 1.0, falloff_k=1.0)
        w_hard = gb_core.gaussian_field(verts, _identity(), 1.0, falloff_k=6.0)
        self.assertLess(float(w_hard[0]), float(w_soft[0]))

    def test_ball_scale_sets_radius(self):
        # 缩放 2 倍 => 半径 2；世界距离 1.5 处 d=0.75 仍有权重，世界距离 2.0 处为 0
        m = _matrix(scale=(2.0, 2.0, 2.0))
        inside = gb_core.gaussian_field(np.array([[1.5, 0.0, 0.0]]), m, 1.0, 4.6)
        outside = gb_core.gaussian_field(np.array([[2.0, 0.0, 0.0]]), m, 1.0, 4.6)
        self.assertGreater(float(inside[0]), 0.0)
        self.assertEqual(float(outside[0]), 0.0)

    def test_ball_translation_moves_center(self):
        m = _matrix(loc=(1.0, 0.0, 0.0))
        at_center = gb_core.gaussian_field(np.array([[1.0, 0.0, 0.0]]), m, 1.0, 4.6)
        at_origin = gb_core.gaussian_field(np.array([[0.0, 0.0, 0.0]]), m, 1.0, 4.6)
        self.assertAlmostEqual(float(at_center[0]), 1.0, places=6)
        self.assertEqual(float(at_origin[0]), 0.0)

    def test_ellipsoid_prefers_long_axis(self):
        # x 轴拉长 3 倍：世界距离 1.5 在 x 方向 d=0.5（有权重），y 方向 d=1.5（零）
        m = _matrix(scale=(3.0, 1.0, 1.0))
        along_x = gb_core.gaussian_field(np.array([[1.5, 0.0, 0.0]]), m, 1.0, 4.6)
        along_y = gb_core.gaussian_field(np.array([[0.0, 1.5, 0.0]]), m, 1.0, 4.6)
        self.assertGreater(float(along_x[0]), 0.0)
        self.assertEqual(float(along_y[0]), 0.0)

    def test_empty_verts_returns_empty(self):
        w = gb_core.gaussian_field(np.zeros((0, 3)), _identity(), 1.0, 4.6)
        self.assertEqual(w.shape, (0,))

    def test_degenerate_scale_yields_zero(self):
        m = _matrix(scale=(0.0, 0.0, 0.0))
        w = gb_core.gaussian_field(np.array([[0.0, 0.0, 0.0]]), m, 1.0, 4.6)
        np.testing.assert_array_equal(w, np.zeros(1))


class MergeFieldsTests(unittest.TestCase):
    def test_max_merge(self):
        a = np.array([0.1, 0.9, 0.0])
        b = np.array([0.5, 0.2, 0.7])
        merged = gb_core.merge_fields_max([a, b])
        np.testing.assert_allclose(merged, [0.5, 0.9, 0.7])

    def test_merge_never_exceeds_max_strength(self):
        a = np.array([0.4, 0.4])
        b = np.array([0.4, 0.4])
        merged = gb_core.merge_fields_max([a, b])
        self.assertLessEqual(float(merged.max()), 0.4)

    def test_empty_list(self):
        self.assertEqual(gb_core.merge_fields_max([]).shape, (0,))


class VgStatsTests(unittest.TestCase):
    def test_weighted_centroid_biased_to_high_weight(self):
        positions = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        weights = np.array([1.0, 0.01])
        stats = gb_core.compute_vg_stats(positions, weights)
        self.assertLess(float(stats["centroid"][0]), 1.0)
        self.assertAlmostEqual(float(stats["max_weight"]), 1.0, places=6)

    def test_zero_weights_fall_back_to_mean(self):
        positions = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        weights = np.array([0.0, 0.0])
        stats = gb_core.compute_vg_stats(positions, weights)
        np.testing.assert_allclose(stats["centroid"], [5.0, 0.0, 0.0])
        self.assertEqual(float(stats["max_weight"]), 0.0)
        self.assertGreater(float(stats["radius"]), 0.0)

    def test_radius_is_reasonable_quantile(self):
        rng = np.random.default_rng(42)
        positions = rng.normal(size=(500, 3))
        weights = np.ones(500)
        stats = gb_core.compute_vg_stats(positions, weights)
        dists = np.linalg.norm(positions - stats["centroid"], axis=1)
        # 半径应大致处于距离分布的中高段，且严格为正
        self.assertGreater(float(stats["radius"]), float(np.median(dists)) * 0.3)
        self.assertLess(float(stats["radius"]), float(dists.max()) * 2.0)

    def test_empty_input(self):
        stats = gb_core.compute_vg_stats(np.zeros((0, 3)), np.zeros(0))
        self.assertEqual(float(stats["max_weight"]), 0.0)
        self.assertGreater(float(stats["radius"]), 0.0)


class InitialBallParamsTests(unittest.TestCase):
    def test_uses_stats_when_weights_present(self):
        stats = {"centroid": np.array([1.0, 2.0, 3.0]), "max_weight": 0.9, "radius": 0.5}
        params = gb_core.initial_ball_params(stats, fallback_location=(9.0, 9.0, 9.0))
        np.testing.assert_allclose(params["location"], [1.0, 2.0, 3.0])
        self.assertAlmostEqual(params["strength"], 0.9, places=6)
        self.assertAlmostEqual(params["radius"], 0.5, places=6)

    def test_falls_back_to_debug_location(self):
        stats = {"centroid": np.array([1.0, 2.0, 3.0]), "max_weight": 0.0, "radius": 0.5}
        params = gb_core.initial_ball_params(stats, fallback_location=(9.0, 9.0, 9.0))
        np.testing.assert_allclose(params["location"], [9.0, 9.0, 9.0])
        self.assertGreater(params["radius"], 0.0)

    def test_zero_max_weight_strength_falls_back_to_one(self):
        # 退化输入（无有效权重）不应产生强度 0 的废球
        stats = {"centroid": np.array([0.0, 0.0, 0.0]), "max_weight": 0.0, "radius": 0.5}
        params = gb_core.initial_ball_params(stats)
        self.assertAlmostEqual(params["strength"], 1.0, places=6)


class WeightsToColorsTests(unittest.TestCase):
    def test_cold_to_hot_and_shape(self):
        weights = np.array([0.0, 0.5, 1.0])
        colors = gb_core.weights_to_colors(weights, opacity=0.85)
        self.assertEqual(colors.shape, (3, 4))
        self.assertTrue(np.all(colors >= 0.0) and np.all(colors <= 1.0))
        # 冷端偏蓝、热端偏红
        self.assertGreater(float(colors[0, 2]), float(colors[0, 0]))
        self.assertGreater(float(colors[2, 0]), float(colors[2, 2]))
        # 单调红增蓝减
        self.assertLess(float(colors[0, 0]), float(colors[2, 0]))

    def test_alpha_scales_with_weight_and_opacity(self):
        colors = gb_core.weights_to_colors(np.array([0.0, 1.0]), opacity=0.5)
        self.assertAlmostEqual(float(colors[0, 3]), 0.0, places=6)
        self.assertAlmostEqual(float(colors[1, 3]), 0.5, places=6)

    def test_zero_opacity_is_transparent(self):
        colors = gb_core.weights_to_colors(np.array([1.0]), opacity=0.0)
        self.assertEqual(float(colors[0, 3]), 0.0)

    def test_absolute_strength_visible(self):
        # 回归：不同绝对强度必须产生不同颜色（此前按 w_max 归一化会抵消强度差异，
        # 导致调中心强度在热力图上没有任何视觉效果）
        c_strong = gb_core.weights_to_colors(np.array([1.0]), opacity=0.85)
        c_weak = gb_core.weights_to_colors(np.array([0.3]), opacity=0.85)
        self.assertFalse(np.allclose(c_strong, c_weak))
        # 绝对映射：alpha = opacity * w
        self.assertAlmostEqual(float(c_weak[0, 3]), 0.85 * 0.3, places=6)
        self.assertAlmostEqual(float(c_strong[0, 3]), 0.85, places=6)


class EstimateFalloffKTests(unittest.TestCase):
    def test_recovers_synthetic_gaussian_k(self):
        # 合成数据：w = 0.9 * exp(-2.0 * (d/R)^2)，应拟合出 k ≈ 2.0
        radius = 0.5
        true_k = 2.0
        dists = np.linspace(0.1 * radius, 0.95 * radius, 30)
        positions = np.stack(
            [dists, np.zeros_like(dists), np.zeros_like(dists)], axis=1)
        weights = 0.9 * np.exp(-true_k * (dists / radius) ** 2)
        k = gb_core.estimate_falloff_k(
            positions, weights, np.zeros(3), radius, max_weight=0.9)
        self.assertAlmostEqual(k, true_k, delta=0.15)

    def test_flat_weights_fall_back_to_default(self):
        # 所有权重相同（无衰减信息）时回退默认值
        positions = np.array([[0.1, 0.0, 0.0], [0.2, 0.0, 0.0], [0.3, 0.0, 0.0]])
        weights = np.array([1.0, 1.0, 1.0])
        k = gb_core.estimate_falloff_k(
            positions, weights, np.zeros(3), 0.5, max_weight=1.0)
        self.assertAlmostEqual(k, gb_core.DEFAULT_FALLOFF_K, places=6)

    def test_empty_input_falls_back_to_default(self):
        k = gb_core.estimate_falloff_k(
            np.zeros((0, 3)), np.zeros(0), np.zeros(3), 0.5, max_weight=0.0)
        self.assertAlmostEqual(k, gb_core.DEFAULT_FALLOFF_K, places=6)

    def test_result_clamped_to_property_range(self):
        # 极缓衰减（k 远小于属性下限）应被钳制到 [0.5, 8.0]
        radius = 0.5
        dists = np.linspace(0.1 * radius, 0.95 * radius, 30)
        positions = np.stack(
            [dists, np.zeros_like(dists), np.zeros_like(dists)], axis=1)
        weights = 0.9 * np.exp(-0.01 * (dists / radius) ** 2)
        k = gb_core.estimate_falloff_k(
            positions, weights, np.zeros(3), radius, max_weight=0.9)
        self.assertGreaterEqual(k, 0.5)
        self.assertLessEqual(k, 8.0)


class IslandIdsTests(unittest.TestCase):
    def test_two_disjoint_triangles_two_islands(self):
        # 三角形 A: 0,1,2；三角形 B: 3,4,5（不相连）
        edge_verts = np.array([
            [0, 1], [1, 2], [2, 0],
            [3, 4], [4, 5], [5, 3],
        ], dtype=np.int64)
        ids = gb_core.compute_island_ids(6, edge_verts)
        self.assertEqual(len(set(ids.tolist())), 2)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[1], ids[2])
        self.assertEqual(ids[3], ids[4])
        self.assertNotEqual(ids[0], ids[3])

    def test_shared_edge_same_island(self):
        # 两个三角形共享边 1-2
        edge_verts = np.array([
            [0, 1], [1, 2], [2, 0],
            [1, 3], [3, 2], [2, 1],
        ], dtype=np.int64)
        ids = gb_core.compute_island_ids(4, edge_verts)
        self.assertEqual(len(set(ids.tolist())), 1)

    def test_loose_vertex_is_own_island(self):
        edge_verts = np.array([[0, 1], [1, 2], [2, 0]], dtype=np.int64)
        ids = gb_core.compute_island_ids(4, edge_verts)
        self.assertNotEqual(ids[3], ids[0])

    def test_empty_edges(self):
        ids = gb_core.compute_island_ids(3, np.zeros((0, 2), dtype=np.int64))
        self.assertEqual(len(set(ids.tolist())), 3)


class NearestIslandTests(unittest.TestCase):
    def test_nearest_island_at(self):
        verts = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [10.0, 0.0, 0.0]])
        island_ids = np.array([0, 0, 1])
        bound = gb_core.nearest_island_at(verts, island_ids, np.array([0.05, 0.0, 0.0]))
        self.assertEqual(bound, 0)
        bound_far = gb_core.nearest_island_at(verts, island_ids, np.array([9.0, 0.0, 0.0]))
        self.assertEqual(bound_far, 1)

    def test_apply_island_mask(self):
        weights = np.array([0.5, 0.6, 0.7])
        island_ids = np.array([0, 0, 1])
        masked = gb_core.apply_island_mask(weights, island_ids, 0)
        np.testing.assert_allclose(masked, [0.5, 0.6, 0.0])


class IslandMaskIntegrationTests(unittest.TestCase):
    def test_ball_near_island_a_zeroes_island_b(self):
        # 两个不相连"岛"：A 在原点附近，B 在 x=0.5 附近（球半径足以覆盖两者）
        verts_a = np.array([[0.0, 0.0, 0.0], [0.05, 0.0, 0.0]])
        verts_b = np.array([[0.5, 0.0, 0.0], [0.55, 0.0, 0.0]])
        verts = np.vstack([verts_a, verts_b])
        island_ids = np.array([0, 0, 1, 1])

        m = _matrix(scale=(1.0, 1.0, 1.0))  # 球心在原点，半径 1，覆盖两岛
        w = gb_core.gaussian_field(verts, m, 1.0, 4.6)
        # 无遮罩时两岛都有权重
        self.assertGreater(float(w[2]), 0.0)

        bound = gb_core.nearest_island_at(verts, island_ids, m[0:3, 3])
        masked = gb_core.apply_island_mask(w, island_ids, bound)
        self.assertEqual(bound, 0)
        self.assertGreater(float(masked[0]), 0.0)
        self.assertEqual(float(masked[2]), 0.0)
        self.assertEqual(float(masked[3]), 0.0)

    def test_two_balls_bind_their_own_islands(self):
        verts_a = np.array([[0.0, 0.0, 0.0], [0.05, 0.0, 0.0]])
        verts_b = np.array([[0.5, 0.0, 0.0], [0.55, 0.0, 0.0]])
        verts = np.vstack([verts_a, verts_b])
        island_ids = np.array([0, 0, 1, 1])

        m_a = _matrix(loc=(0.0, 0.0, 0.0), scale=(0.3, 0.3, 0.3))
        m_b = _matrix(loc=(0.5, 0.0, 0.0), scale=(0.3, 0.3, 0.3))

        w_a = gb_core.gaussian_field(verts, m_a, 1.0, 4.6)
        w_b = gb_core.gaussian_field(verts, m_b, 1.0, 4.6)
        w_a = gb_core.apply_island_mask(
            w_a, island_ids, gb_core.nearest_island_at(verts, island_ids, m_a[0:3, 3]))
        w_b = gb_core.apply_island_mask(
            w_b, island_ids, gb_core.nearest_island_at(verts, island_ids, m_b[0:3, 3]))

        merged = gb_core.merge_fields_max([w_a, w_b])
        # 球 A 只影响岛 A、球 B 只影响岛 B，合并后两岛都有权重
        self.assertGreater(float(merged[0]), 0.0)
        self.assertGreater(float(merged[3]), 0.0)


class CombineWeightSourcesTests(unittest.TestCase):
    """合集匹配的多物体权重聚合（gb_core.combine_weight_sources）。"""

    def test_concatenates_in_order(self):
        p1 = np.array([[0.0, 0.0, 0.0]])
        w1 = np.array([0.5])
        p2 = np.array([[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
        w2 = np.array([0.8, 0.3])
        pos, w = gb_core.combine_weight_sources([(p1, w1), (p2, w2)])
        np.testing.assert_allclose(pos, [[0, 0, 0], [1, 0, 0], [2, 0, 0]])
        np.testing.assert_allclose(w, [0.5, 0.8, 0.3])

    def test_empty_sources(self):
        pos, w = gb_core.combine_weight_sources([])
        self.assertEqual(pos.shape, (0, 3))
        self.assertEqual(w.shape, (0,))

    def test_single_source_passthrough(self):
        p = np.array([[1.0, 2.0, 3.0]])
        w = np.array([0.9])
        pos, out_w = gb_core.combine_weight_sources([(p, w)])
        np.testing.assert_allclose(pos, p)
        np.testing.assert_allclose(out_w, w)

    def test_merged_stats_cover_all_objects(self):
        # 模拟合集匹配：组只存在于第 2 个物体时，聚合后统计必须覆盖它
        # （修复前只读合集首个网格会报"不存在顶点组"或统计缺失）
        p1 = np.zeros((3, 3))
        w1 = np.zeros(3)
        p2 = np.array([[10.0, 0.0, 0.0], [10.1, 0.0, 0.0]])
        w2 = np.array([0.9, 0.8])
        pos, w = gb_core.combine_weight_sources([(p1, w1), (p2, w2)])
        stats = gb_core.compute_vg_stats(pos, w)
        self.assertGreater(float(stats["centroid"][0]), 9.0)


class SampledFieldTests(unittest.TestCase):
    """源权重采样场（gb_core.sampled_field）——多峰/非对称分布保真传递。"""

    def _field(self, target_verts, src_pos, src_w, matrix=None, scale=1.0):
        if matrix is None:
            matrix = _identity()
        return gb_core.sampled_field(
            target_verts, src_pos, src_w, matrix, strength_scale=scale)

    def test_takes_nearest_source_weight(self):
        # 目标顶点取最近源顶点的原始权重
        src_pos = np.array([[0.0, 0.0, 0.0], [0.5, 0.0, 0.0]])
        src_w = np.array([0.9, 0.3])
        verts = np.array([[0.05, 0.0, 0.0], [0.45, 0.0, 0.0]])
        field = self._field(verts, src_pos, src_w)
        self.assertAlmostEqual(float(field[0]), 0.9, places=6)
        self.assertAlmostEqual(float(field[1]), 0.3, places=6)

    def test_outside_ball_is_zero(self):
        src_pos = np.array([[0.0, 0.0, 0.0]])
        src_w = np.array([0.9])
        verts = np.array([[2.0, 0.0, 0.0], [-1.5, 0.0, 0.0]])
        field = self._field(verts, src_pos, src_w)
        np.testing.assert_array_equal(field, np.zeros(2))

    def test_multi_peak_distribution_preserved(self):
        # 核心卖点：两个分离簇（解析高斯只能拟合一个峰，采样场两峰都保留）
        src_pos = np.array([
            [0.0, 0.0, 0.0], [0.05, 0.0, 0.0],      # 峰 A
            [0.9, 0.0, 0.0], [0.95, 0.0, 0.0],      # 峰 B
        ])
        src_w = np.array([0.9, 0.85, 0.6, 0.55])
        verts = np.array([[0.02, 0.0, 0.0], [0.93, 0.0, 0.0]])
        field = self._field(verts, src_pos, src_w)
        self.assertAlmostEqual(float(field[0]), 0.9, places=6)
        # 0.93 距 0.95（0.55）比距 0.9（0.6）更近，取最近源顶点权重
        self.assertAlmostEqual(float(field[1]), 0.55, places=6)

    def test_strength_scale_multiplies(self):
        src_pos = np.array([[0.0, 0.0, 0.0]])
        src_w = np.array([0.8])
        verts = np.array([[0.1, 0.0, 0.0]])
        field = self._field(verts, src_pos, src_w, scale=0.5)
        self.assertAlmostEqual(float(field[0]), 0.4, places=6)

    def test_empty_source_cloud_is_zero(self):
        verts = np.array([[0.0, 0.0, 0.0], [0.2, 0.0, 0.0]])
        field = self._field(verts, np.zeros((0, 3)), np.zeros(0))
        np.testing.assert_array_equal(field, np.zeros(2))

    def test_no_source_inside_ball_is_zero(self):
        # 球内没有源点时（源点全在球外）权重为 0
        src_pos = np.array([[5.0, 0.0, 0.0]])
        src_w = np.array([0.9])
        verts = np.array([[0.0, 0.0, 0.0]])
        field = self._field(verts, src_pos, src_w)
        self.assertEqual(float(field[0]), 0.0)

    def test_ball_scale_and_translation_respected(self):
        # 球缩放 2 倍（半径 2）+ 平移 (1,0,0)：x=2.5 处 d=0.75 有权重，x=3.5 处 d=1.25 为 0
        m = _matrix(loc=(1.0, 0.0, 0.0), scale=(2.0, 2.0, 2.0))
        src_pos = np.array([[1.0, 0.0, 0.0]])
        src_w = np.array([0.7])
        verts = np.array([[2.5, 0.0, 0.0], [3.5, 0.0, 0.0]])
        field = self._field(verts, src_pos, src_w, matrix=m)
        self.assertAlmostEqual(float(field[0]), 0.7, places=6)
        self.assertEqual(float(field[1]), 0.0)

    def test_ellipsoid_long_axis(self):
        # 非均匀缩放（椭球）：长轴方向世界距离更远的点仍在球内
        m = _matrix(scale=(3.0, 1.0, 1.0))
        src_pos = np.array([[0.0, 0.0, 0.0]])
        src_w = np.array([0.8])
        verts = np.array([[1.5, 0.0, 0.0], [0.0, 1.5, 0.0]])
        field = self._field(verts, src_pos, src_w, matrix=m)
        self.assertAlmostEqual(float(field[0]), 0.8, places=6)  # x 方向 d=0.5
        self.assertEqual(float(field[1]), 0.0)                  # y 方向 d=1.5

    def test_no_penetration_to_far_internal_point(self):
        # 模拟穿透场景：源点全在球外（球覆盖的是空区域）→ 不产生权重
        src_pos = np.array([[-2.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
        src_w = np.array([0.9, 0.9])
        verts = np.array([[0.0, 0.0, 0.0]])  # 球心在两个源点之间
        field = self._field(verts, src_pos, src_w)
        self.assertEqual(float(field[0]), 0.0)


class ProjectedSampledFieldTests(unittest.TestCase):
    """接收侧投影采样场（gb_core.projected_sampled_field）。

    预览方向修复（gaussian-preview-target-fix t3）：GB 会话的采样场语义从
    “球内源点最近邻”（sampled_field，球=权重来源范围）改为“球内目标顶点取
    **全源点云**最近源点权重”（本函数，球=接收区域）。这让“源侧与接收侧
    不重叠”时接收侧（目标对象/合集）也能即时显示非零权重——不再停留在源
    调试物体自身。sampled_field 原样保留（既有 gb_core 语义与测试锁定不动）。
    """

    def _field(self, target_verts, src_pos, src_w, matrix=None, scale=1.0):
        if matrix is None:
            matrix = _identity()
        return gb_core.projected_sampled_field(
            target_verts, src_pos, src_w, matrix, strength_scale=scale)

    def test_nonzero_when_source_outside_ball(self):
        # 回归核心：源点全在球外（源/接收侧不重叠）时，球内目标顶点仍取
        # 最近源点权重（旧 sampled_field 语义返回全零——预览停留在源调试物体）
        src_pos = np.array([[5.0, 0.0, 0.0]])
        src_w = np.array([0.9])
        verts = np.array([[0.0, 0.0, 0.0]])
        field = self._field(verts, src_pos, src_w)
        self.assertAlmostEqual(float(field[0]), 0.9, places=6)

    def test_projection_between_two_source_clusters(self):
        # 球覆盖空区域、源分居两侧：目标取最近（而非更远但更重）的源点权重
        src_pos = np.array([[-2.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
        src_w = np.array([0.9, 0.6])
        verts = np.array([[0.2, 0.0, 0.0]])  # 距 (2,0,0)=1.8 < 距 (-2,0,0)=2.2
        field = self._field(verts, src_pos, src_w)
        self.assertAlmostEqual(float(field[0]), 0.6, places=6)

    def test_outside_target_vertices_still_zero(self):
        # 球仍是接收区域：球外目标顶点严格为 0
        src_pos = np.array([[0.0, 0.0, 0.0]])
        src_w = np.array([0.9])
        verts = np.array([[2.0, 0.0, 0.0], [-1.5, 0.0, 0.0]])
        field = self._field(verts, src_pos, src_w)
        np.testing.assert_array_equal(field, np.zeros(2))

    def test_nearest_source_from_full_cloud(self):
        # 目标取全点云最近源顶点权重（不限于球内源点）
        src_pos = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        src_w = np.array([0.4, 0.9])
        verts = np.array([[0.9, 0.0, 0.0]])
        field = self._field(verts, src_pos, src_w)
        self.assertAlmostEqual(float(field[0]), 0.9, places=6)

    def test_matches_sampled_when_sources_inside_ball(self):
        # 重叠场景（源/目标同位）：投影语义与既有 sampled_field 一致（无回归）
        src_pos = np.array([[0.0, 0.0, 0.0], [0.5, 0.0, 0.0]])
        src_w = np.array([0.9, 0.3])
        verts = np.array([[0.05, 0.0, 0.0], [0.45, 0.0, 0.0]])
        proj = self._field(verts, src_pos, src_w)
        samp = gb_core.sampled_field(verts, src_pos, src_w, _identity())
        np.testing.assert_allclose(proj, samp, atol=1e-12)

    def test_strength_scale_and_empty_source(self):
        src_pos = np.array([[0.0, 0.0, 0.0]])
        src_w = np.array([0.8])
        verts = np.array([[0.1, 0.0, 0.0]])
        self.assertAlmostEqual(
            float(self._field(verts, src_pos, src_w, scale=0.5)[0]), 0.4, places=6)
        np.testing.assert_array_equal(
            self._field(verts, np.zeros((0, 3)), np.zeros(0)), np.zeros(1))


class GeodesicFieldTests(unittest.TestCase):
    """沿表面传播（测地）权重场：接触点种子 + Dijkstra 表面距离。"""

    def _two_strips(self):
        """front z=0（顶点0-4）、back z=0.5（顶点5-9），各自连边、片间不连通。"""
        front = np.array([[x * 0.1, 0.0, 0.0] for x in range(5)])
        back = np.array([[x * 0.1, 0.0, 0.5] for x in range(5)])
        verts = np.vstack([front, back])
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 4],
                         [5, 6, 7], [5, 7, 8], [5, 8, 9]])
        return verts, gb_core.edges_from_triangles(tris)

    def _ball(self, radius=0.6):
        return _matrix(scale=(radius, radius, radius))

    def test_edges_from_triangles_dedup(self):
        tris = np.array([[2, 0, 1], [1, 0, 2]])  # 同一三角形写两遍（边重复）
        e = gb_core.edges_from_triangles(tris)
        np.testing.assert_array_equal(e, np.array([[0, 1], [0, 2], [1, 2]]))

    def test_contact_point_matches_volumetric(self):
        verts, edges = self._two_strips()
        ball = self._ball()
        local = gb_core._to_ball_local(verts, ball)
        contact = int(np.argmin(np.einsum("ij,ij->i", local, local)))
        g_geo = gb_core.geodesic_field(verts, ball, 1.0, 4.6, edges)
        g_vol = gb_core.gaussian_field(verts, ball, 1.0, 4.6)
        self.assertAlmostEqual(float(g_geo[contact]), float(g_vol[contact]), places=9)

    def test_disconnected_back_strip_gets_zero(self):
        verts, edges = self._two_strips()
        ball = self._ball()
        g_vol = gb_core.gaussian_field(verts, ball, 1.0, 4.6)
        g_geo = gb_core.geodesic_field(verts, ball, 1.0, 4.6, edges)
        self.assertGreater(float(g_vol[7]), 0.0)               # 体积球穿透到 back
        np.testing.assert_array_equal(g_geo[5:], np.zeros(5))  # 测地：back 全 0
        self.assertGreater(float(g_geo[2]), 0.3)               # 接触面仍有权重

    def test_surface_distance_wraps_around(self):
        verts, edges = self._two_strips()
        ball = _matrix(loc=(0.2, 0.0, 0.0), scale=(0.6, 0.6, 0.6))  # 球心正对 front[2]
        edges2 = np.vstack([edges, [0, 5]])  # 长边连通两片
        local = gb_core._to_ball_local(verts, ball)
        seeds = np.zeros(10, dtype=bool)
        seeds[2] = True
        d = gb_core.surface_distances(local, edges2, seeds)
        # 接触点 front[2]（|local|=0）→ front[0]：1/3；→ back[5]：1/3+5/6=7/6 > 1
        self.assertAlmostEqual(float(d[0]), 1.0 / 3.0, places=3)
        self.assertAlmostEqual(float(d[5]), 7.0 / 6.0, places=3)
        g_vol = gb_core.gaussian_field(verts, ball, 1.0, 4.6)
        g_geo = gb_core.geodesic_field(verts, ball, 1.0, 4.6, edges2)
        self.assertGreater(float(g_vol[5]), 0.0)   # 体积球穿透到 back[5]
        self.assertEqual(float(g_geo[5]), 0.0)     # 测地：绕行距离 > 1 → 0

    def test_prebuilt_adjacency_matches_internal_build(self):
        verts, edges = self._two_strips()
        ball = _matrix(loc=(0.2, 0.0, 0.0), scale=(0.6, 0.6, 0.6))
        local = gb_core._to_ball_local(verts, ball)
        seeds = np.zeros(10, dtype=bool)
        seeds[2] = True
        d_internal = gb_core.surface_distances(local, edges, seeds)
        adj = gb_core.build_surface_adjacency(local, edges)
        d_reused = gb_core.surface_distances(local, edges, seeds, adjacency=adj)
        np.testing.assert_allclose(d_reused, d_internal, atol=1e-12)
        # 多次调用复用同一邻接表结果一致（邻接表构建只发生一次）
        d_reused2 = gb_core.surface_distances(local, edges, seeds, adjacency=adj)
        np.testing.assert_allclose(d_reused2, d_internal, atol=1e-12)

    def test_uniform_scale_fast_path_matches_per_ball(self):
        """均匀缩放球（旋转/平移/等比缩放）快速路径复用世界坐标邻接表，
        结果必须与逐球局部构建完全一致。"""
        verts, edges = self._two_strips()
        # 球带旋转 + 平移 + 等比缩放
        theta = 0.7
        rot = np.array([
            [np.cos(theta), -np.sin(theta), 0.0],
            [np.sin(theta), np.cos(theta), 0.0],
            [0.0, 0.0, 1.0],
        ])
        m = np.eye(4)
        m[:3, :3] = rot * 0.6
        m[:3, 3] = (0.2, 0.1, 0.0)
        center = m[:3, 3]
        scale = 0.6

        local = gb_core._to_ball_local(verts, m)
        seeds = np.zeros(len(verts), dtype=bool)
        seeds[int(np.argmin(np.einsum("ij,ij->i", local, local)))] = True
        d_ref = gb_core.surface_distances(local, edges, seeds)

        adj_world = gb_core.build_surface_adjacency(verts, edges)
        d_fast = gb_core.surface_distances_uniform_scale(adj_world, verts, center, scale)
        np.testing.assert_allclose(d_fast, d_ref, atol=1e-12)

    def test_uniform_scale_fast_path_respects_cutoff(self):
        """快速路径按 scale 截断：球外表面距离归一后 ≥1 → 权重 0。"""
        verts, edges = self._two_strips()
        m = np.eye(4) * 0.25
        m[3, 3] = 1.0
        adj_world = gb_core.build_surface_adjacency(verts, edges)
        d = gb_core.surface_distances_uniform_scale(adj_world, verts, np.zeros(3), 0.25)
        # 种子最近顶点距离 < 0.25 有权重，远端沿表面距离 / 0.25 > 1 归 0
        self.assertLess(float(np.nanmin(d)), 1.0)
        self.assertGreaterEqual(float(np.nanmax(d)), 1.0)

    def test_surface_distances_allowed_mask_blocks_propagation(self):
        """allowed_mask：传播不能越过未允许顶点，种子也必须在 allowed 内。"""
        verts = np.array(
            [[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.2, 0.0, 0.0], [0.3, 0.0, 0.0]]
        )
        edges = np.array([[0, 1], [1, 2], [2, 3]])
        seeds = np.zeros(4, dtype=bool)
        seeds[0] = True
        allowed = np.array([True, True, False, True])  # 顶点 2 被排除 → 0-1 与 3 不连通
        d = gb_core.surface_distances(verts, edges, seeds, allowed_mask=allowed)
        self.assertTrue(np.isfinite(d[0]))
        self.assertTrue(np.isfinite(d[1]))
        self.assertEqual(d[2], np.inf)
        self.assertEqual(d[3], np.inf)

    def test_surface_distances_allowed_mask_seed_outside_returns_inf(self):
        """种子在 allowed 外：无可达顶点 → 全部 inf。"""
        verts = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0]])
        edges = np.array([[0, 1]])
        seeds = np.array([True, False])
        allowed = np.array([False, True])
        d = gb_core.surface_distances(verts, edges, seeds, allowed_mask=allowed)
        np.testing.assert_array_equal(d, np.full(2, np.inf))

    def test_uniform_scale_allowed_mask_blocks_propagation(self):
        """快速路径同样遵守 allowed_mask（包含物体列表过滤）。"""
        verts = np.array(
            [[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.2, 0.0, 0.0]]
        )
        edges = np.array([[0, 1], [1, 2]])
        adj = gb_core.build_surface_adjacency(verts, edges)
        allowed = np.array([True, True, False])
        d = gb_core.surface_distances_uniform_scale(
            adj, verts, np.zeros(3), 0.5, allowed_mask=allowed
        )
        self.assertTrue(np.isfinite(d[0]))
        self.assertTrue(np.isfinite(d[1]))
        self.assertEqual(d[2], np.inf)

    def test_degenerate_inputs(self):
        verts, edges = self._two_strips()
        ball = self._ball()
        # 空边：仅剩接触点一个种子，不炸且有权重
        f = gb_core.geodesic_field(verts, ball, 1.0, 4.6, np.zeros((0, 2), dtype=np.int64))
        self.assertGreater(float(f.max()), 0.0)
        # 球远离表面：最近顶点距离 > 1 → 全 0
        far = _matrix(loc=(0.0, 0.0, 5.0), scale=(0.6, 0.6, 0.6))
        np.testing.assert_array_equal(gb_core.geodesic_field(verts, far, 1.0, 4.6, edges), np.zeros(10))
        # 零顶点
        self.assertEqual(gb_core.geodesic_field(np.zeros((0, 3)), ball, 1.0, 4.6, edges).shape, (0,))


class MultiBallMergeDeterminismTests(unittest.TestCase):
    """多球组合规则固化（NW3）：max 合并的结果与球顺序无关、确定性。"""

    def test_merge_max_permutation_invariant(self):
        fields = [
            np.array([0.1, 0.9, 0.0, 0.3]),
            np.array([0.5, 0.2, 0.7, 0.3]),
            np.array([0.4, 0.8, 0.2, 0.0]),
        ]
        base = gb_core.merge_fields_max(fields)
        shuffled = gb_core.merge_fields_max(list(reversed(fields)))
        np.testing.assert_array_equal(base, shuffled)
        np.testing.assert_array_equal(base, [0.5, 0.9, 0.7, 0.3])

    def test_merge_max_disjoint_and_overlap_deterministic(self):
        # 不相交球（互补支撑）+ 重叠区取最大：结果与逐球顺序无关
        left = np.array([0.8, 0.5, 0.0, 0.0])
        right = np.array([0.2, 0.6, 0.9, 0.1])
        a = gb_core.merge_fields_max([left, right])
        b = gb_core.merge_fields_max([right, left])
        np.testing.assert_array_equal(a, b)
        np.testing.assert_array_equal(a, [0.8, 0.6, 0.9, 0.1])

    def test_merge_max_single_enabled_ball_identity(self):
        field = np.array([0.7, 0.0, 0.4])
        merged = gb_core.merge_fields_max([field])
        np.testing.assert_array_equal(merged, field)

    def test_merge_max_matches_maximum_of_all_balls(self):
        # 多球重叠：任一顶点的组合权重 = 该顶点在所有球上的最大值
        rng = np.random.default_rng(42)
        n_verts, n_balls = 64, 5
        fields = [rng.random(n_verts) for _ in range(n_balls)]
        merged = gb_core.merge_fields_max(fields)
        expected = np.maximum.reduce(fields)
        np.testing.assert_allclose(merged, expected, rtol=0, atol=0)


if __name__ == "__main__":
    unittest.main()
