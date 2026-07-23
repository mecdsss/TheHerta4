# -*- coding: utf-8 -*-
"""pc_engine 重合率指标测试（纯 numpy，不依赖 bpy）。"""
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_engine.py"
spec = importlib.util.spec_from_file_location("_pc_engine_metric_test", module_path)
pc_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_engine
spec.loader.exec_module(pc_engine)


class OverlapMetricTests(unittest.TestCase):
    @staticmethod
    def _cube_shell(origin=(0, 0, 0), size=3, voxel_size=1.0):
        ox, oy, oz = origin
        cells = []
        for x in range(size):
            for y in range(size):
                for z in range(size):
                    if x in (0, size - 1) or y in (0, size - 1) or z in (0, size - 1):
                        cells.append((ox + x, oy + y, oz + z))
        return (np.asarray(cells, dtype=float) + 0.25) * voxel_size

    def test_closed_surface_keeps_only_model_occupied_voxels(self):
        shell = self._cube_shell()
        cells = pc_engine.solid_voxel_cells(shell, 1.0)
        self.assertEqual(len(cells), 26)

    def test_voxel_overlap_does_not_fill_unoccupied_interior(self):
        a = self._cube_shell()
        b = self._cube_shell(origin=(1, 0, 0))
        f1, precision, recall = pc_engine.voxel_overlap(a, b, 1.0)
        self.assertAlmostEqual(f1, 16.0 / 26.0, places=9)
        self.assertAlmostEqual(precision, 16.0 / 26.0, places=9)
        self.assertAlmostEqual(recall, 16.0 / 26.0, places=9)

    def test_equal_axis_projections_do_not_create_false_overlap(self):
        a = np.array([
            [0.25, 0.25, 0.25], [0.25, 1.25, 1.25],
            [1.25, 0.25, 1.25], [1.25, 1.25, 0.25]])
        b = np.array([
            [0.25, 0.25, 1.25], [0.25, 1.25, 0.25],
            [1.25, 0.25, 0.25], [1.25, 1.25, 1.25]])
        f1, precision, recall = pc_engine.voxel_overlap(a, b, 1.0)
        self.assertEqual((f1, precision, recall), (0.0, 0.0, 0.0))

    def test_identical_clouds_full_overlap(self):
        a = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        b = a.copy()
        nn_a = pc_engine.brute_force_nn(a)
        nn_b = pc_engine.brute_force_nn(b)
        m = pc_engine.overlap_metric(a, b, nn_a, nn_b, tau=0.1)
        self.assertAlmostEqual(m.f1, 1.0, places=9)
        self.assertAlmostEqual(m.precision, 1.0, places=9)
        self.assertAlmostEqual(m.recall, 1.0, places=9)
        self.assertAlmostEqual(m.chamfer, 0.0, places=9)

    def test_unequal_point_counts_can_still_have_full_overlap(self):
        a = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        b = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0],
                      [1.0, 0.0, 0.0], [1.0, 0.0, 0.0],
                      [1.0, 0.0, 0.0]])
        metric = pc_engine.overlap_metric(
            a, b, pc_engine.brute_force_nn(a),
            pc_engine.brute_force_nn(b), tau=0.1)
        self.assertAlmostEqual(metric.precision, 1.0, places=9)
        self.assertAlmostEqual(metric.recall, 1.0, places=9)
        self.assertAlmostEqual(metric.f1, 1.0, places=9)

    def test_duplicate_density_does_not_change_voxel_overlap(self):
        a = np.array([[0.01, 0.01, 0.01], [0.11, 0.01, 0.01]])
        b = np.array([[0.02, 0.02, 0.02]] * 20 +
                     [[0.19, 0.09, 0.09]] * 3)
        f1, precision, recall = pc_engine.voxel_overlap(a, b, 0.1)
        self.assertAlmostEqual(f1, 1.0, places=9)
        self.assertAlmostEqual(precision, 1.0, places=9)
        self.assertAlmostEqual(recall, 1.0, places=9)

    def test_points_in_same_voxel_overlap_without_coordinate_match(self):
        a = np.array([[0.01, 0.01, 0.01]])
        b = np.array([[0.05, 0.05, 0.05]])
        metric = pc_engine.overlap_metric(
            a, b, pc_engine.brute_force_nn(a),
            pc_engine.brute_force_nn(b), tau=0.1)
        self.assertAlmostEqual(metric.f1, 1.0, places=9)
        self.assertGreater(metric.chamfer, 0.0)

    def test_non_overlapping_voxels_have_zero_overlap(self):
        a = np.array([[0.01, 0.01, 0.01]])
        b = np.array([[0.11, 0.01, 0.01]])
        f1, precision, recall = pc_engine.voxel_overlap(a, b, 0.1)
        self.assertEqual((f1, precision, recall), (0.0, 0.0, 0.0))

    def test_distance_overlap_is_not_limited_by_voxel_boundary(self):
        a = np.array([[0.099, 0.01, 0.01]])
        b = np.array([[0.101, 0.01, 0.01]])
        nn_a = pc_engine.brute_force_nn(a)
        nn_b = pc_engine.brute_force_nn(b)

        strict = pc_engine.voxel_overlap(a, b, 0.1)
        tolerant = pc_engine.overlap_metric(a, b, nn_a, nn_b, tau=0.1)

        self.assertAlmostEqual(strict[0], 0.0, places=9)
        self.assertAlmostEqual(tolerant.f1, 1.0, places=9)
        self.assertAlmostEqual(tolerant.precision, 1.0, places=9)
        self.assertAlmostEqual(tolerant.recall, 1.0, places=9)

    def test_lower_voxel_f1_is_never_better_even_with_lower_chamfer(self):
        baseline = pc_engine.PCMetric(
            f1=0.8, precision=0.8, recall=0.8, chamfer=10.0)
        candidate = pc_engine.PCMetric(
            f1=0.7, precision=0.7, recall=0.7, chamfer=0.01)
        self.assertFalse(pc_engine.metric_improves(candidate, baseline))
        self.assertLess(pc_engine.metric_reward(candidate, baseline), 0.0)

    def test_continuous_score_can_accept_across_hard_f1_boundary(self):
        baseline = pc_engine.PCMetric(
            f1=0.84, precision=0.84, recall=0.84,
            chamfer=0.020, score=0.620)
        candidate = pc_engine.PCMetric(
            f1=0.83, precision=0.83, recall=0.83,
            chamfer=0.010, score=0.700)
        self.assertTrue(pc_engine.metric_improves(candidate, baseline))
        self.assertGreater(pc_engine.metric_reward(candidate, baseline), 0.0)

    def test_distance_score_accepts_closer_cloud_even_if_hard_f1_drops(self):
        a = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        baseline_b = np.array([[0.09, 0.0, 0.0], [1.5, 0.0, 0.0]])
        candidate_b = np.array([[0.11, 0.0, 0.0], [1.11, 0.0, 0.0]])
        baseline = pc_engine.overlap_metric(
            a, baseline_b, pc_engine.brute_force_nn(a),
            pc_engine.brute_force_nn(baseline_b), tau=0.1)
        candidate = pc_engine.overlap_metric(
            a, candidate_b, pc_engine.brute_force_nn(a),
            pc_engine.brute_force_nn(candidate_b), tau=0.1)

        self.assertLess(candidate.f1, baseline.f1)
        self.assertLess(candidate.chamfer, baseline.chamfer)
        self.assertGreater(candidate.score, baseline.score)
        self.assertTrue(pc_engine.metric_improves(candidate, baseline))

    def test_chamfer_breaks_an_exact_voxel_f1_tie(self):
        baseline = pc_engine.PCMetric(
            f1=0.8, precision=0.8, recall=0.8, chamfer=2.0)
        candidate = pc_engine.PCMetric(
            f1=0.8, precision=0.8, recall=0.8, chamfer=1.0)
        self.assertTrue(pc_engine.metric_improves(candidate, baseline))
        self.assertGreater(pc_engine.metric_reward(candidate, baseline), 0.0)

    def test_half_overlap_precision(self):
        # A 两个点，B 两个点：B[0] 与 A[0] 重合，B[1] 远离
        a = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        b = np.array([[0.0, 0.0, 0.0], [5.0, 5.0, 5.0]])
        nn_a = pc_engine.brute_force_nn(a)
        nn_b = pc_engine.brute_force_nn(b)
        m = pc_engine.overlap_metric(a, b, nn_a, nn_b, tau=0.1)
        self.assertAlmostEqual(m.precision, 0.5, places=9)
        self.assertAlmostEqual(m.recall, 0.5, places=9)
        self.assertAlmostEqual(m.f1, 0.5, places=9)

    def test_tau_threshold_boundary(self):
        a = np.array([[0.0, 0.0, 0.0]])
        b = np.array([[0.05, 0.0, 0.0]])
        nn_a = pc_engine.brute_force_nn(a)
        nn_b = pc_engine.brute_force_nn(b)
        # tau 小于距离 -> 不重合
        m = pc_engine.overlap_metric(a, b, nn_a, nn_b, tau=0.01)
        self.assertAlmostEqual(m.f1, 0.0, places=9)
        # tau 大于距离 -> 重合
        m = pc_engine.overlap_metric(a, b, nn_a, nn_b, tau=0.1)
        self.assertAlmostEqual(m.f1, 1.0, places=9)

    def test_invalid_inputs_return_zero(self):
        a = np.zeros((0, 3))
        b = np.array([[0.0, 0.0, 0.0]])
        nn = pc_engine.brute_force_nn(b)
        m = pc_engine.overlap_metric(a, b, nn, nn, tau=0.1)
        self.assertEqual(m.f1, 0.0)
        m = pc_engine.overlap_metric(b, b, nn, nn, tau=0.0)
        self.assertEqual(m.f1, 0.0)

