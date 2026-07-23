# -*- coding: utf-8 -*-
"""Background worker history-view snapshot tests."""
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_worker.py"
spec = importlib.util.spec_from_file_location("_pc_worker_test", module_path)
pc_worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_worker
spec.loader.exec_module(pc_worker)


class _HistorySession:
    def __init__(self):
        self.step_count = 33
        self.current_metric = types.SimpleNamespace(f1=0.8, chamfer=0.1)
        self.best_step = 25
        self.best_f1 = 0.85
        self.view_step = 33

    def seek(self, step):
        self.view_step = step

    def _snapshot_state(self):
        return {'Bone': np.identity(4)}


class _CountingSession:
    def __init__(self):
        self.step_count = 0
        self.current_metric = types.SimpleNamespace(f1=0.0, chamfer=0.0)
        self.best_step = 0
        self.best_f1 = 0.0
        self.basis_map = {'Bone': np.identity(4)}

    def step(self):
        time.sleep(0.03)
        self.step_count += 1
        self.current_metric.f1 = float(self.step_count)
        self.best_step = self.step_count
        self.best_f1 = float(self.step_count)
        return types.SimpleNamespace(
            bone_name='Bone',
            tf_type='rotation',
            accepted=True,
            axis=0,
            delta_components=(0.0, 0.0, 0.0),
            linked_count=0,
            f1_delta=1.0,
            chamfer_delta=0.0,
            reward=1.0,
            score_delta=0.0,
            applied_names=('Bone',),
        )

    def _snapshot_state(self):
        return {'Bone': self.basis_map['Bone'].copy()}


class WorkerHistoryTests(unittest.TestCase):
    def test_seek_reports_view_step_without_truncating_total_history(self):
        session = _HistorySession()
        worker = pc_worker.PCWorkerController(session)
        result = worker.seek(20)
        self.assertEqual(result.step, 20)
        self.assertEqual(session.view_step, 20)
        self.assertEqual(session.step_count, 33)

    def test_latest_result_merges_changed_bases_from_drained_queue(self):
        session = _HistorySession()
        worker = pc_worker.PCWorkerController(session)
        a = np.identity(4)
        b = np.identity(4)
        b[0, 3] = 2.0
        worker._push_latest(pc_worker.PCWorkerResult(
            step=10,
            metric_f1=0.8,
            metric_chamfer=0.1,
            best_step=10,
            best_f1=0.8,
            changed_bases={'BoneA': a},
        ))
        worker._push_latest(pc_worker.PCWorkerResult(
            step=11,
            metric_f1=0.81,
            metric_chamfer=0.09,
            best_step=11,
            best_f1=0.81,
            changed_bases={'BoneB': b},
        ))

        result = worker.latest_result()

        self.assertEqual(result.step, 11)
        self.assertIn('BoneA', result.changed_bases)
        self.assertIn('BoneB', result.changed_bases)
        np.testing.assert_allclose(result.changed_bases['BoneB'], b)

    def test_stop_timeout_keeps_live_thread_reference(self):
        session = _HistorySession()
        worker = pc_worker.PCWorkerController(session)

        class _FakeThread:
            def join(self, timeout=None):
                return

            def is_alive(self):
                return True

        worker._thread = _FakeThread()

        stopped = worker.stop()

        self.assertFalse(stopped)
        self.assertTrue(worker.is_alive())
        self.assertIsNotNone(worker.error)

    def test_pause_and_wait_stops_additional_chunks(self):
        session = _CountingSession()
        worker = pc_worker.PCWorkerController(session, steps_per_chunk=2)
        worker.start()
        deadline = time.time() + 2.0
        while session.step_count == 0 and time.time() < deadline:
            time.sleep(0.01)

        self.assertTrue(worker.pause_and_wait(timeout=2.0))
        count_after_pause = session.step_count
        time.sleep(0.12)
        self.assertEqual(session.step_count, count_after_pause)
        worker.stop()


if __name__ == '__main__':
    unittest.main()
