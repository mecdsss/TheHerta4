# -*- coding: utf-8 -*-
"""pc_engine 小批量（minibatch）模式测试：配对比较、全量锚点、最佳步落点。"""
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


pc_engine = _load("_pc_engine_mb_test", "toolkit/pc_engine.py")
pc_backend = _load("_pc_backend_mb_test", "toolkit/pc_backend.py")


class _FakeRig:
    """单骨骼刚体假骨架：basis 旋转作用于全部点（绕 pivot）。"""

    def __init__(self, b_points: np.ndarray, pivot: np.ndarray):
        self.b_points = b_points.copy()
        self.rest = b_points.copy()
        self.pivot = pivot
        self.basis = np.identity(4)

    def apply_basis(self, name: str, basis: np.ndarray) -> None:
        self.basis = basis.copy()
        m = basis
        self.b_points = (self.rest - self.pivot) @ m[:3, :3].T + self.pivot + m[:3, 3]

    def read_samples(self) -> np.ndarray:
        return self.b_points.copy()

    def provider(self, name: str, attr: str) -> np.ndarray:
        if attr == 'pivot':
            return self.pivot
        return np.identity(3)


class _RecordingBackend(pc_backend.NumpyBackend):
    def __init__(self):
        super().__init__()
        self.shapes = []

    def nearest(self, ref, query):
        self.shapes.append((len(ref), len(query)))
        return super().nearest(ref, query)


def _rot_z(deg: float) -> np.ndarray:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    m = np.identity(4)
    m[:3, :3] = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return m