class ManualRecomputeTests(unittest.TestCase):
    def test_recompute_current_metric_tracks_manual_pose_without_advancing_history(self):
        a = np.array([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 1.0, 0.0],
        ])
        rest = a + np.array([0.3, 0.0, 0.0])

        class _Rig:
            def __init__(self, points):
                self.rest = points.copy()
                self.b_points = points.copy()
                self.basis = {'Bone': np.identity(4)}

            def apply_basis(self, _name, basis):
                self.basis['Bone'] = basis.copy()
                self.b_points = (self.rest @ basis[:3, :3].T) + basis[:3, 3]

            def read_samples(self):
                return self.b_points.copy()

            def provider(self, _name, attr):
                return np.zeros(3) if attr == 'pivot' else np.identity(3)

        rig = _Rig(rest)
        session = pc_engine.PCFitSession(
            bones=[pc_engine.PCBoneSpec(
                name='Bone', enabled=True, kind='deform', rotation_mode='XYZ',
                lock_rotation=(False, False, False),
                lock_scale=(False, False, False),
                lock_location=(False, False, False), has_constraints=False,
                influence_indices=np.arange(len(a)),
                influence_weights=np.ones(len(a)))],
            a_points=a, b_points=rig.read_samples(),
            nn_a=pc_engine.brute_force_nn(a),
            config=pc_engine.PCFitConfig(seed=0),
            apply_basis=rig.apply_basis, read_samples=rig.read_samples,
            bone_point_provider=rig.provider, tau=0.1,
            basis_map={'Bone': np.identity(4)},
        )

        before = session.current_metric.f1
        manual = np.identity(4)
        manual[0, 3] = -0.3
        rig.apply_basis('Bone', manual)
        metric = session.recompute_current_metric()

        self.assertEqual(session.step_count, 0)
        self.assertGreater(metric.f1, before)
        self.assertAlmostEqual(metric.f1, session.current_metric.f1, places=9)


