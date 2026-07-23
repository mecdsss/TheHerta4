# -*- coding: utf-8 -*-
"""pc_engine 骨骼规格/通道枚举与分类规则测试（纯 numpy，不依赖 bpy）。"""
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_engine.py"
spec = importlib.util.spec_from_file_location("_pc_engine_params_test", module_path)
pc_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_engine
spec.loader.exec_module(pc_engine)


def _make_spec(name="Bone", kind="deform",
               lock_r=(False, False, False),
               lock_s=(False, False, False),
               lock_l=(False, False, False),
               has_constraints=False):
    return pc_engine.PCBoneSpec(
        name=name, enabled=True, kind=kind, rotation_mode='XYZ',
        lock_rotation=lock_r, lock_scale=lock_s, lock_location=lock_l,
        has_constraints=has_constraints,
        influence_indices=np.arange(4),
        influence_weights=np.ones(4),
    )


class ChannelEnumTests(unittest.TestCase):
    def _session(self, spec):
        a = np.zeros((4, 3))
        b = np.zeros((4, 3))
        nn = pc_engine.brute_force_nn(a)
        return pc_engine.PCFitSession(
            bones=[spec], a_points=a, b_points=b, nn_a=nn,
            config=pc_engine.PCFitConfig(),
            apply_basis=lambda n, m: None,
            read_samples=lambda: b.copy(),
            bone_point_provider=lambda n, attr: np.zeros(3) if attr == 'pivot' else np.identity(3),
            tau=0.1,
        )

    def test_unlocked_channels_reflect_locks(self):
        spec = _make_spec(lock_r=(True, False, True), lock_s=(False, True, False),
                          lock_l=(True, True, True))
        session = self._session(spec)
        ch = session._unlocked_channels(spec)
        self.assertEqual(ch[pc_engine.TF_ROTATION], (False, True, False))
        self.assertEqual(ch[pc_engine.TF_SCALE], (True, False, True))
        self.assertEqual(ch[pc_engine.TF_LOCATION], (False, False, False))

    def test_all_unlocked_by_default(self):
        spec = _make_spec()
        session = self._session(spec)
        ch = session._unlocked_channels(spec)
        self.assertTrue(all(all(v) for v in ch.values()))

    def test_curriculum_skips_fully_locked_phase_without_unlocking_axes(self):
        spec = _make_spec(
            lock_r=(True, True, True),
            lock_s=(True, False, True),
            lock_l=(True, True, True))
        session = self._session(spec)
        proposal = session.propose()
        self.assertIsNotNone(proposal)
        self.assertEqual(session.schedule.stage, 1)
        self.assertEqual(proposal.tf_type, pc_engine.TF_SCALE)
        self.assertAlmostEqual(proposal.basis_after[0, 0], 1.0)
        self.assertAlmostEqual(proposal.basis_after[2, 2], 1.0)

    def test_zero_analytic_delta_falls_back_to_nonzero_axis_probe(self):
        session = self._session(_make_spec(kind='deform'))
        proposal = session.propose()
        self.assertIsNotNone(proposal)
        delta = (pc_engine.basis_rotation_matrix(proposal.basis_after)
                 @ pc_engine.basis_rotation_matrix(proposal.basis_before).T)
        angle = np.linalg.norm(pc_engine.mat3_to_rotvec(delta))
        self.assertGreater(angle, 1e-6)
        self.assertIsNotNone(proposal.axis)
        result = session._step_exact(proposal)
        self.assertFalse(result.accepted)
        self.assertGreater(np.linalg.norm(result.delta_components), 1e-6)
        self.assertAlmostEqual(result.reward, 0.0, places=12)
        next_proposal = session.propose()
        self.assertNotEqual(next_proposal.axis, proposal.axis)

    def test_success_grows_step_and_repeated_rollbacks_shrink_search_radius(self):
        session = self._session(_make_spec(kind='controller'))
        key = ('Bone', pc_engine.TF_ROTATION)
        session._parameter_step(key, initial=1.0, low=0.1, high=10.0)
        proposal = pc_engine.PCProposal(
            'Bone', 'controller', 0, np.identity(4), np.identity(4),
            pc_engine.TF_ROTATION)
        session._adapt_parameter_step(proposal, accepted=True)
        self.assertAlmostEqual(session.step_sizes[key], 1.5)
        for _ in range(4):
            session._adapt_parameter_step(proposal, accepted=False)
        self.assertLess(session.step_sizes[key], 1.5)
        self.assertGreaterEqual(session.step_sizes[key], 0.1)

    def test_plateau_refine_reuses_historical_larger_step_sizes(self):
        session = self._session(_make_spec(kind='controller'))
        key = ('Bone', pc_engine.TF_SCALE)
        session.step_sizes[key] = 0.05
        session.step_size_limits[key] = (0.001, 1.0)
        session.step_size_max_seen[key] = 0.2
        session.axis_f1_plateau_streak[key] = 1
        scales = session._candidate_scale_factors(session.bones['Bone'],
                                                  pc_engine.TF_SCALE,
                                                  plateau_refine=True)
        self.assertIn(4.0, scales)

    def test_f1_plateau_streak_resets_after_real_f1_gain(self):
        session = self._session(_make_spec(kind='controller'))
        proposal = pc_engine.PCProposal(
            'Bone', 'controller', 0, np.identity(4), np.identity(4),
            pc_engine.TF_ROTATION)
        key = ('Bone', pc_engine.TF_ROTATION)
        session._observe_axis_f1_progress(proposal, True, 0.0)
        session._observe_axis_f1_progress(proposal, True, 0.0)
        self.assertEqual(session.axis_f1_plateau_streak[key], 2)
        session._observe_axis_f1_progress(proposal, True, 1e-3)
        self.assertEqual(session.axis_f1_plateau_streak[key], 0)

    def test_compose_basis_clamps_scale_away_from_zero(self):
        basis = pc_engine.compose_basis(
            np.zeros(3), np.identity(3), np.array([0.0, 1.0, 100.0]))
        scale = pc_engine.basis_scale(basis)
        self.assertGreaterEqual(scale[0], pc_engine.MIN_SCALE_ABS)
        self.assertLessEqual(scale[2], pc_engine.MAX_SCALE_ABS)


class BoneKindRuleTests(unittest.TestCase):
    def test_constrained_bone_must_be_controller(self):
        # 带约束骨即使给了影响表，也应被标记为 controller（bridge 负责判定时我们验证语义字段）
        spec = _make_spec(kind='controller', has_constraints=True)
        self.assertEqual(spec.kind, 'controller')
        self.assertTrue(spec.has_constraints)

    def test_bone_without_influence_can_be_controller(self):
        spec = pc_engine.PCBoneSpec(
            name="Ctrl", enabled=True, kind='controller', rotation_mode='QUATERNION',
            lock_rotation=(False, False, False), lock_scale=(False, False, False),
            lock_location=(False, False, False), has_constraints=False,
            influence_indices=np.array([], dtype=int),
            influence_weights=np.array([], dtype=float),
        )
        self.assertEqual(spec.kind, 'controller')
        self.assertEqual(len(spec.influence_indices), 0)


if __name__ == "__main__":
    unittest.main()
