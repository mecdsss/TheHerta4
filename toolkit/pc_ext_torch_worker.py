# -*- coding: utf-8 -*-
"""External torch CUDA worker for point-cloud nearest-neighbor queries."""

from __future__ import annotations

import pickle
import struct
import sys
from typing import Dict, Tuple

import numpy as np
import torch


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


def _read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError("unexpected EOF")
        data.extend(chunk)
    return bytes(data)


def _recv(stream):
    header = stream.read(4)
    if not header:
        raise EOFError
    size = struct.unpack("<I", header)[0]
    payload = _read_exact(stream, size)
    return _unpack_value(pickle.loads(payload))


def _send(stream, obj) -> None:
    payload = pickle.dumps(_pack_value(obj), protocol=5)
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


def _nearest_torch(ref_bundle, query, chunk: int):
    ref, ref_t, r2 = ref_bundle[:3]
    n_q = int(query.shape[0])
    if int(ref.shape[0]) == 0 or n_q == 0:
        return (
            np.full(n_q, np.inf, dtype=np.float64),
            np.full(n_q, -1, dtype=np.int64),
        )
    out_d = np.empty(n_q, dtype=np.float64)
    out_i = np.empty(n_q, dtype=np.int64)
    with torch.inference_mode():
        for start in range(0, n_q, chunk):
            q = query[start:start + chunk]
            q2 = (q * q).sum(dim=1, keepdim=True)
            d2 = q2 + r2 - 2.0 * (q @ ref_t)
            dmin, idx = d2.min(dim=1)
            q_len = int(q.shape[0])
            out_d[start:start + q_len] = np.sqrt(
                np.maximum(dmin.detach().cpu().numpy(), 0.0),
                dtype=np.float64,
            )
            out_i[start:start + q_len] = idx.detach().cpu().numpy().astype(
                np.int64)
    return out_d, out_i


def _score_batch_torch(ref_bundle, queries, tau: float, chunk: int):
    ref, ref_t, r2 = ref_bundle[:3]
    ref_t_half = ref_bundle[4] if len(ref_bundle) > 4 else None
    r2_half = ref_bundle[5] if len(ref_bundle) > 5 else None
    if queries.ndim == 2:
        queries = queries.unsqueeze(0)
    batch = int(queries.shape[0])
    n_q = int(queries.shape[1]) if queries.ndim >= 2 else 0
    if int(ref.shape[0]) == 0 or batch == 0 or n_q == 0 or tau <= 0.0:
        return np.zeros(batch, dtype=np.float64)
    tau = float(max(tau, 1e-12))
    if ref_t_half is not None and queries.device.type == "cuda":
        queries = queries.to(dtype=torch.float16)
        ref_t = ref_t_half
        r2 = r2_half
    soft_sum = torch.zeros(batch, dtype=torch.float32, device=queries.device)
    hit_sum = torch.zeros(batch, dtype=torch.float32, device=queries.device)
    with torch.inference_mode():
        r2b = r2.unsqueeze(0)
        for start in range(0, n_q, chunk):
            q = queries[:, start:start + chunk, :]
            q2 = (q * q).sum(dim=2, keepdim=True)
            d2 = q2 + r2b - 2.0 * torch.matmul(q, ref_t)
            dmin = torch.sqrt(torch.clamp(d2.min(dim=2).values, min=0.0))
            soft_sum += torch.exp(-0.5 * torch.square(dmin / tau)).sum(dim=1)
            hit_sum += (dmin <= tau).to(torch.float32).sum(dim=1)
    count = float(n_q)
    score = (soft_sum / count) + 0.25 * (hit_sum / count)
    return score.detach().cpu().numpy().astype(np.float64, copy=False)


def main() -> int:
    if not torch.cuda.is_available():
        raise RuntimeError("torch installed but CUDA unavailable")
    device = torch.device("cuda")
    refs: Dict[str, Tuple[object, object, object, np.dtype]] = {}
    torch.backends.cuda.matmul.allow_tf32 = True
    try:
        torch.set_float32_matmul_precision('high')
    except Exception:
        pass
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    _send(stdout, {
        "ok": True,
        "device": str(device),
        "torch": str(torch.__version__),
    })
    while True:
        cmd = _recv(stdin)
        op = cmd.get("op")
        if op == "close":
            _send(stdout, {"ok": True})
            return 0
        if op == "set_ref":
            ref_id = str(cmd["ref_id"])
            req_dtype = str(cmd.get("dtype", "float32")).lower()
            np_dtype = np.float16 if req_dtype == 'float16' else np.float32
            arr = np.asarray(cmd["array"], dtype=np_dtype)
            ref = torch.as_tensor(np.ascontiguousarray(arr), device=device)
            ref_t = ref.transpose(0, 1).contiguous()
            r2 = (ref * ref).sum(dim=1).unsqueeze(0)
            ref_half = ref if ref.dtype == torch.float16 else ref.to(dtype=torch.float16)
            ref_half_t = ref_half.transpose(0, 1).contiguous()
            r2_half = (ref_half * ref_half).sum(dim=1).unsqueeze(0)
            refs[ref_id] = (ref, ref_t, r2, np_dtype, ref_half_t, r2_half)
            _send(stdout, {"ok": True})
            continue
        if op == "nearest":
            ref_id = str(cmd["ref_id"])
            ref_bundle = refs.get(ref_id)
            if ref_bundle is None:
                _send(stdout, {"ok": False, "error": f"unknown ref_id {ref_id}"})
                continue
            ref, ref_t, r2, np_dtype = ref_bundle[:4]
            query = torch.as_tensor(
                np.ascontiguousarray(cmd["query"], dtype=np_dtype),
                device=device,
            )
            d, i = _nearest_torch((ref, ref_t, r2), query, int(cmd.get("chunk", 2048)))
            _send(stdout, {"ok": True, "dist": d, "idx": i})
            continue
        if op == "score_batch":
            ref_id = str(cmd["ref_id"])
            ref_bundle = refs.get(ref_id)
            if ref_bundle is None:
                _send(stdout, {"ok": False, "error": f"unknown ref_id {ref_id}"})
                continue
            ref, ref_t, r2, np_dtype = ref_bundle[:4]
            queries = torch.as_tensor(
                np.ascontiguousarray(cmd["queries"], dtype=np_dtype),
                device=device,
            )
            scores = _score_batch_torch(
                (ref, ref_t, r2),
                queries,
                float(cmd.get("tau", 0.0)),
                int(cmd.get("chunk", 256)),
            )
            _send(stdout, {"ok": True, "scores": scores})
            continue
        if op == "delete_ref":
            refs.pop(str(cmd["ref_id"]), None)
            _send(stdout, {"ok": True})
            continue
        _send(stdout, {"ok": False, "error": f"unknown op {op}"})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EOFError:
        raise SystemExit(0)