class ProbeRecoveryTests(unittest.TestCase):
    def test_probe_metric_restores_state_after_invalid_sample_read(self):
        baseline = np.array([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
        ])
        bad = np.array([
            [np.nan, 0.0, 0.0],
            [np.inf, 0.0, 0.0],
        ])

        class _Rig:
            def __init__(self):
                self.bad = False

            def apply_basis(self, _name, basis):
                self.bad = not np.allclose(basis, np.identity(4))

            def read_samples(self):
                return bad.copy() if self.bad else baseline.copy()

            def provider(self, _name, attr):
                return np.zeros(3) if attr == 'pivot' else np.identity(3)

        rig = _Rig()
        session = pc_engine.PCFitSession(
            bones=[pc_engine.PCBoneSpec(
                name='Bone', enabled=True, kind='deform', rotation_mode='XYZ',
                lock_rotation=(False, False, False),
                lock_scale=(False, False, False),
                lock_location=(False, False, False), has_constraints=False,
                influence_indices=np.arange(len(baseline)),
                influence_weights=np.ones(len(baseline)))],
            a_points=baseline,
            b_points=baseline.copy(),
            nn_a=pc_engine.brute_force_nn(baseline),
            config=pc_engine.PCFitConfig(seed=0),
            apply_basis=rig.apply_basis,
            read_samples=rig.read_samples,
            bone_point_provider=rig.provider,
            tau=0.1,
            basis_map={'Bone': np.identity(4)},
        )

        proposal = pc_engine.PCProposal(
            bone_name='Bone',
            kind='deform',
            axis=0,
            basis_before=np.identity(4),
            basis_after=np.array([
                [1.0, 0.0, 0.0, 0.1],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]),
            tf_type=pc_engine.TF_LOCATION,
        )

        metric = session._probe_metric(proposal, session._full_metric)

        self.assertLess(metric.f1, 0.0)
        np.testing.assert_allclose(session.b_points, baseline, atol=1e-12)
        np.testing.assert_allclose(
            session.basis_map['Bone'], np.identity(4), atol=1e-12)
        self.assertFalse(rig.bad)


