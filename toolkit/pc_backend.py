# -*- coding: utf-8 -*-
"""Point-cloud nearest-neighbor backends.

NumPy backend stays as the CPU fallback. Torch backend uses CUDA when
available and caches static references on device.
"""

import ctypes
import importlib.util
import os
import pickle
import struct
import subprocess
import sys
import hashlib
import threading
from typing import Callable, Dict, List, Tuple

import numpy as np

try:
    from scipy.spatial import cKDTree as _SciPyKDTree
except Exception:
    _SciPyKDTree = None

NNResult = Tuple[np.ndarray, np.ndarray]


def _candidate_torch_paths() -> List[str]:
    paths: List[str] = []
    env = os.environ.get('PC_TORCH_SITE_PACKAGES', '')
    if env:
        paths.extend(p for p in env.split(os.pathsep) if p)
    for root in (
        r'C:\Python311\Lib\site-packages',
        r'C:\Python312\Lib\site-packages',
        r'C:\Python310\Lib\site-packages',
        r'C:\Python313\Lib\site-packages',
    ):
        paths.append(root)
    return paths


def _candidate_torch_python_exes() -> List[str]:
    paths: List[str] = []
    env = os.environ.get('PC_TORCH_PYTHON', '')
    if env:
        paths.extend(p for p in env.split(os.pathsep) if p)
    for exe in (
        r'C:\Python311\python.exe',
        r'C:\Python312\python.exe',
        r'C:\Python310\python.exe',
        r'C:\Python313\python.exe',
    ):
        paths.append(exe)
    ordered: List[str] = []
    seen = set()
    for path in paths:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        seen.add(norm)
        ordered.append(path)
    return ordered


_last_torch_errors: List[str] = []
_dll_dir_handles: List[object] = []


def _pack_value(value):
    if isinstance(value, np.ndarray):
        arr = np.ascontiguousarray(value)
        return {
            "__ndarray__": True,
            "dtype": arr.dtype.str,
            "shape": arr.shape,
            "data": arr.tobytes(),
        }
    if isinstance(value, dict):
        return {key: _pack_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_pack_value(item) for item in value]
    return value


def _unpack_value(value):
    if isinstance(value, dict) and value.get("__ndarray__"):
        arr = np.frombuffer(value["data"], dtype=np.dtype(value["dtype"]))
        return arr.reshape(tuple(value["shape"])).copy()
    if isinstance(value, dict):
        return {key: _unpack_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_unpack_value(item) for item in value]
    return value


def _purge_torch_modules() -> None:
    for name in [m for m in list(sys.modules) if m == 'torch' or m.startswith('torch.')]:
        sys.modules.pop(name, None)


def _array_fingerprint(arr: np.ndarray) -> bytes:
    view = memoryview(np.ascontiguousarray(arr)).cast('B')
    return hashlib.blake2b(view, digest_size=16).digest()


def _is_blender_runtime() -> bool:
    exe = os.path.basename(sys.executable).lower()
    prefix = os.path.normcase(os.path.abspath(sys.prefix)).lower()
    return ('blender' in exe or 'blender' in prefix)


def _candidate_dll_dirs(site_packages: str) -> List[str]:
    ext_site = os.path.abspath(site_packages) if site_packages else ''
    ext_python = (os.path.abspath(os.path.join(ext_site, os.pardir, os.pardir))
                  if ext_site else '')
    dirs: List[str] = []
    for path in (
        site_packages,
        os.path.join(site_packages, 'torch'),
        os.path.join(site_packages, 'torch', 'lib'),
        ext_python,
        os.path.join(ext_python, 'DLLs'),
        sys.prefix,
        os.path.join(sys.prefix, 'DLLs'),
        os.path.dirname(sys.executable),
        r'C:\Windows\System32',
        os.environ.get('CUDA_PATH', ''),
        os.path.join(os.environ.get('CUDA_PATH', ''), 'bin'),
    ):
        if path and os.path.isdir(path):
            dirs.append(path)
    ordered: List[str] = []
    seen = set()
    for path in dirs:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        seen.add(norm)
        ordered.append(path)
    return ordered


