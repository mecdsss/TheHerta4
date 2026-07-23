# -*- coding: utf-8 -*-
"""Residual-aware bone selection fairness and coverage tests."""
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_engine.py"
spec = importlib.util.spec_from_file_location("_pc_engine_selection_test", module_path)
pc_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_engine
spec.loader.exec_module(pc_engine)


def _bone(name, indices):
    indices = np.asarray(indices, dtype=np.int64)
    return pc_engine.PCBoneSpec(
        name=name, enabled=True, kind='deform', rotation_mode='XYZ',
        lock_rotation=(False, False, False),
        lock_scale=(False, False, False),
        lock_location=(False, False, False), has_constraints=False,
        influence_indices=indices,
        influence_weights=np.ones(len(indices)))


def _session(bones, point_count):
    points = np.zeros((point_count, 3))
    return pc_engine.PCFitSession(
        bones=bones, a_points=points, b_points=points.copy(),
        nn_a=lambda query: (np.ones(len(query)),
                            np.zeros(len(query), dtype=np.int64)),
        config=pc_engine.PCFitConfig(residual_samples=230, seed=4),
        apply_basis=lambda _name, _basis: None,
        read_samples=lambda: points.copy(),
        bone_point_provider=lambda _name, attr: (
            np.zeros(3) if attr == 'pivot' else np.identity(3)),
        tau=2.0)