class ScreenMetricBackendTests(unittest.TestCase):
    def test_screen_metric_pair_can_use_gpu_backend_without_changing_metric(self):
        points = np.array([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 1.0, 0.0],
        ], dtype=float)
        forward_sub = points + np.array([0.02, 0.0, 0.0])
        backward_sub = points + np.array([-0.02, 0.0, 0.0])

        class _Backend:
            is_gpu = True

            def __init__(self):
                self.nearest_calls = 0
                self.nearest_transient_calls = 0

            def nearest(self, ref, query):
                self.nearest_calls += 1
                return pc_engine.brute_force_nn(np.asarray(ref, dtype=float))(
                    np.asarray(query, dtype=float))

            def nearest_transient(self, ref, query):
                self.nearest_transient_calls += 1
                return pc_engine.brute_force_nn(np.asarray(ref, dtype=float))(
                    np.asarray(query, dtype=float))

        class _Rig:
            def set_basis_map(self, _basis_map):
                return

            def refresh_pose(self):
                return

            def probe_pair_subpoints(self, _forward_entries, _backward_entries, _indices, _transform_fn):
                return forward_sub.copy(), backward_sub.copy()

        backend = _Backend()
        session = pc_engine.PCFitSession(
            bones=[pc_engine.PCBoneSpec(
                name='Bone', enabled=True, kind='deform', rotation_mode='XYZ',
                lock_rotation=(False, False, False),
                lock_scale=(False, False, False),
                lock_location=(False, False, False), has_constraints=False,
                influence_indices=np.arange(len(points)),
                influence_weights=np.ones(len(points)))],
            a_points=points,
            b_points=points.copy(),
            nn_a=pc_engine.brute_force_nn(points),
            config=pc_engine.PCFitConfig(seed=0),
            apply_basis=lambda _name, _basis: None,
            read_samples=lambda: points.copy(),
            bone_point_provider=lambda _name, attr: (
                np.zeros(3) if attr == 'pivot' else np.identity(3)),
            tau=0.1,
            basis_map={'Bone': np.identity(4)},
            nn_factory=pc_engine.brute_force_nn,
            backend=backend,
            screen_rig=_Rig(),
        )
        session._screen_rig_needs_sync = False

        proposal = pc_engine.PCProposal(
            bone_name='Bone',
            kind='deform',
            axis=0,
            basis_before=np.identity(4),
            basis_after=np.identity(4),
            tf_type=pc_engine.TF_LOCATION,
        )
        forward_metric, backward_metric = session._screen_metric_pair(
            proposal, proposal)

        expected_forward = pc_engine.overlap_metric(
            session._screen_a_points,
            forward_sub,
            session._screen_nn_a,
            pc_engine.brute_force_nn(forward_sub),
            session.tau,
        )
        expected_backward = pc_engine.overlap_metric(
            session._screen_a_points,
            backward_sub,
            session._screen_nn_a,
            pc_engine.brute_force_nn(backward_sub),
            session.tau,
        )

        self.assertGreater(backend.nearest_calls, 0)
        self.assertGreater(backend.nearest_transient_calls, 0)
        self.assertAlmostEqual(forward_metric.f1, expected_forward.f1, places=9)
        self.assertAlmostEqual(backward_metric.f1, expected_backward.f1, places=9)
        self.assertAlmostEqual(
            float(forward_metric.score), float(expected_forward.score), places=6)
        self.assertAlmostEqual(
            float(backward_metric.score), float(expected_backward.score), places=6)


if __name__ == "__main__":
    unittest.main()