def _register_dll_dirs(site_packages: str) -> None:
    global _dll_dir_handles
    for path in _candidate_dll_dirs(site_packages):
        try:
            _dll_dir_handles.append(os.add_dll_directory(path))
        except (AttributeError, FileNotFoundError, OSError):
            pass
    for candidate in (
        os.path.join(r'C:\Windows\System32', 'libomp140.x86_64.dll'),
        os.path.join(site_packages, 'torch', 'lib', 'libomp140.x86_64.dll'),
    ):
        if os.path.isfile(candidate):
            try:
                ctypes.CDLL(candidate)
                break
            except OSError:
                continue


def _import_bundled_torch() -> object:
    if 'torch' in sys.modules:
        loaded = sys.modules['torch']
        if _is_blender_runtime() or getattr(loaded, '__file__', None) is None:
            return loaded
        raise ImportError('ignore system torch outside Blender runtime')
    if not _is_blender_runtime():
        raise ImportError('Blender bundled torch is only probed inside Blender runtime')
    try:
        spec = importlib.util.find_spec('torch')
    except (ImportError, ValueError):
        spec = None
    origin = getattr(spec, 'origin', None)
    if not origin:
        raise ImportError('Blender Python 未安装 torch')
    prefix = os.path.normcase(os.path.abspath(sys.prefix))
    module_path = os.path.normcase(os.path.abspath(origin))
    try:
        bundled = os.path.commonpath((prefix, module_path)) == prefix
    except ValueError:
        bundled = False
    if not bundled:
        raise ImportError('忽略 Blender Python 隔离的系统 torch')
    import torch
    return torch


def _import_torch_with_fallback(allow_external: bool = True) -> Tuple[object, str]:
    global _last_torch_errors
    _last_torch_errors = []
    try:
        return _import_bundled_torch(), 'bundled'
    except Exception as exc:
        _last_torch_errors.append(f'direct: {exc!r}')
    if not allow_external:
        raise ImportError(_last_torch_errors[-1])
    for path in _candidate_torch_paths():
        if not path or not os.path.isdir(path):
            continue
        if path not in sys.path:
            sys.path.insert(0, path)
        _register_dll_dirs(path)
        _purge_torch_modules()
        try:
            import torch
            return torch, f'system:{path}'
        except Exception as exc:
            _last_torch_errors.append(f'{path}: {exc!r}')
    raise ImportError(' | '.join(_last_torch_errors) or 'no torch found')


class NumpyBackend:
    """CPU fallback using SciPy cKDTree when available, else chunked GEMM."""

    is_gpu: bool = False

    def __init__(self, chunk: int = 2048) -> None:
        self.chunk: int = max(64, int(chunk))
        self.name: str = (
            "numpy + scipy cKDTree (CPU)"
            if _SciPyKDTree is not None else
            "numpy (CPU BLAS)"
        )

    def _tree_query(self, tree, query: np.ndarray) -> NNResult:
        q32 = np.ascontiguousarray(query, dtype=np.float32)
        n_q = q32.shape[0]
        if n_q == 0:
            return (np.zeros(0, dtype=np.float64),
                    np.zeros(0, dtype=np.int64))
        try:
            dist, idx = tree.query(q32, k=1, workers=-1)
        except TypeError:
            dist, idx = tree.query(q32, k=1)
        return (np.asarray(dist, dtype=np.float64),
                np.asarray(idx, dtype=np.int64))

    def _gemm_nearest(self, ref32: np.ndarray, query: np.ndarray) -> NNResult:
        q32 = np.ascontiguousarray(query, dtype=np.float32)
        n_ref = ref32.shape[0]
        n_q = q32.shape[0]
        if n_ref == 0 or n_q == 0:
            return (np.full(n_q, np.inf), np.full(n_q, -1, dtype=np.int64))
        r2 = np.einsum('ij,ij->i', ref32, ref32)
        out_d = np.empty(n_q, dtype=np.float64)
        out_i = np.empty(n_q, dtype=np.int64)
        for start in range(0, n_q, self.chunk):
            q = q32[start:start + self.chunk]
            q2 = np.einsum('ij,ij->i', q, q)
            d2 = q2[:, None] + r2[None, :] - 2.0 * (q @ ref32.T)
            idx = np.argmin(d2, axis=1)
            dmin = d2[np.arange(len(q)), idx]
            out_d[start:start + len(q)] = np.sqrt(
                np.maximum(dmin, 0.0), dtype=np.float64)
            out_i[start:start + len(q)] = idx
        return out_d, out_i

    def warmup(self) -> None:
        return

    def nearest_provider(self, ref: np.ndarray) -> Callable[[np.ndarray], NNResult]:
        ref32 = np.ascontiguousarray(ref, dtype=np.float32)
        if _SciPyKDTree is not None and len(ref32) > 0:
            tree = _SciPyKDTree(ref32)
            return lambda query, tree=tree, backend=self: backend._tree_query(tree, query)
        return lambda query, ref=ref32, backend=self: backend._gemm_nearest(ref, query)

    def nearest(self, ref: np.ndarray, query: np.ndarray) -> NNResult:
        ref32 = np.ascontiguousarray(ref, dtype=np.float32)
        if _SciPyKDTree is not None and len(ref32) > 0:
            return self._tree_query(_SciPyKDTree(ref32), query)
        return self._gemm_nearest(ref32, query)

    def nearest_transient(self, ref: np.ndarray, query: np.ndarray) -> NNResult:
        return self.nearest(ref, query)


