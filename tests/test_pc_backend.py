# -*- coding: utf-8 -*-
"""pc_backend 计算后端测试：最近邻正确性、分块边界、后端选择回退逻辑。"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "pc_backend.py"
spec = importlib.util.spec_from_file_location("_pc_backend_test", module_path)
pc_backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc_backend
spec.loader.exec_module(pc_backend)


def _brute(ref: np.ndarray, query: np.ndarray):
    d2 = ((query[:, None, :] - ref[None, :, :]) ** 2).sum(-1)
    idx = d2.argmin(axis=1)
    return np.sqrt(d2[np.arange(len(query)), idx]), idx


class NumpyBackendNearestTests(unittest.TestCase):
    def test_matches_brute_force(self):
        for seed in (0, 1, 2):
            rng = np.random.default_rng(seed)
            ref = rng.normal(size=(300, 3)) * 2.0
            query = rng.normal(size=(97, 3))
            backend = pc_backend.NumpyBackend()
            d, i = backend.nearest(ref, query)
            d_ref, i_ref = _brute(ref, query)
            np.testing.assert_allclose(d, d_ref, atol=1e-4)
            np.testing.assert_array_equal(i, i_ref)

    def test_chunk_boundaries(self):
        rng = np.random.default_rng(7)
        ref = rng.normal(size=(128, 3))
        query = rng.normal(size=(25, 3))
        backend = pc_backend.NumpyBackend(chunk=10)  # 强制 3 个分块
        d, i = backend.nearest(ref, query)
        d_ref, i_ref = _brute(ref, query)
        np.testing.assert_allclose(d, d_ref, atol=1e-4)
        np.testing.assert_array_equal(i, i_ref)

    def test_empty_inputs(self):
        backend = pc_backend.NumpyBackend()
        d, i = backend.nearest(np.zeros((0, 3)), np.zeros((4, 3)))
        self.assertTrue(np.isinf(d).all())
        self.assertTrue((i == -1).all())
        d, i = backend.nearest(np.zeros((4, 3)), np.zeros((0, 3)))
        self.assertEqual(len(d), 0)
        self.assertEqual(len(i), 0)

    def test_self_nearest_distance_zero(self):
        # float32 GEMM 消去误差 ~1e-3：距离只需足够小，
        # 关键是每个点都正确找到自己（配对下标必须精确）
        rng = np.random.default_rng(3)
        pts = rng.normal(size=(50, 3))
        backend = pc_backend.NumpyBackend()
        d, i = backend.nearest(pts, pts)
        np.testing.assert_array_less(d, np.full(50, 1e-2))
        np.testing.assert_array_equal(i, np.arange(50))

    def test_array_fingerprint_tracks_content_not_address(self):
        a = np.arange(12, dtype=np.float32).reshape(4, 3)
        b = a.copy()
        self.assertEqual(
            pc_backend._array_fingerprint(a),
            pc_backend._array_fingerprint(b))
        b[0, 0] += 1.0
        self.assertNotEqual(
            pc_backend._array_fingerprint(a),
            pc_backend._array_fingerprint(b))


class SelectBackendTests(unittest.TestCase):
    def test_numpy_mode(self):
        backend, info = pc_backend.select_backend('NUMPY')
        self.assertIsInstance(backend, pc_backend.NumpyBackend)
        self.assertFalse(backend.is_gpu)

    def test_auto_falls_back_to_numpy_without_torch(self):
        # venv 无 torch：AUTO 必须回退 numpy 且不抛异常
        # （隔离本机系统 Python 里的真实 torch：候选路径置空）
        sys.modules.pop('torch', None)
        old_candidates = pc_backend._candidate_torch_paths
        pc_backend._candidate_torch_paths = lambda: []
        try:
            backend, info = pc_backend.select_backend('AUTO')
        finally:
            pc_backend._candidate_torch_paths = old_candidates
        self.assertIsInstance(backend, pc_backend.NumpyBackend)

    def test_auto_does_not_probe_external_python_packages(self):
        sys.modules.pop('torch', None)
        calls = []
        old_candidates = pc_backend._candidate_torch_paths
        pc_backend._candidate_torch_paths = lambda: calls.append(True) or []
        try:
            backend, info = pc_backend.select_backend('AUTO')
        finally:
            pc_backend._candidate_torch_paths = old_candidates
        self.assertIsInstance(backend, pc_backend.NumpyBackend)
        self.assertEqual(calls, [])
        self.assertEqual(info, backend.name)

    def test_torch_mode_unavailable_falls_back(self):
        sys.modules.pop('torch', None)
        old_candidates = pc_backend._candidate_torch_paths
        pc_backend._candidate_torch_paths = lambda: []
        try:
            backend, info = pc_backend.select_backend('TORCH')
        finally:
            pc_backend._candidate_torch_paths = old_candidates
        self.assertIsInstance(backend, pc_backend.NumpyBackend)
        self.assertIn('numpy', info)

    def test_fake_torch_with_cuda_selected(self):
        fake_torch = types.ModuleType('torch')
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: True)
        fake_torch.device = lambda name: name
        sys.modules['torch'] = fake_torch
        try:
            backend, info = pc_backend.select_backend('AUTO')
            self.assertTrue(backend.is_gpu)
            self.assertIn('CUDA', info)
        finally:
            sys.modules.pop('torch', None)


if __name__ == "__main__":
    unittest.main()
