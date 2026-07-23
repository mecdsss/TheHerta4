# -*- coding: utf-8 -*-
"""pc_engine lbs_transform 线性混合蒙皮数学测试（纯 numpy）。"""
import importlib.util
import math
import sys
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_engine.py"
spec = importlib.util.spec_from_file_location("_pc_engine_lbs_test", module_path)
pc_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_engine
spec.loader.exec_module(pc_engine)


def _delta_rot_z(deg: float) -> np.ndarray:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    m = np.identity(4)
    m[:3, :3] = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return m


class LbsTransformTests(unittest.TestCase):
    def test_identity_delta_keeps_points(self):
        rest = np.array([[1.0, 2.0, 3.0], [0.0, 0.0, 0.0]])
        deltas = np.stack([np.identity(4), np.identity(4)])
        weights = np.array([[1.0, 0.0], [0.0, 1.0]])
        bone_idx = np.array([[0, 1], [0, 1]])
        out = pc_engine.lbs_transform(rest, deltas, weights, bone_idx)
        np.testing.assert_allclose(out, rest, atol=1e-12)

    def test_full_weight_rotation(self):
        rest = np.array([[1.0, 0.0, 0.0]])
        deltas = np.stack([np.identity(4), _delta_rot_z(90.0)])
        weights = np.array([[0.0, 1.0]])
        bone_idx = np.array([[0, 1]])
        out = pc_engine.lbs_transform(rest, deltas, weights, bone_idx)
        np.testing.assert_allclose(out[0], [0.0, 1.0, 0.0], atol=1e-12)

    def test_blended_weights_average(self):
        rest = np.array([[1.0, 0.0, 0.0]])
        deltas = np.stack([np.identity(4), _delta_rot_z(90.0)])
        weights = np.array([[0.5, 0.5]])
        bone_idx = np.array([[0, 1]])
        out = pc_engine.lbs_transform(rest, deltas, weights, bone_idx)
        # 0.5*(1,0,0) + 0.5*(0,1,0) = (0.5, 0.5, 0)
        np.testing.assert_allclose(out[0], [0.5, 0.5, 0.0], atol=1e-12)

    def test_empty_slot_contributes_nothing(self):
        rest = np.array([[1.0, 0.0, 0.0]])
        deltas = np.stack([_delta_rot_z(33.0)])
        weights = np.array([[1.0, 0.0, 0.0]])
        bone_idx = np.array([[0, -1, -1]])
        out = pc_engine.lbs_transform(rest, deltas, weights, bone_idx)
        expected = rest @ _delta_rot_z(33.0)[:3, :3].T
        np.testing.assert_allclose(out[0], expected[0], atol=1e-12)

    def test_translation_applied(self):
        rest = np.array([[1.0, 1.0, 1.0]])
        m = np.identity(4)
        m[:3, 3] = [10.0, 0.0, 0.0]
        deltas = np.stack([m])
        weights = np.array([[1.0]])
        bone_idx = np.array([[0]])
        out = pc_engine.lbs_transform(rest, deltas, weights, bone_idx)
        np.testing.assert_allclose(out[0], [11.0, 1.0, 1.0], atol=1e-12)

    def test_empty_input(self):
        out = pc_engine.lbs_transform(np.zeros((0, 3)), np.zeros((1, 4, 4)),
                                      np.zeros((0, 1)), np.zeros((0, 1), dtype=int))
        self.assertEqual(out.shape, (0, 3))


class LbsIdentityRemainderTests(unittest.TestCase):
    """Blender Armature 语义：顶点权重和 <1 时剩余部分保持不动（恒等贡献）。"""

    def test_partial_weight_moves_half(self):
        rest = np.array([[0.0, 0.0, 0.0]])
        m = np.identity(4)
        m[:3, 3] = [0.0, 10.0, 0.0]
        deltas = np.stack([m])
        out = pc_engine.lbs_transform_with_remainder(
            rest, deltas, np.array([[0.5]]), np.array([[0]]))
        np.testing.assert_allclose(out[0], [0.0, 5.0, 0.0], atol=1e-12)

    def test_full_weight_no_remainder(self):
        rest = np.array([[1.0, 2.0, 3.0]])
        m = np.identity(4)
        m[:3, 3] = [0.0, 10.0, 0.0]
        deltas = np.stack([m])
        out = pc_engine.lbs_transform_with_remainder(
            rest, deltas, np.array([[1.0]]), np.array([[0]]))
        np.testing.assert_allclose(out[0], [1.0, 12.0, 3.0], atol=1e-12)

    def test_zero_weight_stays(self):
        rest = np.array([[1.0, 0.0, 0.0]])
        m = np.identity(4)
        m[:3, 3] = [5.0, 0.0, 0.0]
        deltas = np.stack([m])
        out = pc_engine.lbs_transform_with_remainder(
            rest, deltas, np.array([[0.0]]), np.array([[0]]))
        np.testing.assert_allclose(out[0], [1.0, 0.0, 0.0], atol=1e-12)

    def test_over_one_sum_clamps_remainder_to_zero(self):
        rest = np.array([[0.0, 0.0, 0.0]])
        m = np.identity(4)
        m[:3, 3] = [0.0, 10.0, 0.0]
        deltas = np.stack([m, m])
        out = pc_engine.lbs_transform_with_remainder(
            rest, deltas, np.array([[0.7, 0.7]]), np.array([[0, 1]]))
        np.testing.assert_allclose(out[0], [0.0, 14.0, 0.0], atol=1e-12)


if __name__ == "__main__":
    unittest.main()