class BoneSelectionTests(unittest.TestCase):
    def test_largest_coverage_runs_until_ten_no_gain_steps(self):
        small = _bone('small', np.arange(10))
        medium = _bone('medium', np.arange(10, 30))
        broad = _bone('broad', np.arange(30, 100))
        session = _session([small, medium, broad], 100)
        self.assertEqual(session._pick_bone().name, 'broad')
        for _ in range(9):
            session._update_bone_curriculum(0.0)
            self.assertEqual(session._pick_bone().name, 'broad')
        session._update_bone_curriculum(0.0)
        self.assertEqual(session._pick_bone().name, 'medium')

    def test_positive_gain_resets_current_bone_patience(self):
        broad = _bone('broad', np.arange(80))
        small = _bone('small', np.arange(80, 100))
        session = _session([small, broad], 100)
        self.assertEqual(session._pick_bone().name, 'broad')
        for _ in range(9):
            session._update_bone_curriculum(0.0)
        session._update_bone_curriculum(0.1)
        for _ in range(9):
            session._update_bone_curriculum(0.0)
        self.assertEqual(session._pick_bone().name, 'broad')
        session._update_bone_curriculum(0.0)
        self.assertEqual(session._pick_bone().name, 'small')

    def test_tiny_positive_gain_is_not_counted_as_no_gain(self):
        broad = _bone('broad', np.arange(80))
        small = _bone('small', np.arange(80, 100))
        session = _session([small, broad], 100)
        self.assertEqual(session._pick_bone().name, 'broad')

    def test_rejected_attempts_do_not_advance_bone_patience(self):
        broad = _bone('broad', np.arange(80))
        small = _bone('small', np.arange(80, 100))
        session = _session([small, broad], 100)
        self.assertEqual(session._pick_bone().name, 'broad')
        for _ in range(100):
            session._update_bone_curriculum(-0.1, accepted=False)
        self.assertEqual(session._pick_bone().name, 'broad')
        self.assertEqual(session._bone_curriculum_no_gain, 0)
        for _ in range(9):
            session._update_bone_curriculum(0.0)
        session._update_bone_curriculum(1e-8)
        for _ in range(9):
            session._update_bone_curriculum(0.0)
        self.assertEqual(session._pick_bone().name, 'broad')

    def test_each_transform_phase_restarts_from_largest_bone(self):
        broad = _bone('broad', np.arange(80))
        small = _bone('small', np.arange(80, 100))
        session = _session([small, broad], 100)
        self.assertEqual(session._pick_bone().name, 'broad')
        for _ in range(10):
            session._update_bone_curriculum(0.0)
        self.assertEqual(session._pick_bone().name, 'small')
        session.schedule.advance_stage()
        self.assertEqual(session._pick_bone().name, 'broad')

    def test_signed_gain_is_still_recorded_for_diagnostics(self):
        bone = _bone('bone', np.arange(10))
        session = _session([bone], 10)
        session._update_bone_gain('bone', -0.2)
        self.assertLess(session.bone_gain_ema['bone'], 0.0)

    def test_high_overlap_refine_scans_beyond_screen_topk(self):
        bone = _bone('bone', np.arange(200))
        session = _session([bone], 1200)
        baseline = pc_engine.PCMetric(
            f1=0.981, precision=0.981, recall=0.981,
            chamfer=0.01, score=0.981)
        session.current_metric = baseline
        session.best_f1 = baseline.f1
        session.step_count = 9
        session._pick_bone()
        session._bone_curriculum_no_gain = 2

        pair_id_by_basis = {}
        scale_slots = {1.0: 0, 0.5: 1, 0.25: 2, 2.0: 3}

        def fake_axis_probe(spec, tf_type, axis, sign, scale=1.0):
            pair_id = axis * 10 + scale_slots[float(scale)]
            marker = float(pair_id if sign > 0 else -pair_id)
            after = np.identity(4)
            after[0, 3] = marker
            pair_id_by_basis[marker] = pair_id
            return pc_engine.PCProposal(
                spec.name, spec.kind, axis,
                np.identity(4), after, tf_type)

        screen_rewards = {}
        ordered_pair_ids = [0, 1, 2, 3, 10, 11, 12, 13, 20, 21, 22, 23]
        for rank, pair_id in enumerate(ordered_pair_ids):
            screen_rewards[pair_id] = float(12 - rank)
        target_pair = 12  # Outside the default top-3 window.
        probe_calls = {'count': 0}

        session._proposal_for_spec = lambda *_args, **_kwargs: None
        session._axis_probe_proposal = fake_axis_probe
        session._screen_metric_for_points = lambda _points: baseline

        def fake_screen_pair(forward, backward):
            pair_id = int(abs(round(float(forward.basis_after[0, 3]))))
            reward = screen_rewards[pair_id]
            forward_metric = pc_engine.PCMetric(
                f1=baseline.f1,
                precision=baseline.precision,
                recall=baseline.recall,
                chamfer=baseline.chamfer,
                score=baseline.score + reward * 1e-3)
            return forward_metric, baseline

        def fake_probe_pair(forward, backward, _metric_fn):
            probe_calls['count'] += 1
            pair_id = int(abs(round(float(forward.basis_after[0, 3]))))
            if pair_id == target_pair:
                improved = pc_engine.PCMetric(
                    f1=baseline.f1,
                    precision=baseline.precision,
                    recall=baseline.recall,
                    chamfer=baseline.chamfer - 1e-5,
                    score=baseline.score + 5e-6)
                return improved, baseline
            return baseline, baseline

        session._screen_metric_pair = fake_screen_pair
        session._probe_metric_pair = fake_probe_pair

        result = session.step()
        self.assertIsNotNone(result)
        self.assertTrue(result.accepted)
        self.assertGreater(probe_calls['count'], 3)

    def test_high_overlap_refine_checks_second_enabled_bone(self):
        broad = _bone('broad', np.arange(900))
        small = _bone('small', np.arange(900, 1000))
        session = _session([small, broad], 1200)
        baseline = pc_engine.PCMetric(
            f1=0.981, precision=0.981, recall=0.981,
            chamfer=0.01, score=0.981)
        session.current_metric = baseline
        session.best_f1 = baseline.f1
        session.step_count = 9
        session._pick_bone()
        session._bone_curriculum_no_gain = 2

        def fake_axis_probe(spec, tf_type, axis, sign, scale=1.0):
            after = np.identity(4)
            after[0, 3] = (1.0 if spec.name == 'small' else 2.0) * (1.0 if sign > 0 else -1.0)
            return pc_engine.PCProposal(
                spec.name, spec.kind, axis,
                np.identity(4), after, tf_type)

        session._proposal_for_spec = lambda *_args, **_kwargs: None
        session._axis_probe_proposal = fake_axis_probe
        session._screen_metric_for_points = lambda _points: baseline

        def fake_screen_pair(forward, backward):
            bonus = 1e-3 if forward.bone_name == 'small' else 0.0
            metric = pc_engine.PCMetric(
                f1=baseline.f1,
                precision=baseline.precision,
                recall=baseline.recall,
                chamfer=baseline.chamfer,
                score=baseline.score + bonus)
            return metric, baseline

        def fake_probe_pair(forward, backward, _metric_fn):
            if forward.bone_name == 'small':
                improved = pc_engine.PCMetric(
                    f1=baseline.f1,
                    precision=baseline.precision,
                    recall=baseline.recall,
                    chamfer=baseline.chamfer - 1e-5,
                    score=baseline.score + 5e-6)
                return improved, baseline
            return baseline, baseline

        session._screen_metric_pair = fake_screen_pair
        session._probe_metric_pair = fake_probe_pair

        result = session.step()
        self.assertIsNotNone(result)
        self.assertTrue(result.accepted)
        self.assertEqual(result.bone_name, 'small')

    def test_high_overlap_refine_overrides_primary_score_gain_with_second_bone_f1_gain(self):
        broad = _bone('broad', np.arange(900))
        small = _bone('small', np.arange(900, 1000))
        session = _session([small, broad], 1200)
        baseline = pc_engine.PCMetric(
            f1=0.981, precision=0.981, recall=0.981,
            chamfer=0.01, score=0.981)
        session.current_metric = baseline
        session.best_f1 = baseline.f1
        session.step_count = 9
        session._pick_bone()
        session._bone_curriculum_no_gain = 2

        def fake_proposal(spec, tf_type, axis_override=None):
            after = np.identity(4)
            after[0, 3] = 1.0 if spec.name == 'broad' else 2.0
            return pc_engine.PCProposal(
                spec.name, spec.kind, 0, np.identity(4), after, tf_type)

        session._proposal_for_spec = fake_proposal
        session._screen_metric_for_points = lambda _points: baseline
        session._screen_metric_pair = lambda _fwd, _back: (baseline, baseline)

        def fake_probe_pair(forward, backward, _metric_fn):
            if forward.bone_name == 'broad':
                improved = pc_engine.PCMetric(
                    f1=baseline.f1,
                    precision=baseline.precision,
                    recall=baseline.recall,
                    chamfer=baseline.chamfer - 1e-5,
                    score=baseline.score + 5e-6)
                return improved, baseline
            improved = pc_engine.PCMetric(
                f1=baseline.f1 + 5e-4,
                precision=baseline.precision,
                recall=baseline.recall,
                chamfer=baseline.chamfer,
                score=baseline.score + 1e-6)
            return improved, baseline

        session._probe_metric_pair = fake_probe_pair

        result = session.step()
        self.assertIsNotNone(result)
        self.assertTrue(result.accepted)
        self.assertEqual(result.bone_name, 'small')
        self.assertGreater(result.metric.f1, baseline.f1)

    def test_high_overlap_tail_search_checks_second_bone_outside_plateau_step(self):
        broad = _bone('broad', np.arange(900))
        small = _bone('small', np.arange(900, 1000))
        session = _session([small, broad], 1200)
        baseline = pc_engine.PCMetric(
            f1=0.981, precision=0.981, recall=0.981,
            chamfer=0.01, score=0.981)
        session.current_metric = baseline
        session.best_f1 = baseline.f1
        session.step_count = 41
        session._pick_bone()

        def fake_proposal(spec, tf_type, axis_override=None):
            after = np.identity(4)
            after[0, 3] = 1.0 if spec.name == 'broad' else 2.0
            return pc_engine.PCProposal(
                spec.name, spec.kind, 0, np.identity(4), after, tf_type)

        session._proposal_for_spec = fake_proposal
        session._screen_metric_for_points = lambda _points: baseline
        session._screen_metric_pair = lambda _fwd, _back: (baseline, baseline)

        def fake_probe_pair(forward, backward, _metric_fn):
            if forward.bone_name == 'broad':
                improved = pc_engine.PCMetric(
                    f1=baseline.f1,
                    precision=baseline.precision,
                    recall=baseline.recall,
                    chamfer=baseline.chamfer - 1e-5,
                    score=baseline.score + 5e-6)
                return improved, baseline
            improved = pc_engine.PCMetric(
                f1=baseline.f1 + 5e-4,
                precision=baseline.precision,
                recall=baseline.recall,
                chamfer=baseline.chamfer,
                score=baseline.score + 1e-6)
            return improved, baseline

        session._probe_metric_pair = fake_probe_pair

        result = session.step()
        self.assertIsNotNone(result)
        self.assertTrue(result.accepted)
        self.assertEqual(result.bone_name, 'small')
        self.assertGreater(result.metric.f1, baseline.f1)

    def test_mid_overlap_cross_type_probe_can_override_tiny_scale_gain(self):
        broad = _bone('broad', np.arange(900))
        session = _session([broad], 1200)
        baseline = pc_engine.PCMetric(
            f1=0.913, precision=0.913, recall=0.913,
            chamfer=0.01, score=0.889)
        session.current_metric = baseline
        session.best_f1 = baseline.f1
        session.schedule.stage = 1  # scale-only phase

        def fake_proposal(spec, tf_type, axis_override=None):
            after = np.identity(4)
            after[0, 3] = 1.0 if tf_type == pc_engine.TF_SCALE else 2.0
            return pc_engine.PCProposal(
                spec.name, spec.kind, 0, np.identity(4), after, tf_type)

        session._proposal_for_spec = fake_proposal

        def fake_probe_pair(forward, backward, _metric_fn):
            if forward.tf_type == pc_engine.TF_SCALE:
                improved = pc_engine.PCMetric(
                    f1=baseline.f1,
                    precision=baseline.precision,
                    recall=baseline.recall,
                    chamfer=baseline.chamfer - 1e-6,
                    score=baseline.score + 5e-6)
                return improved, baseline
            improved = pc_engine.PCMetric(
                f1=baseline.f1 + 1e-2,
                precision=baseline.precision,
                recall=baseline.recall,
                chamfer=baseline.chamfer,
                score=baseline.score + 1e-3)
            return improved, baseline

        session._probe_metric_pair = fake_probe_pair
        session._screen_metric_for_points = lambda _points: baseline
        session._screen_metric_pair = lambda _fwd, _back: (baseline, baseline)

        result = session.step()
        self.assertIsNotNone(result)
        self.assertTrue(result.accepted)
        self.assertEqual(result.tf_type, pc_engine.TF_ROTATION)
        self.assertGreater(result.metric.f1, baseline.f1)

    def test_exact_pair_cache_reuses_results_while_state_is_unchanged(self):
        points = np.zeros((32, 3))
        read_calls = {'count': 0}
        session = pc_engine.PCFitSession(
            bones=[_bone('bone', np.arange(32))],
            a_points=points,
            b_points=points.copy(),
            nn_a=lambda query: (
                np.zeros(len(query), dtype=np.float64),
                np.zeros(len(query), dtype=np.int64)),
            config=pc_engine.PCFitConfig(seed=3),
            apply_basis=lambda _name, _basis: None,
            read_samples=lambda: read_calls.__setitem__('count', read_calls['count'] + 1) or points.copy(),
            bone_point_provider=lambda _name, attr: (
                np.zeros(3) if attr == 'pivot' else np.identity(3)),
            tau=0.1,
        )
        spec = session.bones['bone']
        forward = session._axis_probe_proposal(
            spec, pc_engine.TF_ROTATION, axis=0, sign=1)
        backward = session._axis_probe_proposal(
            spec, pc_engine.TF_ROTATION, axis=0, sign=-1)
        session._probe_metric_pair(forward, backward, session._full_metric)
        first_reads = read_calls['count']
        session._probe_metric_pair(forward, backward, session._full_metric)
        self.assertEqual(read_calls['count'], first_reads)
        session._commit_proposal_group(forward, already_applied=False)
        session._probe_metric_pair(forward, backward, session._full_metric)
        self.assertGreater(read_calls['count'], first_reads)

    def test_screen_pair_cache_reuses_results_while_state_is_unchanged(self):
        points = np.zeros((64, 3))
        session = _session([_bone('bone', np.arange(64))], 64)
        spec = session.bones['bone']
        forward = session._axis_probe_proposal(
            spec, pc_engine.TF_SCALE, axis=0, sign=1)
        backward = session._axis_probe_proposal(
            spec, pc_engine.TF_SCALE, axis=0, sign=-1)
        calls = {'count': 0}
        session._screen_points_pair = lambda *_args: (
            calls.__setitem__('count', calls['count'] + 1) or points.copy(),
            points.copy())
        session._screen_metric_pair(forward, backward)
        first_calls = calls['count']
        session._screen_metric_pair(forward, backward)
        self.assertEqual(calls['count'], first_calls)
        session._commit_proposal_group(forward, already_applied=False)
        session._screen_metric_pair(forward, backward)
        self.assertGreater(calls['count'], first_calls)

    def test_high_overlap_accept_patience_stops_tail_refinement(self):
        session = _session([_bone('bone', np.arange(32))], 32)
        session.current_metric = pc_engine.PCMetric(
            f1=0.981, precision=0.981, recall=0.981,
            chamfer=0.01, score=0.981)
        session._high_overlap_no_f1_accepts = session.high_overlap_accept_patience
        result = session._step_small_exact_search()
        self.assertIsNone(result)

    def test_high_overlap_stops_when_no_candidate_can_raise_f1(self):
        broad = _bone('broad', np.arange(80))
        small = _bone('small', np.arange(80, 100))
        session = _session([small, broad], 100)
        baseline = pc_engine.PCMetric(
            f1=0.981, precision=0.981, recall=0.981,
            chamfer=0.01, score=0.981)
        session.current_metric = baseline
        session.best_f1 = baseline.f1
        session.step_count = 29
        session._pick_bone()
        session._bone_curriculum_no_gain = 2

        def fake_axis_probe(spec, tf_type, axis, sign, scale=1.0):
            after = np.identity(4)
            after[0, 3] = (1.0 if spec.name == 'broad' else 2.0) * float(sign) * float(scale)
            return pc_engine.PCProposal(
                spec.name, spec.kind, axis,
                np.identity(4), after, tf_type)

        session._proposal_for_spec = lambda *_args, **_kwargs: None
        session._axis_probe_proposal = fake_axis_probe
        session._screen_metric_for_points = lambda _points: baseline
        session._screen_metric_pair = lambda _fwd, _back: (baseline, baseline)

        def fake_probe_pair(forward, backward, _metric_fn):
            improved = pc_engine.PCMetric(
                f1=baseline.f1,
                precision=baseline.precision,
                recall=baseline.recall,
                chamfer=baseline.chamfer - 1e-5,
                score=baseline.score + 5e-6)
            return improved, baseline

        session._probe_metric_pair = fake_probe_pair

        result = session.step()
        self.assertIsNone(result)
        self.assertEqual(session.step_count, 29)


if __name__ == '__main__':
    unittest.main()
