# -*- coding: utf-8 -*-
"""Spatial mirror detection and atomic symmetric proposal tests."""
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_engine.py"
spec = importlib.util.spec_from_file_location("_pc_engine_symmetry_test", module_path)
pc_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_engine
spec.loader.exec_module(pc_engine)


class SymmetryTests(unittest.TestCase):
    @staticmethod
    def _session(bones):
        rng = np.random.default_rng(2)
        points = rng.normal(size=(32, 3))
        return pc_engine.PCFitSession(
            bones=bones, a_points=points, b_points=points.copy(),
            nn_a=pc_engine.brute_force_nn(points),
            config=pc_engine.PCFitConfig(seed=3),
            apply_basis=lambda _name, _basis: None,
            read_samples=lambda: points.copy(),
            bone_point_provider=lambda _name, attr: (
                np.zeros(3) if attr == 'pivot' else np.identity(3)),
            tau=0.2,
            basis_map={bone.name: np.identity(4) for bone in bones})

    def test_detects_pairs_by_space_not_name(self):
        segments = {
            'alpha': (np.array([-1.0, 0.0, 0.0]), np.array([-1.0, 1.0, 0.0])),
            'unrelated_name': (np.array([1.0, 0.0, 0.0]), np.array([1.0, 1.0, 0.0])),
            'center': (np.array([0.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])),
        }
        pairs = pc_engine.detect_mirror_pairs(segments, tolerance=1e-6)
        self.assertEqual(pairs['alpha'], 'unrelated_name')
        self.assertEqual(pairs['unrelated_name'], 'alpha')
        self.assertNotIn('center', pairs)

    def test_symmetric_bones_link_only_in_final_mirror_stage(self):
        bones = []
        for name, mirror in (('left', 'right'), ('right', 'left')):
            bones.append(pc_engine.PCBoneSpec(
                name=name, enabled=True, kind='deform', rotation_mode='XYZ',
                lock_rotation=(False, False, False),
                lock_scale=(False, False, False),
                lock_location=(False, False, False), has_constraints=False,
                influence_indices=np.arange(32),
                influence_weights=np.ones(32), mirror_name=mirror))
        session = self._session(bones)
        for stage in range(4):
            session.schedule.stage = stage
            proposal = session.propose()
            self.assertIsNotNone(proposal)
            self.assertEqual(proposal.linked, [])
        session.schedule.stage = 4
        proposal = session.propose()
        self.assertIsNotNone(proposal)
        self.assertEqual(len(proposal.linked), 1)
        self.assertEqual({proposal.bone_name, proposal.linked[0].bone_name},
                         {'left', 'right'})

    def test_locked_mirror_axis_prevents_grouping_in_that_phase(self):
        # Grouping must never bypass the partner's channel locks.
        unlocked = pc_engine.PCBoneSpec(
            'left', True, 'deform', 'XYZ', (False, False, False),
            (False, False, False), (False, False, False), False,
            np.arange(4), np.ones(4), mirror_name='right')
        locked = pc_engine.PCBoneSpec(
            'right', True, 'deform', 'XYZ', (True, True, True),
            (False, False, False), (False, False, False), False,
            np.arange(4), np.ones(4), mirror_name='left')
        session = self._session([unlocked, locked])
        proposal = session.propose()
        self.assertEqual(proposal.bone_name, 'left')
        self.assertEqual(proposal.linked, [])

    def test_group_commit_uses_one_history_step_for_both_sides(self):
        bones = [pc_engine.PCBoneSpec(
            name=name, enabled=True, kind='deform', rotation_mode='XYZ',
            lock_rotation=(False, False, False),
            lock_scale=(False, False, False),
            lock_location=(False, False, False), has_constraints=False,
            influence_indices=np.arange(32), influence_weights=np.ones(32))
            for name in ('left', 'right')]
        session = self._session(bones)
        left_after = np.identity(4); left_after[1, 3] = 0.1
        right_after = np.identity(4); right_after[1, 3] = 0.1
        proposal = pc_engine.PCProposal(
            'left', 'deform', None, np.identity(4), left_after,
            pc_engine.TF_LOCATION,
            linked=[pc_engine.PCLinkedBasis(
                'right', np.identity(4), right_after)])
        session.step_count = 12
        session._commit_proposal_group(proposal)
        self.assertEqual([(step, name) for step, name, _basis in session.deltas],
                         [(12, 'left'), (12, 'right')])

    def test_linked_rotations_have_identical_angle(self):
        left_after = np.identity(4)
        left_after[:3, :3] = pc_engine.rotvec_to_mat3(
            np.array([0.0, 0.0, np.deg2rad(5.0)]))
        right_after = np.identity(4)
        right_after[:3, :3] = pc_engine.rotvec_to_mat3(
            np.array([0.0, 0.0, np.deg2rad(-11.0)]))
        proposal = pc_engine.PCProposal(
            'left', 'deform', None, np.identity(4), left_after,
            pc_engine.TF_ROTATION,
            linked=[pc_engine.PCLinkedBasis(
                'right', np.identity(4), right_after)])
        pc_engine.PCFitSession._synchronize_linked_magnitudes(proposal)
        left_angle = np.linalg.norm(pc_engine.mat3_to_rotvec(
            proposal.basis_after[:3, :3]))
        right_angle = np.linalg.norm(pc_engine.mat3_to_rotvec(
            proposal.linked[0].basis_after[:3, :3]))
        self.assertAlmostEqual(left_angle, np.deg2rad(5.0), places=10)
        self.assertAlmostEqual(right_angle, left_angle, places=10)


if __name__ == '__main__':
    unittest.main()
