# -*- coding: utf-8 -*-
"""pc_engine 迭代历史（快照+增量 seek、截断、最佳步、压缩）测试（纯 numpy，不依赖 bpy）。"""
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_engine.py"
spec = importlib.util.spec_from_file_location("_pc_engine_history_test", module_path)
pc_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_engine
spec.loader.exec_module(pc_engine)


class _FakeRig:
    """模拟两根骨骼对点云做刚体变换的假骨架（测试用）。"""

    def __init__(self, b_points: np.ndarray, influence_a: np.ndarray, influence_b: np.ndarray):
        self.b_points = b_points.copy()
        self.rest = b_points.copy()
        self.influence = {'BoneA': influence_a, 'BoneB': influence_b}
        self.basis = {'BoneA': np.identity(4), 'BoneB': np.identity(4)}
        self.pivots = {'BoneA': np.array([0.0, 0.0, 0.0]),
                       'BoneB': np.array([1.0, 0.0, 0.0])}

    def apply_basis(self, name: str, basis: np.ndarray) -> None:
        self.basis[name] = basis.copy()
        # 重算点云：受影响点做 basis 变换（绕 pivot）
        idx = self.influence[name]
        pivot = self.pivots[name]
        pts = self.rest[idx]
        m = basis
        self.b_points[idx] = (pts - pivot) @ m[:3, :3].T + pivot + m[:3, 3]

    def read_samples(self) -> np.ndarray:
        return self.b_points.copy()

    def provider(self, name: str, attr: str) -> np.ndarray:
        if attr == 'pivot':
            return self.pivots[name]
        return np.identity(3)


def _make_session(rig: _FakeRig, a_points: np.ndarray, cfg=None, seed=0):
    idx_a = rig.influence['BoneA']
    idx_b = rig.influence['BoneB']
    bones = [
        pc_engine.PCBoneSpec(name='BoneA', enabled=True, kind='deform', rotation_mode='XYZ',
                             lock_rotation=(False, False, False), lock_scale=(False, False, False),
                             lock_location=(False, False, False), has_constraints=False,
                             influence_indices=idx_a, influence_weights=np.ones(len(idx_a))),
        pc_engine.PCBoneSpec(name='BoneB', enabled=True, kind='deform', rotation_mode='XYZ',
                             lock_rotation=(False, False, False), lock_scale=(False, False, False),
                             lock_location=(False, False, False), has_constraints=False,
                             influence_indices=idx_b, influence_weights=np.ones(len(idx_b))),
    ]
    cfg = cfg or pc_engine.PCFitConfig(seed=seed, snapshot_interval=10)
    session = pc_engine.PCFitSession(
        bones=bones, a_points=a_points, b_points=rig.read_samples(),
        nn_a=pc_engine.brute_force_nn(a_points),
        config=cfg, apply_basis=rig.apply_basis, read_samples=rig.read_samples,
        bone_point_provider=rig.provider,
        tau=cfg.threshold if cfg.threshold > 0 else 0.2,
        basis_map={k: v.copy() for k, v in rig.basis.items()},
    )
    return session


def _run_steps(session, n):
    results = []
    for _ in range(n):
        r = session.step()
        if r is not None:
            results.append(r)
    return results