class MinibatchSessionTests(unittest.TestCase):
    def _make_session(self, n=512, offset_deg=30.0, minibatch=32, interval=50, seed=11):
        rng = np.random.default_rng(seed)
        # An asymmetric anisotropic cloud has a structurally identifiable
        # orientation without relying on equal point counts or point indices.
        a = rng.normal(size=(n, 3))
        a /= np.linalg.norm(a, axis=1)[:, None]
        a *= np.array([2.0, 0.7, 0.25])
        a[:, 1] += 0.15 * np.square(a[:, 0])
        b = (a - 0.0) @ _rot_z(offset_deg)[:3, :3].T
        rig = _FakeRig(b, pivot=np.zeros(3))
        bones = [pc_engine.PCBoneSpec(
            name='Bone', enabled=True, kind='deform', rotation_mode='XYZ',
            lock_rotation=(False, False, False), lock_scale=(False, False, False),
            lock_location=(False, False, False), has_constraints=False,
            influence_indices=np.arange(n), influence_weights=np.ones(n))]
        cfg = pc_engine.PCFitConfig(seed=5, minibatch_size=minibatch,
                                    full_eval_interval=interval,
                                    snapshot_interval=100)
        session = pc_engine.PCFitSession(
            bones=bones, a_points=a, b_points=rig.read_samples(),
            nn_a=pc_engine.brute_force_nn(a), config=cfg,
            apply_basis=rig.apply_basis, read_samples=rig.read_samples,
            bone_point_provider=rig.provider, tau=0.2,
            basis_map={'Bone': rig.basis.copy()},
            backend=pc_backend.NumpyBackend(),
        )
        return session, a

    def test_minibatch_converges(self):
        session, a = self._make_session()
        exact0 = pc_engine.overlap_metric(
            session.a_points, session.b_points,
            session.nn_a, pc_engine.brute_force_nn(session.b_points), session.tau)
        for _ in range(200):
            self.assertIsNotNone(session.step())
        exact1 = pc_engine.overlap_metric(
            session.a_points, session.b_points,
            session.nn_a, pc_engine.brute_force_nn(session.b_points), session.tau)
        self.assertGreater(exact1.f1, exact0.f1)
        self.assertGreater(exact1.f1, 0.8)

    def test_adaptive_optimizer_reaches_target_within_100_steps(self):
        session, _ = self._make_session(n=512, minibatch=128)
        for _ in range(100):
            self.assertIsNotNone(session.step())
        exact = pc_engine.overlap_metric(
            session.a_points, session.b_points,
            session.nn_a, pc_engine.brute_force_nn(session.b_points),
            session.tau)
        self.assertGreater(exact.f1, 0.79)

    def test_curriculum_reaches_scale_and_location_stages(self):
        rng = np.random.default_rng(7)
        n = 512
        a = rng.normal(size=(n, 3))
        a /= np.linalg.norm(a, axis=1)[:, None]
        a *= np.array([2.0, 0.7, 0.25])
        a[:, 1] += 0.15 * np.square(a[:, 0])
        b = a * 1.2 + np.array([0.25, -0.15, 0.08])
        rig = _FakeRig(b, pivot=np.zeros(3))
        spec = pc_engine.PCBoneSpec(
            name='Bone', enabled=True, kind='deform', rotation_mode='XYZ',
            lock_rotation=(False, False, False),
            lock_scale=(False, False, False),
            lock_location=(False, False, False), has_constraints=False,
            influence_indices=np.arange(n), influence_weights=np.ones(n))
        session = pc_engine.PCFitSession(
            bones=[spec], a_points=a, b_points=rig.read_samples(),
            nn_a=pc_engine.brute_force_nn(a),
            config=pc_engine.PCFitConfig(
                seed=5, minibatch_size=32, full_eval_interval=50,
                phase_eval_interval=5, phase_plateau_checks=2),
            apply_basis=rig.apply_basis, read_samples=rig.read_samples,
            bone_point_provider=rig.provider, tau=0.2,
            basis_map={'Bone': rig.basis.copy()},
            backend=pc_backend.NumpyBackend())
        initial_f1 = session.current_metric.f1
        # Rejected probes do not count as convergence; allow complete
        # shrink/expand search rounds before each phase can advance.
        for _ in range(1200):
            self.assertIsNotNone(session.step())
        exact = pc_engine.overlap_metric(
            a, session.b_points, session.nn_a,
            pc_engine.brute_force_nn(session.b_points), session.tau)
        self.assertGreaterEqual(session.schedule.stage, 3)
        self.assertGreater(exact.f1, 0.65)
        self.assertGreater(exact.f1, initial_f1 + 0.2)

    def test_metrics_and_step_count(self):
        session, _ = self._make_session()
        for _ in range(120):
            session.step()
        self.assertEqual(session.step_count, 120)
        self.assertEqual(len(session.metrics), 120)

    def test_best_step_is_not_forced_to_full_eval_anchor(self):
        session, _ = self._make_session(interval=50)
        for _ in range(180):
            session.step()
        self.assertGreater(session.best_step, 0)
        best_before = session.best_step
        session.jump_to_best()
        self.assertEqual(session.best_step, best_before)

    def test_minibatch_uses_paired_indices(self):
        """配对比较：同批下标前后各评一次，接受时 f1 严格提升才接受。"""
        session, _ = self._make_session()
        # 直接验证 _minibatch_metric 对固定下标可重复
        ia = np.arange(16)
        ib = np.arange(16)
        m1 = session._minibatch_metric(ia, ib)
        m2 = session._minibatch_metric(ia, ib)
        self.assertAlmostEqual(m1.f1, m2.f1, places=12)

    def test_deform_scale_proposal_changes_only_one_channel_axis(self):
        session, _ = self._make_session()
        session.schedule.stage = 1
        proposal = session.propose()
        self.assertIsNotNone(proposal)
        self.assertIsNotNone(proposal.axis)
        before = pc_engine.basis_scale(proposal.basis_before)
        after = pc_engine.basis_scale(proposal.basis_after)
        changed = np.flatnonzero(np.abs(after / before - 1.0) > 1e-10)
        self.assertLessEqual(len(changed), 1)
        if len(changed):
            self.assertEqual(int(changed[0]), proposal.axis)

    def test_minibatch_bounds_both_nearest_neighbor_sides(self):
        session, _ = self._make_session(n=4096, minibatch=16)
        backend = _RecordingBackend()
        session.backend = backend
        session._minibatch_metric(np.arange(16), np.arange(16))
        self.assertEqual(backend.shapes, [(2048, 16), (2048, 16)])

    def test_minibatch_metric_does_not_voxelize_the_complete_b_cloud(self):
        session, _ = self._make_session(n=4096, minibatch=16)
        original = pc_engine.occupied_voxel_keys

        def fail_keys(points, voxel_size):
            raise AssertionError("minibatch metric should use nearest-distance F1")

        pc_engine.occupied_voxel_keys = fail_keys
        try:
            session._minibatch_metric(np.arange(16), np.arange(16))
        finally:
            pc_engine.occupied_voxel_keys = original

    def test_minibatch_bounds_deform_proposal_points(self):
        session, _ = self._make_session(n=4096, minibatch=16)
        spec = session.bones['Bone']
        points, weights = session._influence_world_points(spec)
        self.assertEqual(len(points), 256)
        self.assertEqual(len(weights), 256)

    def test_rejected_direction_tries_and_accepts_opposite(self):
        rng = np.random.default_rng(12)
        a = rng.normal(size=(64, 3))
        b = a + np.array([0.2, 0.0, 0.0])
        rig = _FakeRig(b, pivot=np.zeros(3))
        spec = pc_engine.PCBoneSpec(
            name='Bone', enabled=True, kind='deform', rotation_mode='XYZ',
            lock_rotation=(False, False, False),
            lock_scale=(False, False, False),
            lock_location=(False, False, False), has_constraints=False,
            influence_indices=np.arange(64), influence_weights=np.ones(64))
        session = pc_engine.PCFitSession(
            bones=[spec], a_points=a, b_points=rig.read_samples(),
            nn_a=pc_engine.brute_force_nn(a),
            config=pc_engine.PCFitConfig(seed=2, minibatch_size=64),
            apply_basis=rig.apply_basis, read_samples=rig.read_samples,
            bone_point_provider=rig.provider, tau=0.3,
            basis_map={'Bone': np.identity(4)},
            backend=pc_backend.NumpyBackend())
        wrong = np.identity(4)
        wrong[0, 3] = 0.1
        proposal = pc_engine.PCProposal(
            bone_name='Bone', kind='deform', axis=None,
            basis_before=np.identity(4), basis_after=wrong,
            tf_type=pc_engine.TF_LOCATION)
        result = session._step_minibatch(proposal)
        self.assertTrue(result.accepted)
        self.assertLess(session.basis_map['Bone'][0, 3], 0.0)
        self.assertLess(result.delta_components[0], 0.0)
        self.assertAlmostEqual(result.delta_components[0], -0.1, places=9)
        self.assertGreater(result.reward, 0.0)

    def test_accepted_rotation_direction_is_reused_as_momentum(self):
        session, _ = self._make_session()
        session.bones['Bone'].kind = 'controller'
        accepted_basis = _rot_z(-2.0)
        accepted = pc_engine.PCProposal(
            bone_name='Bone', kind='controller', axis=2,
            basis_before=np.identity(4), basis_after=accepted_basis,
            tf_type=pc_engine.TF_ROTATION)
        session._remember_group_momentum(accepted)
        components = session._proposal_channel_components(accepted)
        self.assertAlmostEqual(components[2], -2.0, places=9)

        proposal = session.propose()
        self.assertIsNotNone(proposal)
        delta = (pc_engine.basis_rotation_matrix(proposal.basis_after)
                 @ pc_engine.basis_rotation_matrix(proposal.basis_before).T)
        np.testing.assert_allclose(
            delta, accepted_basis[:3, :3], atol=1e-10)
        self.assertEqual(proposal.axis, 2)

        session._clear_group_momentum(proposal)
        self.assertNotIn(
            ('Bone', pc_engine.TF_ROTATION), session.proposal_momentum)

    def test_exact_mode_unchanged_when_minibatch_off(self):
        """minibatch_size=0 时必须走精确全量路径（向后兼容）。"""
        session, _ = self._make_session(minibatch=0)
        self.assertFalse(session._minibatch_active())
        for _ in range(30):
            session.step()
        # 精确模式下 best 每步都可能刷新（不受锚点约束）
        self.assertGreaterEqual(session.best_step, 0)
        self.assertEqual(session.step_count, 30)


if __name__ == "__main__":
    unittest.main()
