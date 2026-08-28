"""高斯权重球：真实热力图预览纯逻辑测试（gb_preview，无 bpy 依赖）。

覆盖 t3 需求 NU1-NU6 的可测纯逻辑：
- 方向标签（面板方向可视化）；
- 脏检测签名（矩阵/拓扑/姿态/形态键，持续拖动不反复重算的依据）；
- 变形位置评估守卫（顶点数不匹配回退基础网格，索引一一对应保证）；
- 均匀缩放判定（决定测地快速路径是否可用）；
- 测地快速路径与逐球 geodesic_field 等价；
- 单球贡献 / 组合权重选择；
- 可写性判定与缺边/能力反馈。

加载方式与 test_gb_core 一致：importlib 按文件路径加载，包命名空间用 sys.modules
stub（不执行任何真实 __init__.py）。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


def _recover_real_numpy():
    """discover 模式下其他测试可能把 sys.modules['numpy'] 换成假实现；
    从已加载模块找回真实 numpy 并同步 sys.modules 与本模块 np 引用。
    """
    global np
    current = sys.modules.get("numpy")
    if not hasattr(current, "float64") or not hasattr(np, "eye"):
        real_numpy = None
        for module in list(sys.modules.values()):
            for attr in ("np", "numpy"):
                try:
                    candidate = getattr(module, attr, None)
                except Exception:
                    continue
                if getattr(candidate, "__name__", None) == "numpy" and hasattr(candidate, "float64"):
                    real_numpy = candidate
                    break
            if real_numpy is not None:
                break
        if real_numpy is not None:
            sys.modules["numpy"] = real_numpy
            np = real_numpy


PKG = "_gb_preview_pkg"
for name in (PKG, f"{PKG}.toolkit"):
    pkg = types.ModuleType(name)
    pkg.__path__ = []
    sys.modules[name] = pkg

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module(pkg_name, rel_path, file_name):
    _recover_real_numpy()
    path = REPO_ROOT / rel_path / file_name
    full = f"{PKG}.toolkit.{pkg_name}"
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    sys.modules[f"{PKG}.toolkit"].__dict__[pkg_name] = mod
    return mod


gb_core = _load_module("gb_core", "toolkit", "gb_core.py")
gb_resolve = _load_module("gb_resolve", "toolkit", "gb_resolve.py")
gb_preview = _load_module("gb_preview", "toolkit", "gb_preview.py")


class DirectionLabelTests(unittest.TestCase):
    """NU1：方向标签可视化。"""

    def test_forward_label(self):
        self.assertEqual(gb_preview.direction_label("source", ""), "源→目标")
        self.assertEqual(
            gb_preview.direction_label("", gb_resolve.DIRECTION_FORWARD),
            "源→目标")

    def test_self_label(self):
        self.assertEqual(gb_preview.direction_label("target", ""), "目标自身")
        self.assertEqual(
            gb_preview.direction_label("", gb_resolve.DIRECTION_SELF),
            "目标自身")

    def test_reverse_label(self):
        self.assertEqual(
            gb_preview.direction_label("target", gb_resolve.DIRECTION_REVERSE),
            "目标→源合集")

    def test_unknown_label(self):
        self.assertEqual(gb_preview.direction_label("", ""), "方向未知")


class SignatureTests(unittest.TestCase):
    """NU4：脏检测签名稳定且能捕捉变化。"""

    def test_hash_state_stable(self):
        a = gb_preview.hash_state([np.array([1.0, 2.0]), np.array([[0.0]])])
        b = gb_preview.hash_state([np.array([1.0, 2.0]), np.array([[0.0]])])
        self.assertEqual(a, b)

    def test_hash_state_sensitive_to_value_and_token(self):
        base = gb_preview.hash_state([np.array([1.0, 2.0])])
        self.assertNotEqual(base, gb_preview.hash_state([np.array([1.0, 2.1])]))
        self.assertNotEqual(
            base, gb_preview.hash_state([np.array([1.0, 2.0])], token="mesh"))

    def test_topo_signature(self):
        edges1 = np.array([[0, 1], [1, 2]])
        edges2 = np.array([[0, 1], [1, 2], [2, 0]])
        self.assertEqual(
            gb_preview.topo_signature(3, edges1),
            gb_preview.topo_signature(3, edges1))
        self.assertNotEqual(
            gb_preview.topo_signature(3, edges1),
            gb_preview.topo_signature(3, edges2))
        self.assertNotEqual(
            gb_preview.topo_signature(3, edges1),
            gb_preview.topo_signature(4, edges1))

    def test_pose_signature(self):
        eye = np.eye(4)
        moved = np.eye(4)
        moved[1, 3] = 0.5
        self.assertEqual(
            gb_preview.pose_signature([eye]), gb_preview.pose_signature([eye]))
        self.assertNotEqual(
            gb_preview.pose_signature([eye]),
            gb_preview.pose_signature([moved]))
        self.assertEqual(gb_preview.pose_signature([]), "no-bones")

    def test_shapekey_signature(self):
        self.assertEqual(gb_preview.shapekey_signature([1.0, 0.0]),
                         gb_preview.shapekey_signature([1.0, 0.0]))
        self.assertNotEqual(gb_preview.shapekey_signature([1.0]),
                            gb_preview.shapekey_signature([0.5]))
        self.assertEqual(gb_preview.shapekey_signature([]), "no-shapekeys")


class EvaluationGuardTests(unittest.TestCase):
    """NU2：变形位置评估守卫。"""

    def test_equal_count_uses_evaluated(self):
        use_eval, message = gb_preview.evaluation_decision(100, 100)
        self.assertTrue(use_eval)
        self.assertEqual(message, "")

    def test_mismatch_falls_back(self):
        use_eval, message = gb_preview.evaluation_decision(100, 120)
        self.assertFalse(use_eval)
        self.assertIn("120", message)
        self.assertIn("100", message)

    def test_failed_evaluation_falls_back(self):
        use_eval, message = gb_preview.evaluation_decision(100, None)
        self.assertFalse(use_eval)
        self.assertIn("回退", message)


class UniformScaleTests(unittest.TestCase):
    """NU4：均匀缩放判定（测地快速路径前提）。"""

    def test_uniform_true(self):
        self.assertTrue(gb_preview.is_uniform_scale((1.0, 1.0, 1.0)))
        self.assertTrue(gb_preview.is_uniform_scale((0.3, 0.3, 0.3)))

    def test_non_uniform_false(self):
        self.assertFalse(gb_preview.is_uniform_scale((1.0, 1.0, 2.0)))
        self.assertFalse(gb_preview.is_uniform_scale((0.1, 1.0, 0.1)))

    def test_small_noise_still_uniform(self):
        self.assertTrue(gb_preview.is_uniform_scale((1.0, 1.0 + 1e-6, 1.0)))

    def test_non_positive_uniform_rejected(self):
        """回归：负/零缩放不是合法快速路径前提（负 cutoff 语义错误）。"""
        self.assertFalse(gb_preview.is_uniform_scale((-1.0, -1.0, -1.0)))
        self.assertFalse(gb_preview.is_uniform_scale((-0.5, -0.5, -0.5)))
        self.assertFalse(gb_preview.is_uniform_scale((0.0, 0.0, 0.0)))
        self.assertTrue(gb_preview.is_uniform_scale((0.5, 0.5, 0.5)))


class GeodesicFastPathTests(unittest.TestCase):
    """NU2/NU4：均匀缩放快速路径与逐球 geodesic_field 结果一致。"""

    def _small_mesh(self):
        # 4 顶点三角形网格（两个共享边三角）
        verts = np.array([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
        ])
        edges = np.array([[0, 1], [1, 2], [2, 3], [3, 0], [0, 2]])
        return verts, edges

    def test_fast_path_equals_geodesic_field(self):
        verts, edges = self._small_mesh()
        center = np.array([0.2, 0.2, 0.0])
        scale = 0.9
        strength, falloff_k = 1.0, 4.6

        # 均匀缩放球的等效世界矩阵
        m = np.eye(4)
        m[:3, 3] = center
        m[0, 0] = m[1, 1] = m[2, 2] = scale
        slow = gb_core.geodesic_field(verts, m, strength, falloff_k, edges)

        adjacency = gb_core.build_surface_adjacency(verts, edges)
        fast = gb_preview.geodesic_field_fast(
            verts, adjacency, center, scale, strength, falloff_k)

        np.testing.assert_allclose(fast, slow, atol=1e-9)

    def test_fast_path_zero_strength(self):
        verts, edges = self._small_mesh()
        adjacency = gb_core.build_surface_adjacency(verts, edges)
        field = gb_preview.geodesic_field_fast(
            verts, adjacency, np.zeros(3), 10.0, 0.0, 4.6)
        self.assertEqual(field.shape[0], verts.shape[0])
        self.assertTrue(np.all(field == 0.0))


class SingleOrMergedTests(unittest.TestCase):
    """NU3：单球贡献 vs 组合权重。"""

    def _fields(self):
        a = np.array([0.0, 0.5, 0.8, 0.0])
        b = np.array([0.9, 0.2, 0.0, 0.0])
        return [("ballA", a), ("ballB", b)]

    def test_single_ball_selection(self):
        fields = self._fields()
        field, name, note = gb_preview.pick_single_or_merged(
            fields, ["ballA", "ballB", "ballC"], "ballB")
        np.testing.assert_allclose(field, fields[1][1])
        self.assertEqual(name, "ballB")
        self.assertEqual(note, "")

    def test_merged_uses_max(self):
        fields = self._fields()
        field, name, note = gb_preview.pick_single_or_merged(
            fields, ["ballA", "ballB"], None)
        np.testing.assert_allclose(field, np.maximum(fields[0][1], fields[1][1]))
        self.assertIsNone(name)
        self.assertIn("max 合并", note)

    def test_unknown_ball_falls_back_to_merged(self):
        fields = self._fields()
        field, name, note = gb_preview.pick_single_or_merged(
            fields, ["ballA", "ballB"], "ghost")
        np.testing.assert_allclose(
            field, np.maximum(fields[0][1], fields[1][1]))
        self.assertIsNone(name)

    def test_disabled_ball_note(self):
        fields = self._fields()
        field, name, note = gb_preview.pick_single_or_merged(
            fields, ["ballA", "ballB", "ballC"], "ballC")
        np.testing.assert_allclose(
            field, np.maximum(fields[0][1], fields[1][1]))
        self.assertIsNone(name)
        self.assertIn("停用", note)

    def test_empty_fields(self):
        field, name, note = gb_preview.pick_single_or_merged([], [], None)
        self.assertEqual(field.shape[0], 0)
        self.assertIsNone(name)


class WritabilityAndFeedbackTests(unittest.TestCase):
    """NU5：可写性判定与清晰反馈。"""

    def test_missing_object(self):
        self.assertIn("不存在", gb_preview.not_writable_reason(None))

    def test_library_linked_object(self):
        obj = types.SimpleNamespace(name="Linked", library=object(),
                                    type="MESH", data=types.SimpleNamespace())
        self.assertIn("链接库", gb_preview.not_writable_reason(obj))

    def test_non_mesh(self):
        obj = types.SimpleNamespace(name="Empty", library=None,
                                    type="EMPTY", data=None)
        self.assertIn("不是网格", gb_preview.not_writable_reason(obj))

    def test_writable(self):
        obj = types.SimpleNamespace(name="Mesh", library=None, type="MESH",
                                    data=types.SimpleNamespace(library=None))
        self.assertEqual(gb_preview.not_writable_reason(obj), "")

    def test_matched_edge_feedback(self):
        self.assertIn("无任何匹配边",
                      gb_preview.matched_edge_feedback(True, 0))
        self.assertIn("无匹配边",
                      gb_preview.matched_edge_feedback(False, 3))
        self.assertEqual(gb_preview.matched_edge_feedback(True, 3), "")

    def test_eval_capability_feedback(self):
        with_sk = types.SimpleNamespace(
            data=types.SimpleNamespace(
                shape_keys=types.SimpleNamespace(key_blocks=[object()])),
            modifiers=[])
        with_arm = types.SimpleNamespace(
            data=types.SimpleNamespace(shape_keys=None),
            modifiers=[types.SimpleNamespace(type="ARMATURE", object=object())])
        plain = types.SimpleNamespace(
            data=types.SimpleNamespace(shape_keys=None),
            modifiers=[])
        self.assertEqual(gb_preview.eval_capability_feedback(with_sk), "")
        self.assertEqual(gb_preview.eval_capability_feedback(with_arm), "")
        self.assertIn("无骨骼/形态键",
                      gb_preview.eval_capability_feedback(plain))


if __name__ == "__main__":
    unittest.main()