class HistorySeekTests(unittest.TestCase):
    def test_seek_skips_disabled_bone_missing_from_basis_map(self):
        points = np.zeros((2, 3))
        applied = []
        bones = [
            pc_engine.PCBoneSpec(
                name='BoneA', enabled=True, kind='deform', rotation_mode='XYZ',
                lock_rotation=(False, False, False),
                lock_scale=(False, False, False),
                lock_location=(False, False, False), has_constraints=False,
                influence_indices=np.array([0]), influence_weights=np.ones(1)),
            pc_engine.PCBoneSpec(
                name='*dummy*ankle.L', enabled=False, kind='controller',
                rotation_mode='XYZ', lock_rotation=(False, False, False),
                lock_scale=(False, False, False),
                lock_location=(False, False, False), has_constraints=False,
                influence_indices=np.array([], dtype=np.int64),
                influence_weights=np.array([], dtype=np.float64)),
        ]
        session = pc_engine.PCFitSession(
            bones=bones, a_points=points, b_points=points.copy(),
            nn_a=pc_engine.brute_force_nn(points),
            config=pc_engine.PCFitConfig(seed=0),
            apply_basis=lambda name, _basis: applied.append(name),
            read_samples=lambda: points.copy(),
            bone_point_provider=lambda _name, attr: (
                np.zeros(3) if attr == 'pivot' else np.identity(3)),
            tau=0.2, basis_map={'BoneA': np.identity(4)},
        )

        session.seek(0)

        self.assertEqual(applied, ['BoneA'])
        self.assertNotIn('*dummy*ankle.L', session.basis_map)

    def test_seek_reproduces_step_state(self):
        rng = np.random.default_rng(1)
        a = rng.normal(size=(24, 3)) * 0.5
        b = a + np.array([0.4, 0.2, 0.1])  # 初始有位移偏差
        rig = _FakeRig(b, np.arange(12), np.arange(12, 24))
        session = _make_session(rig, a, cfg=pc_engine.PCFitConfig(seed=3, snapshot_interval=10))

        _run_steps(session, 60)
        # 记录第 30 步各骨 basis
        target_step = 30
        state_30 = session._state_at(target_step)

        session.seek(target_step)
        for name in state_30:
            np.testing.assert_allclose(session.basis_map[name], state_30[name], atol=1e-10)
        self.assertEqual(session.step_count, 60)  # seek 不改变步数

    def test_seek_zero_returns_rest_pose(self):
        rng = np.random.default_rng(2)
        a = rng.normal(size=(16, 3))
        b = a + 0.3
        rig = _FakeRig(b, np.arange(8), np.arange(8, 16))
        session = _make_session(rig, a)
        rest_points = rig.read_samples().copy()

        _run_steps(session, 25)
        session.seek(0)
        np.testing.assert_allclose(rig.read_samples(), rest_points, atol=1e-10)
        for name in session.basis_map:
            np.testing.assert_allclose(session.basis_map[name], np.identity(4), atol=1e-10)

    def test_truncate_after_and_branch(self):
        rng = np.random.default_rng(4)
        a = rng.normal(size=(16, 3))
        b = a + 0.3
        rig = _FakeRig(b, np.arange(8), np.arange(8, 16))
        session = _make_session(rig, a)

        _run_steps(session, 50)
        total = session.history_total()
        self.assertEqual(total, 50)

        session.seek(20)
        session.truncate_after(20)
        self.assertEqual(session.history_total(), 20)
        self.assertTrue(all(d[0] <= 20 for d in session.deltas))

        # 从截断点继续迭代，步号应接续
        _run_steps(session, 10)
        self.assertEqual(session.history_total(), 30)


class BestStepTests(unittest.TestCase):
    def test_best_step_recorded_and_jump(self):
        rng = np.random.default_rng(5)
        a = rng.normal(size=(16, 3)) * 0.4
        b = a + np.array([0.5, 0.0, 0.0])
        rig = _FakeRig(b, np.arange(8), np.arange(8, 16))
        session = _make_session(rig, a)
        session.schedule.stage = 2

        _run_steps(session, 80)
        self.assertGreater(session.best_step, 0)
        self.assertGreaterEqual(session.best_f1, session.current_metric.f1 - 1e-9)

        # 偏离后再跳回最佳
        session.seek(session.history_total())
        step = session.jump_to_best()
        self.assertEqual(step, session.best_step)

    def test_worse_full_eval_does_not_override_best_snapshot(self):
        points = np.zeros((4, 3))
        basis0 = np.identity(4)
        session = pc_engine.PCFitSession(
            bones=[pc_engine.PCBoneSpec(
                name='Bone', enabled=True, kind='deform', rotation_mode='XYZ',
                lock_rotation=(False, False, False),
                lock_scale=(False, False, False),
                lock_location=(False, False, False), has_constraints=False,
                influence_indices=np.arange(4), influence_weights=np.ones(4))],
            a_points=points,
            b_points=points.copy(),
            nn_a=pc_engine.brute_force_nn(points),
            config=pc_engine.PCFitConfig(seed=0, full_eval_interval=2),
            apply_basis=lambda _name, _basis: None,
            read_samples=lambda: points.copy(),
            bone_point_provider=lambda _name, attr: (
                np.zeros(3) if attr == 'pivot' else np.identity(3)),
            tau=0.1,
            basis_map={'Bone': basis0.copy()},
        )
        session.best_f1 = -1.0
        session.best_step = 0
        session.best_snapshot = session._snapshot_state()

        proposal = pc_engine.PCProposal(
            bone_name='Bone',
            kind='deform',
            axis=0,
            basis_before=basis0.copy(),
            basis_after=basis0.copy(),
            tf_type=pc_engine.TF_LOCATION,
        )
        metrics = [
            pc_engine.PCMetric(
                f1=0.80, precision=0.80, recall=0.80, chamfer=0.10, score=0.80),
            pc_engine.PCMetric(
                f1=0.70, precision=0.70, recall=0.70, chamfer=0.20, score=0.70),
        ]

        def fake_propose():
            return proposal

        def fake_step_exact(_proposal):
            metric = metrics[session.step_count - 1]
            basis = np.identity(4)
            basis[0, 3] = float(session.step_count)
            session.basis_map['Bone'] = basis
            session.current_metric = metric
            if metric.f1 > session.best_f1 + pc_engine.EPS:
                session.best_f1 = metric.f1
                session.best_step = session.step_count
                session.best_snapshot = session._snapshot_state()
            return pc_engine.PCStepResult(
                step=session.step_count,
                accepted=True,
                bone_name='Bone',
                tf_type=pc_engine.TF_LOCATION,
                metric=metric,
                axis=0,
                reward=metric.f1,
            )

        session.propose = fake_propose
        session._step_exact = fake_step_exact

        self.assertIsNotNone(session.step())
        best_snapshot = session.best_snapshot['Bone'].copy()
        self.assertEqual(session.best_step, 1)
        self.assertAlmostEqual(best_snapshot[0, 3], 1.0)

        self.assertIsNotNone(session.step())
        self.assertEqual(session.best_step, 1)
        np.testing.assert_allclose(session.best_snapshot['Bone'], best_snapshot)


