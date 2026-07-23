# -*- coding: utf-8 -*-
"""pc_engine 变换类型调度（自适应先验权重）测试（纯 numpy，不依赖 bpy）。"""
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_engine.py"
spec = importlib.util.spec_from_file_location("_pc_engine_schedule_test", module_path)
pc_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_engine
spec.loader.exec_module(pc_engine)


def _all_unlocked():
    return {
        pc_engine.TF_ROTATION: (True, True, True),
        pc_engine.TF_SCALE: (True, True, True),
        pc_engine.TF_LOCATION: (True, True, True),
    }


class PCScheduleTests(unittest.TestCase):
    def test_initial_stage_is_rotation_only(self):
        sched = pc_engine.PCSchedule({
            pc_engine.TF_ROTATION: 0.7,
            pc_engine.TF_SCALE: 0.2,
            pc_engine.TF_LOCATION: 0.1,
        })
        w = sched.weights_for(_all_unlocked())
        self.assertEqual(w, {pc_engine.TF_ROTATION: 1.0})

    def test_stalled_phases_are_strictly_exclusive_then_joint(self):
        sched = pc_engine.PCSchedule({
            pc_engine.TF_ROTATION: 0.7,
            pc_engine.TF_SCALE: 0.2,
            pc_engine.TF_LOCATION: 0.1,
        }, plateau_delta=0.0001, plateau_checks=3)
        for _ in range(100):
            sched.update(pc_engine.TF_ROTATION, 0.0)
        self.assertEqual(sched.stage, 0)
        for value in (0.5, 0.50005, 0.50005, 0.50005):
            sched.observe_overlap(value)
        self.assertEqual(sched.stage, 1)
        self.assertEqual(sched.weights_for(_all_unlocked()),
                         {pc_engine.TF_SCALE: 1.0})
        for value in (0.6, 0.60001, 0.60001, 0.60001):
            sched.observe_overlap(value)
        self.assertEqual(sched.stage, 2)
        self.assertEqual(sched.weights_for(_all_unlocked()),
                         {pc_engine.TF_LOCATION: 1.0})
        for value in (0.7, 0.70001, 0.70001, 0.70001):
            sched.observe_overlap(value)
        self.assertEqual(sched.stage, 3)
        self.assertEqual(set(sched.weights_for(_all_unlocked())),
                         set(pc_engine.TF_TYPES))
        for value in (0.8, 0.80001, 0.80001, 0.80001):
            sched.observe_overlap(value)
        self.assertEqual(sched.stage, 4)
        self.assertEqual(sched.phase_name, '镜像联合微调')
        self.assertEqual(set(sched.weights_for(_all_unlocked())),
                         set(pc_engine.TF_TYPES))

    def test_weight_migration_when_rotation_stalls(self):
        sched = pc_engine.PCSchedule({
            pc_engine.TF_ROTATION: 0.7,
            pc_engine.TF_SCALE: 0.2,
            pc_engine.TF_LOCATION: 0.1,
        })
        # 进入最终联合阶段后，高收益类型应取得更高权重。
        for baseline in (0.5, 0.6, 0.7):
            for value in (baseline, baseline, baseline, baseline):
                sched.observe_overlap(value)
        for _ in range(100):
            sched.update(pc_engine.TF_SCALE, 0.5)
        w = sched.weights_for(_all_unlocked())
        self.assertGreater(w[pc_engine.TF_SCALE], w[pc_engine.TF_ROTATION])
        self.assertAlmostEqual(sum(w.values()), 1.0, places=9)

    def test_locked_types_filtered_out(self):
        sched = pc_engine.PCSchedule({
            pc_engine.TF_ROTATION: 0.7,
            pc_engine.TF_SCALE: 0.2,
            pc_engine.TF_LOCATION: 0.1,
        })
        # 骨骼锁定位移全部轴 + 缩放全部轴 -> 只剩旋转
        unlocked = {
            pc_engine.TF_ROTATION: (False, True, False),
            pc_engine.TF_SCALE: (False, False, False),
            pc_engine.TF_LOCATION: (False, False, False),
        }
        w = sched.weights_for(unlocked)
        self.assertEqual(set(w.keys()), {pc_engine.TF_ROTATION})
        self.assertAlmostEqual(w[pc_engine.TF_ROTATION], 1.0, places=9)

    def test_all_locked_returns_empty(self):
        sched = pc_engine.PCSchedule({
            pc_engine.TF_ROTATION: 0.7,
            pc_engine.TF_SCALE: 0.2,
            pc_engine.TF_LOCATION: 0.1,
        })
        locked = {
            pc_engine.TF_ROTATION: (False, False, False),
            pc_engine.TF_SCALE: (False, False, False),
            pc_engine.TF_LOCATION: (False, False, False),
        }
        self.assertEqual(sched.weights_for(locked), {})

    def test_negative_gain_penalizes_transform(self):
        sched = pc_engine.PCSchedule({pc_engine.TF_ROTATION: 1.0,
                                      pc_engine.TF_SCALE: 0.0,
                                      pc_engine.TF_LOCATION: 0.0})
        for _ in range(50):
            sched.update(pc_engine.TF_ROTATION, -1.0)
        self.assertLess(sched.ema_gain[pc_engine.TF_ROTATION], 0.0)

    def test_rejected_session_steps_do_not_advance_phase_plateau(self):
        points = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
                           [0.0, 1.0, 0.0]])
        bone = pc_engine.PCBoneSpec(
            'bone', True, 'deform', 'XYZ',
            (False, False, False), (False, False, False),
            (False, False, False), False,
            np.arange(3), np.ones(3))
        session = pc_engine.PCFitSession(
            bones=[bone], a_points=points, b_points=points.copy(),
            nn_a=pc_engine.brute_force_nn(points),
            config=pc_engine.PCFitConfig(
                minibatch_size=3, phase_eval_interval=2,
                phase_plateau_checks=1),
            apply_basis=lambda _name, _basis: None,
            read_samples=lambda: points.copy(),
            bone_point_provider=lambda _name, attr: (
                np.zeros(3) if attr == 'pivot' else np.identity(3)),
            tau=0.1, basis_map={'bone': np.identity(4)})
        for _ in range(20):
            session._observe_phase_convergence(accepted=False)
        self.assertEqual(session.schedule.stage, 0)
        self.assertEqual(session.schedule.phase_plateau_count, 0)


if __name__ == "__main__":
    unittest.main()