class TorchBackend:
    """CUDA backend with cached reference tensors."""

    name: str = "torch (CUDA GPU)"
    is_gpu: bool = True

    def __init__(self, chunk: int = 2048, allow_external: bool = True) -> None:
        torch, self._source = _import_torch_with_fallback(allow_external)
        if not torch.cuda.is_available():
            raise RuntimeError("torch 已安装但 CUDA 不可用")
        self._torch = torch
        self._device = torch.device('cuda')
        self.chunk: int = max(64, int(chunk))
        self._ref_cache: Dict[Tuple[Tuple[int, ...], str, Tuple[int, ...], bytes], object] = {}
        self.warmup()

    def warmup(self) -> None:
        torch = self._torch
        if not all(hasattr(torch, attr) for attr in ('inference_mode', 'zeros', 'ones')):
            return
        with torch.inference_mode():
            ref = torch.zeros((16, 3), dtype=torch.float32, device=self._device)
            query = torch.ones((16, 3), dtype=torch.float32, device=self._device)
            _ = self._nearest_torch(ref, query)
            if hasattr(torch.cuda, 'synchronize'):
                torch.cuda.synchronize()

    def nearest_provider(self, ref: np.ndarray) -> Callable[[np.ndarray], NNResult]:
        return lambda query, ref=ref, backend=self: backend.nearest(ref, query)

    def _cache_key(self, ref: np.ndarray) -> Tuple[Tuple[int, ...], str, Tuple[int, ...], bytes]:
        arr = np.asarray(ref)
        return (
            tuple(arr.shape),
            arr.dtype.str,
            tuple(int(s) for s in arr.strides),
            _array_fingerprint(arr),
        )

    def _reference_tensor(self, ref: np.ndarray):
        torch = self._torch
        key = self._cache_key(ref)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        arr = np.ascontiguousarray(ref, dtype=np.float32)
        tensor = torch.as_tensor(arr, device=self._device)
        self._ref_cache[key] = tensor
        return tensor

    def _nearest_torch(self, ref, query) -> NNResult:
        torch = self._torch
        n_q = int(query.shape[0])
        if int(ref.shape[0]) == 0 or n_q == 0:
            return (np.full(n_q, np.inf), np.full(n_q, -1, dtype=np.int64))
        out_d = np.empty(n_q, dtype=np.float64)
        out_i = np.empty(n_q, dtype=np.int64)
        with torch.inference_mode():
            for start in range(0, n_q, self.chunk):
                q = query[start:start + self.chunk]
                q2 = (q * q).sum(dim=1, keepdim=True)
                r2 = (ref * ref).sum(dim=1).unsqueeze(0)
                d2 = q2 + r2 - 2.0 * (q @ ref.T)
                dmin, idx = d2.min(dim=1)
                q_len = int(q.shape[0])
                out_d[start:start + q_len] = np.sqrt(
                    np.maximum(dmin.detach().cpu().numpy(), 0.0),
                    dtype=np.float64,
                )
                out_i[start:start + q_len] = idx.detach().cpu().numpy().astype(np.int64)
        return out_d, out_i

    def nearest(self, ref: np.ndarray, query: np.ndarray) -> NNResult:
        torch = self._torch
        n_q = query.shape[0]
        if ref.shape[0] == 0 or n_q == 0:
            return (np.full(n_q, np.inf), np.full(n_q, -1, dtype=np.int64))
        q_np = np.ascontiguousarray(query, dtype=np.float32)
        q = torch.as_tensor(q_np, device=self._device)
        r = self._reference_tensor(ref)
        return self._nearest_torch(r, q)

    def nearest_transient(self, ref: np.ndarray, query: np.ndarray) -> NNResult:
        return self.nearest(ref, query)


