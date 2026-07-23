# -*- coding: utf-8 -*-
"""pc_virtualrig 虚拟骨架测试：姿态递推、脏行重算、提供者、验证流程、端到端收敛。"""
import importlib.util
import math
import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pc_engine = _load("_pc_engine_vr_test", "toolkit/pc_engine.py")
pc_vrig = _load("_pc_vrig_test", "toolkit/pc_virtualrig.py")
pc_backend = _load("_pc_backend_vr_test", "toolkit/pc_backend.py")


def _T(x, y, z):
    m = np.identity(4)
    m[:3, 3] = [x, y, z]
    return m


def _Rz(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    m = np.identity(4)
    m[:3, :3] = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return m


def _chain_rig():
    """三骨链 root->mid->leaf，绑定姿态沿 X 排列，rest 单位旋转。"""
    bone_names = ['root', 'mid', 'leaf']
    parents = {'root': None, 'mid': 'root', 'leaf': 'mid'}
    local_mats = {'root': _T(1, 0, 0), 'mid': _T(2, 0, 0), 'leaf': _T(3, 0, 0)}
    rest_arm = np.zeros((1, 3))
    topk_w = np.zeros((1, 4))
    topk_idx = np.full((1, 4), -1)
    rig = pc_vrig.build_virtual_rig(
        bone_names=bone_names, parents=parents, local_mats=local_mats,
        rest_arm=rest_arm, topk_w=topk_w, topk_idx=topk_idx,
        bone_to_rows={0: np.array([], dtype=int), 1: np.array([], dtype=int),
                      2: np.array([], dtype=int)},
        arm_mw=np.identity(4))
    return rig


class PoseRecursionTests(unittest.TestCase):
    def test_rest_pose_equals_local(self):
        rig = _chain_rig()
        rig.refresh_pose()
        np.testing.assert_allclose(rig.pose_mats[0], _T(1, 0, 0), atol=1e-12)
        np.testing.assert_allclose(rig.pose_mats[1], _T(2, 0, 0), atol=1e-12)
        np.testing.assert_allclose(rig.pose_mats[2], _T(3, 0, 0), atol=1e-12)

    def test_root_basis_rotation_propagates_to_descendants(self):
        rig = _chain_rig()
        rig.set_basis('root', _Rz(90.0))
        rig.refresh_pose()
        # pose_root = T(1,0,0) @ Rz90
        np.testing.assert_allclose(rig.pose_mats[0][:3, 3], [1, 0, 0], atol=1e-12)
        np.testing.assert_allclose(rig.pose_mats[0][:3, :3], _Rz(90.0)[:3, :3], atol=1e-12)
        # pose_mid 平移 = (1,1,0)，pose_leaf = (1,2,0)，均带 Rz90
        np.testing.assert_allclose(rig.pose_mats[1][:3, 3], [1, 1, 0], atol=1e-12)
        np.testing.assert_allclose(rig.pose_mats[2][:3, 3], [1, 2, 0], atol=1e-12)
        np.testing.assert_allclose(rig.pose_mats[2][:3, :3], _Rz(90.0)[:3, :3], atol=1e-12)

    def test_basis_map_round_trip(self):
        rig = _chain_rig()
        rig.set_basis('mid', _Rz(30.0))
        rig.refresh_pose()
        m = rig.basis_map()
        np.testing.assert_allclose(m['mid'], _Rz(30.0), atol=1e-12)
        rig2 = _chain_rig()
        rig2.set_basis_map(m)
        rig2.refresh_pose()
        np.testing.assert_allclose(rig2.pose_mats[1], rig.pose_mats[1], atol=1e-12)


class ProviderTests(unittest.TestCase):
    def test_pivot_at_rest_and_rotated(self):
        rig = _chain_rig()
        p = rig.provider()
        np.testing.assert_allclose(p('root', 'pivot'), [1, 0, 0], atol=1e-12)
        # 绕自身原点旋转不改变 pivot（pivot = pose 平移列）
        rig.set_basis('root', _Rz(90.0))
        np.testing.assert_allclose(p('root', 'pivot'), [1, 0, 0], atol=1e-12)
        np.testing.assert_allclose(p('leaf', 'pivot'), [1, 2, 0], atol=1e-12)

    def test_rot_cols_are_world_axes_of_bone(self):
        rig = _chain_rig()
        p = rig.provider()
        rig.set_basis('root', _Rz(90.0))
        cols = p('root', 'rot_cols')
        np.testing.assert_allclose(cols, _Rz(90.0)[:3, :3], atol=1e-12)

    def test_chan_to_local_uses_safe_inverse_when_scale_is_singular(self):
        rig = _chain_rig()
        singular = np.identity(4)
        singular[0, 0] = 0.0
        rig.set_basis('root', singular)
        provider = rig.provider()
        chan = provider('root', 'chan_to_local')
        self.assertEqual(chan.shape, (3, 3))
        self.assertTrue(np.isfinite(chan).all())


class LbsReadTests(unittest.TestCase):
    def test_dirty_rows_recomputed_only(self):
        rest_arm = np.array([[0.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        # 样本0绑骨0，样本1绑骨1
        topk_w = np.array([[1.0, 0, 0, 0], [1.0, 0, 0, 0]])
        topk_idx = np.array([[0, -1, -1, -1], [1, -1, -1, -1]])
        rig = pc_vrig.build_virtual_rig(
            bone_names=['b0', 'b1'], parents={'b0': None, 'b1': None},
            local_mats={'b0': np.identity(4), 'b1': np.identity(4)},
            rest_arm=rest_arm, topk_w=topk_w, topk_idx=topk_idx,
            bone_to_rows={0: np.array([0]), 1: np.array([1])},
            arm_mw=np.identity(4))
        rig.read_samples(pc_engine.lbs_transform)  # 初始化缓冲
        rig.set_basis('b0', _T(0, 5, 0))
        out = rig.read_samples(pc_engine.lbs_transform)
        np.testing.assert_allclose(out[0], [0, 5, 0], atol=1e-12)
        np.testing.assert_allclose(out[1], [0, 1, 0], atol=1e-12)

    def test_probe_pair_points_restores_baseline_state(self):
        rest_arm = np.array([[0.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        topk_w = np.array([[1.0, 0, 0, 0], [1.0, 0, 0, 0]])
        topk_idx = np.array([[0, -1, -1, -1], [1, -1, -1, -1]])
        rig = pc_vrig.build_virtual_rig(
            bone_names=['b0', 'b1'], parents={'b0': None, 'b1': None},
            local_mats={'b0': np.identity(4), 'b1': np.identity(4)},
            rest_arm=rest_arm, topk_w=topk_w, topk_idx=topk_idx,
            bone_to_rows={0: np.array([0]), 1: np.array([1])},
            arm_mw=np.identity(4))
        baseline = rig.read_samples(pc_engine.lbs_transform).copy()
        forward, backward = rig.probe_pair_points(
            [('b0', np.identity(4), _T(2.0, 0.0, 0.0))],
            [('b0', np.identity(4), _T(-3.0, 0.0, 0.0))],
            pc_engine.lbs_transform,
        )
        np.testing.assert_allclose(forward[0], [2.0, 0.0, 0.0], atol=1e-12)
        np.testing.assert_allclose(backward[0], [-3.0, 0.0, 0.0], atol=1e-12)
        np.testing.assert_allclose(forward[1], baseline[1], atol=1e-12)
        np.testing.assert_allclose(backward[1], baseline[1], atol=1e-12)
        np.testing.assert_allclose(rig.current, baseline, atol=1e-12)

    def test_probe_pair_subpoints_restores_baseline_state(self):
        rest_arm = np.array([
            [0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 0.0, 0.0],
        ])
        topk_w = np.array([
            [1.0, 0, 0, 0],
            [1.0, 0, 0, 0],
            [1.0, 0, 0, 0],
        ])
        topk_idx = np.array([
            [0, -1, -1, -1],
            [1, -1, -1, -1],
            [0, -1, -1, -1],
        ])
        rig = pc_vrig.build_virtual_rig(
            bone_names=['b0', 'b1'], parents={'b0': None, 'b1': None},
            local_mats={'b0': np.identity(4), 'b1': np.identity(4)},
            rest_arm=rest_arm, topk_w=topk_w, topk_idx=topk_idx,
            bone_to_rows={0: np.array([0, 2]), 1: np.array([1])},
            arm_mw=np.identity(4))
        baseline = rig.read_samples(pc_engine.lbs_transform).copy()
        forward, backward = rig.probe_pair_subpoints(
            [('b0', np.identity(4), _T(2.0, 0.0, 0.0))],
            [('b0', np.identity(4), _T(-3.0, 0.0, 0.0))],
            np.array([2, 0], dtype=np.int64),
            pc_engine.lbs_transform,
        )
        np.testing.assert_allclose(forward, [[3.0, 0.0, 0.0], [2.0, 0.0, 0.0]], atol=1e-12)
        np.testing.assert_allclose(backward, [[-2.0, 0.0, 0.0], [-3.0, 0.0, 0.0]], atol=1e-12)
        np.testing.assert_allclose(rig.current, baseline, atol=1e-12)


class ValidateFlowTests(unittest.TestCase):
    def test_matching_blender_passes(self):
        rig = _chain_rig()
        state = {n: np.identity(4) for n in rig.bone_names}

        def apply(name, basis):
            state[name] = basis.copy()

        def read(name):
            # 与虚拟骨架同一递推语义（模拟一致的理想 Blender）
            idx = rig.bone_names.index(name)
            par = rig.parents[idx]
            if par < 0:
                return rig.local_mats[idx] @ state[name]
            parent_pose = read(rig.bone_names[par])
            return parent_pose @ rig.rel_mats[idx] @ state[name]

        ok, note = pc_vrig.validate_pose_recursion(
            rig, ['root', 'mid'], read, apply)
        self.assertTrue(ok, note)

    def test_mismatching_blender_fails(self):
        rig = _chain_rig()
        state = {n: np.identity(4) for n in rig.bone_names}
        ok, note = pc_vrig.validate_pose_recursion(
            rig, ['root'], lambda name: np.identity(4),
            lambda name, basis: state.__setitem__(name, basis))
        self.assertFalse(ok)
        # 恒等单位阵下偏差由静息平移主导：leaf 平移 3 最大
        self.assertIn('leaf', note)


class HeadlessConvergenceTests(unittest.TestCase):
    """端到端：引擎 + 虚拟骨架 + 小批量，验证会话确实收敛。"""

    def test_minibatch_session_converges(self):
        n = 512
        rng = np.random.default_rng(11)
        # Rotation must be identifiable from occupancy structure alone; an
        # isotropic Gaussian sphere only had a direction through point indices.
        a = rng.normal(size=(n, 3))
        a /= np.linalg.norm(a, axis=1)[:, None]
        a *= np.array([2.0, 0.7, 0.25])
        a[:, 1] += 0.15 * np.square(a[:, 0])
        b_rest = a @ _Rz(30.0)[:3, :3].T
        topk_w = np.zeros((n, 4))
        topk_w[:, 0] = 1.0
        topk_idx = np.zeros((n, 4), dtype=np.int64)
        topk_idx[:, 1:] = -1
        rig = pc_vrig.build_virtual_rig(
            bone_names=['bone'], parents={'bone': None},
            local_mats={'bone': np.identity(4)},
            rest_arm=b_rest, topk_w=topk_w, topk_idx=topk_idx,
            bone_to_rows={0: np.arange(n)}, arm_mw=np.identity(4))
        init_samples = rig.read_samples(pc_engine.lbs_transform)

        bones = [pc_engine.PCBoneSpec(
            name='bone', enabled=True, kind='deform', rotation_mode='XYZ',
            lock_rotation=(False, False, False), lock_scale=(False, False, False),
            lock_location=(False, False, False), has_constraints=False,
            influence_indices=np.arange(n), influence_weights=np.ones(n))]
        cfg = pc_engine.PCFitConfig(seed=5, minibatch_size=32,
                                    full_eval_interval=50, snapshot_interval=100)
        session = pc_engine.PCFitSession(
            bones=bones, a_points=a, b_points=init_samples,
            nn_a=pc_engine.brute_force_nn(a), config=cfg,
            apply_basis=rig.set_basis,
            read_samples=lambda: rig.read_samples(pc_engine.lbs_transform),
            bone_point_provider=rig.provider(), tau=0.2,
            basis_map={'bone': np.identity(4)},
            backend=pc_backend.NumpyBackend())

        for _ in range(200):
            self.assertIsNotNone(session.step())

        exact = pc_engine.overlap_metric(
            session.a_points, session.b_points, session.nn_a,
            pc_engine.brute_force_nn(session.b_points), session.tau)
        self.assertGreater(exact.f1, 0.45)
        # seek 回退到 0：虚拟骨架应恢复初始姿态
        session.seek(0)
        np.testing.assert_allclose(
            rig.read_samples(pc_engine.lbs_transform), init_samples, atol=1e-9)


if __name__ == "__main__":
    unittest.main()