class HistoryCompactionTests(unittest.TestCase):
    def test_compaction_over_max_history(self):
        rng = np.random.default_rng(6)
        a = rng.normal(size=(12, 3))
        b = a + 0.2
        rig = _FakeRig(b, np.arange(6), np.arange(6, 12))
        cfg = pc_engine.PCFitConfig(seed=6, snapshot_interval=10, max_history_steps=1000)
        session = _make_session(rig, a, cfg=cfg)

        _run_steps(session, 2100)
        self.assertEqual(session.history_total(), 2100)
        # 压缩后增量数量应显著减少
        self.assertLess(len(session.deltas), 2100)
        # seek 到压缩边界之前的步应回退到最近快照
        session.seek(500)
        # 快照点可正常 seek
        session.seek(1050)
        state = session._state_at(1050)
        self.assertIn('BoneA', state)

    def test_non_snapshot_before_history_floor_raises(self):
        points = np.zeros((4, 3))
        basis0 = np.identity(4)
        session = pc_engine.PCFitSession(
            bones=[pc_engine.PCBoneSpec(
                name='Bone', enabled=True, kind='deform', rotation_mode='XYZ',
                lock_rotation=(False, False, False),
                lock_scale=(False, False, False),
                lock_location=(False, False, False), has_constraints=False,
                influence_indices=np.arange(4), influence_weights=np.ones(4))],
            a_points=points,
            b_points=points.copy(),
            nn_a=pc_engine.brute_force_nn(points),
            config=pc_engine.PCFitConfig(
                seed=0, snapshot_interval=100, max_history_steps=3),
            apply_basis=lambda _name, _basis: None,
            read_samples=lambda: points.copy(),
            bone_point_provider=lambda _name, attr: (
                np.zeros(3) if attr == 'pivot' else np.identity(3)),
            tau=0.1,
            basis_map={'Bone': basis0.copy()},
        )

        proposal = pc_engine.PCProposal(
            bone_name='Bone',
            kind='deform',
            axis=0,
            basis_before=basis0.copy(),
            basis_after=basis0.copy(),
            tf_type=pc_engine.TF_LOCATION,
        )

        def fake_propose():
            return proposal

        def fake_step_exact(_proposal):
            proposal.basis_before = session.basis_map['Bone'].copy()
            basis = np.identity(4)
            basis[0, 3] = float(session.step_count)
            proposal.basis_after = basis
            session.basis_map['Bone'] = basis.copy()
            session._commit_proposal_group(proposal, already_applied=True)
            metric = pc_engine.PCMetric(
                f1=float(session.step_count),
                precision=1.0,
                recall=1.0,
                chamfer=0.0,
                score=float(session.step_count),
            )
            session.current_metric = metric
            return pc_engine.PCStepResult(
                step=session.step_count,
                accepted=True,
                bone_name='Bone',
                tf_type=pc_engine.TF_LOCATION,
                metric=metric,
                axis=0,
                reward=metric.f1,
            )

        session.propose = fake_propose
        session._step_exact = fake_step_exact

        for _ in range(6):
            self.assertIsNotNone(session.step())

        self.assertGreater(session._history_floor_step, 0)
        with self.assertRaises(ValueError):
            session.seek(session._history_floor_step - 1)


if __name__ == "__main__":
    unittest.main()