class ExternalTorchBackend:
    """CUDA backend hosted in an external Python process."""

    name: str = "torch external (CUDA GPU)"
    is_gpu: bool = True

    def __init__(
            self, python_exe: str, chunk: int = 8192,
            compute_dtype: str = 'float32') -> None:
        self.chunk: int = max(64, int(chunk))
        self.compute_dtype = 'float16' if str(
            compute_dtype).lower() == 'float16' else 'float32'
        self._python_exe = python_exe
        self._proc = self._spawn_worker(python_exe)
        self._ref_cache: Dict[
            Tuple[Tuple[int, ...], str, Tuple[int, ...], bytes], str] = {}
        self._transient_ref_seq: int = 0
        self._rpc_lock = threading.RLock()
        self.warmup()

    def _spawn_worker(self, python_exe: str):
        worker = os.path.join(
            os.path.dirname(__file__), 'pc_ext_torch_worker.py')
        env = os.environ.copy()
        env.pop('PYTHONHOME', None)
        env.pop('PYTHONPATH', None)
        ext_python = os.path.dirname(os.path.abspath(python_exe))
        ext_site = os.path.join(ext_python, 'Lib', 'site-packages')
        if os.path.isdir(ext_site):
            env['PYTHONPATH'] = ext_site
        proc = subprocess.Popen(
            [python_exe, '-u', worker],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            hello = self._recv(proc)
        except Exception:
            proc.kill()
            raise
        if not hello.get('ok'):
            proc.kill()
            raise RuntimeError(
                hello.get('error', 'external torch worker failed'))
        return proc

    @staticmethod
    def _send(proc, obj) -> None:
        assert proc.stdin is not None
        payload = pickle.dumps(_pack_value(obj), protocol=5)
        proc.stdin.write(struct.pack('<I', len(payload)))
        proc.stdin.write(payload)
        proc.stdin.flush()

    @staticmethod
    def _read_exact(stream, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = stream.read(size - len(data))
            if not chunk:
                raise EOFError('unexpected EOF from external torch worker')
            data.extend(chunk)
        return bytes(data)

    @classmethod
    def _recv(cls, proc):
        assert proc.stdout is not None
        header = proc.stdout.read(4)
        if not header:
            stderr = ''
            if proc.stderr is not None:
                try:
                    stderr = proc.stderr.read().decode(
                        'utf-8', errors='ignore')
                except Exception:
                    stderr = ''
            raise RuntimeError(f'external torch worker exited: {stderr}')
        size = struct.unpack('<I', header)[0]
        payload = cls._read_exact(proc.stdout, size)
        out = _unpack_value(pickle.loads(payload))
        if not out.get('ok', False):
            raise RuntimeError(
                out.get('error', 'external torch worker error'))
        return out

    def close(self) -> None:
        proc = getattr(self, '_proc', None)
        if proc is None:
            return
        try:
            self._send(proc, {'op': 'close'})
            self._recv(proc)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        self._proc = None

    def _restart_worker(self) -> None:
        try:
            self.close()
        finally:
            self._proc = self._spawn_worker(self._python_exe)
            self._ref_cache.clear()

    def _call_worker(self, payload, retry: bool = True):
        with self._rpc_lock:
            try:
                self._send(self._proc, payload)
                return self._recv(self._proc)
            except Exception:
                if not retry:
                    raise
                self._restart_worker()
                self._send(self._proc, payload)
                return self._recv(self._proc)

    def __del__(self):
        self.close()

    def warmup(self) -> None:
        return

    def _cache_key(self, ref: np.ndarray) -> Tuple[Tuple[int, ...], str, Tuple[int, ...], bytes]:
        arr = np.asarray(ref)
        return (
            tuple(arr.shape),
            arr.dtype.str,
            tuple(int(s) for s in arr.strides),
            _array_fingerprint(arr),
        )

    def _ensure_ref(self, ref: np.ndarray) -> str:
        arr = np.ascontiguousarray(ref, dtype=np.float32)
        key = self._cache_key(arr)
        ref_id = self._ref_cache.get(key)
        if ref_id is not None:
            return ref_id
        ref_id = f'{key[0]}:{key[1]}:{key[2]}:{key[3].hex()}'
        self._call_worker({
            'op': 'set_ref',
            'ref_id': ref_id,
            'array': arr,
            'dtype': self.compute_dtype,
        })
        self._ref_cache[key] = ref_id
        return ref_id

    def nearest_provider(self, ref: np.ndarray) -> Callable[[np.ndarray], NNResult]:
        return lambda query, ref=ref, backend=self: backend.nearest(ref, query)

    def score_batch(
            self,
            ref: np.ndarray,
            queries: np.ndarray,
            tau: float,
            chunk: int = 256) -> np.ndarray:
        ref_id = self._ensure_ref(ref)
        q32 = np.ascontiguousarray(queries, dtype=np.float32)
        out = self._call_worker({
            'op': 'score_batch',
            'ref_id': ref_id,
            'queries': q32,
            'tau': float(tau),
            'chunk': max(32, int(chunk)),
        })
        return np.asarray(out['scores'], dtype=np.float64)

    def _nearest_id(self, ref_id: str, query: np.ndarray) -> NNResult:
        q32 = np.ascontiguousarray(query, dtype=np.float32)
        out = self._call_worker({
            'op': 'nearest',
            'ref_id': ref_id,
            'query': q32,
            'chunk': self.chunk,
        })
        return (
            np.asarray(out['dist'], dtype=np.float64),
            np.asarray(out['idx'], dtype=np.int64),
        )

    def nearest(self, ref: np.ndarray, query: np.ndarray) -> NNResult:
        ref_id = self._ensure_ref(ref)
        return self._nearest_id(ref_id, query)

    def nearest_transient(self, ref: np.ndarray, query: np.ndarray) -> NNResult:
        arr = np.ascontiguousarray(ref, dtype=np.float32)
        self._transient_ref_seq += 1
        ref_id = f"transient:{self._transient_ref_seq}"
        self._call_worker({
            'op': 'set_ref',
            'ref_id': ref_id,
            'array': arr,
            'dtype': self.compute_dtype,
        })
        try:
            return self._nearest_id(ref_id, query)
        finally:
            try:
                self._call_worker({
                    'op': 'delete_ref',
                    'ref_id': ref_id,
                }, retry=False)
            except Exception:
                pass


def select_backend(mode: str = 'AUTO') -> Tuple[object, str]:
    mode = (mode or 'AUTO').upper()
    if mode in ('AUTO', 'TORCH'):
        try:
            backend = TorchBackend(allow_external=(mode == 'TORCH'))
            src_note = '' if backend._source == 'bundled' else f"（借用 {backend._source}）"
            return backend, f"{backend.name}{src_note}"
        except Exception as exc:
            if mode == 'TORCH' and _is_blender_runtime():
                for python_exe in _candidate_torch_python_exes():
                    if not os.path.isfile(python_exe):
                        continue
                    try:
                        backend = ExternalTorchBackend(python_exe)
                        return backend, f"{backend.name} (worker:{python_exe})"
                    except Exception:
                        continue
            fallback = NumpyBackend()
            if mode == 'TORCH':
                print(f"[TheHerta4][PC][Backend][Warning] 强制 Torch 不可用，使用 NumPy: {exc}")
                return fallback, f"{fallback.name}（强制 Torch 不可用）"
            return fallback, fallback.name
    backend = NumpyBackend()
    return backend, backend.name
