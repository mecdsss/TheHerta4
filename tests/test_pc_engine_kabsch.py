# -*- coding: utf-8 -*-
"""pc_engine Kabsch 求解与轴向锁定掩码测试（纯 numpy，不依赖 bpy）。"""
import importlib.util
import math
import sys
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_engine.py"
spec = importlib.util.spec_from_file_location("_pc_engine_kabsch_test", module_path)
pc_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_engine
spec.loader.exec_module(pc_engine)


def _rot_z(deg: float) -> np.ndarray:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


class KabschTests(unittest.TestCase):
    def test_centered_rotation_does_not_turn_translation_into_rotation(self):
        src = np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0], [0.0, 0.0, 0.5]])
        dst = src * 1.2 + np.array([4.0, -3.0, 2.0])
        rotation = pc_engine.centered_kabsch_rotation(src, dst)
        np.testing.assert_allclose(rotation, np.identity(3), atol=1e-10)

    def test_recovers_known_rotation_around_origin(self):
        rng = np.random.default_rng(42)
        src = rng.normal(size=(64, 3)) * 0.5
        r_true = _rot_z(30.0)
        dst = src @ r_true.T

        r = pc_engine.kabsch_rotation(src, dst, pivot=np.zeros(3))

        np.testing.assert_allclose(r, r_true, atol=1e-8)

    def test_recovers_rotation_around_pivot(self):
        rng = np.random.default_rng(7)
        pivot = np.array([1.0, 2.0, 3.0])
        src = pivot + rng.normal(size=(48, 3)) * 0.3
        r_true = _rot_z(-45.0)
        dst = (src - pivot) @ r_true.T + pivot

        r = pc_engine.kabsch_rotation(src, dst, pivot=pivot)

        np.testing.assert_allclose(r, r_true, atol=1e-8)
        # 绕 pivot 旋转后 pivot 不动
        np.testing.assert_allclose((pivot - pivot) @ r.T + pivot, pivot, atol=1e-8)

    def test_degenerate_input_returns_identity(self):
        r = pc_engine.kabsch_rotation(np.zeros((0, 3)), np.zeros((0, 3)))
        np.testing.assert_allclose(r, np.identity(3), atol=1e-12)

        r = pc_engine.kabsch_rotation(np.array([[0.0, 0.0, 0.0]]),
                                      np.array([[1.0, 0.0, 0.0]]),
                                      weights=np.array([0.0]))
        np.testing.assert_allclose(r, np.identity(3), atol=1e-12)


class MaskRotationTests(unittest.TestCase):
    def test_locked_axis_component_removed(self):
        delta = _rot_z(10.0)
        chan = np.identity(3)
        masked = pc_engine.mask_rotation_delta(
            delta, chan, lock=(False, False, True), mode='XYZ')
        np.testing.assert_allclose(masked, np.identity(3), atol=1e-10)

    def test_unlocked_axis_component_preserved(self):
        delta = _rot_z(10.0)
        chan = np.identity(3)
        masked = pc_engine.mask_rotation_delta(
            delta, chan, lock=(True, True, False), mode='XYZ')
        np.testing.assert_allclose(masked, delta, atol=1e-10)

    def test_quaternion_mode_respects_locked_rotation_vector_axis(self):
        delta = _rot_z(3.0)
        chan = np.identity(3)
        masked = pc_engine.mask_rotation_delta(
            delta, chan, lock=(False, False, True), mode='QUATERNION')
        np.testing.assert_allclose(masked, np.identity(3), atol=1e-3)

    def test_quaternion_mode_projects_combined_axis_without_euler_split(self):
        rotvec = np.array([0.4, 0.3, -0.2])
        delta = pc_engine.rotvec_to_mat3(rotvec)
        masked = pc_engine.mask_rotation_delta(
            delta, np.identity(3), lock=(False, True, False),
            mode='QUATERNION')
        np.testing.assert_allclose(
            pc_engine.mat3_to_rotvec(masked), [0.4, 0.0, -0.2], atol=1e-9)

    def test_quaternion_rotation_vector_round_trip(self):
        rotvec = np.array([0.7, -0.25, 0.45])
        np.testing.assert_allclose(
            pc_engine.mat3_to_rotvec(pc_engine.rotvec_to_mat3(rotvec)),
            rotvec, atol=1e-10)


class MaskVectorTests(unittest.TestCase):
    def test_locked_axes_zeroed(self):
        v = np.array([1.0, 2.0, 3.0])
        out = pc_engine.mask_vector_delta(v, np.identity(3), (True, False, True))
        np.testing.assert_allclose(out, [0.0, 2.0, 0.0], atol=1e-12)

    def test_scale_lock_sets_one(self):
        factors = np.array([1.1, 0.9, 1.2])
        out = pc_engine.mask_scale_factors(factors, (False, True, False))
        np.testing.assert_allclose(out, [1.1, 1.0, 1.2], atol=1e-12)


class EulerRoundTripTests(unittest.TestCase):
    def test_euler_xyz_round_trip(self):
        e = np.array([0.2, -0.3, 0.4])
        m = pc_engine.euler_xyz_to_mat3(e)
        e2 = pc_engine.mat3_to_euler_xyz(m)
        np.testing.assert_allclose(e2, e, atol=1e-10)


if __name__ == "__main__":
    unittest.main()
