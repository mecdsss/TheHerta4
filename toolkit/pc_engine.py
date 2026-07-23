"""Pure numpy point-cloud alignment engine.

The module stays free of bpy/mathutils imports so it can run in tests and in
headless worker processes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

TF_ROTATION: str = "rotation"
TF_SCALE: str = "scale"
TF_LOCATION: str = "location"
TF_TYPES: Tuple[str, ...] = (TF_ROTATION, TF_SCALE, TF_LOCATION)

EPS: float = 1e-9
MIN_SCALE_ABS: float = 5e-2
MAX_SCALE_ABS: float = 20.0

BonePointProvider = Callable[[str, str], np.ndarray]
NNProvider = Callable[[np.ndarray], Tuple[np.ndarray, np.ndarray]]


@dataclass
class PCBoneSpec:
    name: str
    enabled: bool
    kind: str
    rotation_mode: str
    lock_rotation: Tuple[bool, bool, bool]
    lock_scale: Tuple[bool, bool, bool]
    lock_location: Tuple[bool, bool, bool]
    has_constraints: bool
    influence_indices: np.ndarray
    influence_weights: np.ndarray
    mirror_name: Optional[str] = None


@dataclass
class PCFitConfig:
    sample_count: int = 8000
    threshold: float = 0.0
    max_angle_deg: float = 3.0
    max_scale_delta: float = 0.05
    max_translation_ratio: float = 0.02
    learning_rate: float = 1.0
    prior_rotation: float = 0.7
    prior_scale: float = 0.2
    prior_location: float = 0.1
    controller_bone_ratio: float = 0.15
    snapshot_interval: int = 500
    max_history_steps: int = 300000
    residual_samples: int = 256
    minibatch_size: int = 0
    full_eval_interval: int = 500
    phase_eval_interval: int = 50
    phase_plateau_delta: float = 1e-4
    phase_plateau_checks: int = 3
    seed: int = 0


@dataclass
class PCMetric:
    f1: float
    precision: float
    recall: float
    chamfer: float
    score: Optional[float] = None


@dataclass
class PCVoxelOverlapStats:
    f1: float
    precision: float
    recall: float
    a_count: int
    b_count: int
    intersection: int


@dataclass
class PCLinkedBasis:
    bone_name: str
    basis_before: np.ndarray
    basis_after: np.ndarray


@dataclass
class PCProposal:
    bone_name: str
    kind: str
    axis: Optional[int]
    basis_before: np.ndarray
    basis_after: np.ndarray
    tf_type: str
    linked: List[PCLinkedBasis] = field(default_factory=list)


@dataclass
class PCStepResult:
    step: int
    accepted: bool
    bone_name: str
    tf_type: str
    metric: PCMetric
    axis: Optional[int] = None
    delta_components: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    linked_count: int = 0
    f1_delta: float = 0.0
    chamfer_delta: float = 0.0
    reward: float = 0.0
    applied_names: Tuple[str, ...] = ()
    score_delta: float = 0.0


def clamp_scale_components(scale: np.ndarray) -> np.ndarray:
    out = np.asarray(scale, dtype=float).copy()
    return np.clip(out, MIN_SCALE_ABS, MAX_SCALE_ABS)


def basis_scale(basis: np.ndarray) -> np.ndarray:
    return np.linalg.norm(np.asarray(basis, dtype=float)[:3, :3], axis=0)


def basis_rotation_matrix(basis: np.ndarray) -> np.ndarray:
    basis = np.asarray(basis, dtype=float)
    scale = basis_scale(basis)
    scale = np.where(scale < EPS, 1.0, scale)
    return basis[:3, :3] / scale[None, :]


def compose_basis(loc: np.ndarray, rot: np.ndarray, scale: np.ndarray) -> np.ndarray:
    out = np.identity(4, dtype=float)
    out[:3, :3] = np.asarray(rot, dtype=float) * clamp_scale_components(scale)[None, :]
    out[:3, 3] = np.asarray(loc, dtype=float)
    return out


def mat3_to_euler_xyz(rot: np.ndarray) -> np.ndarray:
    rot = np.asarray(rot, dtype=float)
    sy = float(np.clip(-rot[2, 0], -1.0, 1.0))
    ry = math.asin(sy)
    cy = math.cos(ry)
    if abs(cy) > 1e-8:
        rx = math.atan2(rot[2, 1], rot[2, 2])
        rz = math.atan2(rot[1, 0], rot[0, 0])
    else:
        rx = math.atan2(-rot[1, 2], rot[1, 1])
        rz = 0.0
    return np.array([rx, ry, rz], dtype=float)


def euler_xyz_to_mat3(e: np.ndarray) -> np.ndarray:
    rx, ry, rz = float(e[0]), float(e[1]), float(e[2])
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    rxm = np.array([[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]], dtype=float)
    rym = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]], dtype=float)
    rzm = np.array([[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]], dtype=float)
    return rzm @ rym @ rxm


def rotvec_to_mat3(rotvec: np.ndarray) -> np.ndarray:
    rotvec = np.asarray(rotvec, dtype=float)
    angle = float(np.linalg.norm(rotvec))
    if angle < 1e-12:
        return np.identity(3, dtype=float)
    axis = rotvec / angle
    x, y, z = axis
    c = math.cos(angle)
    s = math.sin(angle)
    one_c = 1.0 - c
    return np.array([
        [c + x * x * one_c, x * y * one_c - z * s, x * z * one_c + y * s],
        [y * x * one_c + z * s, c + y * y * one_c, y * z * one_c - x * s],
        [z * x * one_c - y * s, z * y * one_c + x * s, c + z * z * one_c],
    ], dtype=float)


def mat3_to_rotvec(rot: np.ndarray) -> np.ndarray:
    rot = np.asarray(rot, dtype=float)
    trace = float(np.trace(rot))
    cos_angle = float(np.clip((trace - 1.0) * 0.5, -1.0, 1.0))
    angle = math.acos(cos_angle)
    if angle < 1e-12:
        return 0.5 * np.array([
            rot[2, 1] - rot[1, 2],
            rot[0, 2] - rot[2, 0],
            rot[1, 0] - rot[0, 1],
        ], dtype=float)
    if math.pi - angle < 1e-6:
        axis = np.sqrt(np.maximum((np.diag(rot) + 1.0) * 0.5, 0.0))
        if axis[0] > 1e-6:
            axis[1] = math.copysign(axis[1], rot[0, 1] + rot[1, 0])
            axis[2] = math.copysign(axis[2], rot[0, 2] + rot[2, 0])
        elif axis[1] > 1e-6:
            axis[2] = math.copysign(axis[2], rot[1, 2] + rot[2, 1])
        n = float(np.linalg.norm(axis))
        if n < EPS:
            return np.zeros(3, dtype=float)
        return axis / n * angle
    s = 2.0 * math.sin(angle)
    axis = np.array([
        rot[2, 1] - rot[1, 2],
        rot[0, 2] - rot[2, 0],
        rot[1, 0] - rot[0, 1],
    ], dtype=float) / s
    return axis * angle


def kabsch_rotation(
    src: np.ndarray,
    dst: np.ndarray,
    weights: Optional[np.ndarray] = None,
    pivot: Optional[np.ndarray] = None,
) -> np.ndarray:
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    if src.size == 0 or dst.size == 0 or len(src) != len(dst):
        return np.identity(3, dtype=float)
    if weights is None:
        weights = np.ones(len(src), dtype=float)
    weights = np.asarray(weights, dtype=float)
    wsum = float(weights.sum())
    if wsum < EPS:
        return np.identity(3, dtype=float)
    if pivot is None:
        pivot = np.zeros(3, dtype=float)
    pivot = np.asarray(pivot, dtype=float)
    s = src - pivot[None, :]
    d = dst - pivot[None, :]
    w = weights[:, None] / wsum
    h = (s * w).T @ d
    try:
        u, _s, vt = np.linalg.svd(h)
    except np.linalg.LinAlgError:
        return np.identity(3, dtype=float)
    r = vt.T @ u.T
    if np.linalg.det(r) < 0.0:
        vt = vt.copy()
        vt[-1, :] *= -1.0
        r = vt.T @ u.T
    return r


def centered_kabsch_rotation(
    src: np.ndarray,
    dst: np.ndarray,
    weights: Optional[np.ndarray] = None,
) -> np.ndarray:
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    if len(src) == 0 or len(dst) == 0:
        return np.identity(3, dtype=float)
    w = np.ones(len(src), dtype=float) if weights is None else np.asarray(weights, dtype=float)
    total = float(w.sum())
    if total < EPS:
        return np.identity(3, dtype=float)
    center_src = np.sum(src * w[:, None], axis=0) / total
    center_dst = np.sum(dst * w[:, None], axis=0) / total
    return kabsch_rotation(src - center_src, dst - center_dst, w, pivot=np.zeros(3))


def mask_rotation_delta(
    delta_world: np.ndarray,
    chan_to_local: np.ndarray,
    lock: Tuple[bool, bool, bool],
    mode: str,
) -> np.ndarray:
    chan_to_local = np.asarray(chan_to_local, dtype=float)
    delta_world = np.asarray(delta_world, dtype=float)
    delta_chan = chan_to_local.T @ delta_world @ chan_to_local
    if mode == "QUATERNION":
        components = mat3_to_rotvec(delta_chan)
        for axis in range(3):
            if lock[axis]:
                components[axis] = 0.0
        masked = rotvec_to_mat3(components)
    else:
        e = mat3_to_euler_xyz(delta_chan)
        for axis in range(3):
            if lock[axis]:
                e[axis] = 0.0
        masked = euler_xyz_to_mat3(e)
    return chan_to_local @ masked @ chan_to_local.T


def mask_vector_delta(delta_local: np.ndarray, chan_to_local: np.ndarray, lock: Tuple[bool, bool, bool]) -> np.ndarray:
    delta_local = np.asarray(delta_local, dtype=float)
    chan_to_local = np.asarray(chan_to_local, dtype=float)
    v = chan_to_local.T @ delta_local
    for axis in range(3):
        if lock[axis]:
            v[axis] = 0.0
    return chan_to_local @ v


def mask_scale_factors(factors: np.ndarray, lock: Tuple[bool, bool, bool]) -> np.ndarray:
    out = np.asarray(factors, dtype=float).copy()
    for axis in range(3):
        if lock[axis]:
            out[axis] = 1.0
    return out


def basis_transform_delta(tf_type: str, before: np.ndarray, after: np.ndarray) -> np.ndarray:
    if tf_type == TF_LOCATION:
        return np.asarray(after[:3, 3] - before[:3, 3], dtype=float)
    if tf_type == TF_SCALE:
        s0 = basis_scale(before)
        s1 = basis_scale(after)
        return np.divide(s1, s0, out=np.ones(3, dtype=float), where=s0 > EPS)
    return basis_rotation_matrix(after) @ basis_rotation_matrix(before).T


def transform_delta_magnitude(tf_type: str, before: np.ndarray, after: np.ndarray) -> float:
    delta = basis_transform_delta(tf_type, before, after)
    if tf_type == TF_ROTATION:
        return float(np.linalg.norm(mat3_to_rotvec(delta)))
    if tf_type == TF_SCALE:
        return float(np.max(np.abs(delta - 1.0)))
    return float(np.linalg.norm(delta))


def apply_transform_delta(tf_type: str, basis: np.ndarray, delta: np.ndarray) -> np.ndarray:
    basis = np.asarray(basis, dtype=float)
    if tf_type == TF_LOCATION:
        return compose_basis(basis[:3, 3] + np.asarray(delta, dtype=float), basis_rotation_matrix(basis), basis_scale(basis))
    if tf_type == TF_SCALE:
        return compose_basis(basis[:3, 3], basis_rotation_matrix(basis), basis_scale(basis) * np.asarray(delta, dtype=float))
    return compose_basis(basis[:3, 3], np.asarray(delta, dtype=float) @ basis_rotation_matrix(basis), basis_scale(basis))


def resize_transform_delta(tf_type: str, delta: np.ndarray, amount: float) -> np.ndarray:
    if tf_type == TF_ROTATION:
        vec = mat3_to_rotvec(delta)
        n = float(np.linalg.norm(vec))
        return rotvec_to_mat3(vec * (amount / n)) if n > EPS else np.asarray(delta, dtype=float)
    if tf_type == TF_SCALE:
        vec = np.asarray(delta, dtype=float) - 1.0
        n = float(np.max(np.abs(vec)))
        return 1.0 + vec * (amount / n) if n > EPS else np.asarray(delta, dtype=float)
    vec = np.asarray(delta, dtype=float)
    n = float(np.linalg.norm(vec))
    return vec * (amount / n) if n > EPS else vec


def opposite_basis(tf_type: str, before: np.ndarray, after: np.ndarray) -> np.ndarray:
    if tf_type == TF_LOCATION:
        return compose_basis(before[:3, 3] - (after[:3, 3] - before[:3, 3]), basis_rotation_matrix(before), basis_scale(before))
    if tf_type == TF_SCALE:
        s0 = basis_scale(before)
        ratio = np.divide(basis_scale(after), s0, out=np.ones(3, dtype=float), where=s0 > EPS)
        opposite_scale = np.divide(s0, ratio, out=s0.copy(), where=ratio > EPS)
        return compose_basis(before[:3, 3], basis_rotation_matrix(before), opposite_scale)
    r0 = basis_rotation_matrix(before)
    delta = basis_rotation_matrix(after) @ r0.T
    return compose_basis(before[:3, 3], delta.T @ r0, basis_scale(before))


def opposite_proposal_basis(proposal: PCProposal) -> np.ndarray:
    return opposite_basis(proposal.tf_type, proposal.basis_before, proposal.basis_after)


def detect_mirror_pairs(
    rest_segments: Dict[str, Tuple[np.ndarray, np.ndarray]],
    axis: int = 0,
    tolerance: Optional[float] = None,
) -> Dict[str, str]:
    if len(rest_segments) < 2:
        return {}
    names = list(rest_segments)
    all_points = np.concatenate(
        [
            np.asarray(rest_segments[n][0], dtype=float)[None, :]
            for n in names
        ]
        + [
            np.asarray(rest_segments[n][1], dtype=float)[None, :]
            for n in names
        ],
        axis=0,
    )
    diag = float(np.linalg.norm(np.ptp(all_points, axis=0)))
    tol = max(1e-6, diag * 0.01) if tolerance is None else float(tolerance)
    nearest: Dict[str, Tuple[str, float]] = {}
    for name in names:
        head, tail = (np.asarray(v, dtype=float) for v in rest_segments[name])
        if abs(head[axis]) <= tol and abs(tail[axis]) <= tol:
            continue
        mirrored_head = head.copy()
        mirrored_tail = tail.copy()
        mirrored_head[axis] *= -1.0
        mirrored_tail[axis] *= -1.0
        best_name = None
        best_error = float("inf")
        for candidate in names:
            if candidate == name:
                continue
            other_head, other_tail = (np.asarray(v, dtype=float) for v in rest_segments[candidate])
            error = max(
                float(np.linalg.norm(mirrored_head - other_head)),
                float(np.linalg.norm(mirrored_tail - other_tail)),
            )
            if error < best_error:
                best_name, best_error = candidate, error
        if best_name is not None and best_error <= tol:
            nearest[name] = (best_name, best_error)
    pairs: Dict[str, str] = {}
    for name, (other, _error) in nearest.items():
        reverse = nearest.get(other)
        if reverse is not None and reverse[0] == name:
            pairs[name] = other
    return pairs


def _cell_keys(cells: np.ndarray, assume_unique: bool = False) -> np.ndarray:
    cells = np.asarray(cells, dtype=np.int64)
    if len(cells) == 0:
        return np.zeros(0, dtype=np.dtype((np.void, 24)))
    if not assume_unique:
        cells = np.unique(cells, axis=0)
    cells = np.ascontiguousarray(cells)
    return cells.view(np.dtype((np.void, cells.dtype.itemsize * 3))).ravel()


def occupied_voxel_cells(points: np.ndarray, voxel_size: float) -> np.ndarray:
    points = np.asarray(points, dtype=float)
    if voxel_size <= 0.0 or len(points) == 0:
        return np.empty((0, 3), dtype=np.int64)
    return np.unique(np.floor(points / voxel_size).astype(np.int64), axis=0)


def occupied_voxel_keys(points: np.ndarray, voxel_size: float) -> np.ndarray:
    return _cell_keys(occupied_voxel_cells(points, voxel_size), assume_unique=True)


solid_voxel_cells = occupied_voxel_cells
solid_voxel_keys = occupied_voxel_keys


def voxel_overlap_keys(a_keys: np.ndarray, b_keys: np.ndarray) -> Tuple[float, float, float]:
    stats = voxel_overlap_key_stats(a_keys, b_keys)
    return stats.f1, stats.precision, stats.recall


def voxel_overlap_key_stats(a_keys: np.ndarray, b_keys: np.ndarray) -> PCVoxelOverlapStats:
    if len(a_keys) == 0 or len(b_keys) == 0:
        return PCVoxelOverlapStats(
            f1=0.0, precision=0.0, recall=0.0,
            a_count=int(len(a_keys)), b_count=int(len(b_keys)),
            intersection=0)
    intersection = len(np.intersect1d(a_keys, b_keys, assume_unique=True))
    precision = intersection / len(b_keys) if len(b_keys) else 0.0
    recall = intersection / len(a_keys) if len(a_keys) else 0.0
    denom = precision + recall
    f1 = 2.0 * precision * recall / denom if denom > EPS else 0.0
    return PCVoxelOverlapStats(
        f1=float(f1), precision=float(precision), recall=float(recall),
        a_count=int(len(a_keys)), b_count=int(len(b_keys)),
        intersection=int(intersection))


def voxel_overlap(a: np.ndarray, b: np.ndarray, voxel_size: float) -> Tuple[float, float, float]:
    if voxel_size <= 0.0 or len(a) == 0 or len(b) == 0:
        return 0.0, 0.0, 0.0
    return voxel_overlap_keys(occupied_voxel_keys(a, voxel_size), occupied_voxel_keys(b, voxel_size))


def overlap_metric(
    a: np.ndarray,
    b: np.ndarray,
    nn_a: NNProvider,
    nn_b: NNProvider,
    tau: float,
    a_voxel_keys: Optional[np.ndarray] = None,
) -> PCMetric:
    """Point-wise tolerance overlap used by fitting and displayed metrics.

    Precision is the fraction of B samples within tau of A; recall is the
    fraction of A samples within tau of B. This avoids treating two nearly
    coincident surface samples as non-overlapping just because they landed in
    adjacent voxel cells.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) == 0 or len(b) == 0 or tau <= 0.0:
        return PCMetric(f1=0.0, precision=0.0, recall=0.0, chamfer=float("inf"))
    d_b, _ = nn_a(b)
    d_a, _ = nn_b(a)
    precision = float(np.mean(d_b <= tau)) if len(d_b) else 0.0
    recall = float(np.mean(d_a <= tau)) if len(d_a) else 0.0
    denom = precision + recall
    f1 = 2.0 * precision * recall / denom if denom > EPS else 0.0
    score = soft_overlap_score(d_b, d_a, tau)
    chamfer = float(0.5 * (np.mean(d_b) + np.mean(d_a)))
    return PCMetric(
        f1=f1, precision=precision, recall=recall,
        chamfer=chamfer, score=score)


def metric_from_bidirectional_distances(
    d_b: np.ndarray,
    d_a: np.ndarray,
    tau: float,
) -> PCMetric:
    d_b = np.asarray(d_b, dtype=float)
    d_a = np.asarray(d_a, dtype=float)
    if len(d_b) == 0 or len(d_a) == 0 or tau <= 0.0:
        return PCMetric(f1=0.0, precision=0.0, recall=0.0, chamfer=float("inf"))
    precision = float(np.mean(d_b <= tau)) if len(d_b) else 0.0
    recall = float(np.mean(d_a <= tau)) if len(d_a) else 0.0
    denom = precision + recall
    f1 = 2.0 * precision * recall / denom if denom > EPS else 0.0
    score = soft_overlap_score(d_b, d_a, tau)
    chamfer = float(0.5 * (np.mean(d_b) + np.mean(d_a)))
    return PCMetric(
        f1=f1, precision=precision, recall=recall,
        chamfer=chamfer, score=score)


def distance_overlap_metric(
    a: np.ndarray,
    b: np.ndarray,
    nn_a: NNProvider,
    nn_b: NNProvider,
    tau: float,
) -> PCMetric:
    """Compatibility wrapper for callers that explicitly ask for distance F1."""
    return overlap_metric(a, b, nn_a, nn_b, tau)


def soft_overlap_score(d_b: np.ndarray, d_a: np.ndarray, tau: float) -> float:
    """Continuous fit score used for accepting optimization steps.

    Hard overlap F1 is excellent for display/debugging but it is a step
    function: a point only changes the value when it crosses tau. This score
    gives every nearest-neighbor distance a smooth contribution, so a proposal
    that moves the cloud closer can be accepted before hard F1 changes.
    """
    if tau <= 0.0 or len(d_b) == 0 or len(d_a) == 0:
        return 0.0
    scale = max(float(tau), EPS)
    b = np.asarray(d_b, dtype=float) / scale
    a = np.asarray(d_a, dtype=float) / scale
    soft_precision = float(np.mean(np.exp(-0.5 * b * b))) if len(b) else 0.0
    soft_recall = float(np.mean(np.exp(-0.5 * a * a))) if len(a) else 0.0
    denom = soft_precision + soft_recall
    return float(2.0 * soft_precision * soft_recall / denom) if denom > EPS else 0.0


def _fit_score(metric: PCMetric) -> Optional[float]:
    score = metric.score
    if score is None or not math.isfinite(float(score)):
        return None
    return float(score)


def invalid_metric() -> PCMetric:
    return PCMetric(
        f1=-1.0,
        precision=0.0,
        recall=0.0,
        chamfer=float("inf"),
        score=-float("inf"),
    )


def _finite_metric(metric: PCMetric) -> PCMetric:
    if not math.isfinite(float(metric.f1)):
        return invalid_metric()
    if not math.isfinite(float(metric.precision)):
        return invalid_metric()
    if not math.isfinite(float(metric.recall)):
        return invalid_metric()
    if not math.isfinite(float(metric.chamfer)):
        return invalid_metric()
    score = metric.score
    if score is not None and not math.isfinite(float(score)):
        return invalid_metric()
    return metric


def metric_improves(candidate: PCMetric, baseline: PCMetric) -> bool:
    candidate_score = _fit_score(candidate)
    baseline_score = _fit_score(baseline)
    if candidate_score is not None and baseline_score is not None:
        if candidate_score > baseline_score + EPS:
            return True
        if candidate_score < baseline_score - EPS:
            return False
    if candidate.f1 > baseline.f1 + EPS:
        return True
    if candidate.f1 < baseline.f1 - EPS:
        return False
    return candidate.chamfer < baseline.chamfer - 1e-9


def metric_reward(candidate: PCMetric, baseline: PCMetric) -> float:
    candidate_score = _fit_score(candidate)
    baseline_score = _fit_score(baseline)
    if candidate_score is not None and baseline_score is not None:
        score_gain = candidate_score - baseline_score
        if abs(score_gain) > EPS:
            return float(score_gain)
    f1_gain = candidate.f1 - baseline.f1
    if abs(f1_gain) > EPS:
        return float(f1_gain)
    return float(baseline.chamfer - candidate.chamfer)


def overlap_first_improves(candidate: PCMetric, baseline: PCMetric) -> bool:
    if candidate.f1 > baseline.f1 + EPS:
        return True
    if candidate.f1 < baseline.f1 - EPS:
        return False
    candidate_score = _fit_score(candidate)
    baseline_score = _fit_score(baseline)
    if candidate_score is not None and baseline_score is not None:
        if candidate_score > baseline_score + EPS:
            return True
        if candidate_score < baseline_score - EPS:
            return False
    return candidate.chamfer < baseline.chamfer - 1e-9


def overlap_first_reward(candidate: PCMetric, baseline: PCMetric) -> float:
    f1_gain = candidate.f1 - baseline.f1
    if abs(f1_gain) > EPS:
        return float(f1_gain)
    candidate_score = _fit_score(candidate)
    baseline_score = _fit_score(baseline)
    if candidate_score is not None and baseline_score is not None:
        score_gain = candidate_score - baseline_score
        if abs(score_gain) > EPS:
            return float(score_gain)
    return float(baseline.chamfer - candidate.chamfer)


def brute_force_nn(points: np.ndarray) -> NNProvider:
    pts = np.asarray(points, dtype=float)

    def _query(x: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        x = np.asarray(x, dtype=float)
        if len(pts) == 0 or len(x) == 0:
            return np.full(len(x), np.inf), np.full(len(x), -1, dtype=np.int64)
        dists = np.linalg.norm(x[:, None, :] - pts[None, :, :], axis=2)
        idx = np.argmin(dists, axis=1)
        return dists[np.arange(len(x)), idx], idx.astype(np.int64)

    return _query


def lbs_transform(rest_pts: np.ndarray, delta_mats: np.ndarray, weights: np.ndarray, bone_idx: np.ndarray) -> np.ndarray:
    rest_pts = np.asarray(rest_pts, dtype=np.float64)
    n = rest_pts.shape[0]
    if n == 0:
        return rest_pts.copy()
    delta_mats = np.asarray(delta_mats, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64)
    bone_idx = np.asarray(bone_idx, dtype=np.int64)
    hom = np.concatenate([rest_pts, np.ones((n, 1), dtype=np.float64)], axis=1)
    safe_idx = np.clip(bone_idx, 0, max(0, delta_mats.shape[0] - 1))
    mats = delta_mats[safe_idx]
    out = np.einsum("nkij,nj->nki", mats, hom)
    w = weights * (bone_idx >= 0)
    skinned = (out * w[:, :, None]).sum(axis=1)
    return skinned[:, :3]


def lbs_transform_with_remainder(rest_pts: np.ndarray, delta_mats: np.ndarray, weights: np.ndarray, bone_idx: np.ndarray) -> np.ndarray:
    skinned = lbs_transform(rest_pts, delta_mats, weights, bone_idx)
    wsum = np.asarray(weights, dtype=np.float64).sum(axis=1, keepdims=True)
    remainder = np.clip(1.0 - wsum, 0.0, 1.0)
    return skinned + remainder * np.asarray(rest_pts, dtype=np.float64)


class PCSchedule:
    def __init__(
        self,
        priors: Dict[str, float],
        ema_decay: float = 0.95,
        eps: float = 0.05,
        plateau_delta: float = 1e-4,
        plateau_checks: int = 3,
    ) -> None:
        self.priors: Dict[str, float] = {t: max(0.0, priors.get(t, 0.0)) for t in TF_TYPES}
        self.ema_decay = float(ema_decay)
        self.eps = float(eps)
        self.ema_gain: Dict[str, float] = {t: 1.0 for t in TF_TYPES}
        self.stage: int = 0
        self.plateau_delta = max(0.0, float(plateau_delta))
        self.plateau_checks = max(1, int(plateau_checks))
        self.phase_best_f1: float = -1.0
        self.phase_plateau_count: int = 0

    @property
    def phase_name(self) -> str:
        names = ("旋转优先", "缩放优先", "位移优先", "单骨联调", "镜像联合微调")
        return names[min(self.stage, len(names) - 1)]

    def advance_stage(self) -> None:
        if self.stage < 4:
            self.stage += 1
            self.phase_best_f1 = -1.0
            self.phase_plateau_count = 0

    def observe_overlap(self, f1: float) -> bool:
        if self.stage >= 4 or not math.isfinite(f1):
            return False
        f1 = float(f1)
        if self.phase_best_f1 < 0.0:
            self.phase_best_f1 = f1
            return False
        if f1 > self.phase_best_f1 + self.plateau_delta:
            self.phase_best_f1 = f1
            self.phase_plateau_count = 0
            return False
        self.phase_best_f1 = max(self.phase_best_f1, f1)
        self.phase_plateau_count += 1
        if self.phase_plateau_count >= self.plateau_checks:
            self.advance_stage()
            return True
        return False

    def set_priors(self, priors: Dict[str, float]) -> None:
        for t in TF_TYPES:
            if t in priors:
                self.priors[t] = max(0.0, float(priors[t]))

    def weights_for(self, unlocked: Dict[str, Tuple[bool, bool, bool]]) -> Dict[str, float]:
        if self.stage < 3:
            current = TF_TYPES[self.stage]
            return {current: 1.0} if any(unlocked.get(current, (False, False, False))) else {}
        avail: Dict[str, float] = {}
        for t in TF_TYPES:
            if any(unlocked.get(t, (False, False, False))):
                quality = max(self.eps, self.ema_gain[t] + self.eps)
                avail[t] = self.priors[t] * quality
        total = sum(avail.values())
        if total <= EPS:
            return {}
        return {t: v / total for t, v in avail.items()}

    def update(self, tf_type: str, gain: float) -> None:
        for t in TF_TYPES:
            self.ema_gain[t] *= self.ema_decay
        if tf_type in self.ema_gain:
            self.ema_gain[tf_type] += (1.0 - self.ema_decay) * float(gain)

    def current_weights_display(self) -> Dict[str, float]:
        weights = self.weights_for({t: (True, True, True) for t in TF_TYPES})
        return {t: weights.get(t, 0.0) for t in TF_TYPES}


class PCFitSession:
    def __init__(
        self,
        bones: List[PCBoneSpec],
        a_points: np.ndarray,
        b_points: np.ndarray,
        nn_a: NNProvider,
        config: PCFitConfig,
        apply_basis: Callable[[str, np.ndarray], None],
        read_samples: Callable[[], np.ndarray],
        bone_point_provider: BonePointProvider,
        tau: float,
        basis_map: Optional[Dict[str, np.ndarray]] = None,
        nn_factory: Optional[Callable[[np.ndarray], NNProvider]] = None,
        backend: Optional[object] = None,
        screen_rig: Optional[object] = None,
        restore_sample_cache: Optional[Callable[[np.ndarray], None]] = None,
    ) -> None:
        self.config = config
        self.bones: Dict[str, PCBoneSpec] = {b.name: b for b in bones}
        self.a_points = np.asarray(a_points, dtype=float)
        self.nn_a = nn_a
        self.apply_basis = apply_basis
        self.read_samples = read_samples
        self.bone_point_provider = bone_point_provider
        self.tau = float(tau)
        self.rng = np.random.default_rng(config.seed)
        self._nn_factory = nn_factory if nn_factory is not None else brute_force_nn
        self.backend = backend
        self.screen_rig = screen_rig
        self._screen_rig_needs_sync = screen_rig is not None
        self.restore_sample_cache = restore_sample_cache

        self.b_points = np.asarray(b_points, dtype=float)
        self.nn_b = self._nn_factory(self.b_points)
        self._a_voxel_keys = occupied_voxel_keys(self.a_points, self.tau)
        a_diag = float(np.linalg.norm(np.ptp(self.a_points, axis=0))) if len(self.a_points) else 1.0
        self._a_diag = max(a_diag, 1e-6)
        enabled_bones = sum(1 for bone in bones if bone.enabled)
        dual_exact_screen = enabled_bones <= 2 and len(self.a_points) >= 1000
        screen_limit = min(
            2048 if dual_exact_screen else 4096,
            len(self.a_points),
            len(self.b_points),
        )
        if screen_limit > 0:
            screen_rng = np.random.default_rng(config.seed ^ 0x5343524E)
            if len(self.a_points) <= screen_limit:
                self._screen_ia = np.arange(len(self.a_points), dtype=np.int64)
            else:
                self._screen_ia = np.sort(
                    screen_rng.choice(len(self.a_points), screen_limit, replace=False)
                ).astype(np.int64)
            if len(self.b_points) <= screen_limit:
                self._screen_ib = np.arange(len(self.b_points), dtype=np.int64)
            else:
                self._screen_ib = np.sort(
                    screen_rng.choice(len(self.b_points), screen_limit, replace=False)
                ).astype(np.int64)
            self._screen_a_points = self.a_points[self._screen_ia].copy()
            self._screen_nn_a = self._nn_factory(self._screen_a_points)
        else:
            self._screen_ia = np.zeros(0, dtype=np.int64)
            self._screen_ib = np.zeros(0, dtype=np.int64)
            self._screen_a_points = self.a_points[:0]
            self._screen_nn_a = self._nn_factory(self._screen_a_points)

        ref_limit = max(2048, 2 * int(config.minibatch_size))
        ref_rng = np.random.default_rng(config.seed ^ 0x5043)
        self._minibatch_ref_a = np.arange(len(self.a_points), dtype=np.int64) if len(self.a_points) <= ref_limit else np.sort(ref_rng.choice(len(self.a_points), ref_limit, replace=False))
        self._minibatch_ref_b = np.arange(len(self.b_points), dtype=np.int64) if len(self.b_points) <= ref_limit else np.sort(ref_rng.choice(len(self.b_points), ref_limit, replace=False))
        self._minibatch_ref_a_points = self.a_points[self._minibatch_ref_a].copy()
        self._minibatch_ref_b_points = self.b_points[self._minibatch_ref_b].copy()

        self.schedule = PCSchedule(
            {
                TF_ROTATION: config.prior_rotation,
                TF_SCALE: config.prior_scale,
                TF_LOCATION: config.prior_location,
            },
            plateau_delta=config.phase_plateau_delta,
            plateau_checks=config.phase_plateau_checks,
        )

        self.basis_map: Dict[str, np.ndarray] = {}
        if basis_map:
            for k, v in basis_map.items():
                self.basis_map[k] = np.asarray(v, dtype=float).copy()
        for b in bones:
            if b.enabled and b.name not in self.basis_map:
                self.basis_map[b.name] = np.identity(4, dtype=float)

        self.step_sizes: Dict[Tuple[str, str], float] = {}
        self.step_size_limits: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self.step_size_max_seen: Dict[Tuple[str, str], float] = {}
        self.step_reject_streak: Dict[Tuple[str, str], int] = {}
        self.axis_cursors: Dict[Tuple[str, str], int] = {}
        self.axis_f1_plateau_streak: Dict[Tuple[str, str], int] = {}
        self.learning_rates: Dict[Tuple[str, str], float] = {}
        self.proposal_momentum: Dict[Tuple[str, str], np.ndarray] = {}
        self.proposal_momentum_axis: Dict[Tuple[str, str], Optional[int]] = {}
        self.bone_pick_counts: Dict[str, int] = {name: 0 for name in self.bones}
        self.bone_gain_ema: Dict[str, float] = {name: 0.0 for name in self.bones}
        self.bone_no_gain_streak: Dict[str, int] = {name: 0 for name in self.bones}
        self.bone_last_pick_step: Dict[str, int] = {name: -1000000 for name in self.bones}
        self.bone_reject_axes: Dict[Tuple[str, str], set[int]] = {}
        self.bone_curriculum_patience: int = 10
        self.axis_search_patience: int = 10
        self.high_overlap_accept_patience: int = 6
        self._high_overlap_no_f1_accepts: int = 0
        self._bone_curriculum_signature = None
        self._bone_curriculum_order: List[str] = []
        self._bone_curriculum_index: int = 0
        self._bone_curriculum_no_gain: int = 0
        self._bone_curriculum_pass: int = 0
        self._phase_accepted_since_eval: int = 0
        self._phase_last_curriculum_pass: int = 0

        self.step_count: int = 0
        self.metrics: List[float] = []
        self.deltas: List[Tuple[int, str, np.ndarray]] = []
        self.snapshots: List[Tuple[int, Dict[str, np.ndarray]]] = [(0, self._snapshot_state())]
        self.best_step: int = 0
        self.current_metric: PCMetric = self._full_metric()
        self.best_f1: float = self.current_metric.f1
        self.best_snapshot: Optional[Dict[str, np.ndarray]] = self._snapshot_state()
        self.last_step_result: Optional[PCStepResult] = None
        self.last_debug_payload: Dict[str, object] = {}
        self.best_reward: float = -float("inf")
        self._state_revision: int = 0
        self._screen_metric_pair_cache: Dict[Tuple[object, ...], Tuple[PCMetric, PCMetric]] = {}
        self._exact_metric_pair_cache: Dict[Tuple[object, ...], Tuple[PCMetric, PCMetric]] = {}
        self._history_floor_step: int = 0

    def _snapshot_state(self) -> Dict[str, np.ndarray]:
        return {k: v.copy() for k, v in self.basis_map.items()}

    def _snapshot_at_exact_step(self, step: int) -> Optional[Dict[str, np.ndarray]]:
        for snap_step, snap_state in reversed(self.snapshots):
            if snap_step == step:
                return {k: v.copy() for k, v in snap_state.items()}
        return None

    def nearest_recoverable_step(self, step: int) -> int:
        step = int(max(0, min(step, self.history_total())))
        if step >= self._history_floor_step:
            return step
        recoverable = 0
        for snap_step, _snap_state in self.snapshots:
            if snap_step <= step and snap_step >= recoverable:
                recoverable = snap_step
        return int(recoverable)

    def _metric_fn_cache_key(self, metric_fn: Callable[[], PCMetric]) -> str:
        fn_obj = getattr(metric_fn, '__func__', metric_fn)
        return str(
            getattr(fn_obj, '__qualname__',
                    getattr(fn_obj, '__name__', type(fn_obj).__name__)))

    def _proposal_cache_key(self, proposal: PCProposal) -> Tuple[object, ...]:
        entries = []
        for name, _before, after in self._proposal_entries(proposal):
            arr = np.asarray(after, dtype=np.float64)
            entries.append((name, arr.tobytes()))
        return (
            proposal.tf_type,
            proposal.axis,
            tuple(entries),
        )

    def _pair_cache_key(
            self,
            forward_proposal: PCProposal,
            backward_proposal: PCProposal) -> Tuple[object, ...]:
        return (
            self._state_revision,
            self._proposal_cache_key(forward_proposal),
            self._proposal_cache_key(backward_proposal),
        )

    def _touch_committed_state(self) -> None:
        self._state_revision += 1
        self._screen_metric_pair_cache.clear()
        self._exact_metric_pair_cache.clear()

    def _apply_state(self, state: Dict[str, np.ndarray]) -> None:
        self.basis_map = {k: np.asarray(v, dtype=float).copy() for k, v in state.items()}
        for name, spec in self.bones.items():
            if not spec.enabled:
                continue
            basis = self.basis_map.get(name, np.identity(4, dtype=float))
            self.apply_basis(name, basis)
        self._set_b_points(self.read_samples())
        self._touch_committed_state()

    def _set_b_points(self, points: np.ndarray) -> None:
        self.b_points = np.asarray(points, dtype=float)
        self.nn_b = self._nn_factory(self.b_points)
        if self.screen_rig is not None:
            self._screen_rig_needs_sync = True
        if self._minibatch_active():
            self._minibatch_ref_b_points = self.b_points[self._minibatch_ref_b].copy()

    def _restore_b_points_cache(
            self,
            points: np.ndarray,
            nn_b,
            minibatch_ref_b_points: Optional[np.ndarray] = None) -> None:
        self.b_points = np.asarray(points, dtype=float)
        self.nn_b = nn_b
        if self.screen_rig is not None:
            self._screen_rig_needs_sync = True
        if self._minibatch_active() and minibatch_ref_b_points is not None:
            self._minibatch_ref_b_points = np.asarray(
                minibatch_ref_b_points, dtype=float)

    def _full_metric(self) -> PCMetric:
        return overlap_metric(self.a_points, self.b_points, self.nn_a, self.nn_b, self.tau, a_voxel_keys=self._a_voxel_keys)

    def _screen_rank_for_points(self, points: np.ndarray) -> float:
        if len(self._screen_a_points) == 0 or len(self._screen_ib) == 0:
            metric = self._metric_for_points(points)
            score = _fit_score(metric)
            return float(score) if score is not None else float(metric.f1)
        pts = np.asarray(points, dtype=float)
        b_sub = pts[self._screen_ib]
        return self._screen_rank_for_subpoints(b_sub)

    def _screen_rank_for_subpoints(self, b_sub: np.ndarray) -> float:
        b_sub = np.asarray(b_sub, dtype=float)
        if len(self._screen_a_points) == 0:
            return 0.0
        if not np.isfinite(b_sub).all():
            return -float("inf")
        d_b, _ = self._screen_nn_a(b_sub)
        if len(d_b) == 0 or self.tau <= 0.0:
            return 0.0
        precision = float(np.mean(d_b <= self.tau))
        soft_precision = float(
            np.mean(np.exp(-0.5 * np.square(np.asarray(d_b, dtype=float) / max(self.tau, EPS))))
        )
        return soft_precision + 0.25 * precision

    def _screen_rank_batch_for_subpoints(
            self, queries: np.ndarray) -> Optional[np.ndarray]:
        if (self.backend is None or
                not hasattr(self.backend, "score_batch") or
                len(self._screen_a_points) == 0):
            return None
        arr = np.asarray(queries, dtype=np.float32)
        if arr.ndim != 3 or arr.shape[0] == 0:
            return np.zeros(0, dtype=np.float64)
        finite_rows = np.isfinite(arr).all(axis=(1, 2))
        if not finite_rows.any():
            return np.full(arr.shape[0], -float("inf"), dtype=np.float64)
        try:
            scores = np.full(arr.shape[0], -float("inf"), dtype=np.float64)
            valid_scores = np.asarray(
                self.backend.score_batch(
                    self._screen_a_points, arr[finite_rows], self.tau,
                    chunk=2048),
                dtype=np.float64)
            scores[finite_rows] = valid_scores
            return scores
        except Exception:
            return None

    def _screen_rank_batch_for_pairs(
            self,
            pair_items: List[Tuple[PCProposal, PCProposal]],
    ) -> Optional[List[Tuple[float, float]]]:
        rig = self.screen_rig
        if (rig is None
                or not hasattr(rig, "probe_pair_subpoints")
                or len(self._screen_ib) == 0
                or not pair_items):
            return None
        self._sync_screen_rig_to_current()
        queries: List[np.ndarray] = []
        for forward_proposal, backward_proposal in pair_items:
            forward_sub, backward_sub = rig.probe_pair_subpoints(
                self._proposal_entries(forward_proposal),
                self._proposal_entries(backward_proposal),
                self._screen_ib,
                lbs_transform_with_remainder,
            )
            queries.append(np.asarray(forward_sub, dtype=np.float32))
            queries.append(np.asarray(backward_sub, dtype=np.float32))
        batch_scores = self._screen_rank_batch_for_subpoints(
            np.asarray(queries, dtype=np.float32))
        if batch_scores is None or len(batch_scores) != 2 * len(pair_items):
            return None
        out: List[Tuple[float, float]] = []
        for index in range(len(pair_items)):
            out.append((
                float(batch_scores[2 * index]),
                float(batch_scores[2 * index + 1]),
            ))
        return out

    def _screen_metric_for_points(self, points: np.ndarray) -> PCMetric:
        if len(self._screen_a_points) == 0 or len(self._screen_ib) == 0:
            return self._metric_for_points(points)
        pts = np.asarray(points, dtype=float)
        if not np.isfinite(pts).all():
            return invalid_metric()
        b_sub = pts[self._screen_ib]
        if not np.isfinite(b_sub).all():
            return invalid_metric()
        if (self.backend is not None
                and getattr(self.backend, "is_gpu", False)
                and hasattr(self.backend, "nearest_transient")):
            try:
                d_b, _ = self.backend.nearest(self._screen_a_points, b_sub)
                d_a, _ = self.backend.nearest_transient(
                    b_sub, self._screen_a_points)
                return _finite_metric(metric_from_bidirectional_distances(
                    d_b, d_a, self.tau))
            except Exception:
                pass
        return overlap_metric(
            self._screen_a_points,
            b_sub,
            self._screen_nn_a,
            self._nn_factory(b_sub),
            self.tau,
        )

    def _metric_for_points(self, points: np.ndarray) -> PCMetric:
        pts = np.asarray(points, dtype=float)
        if not np.isfinite(pts).all():
            return invalid_metric()
        return overlap_metric(
            self.a_points,
            pts,
            self.nn_a,
            self._nn_factory(pts),
            self.tau,
            a_voxel_keys=self._a_voxel_keys,
        )

    def _screen_points_pair(
            self,
            forward_proposal: PCProposal,
            backward_proposal: PCProposal) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        rig = self.screen_rig
        if rig is None:
            return None
        self._sync_screen_rig_to_current()
        if hasattr(rig, "probe_pair_points"):
            forward_points, backward_points = rig.probe_pair_points(
                self._proposal_entries(forward_proposal),
                self._proposal_entries(backward_proposal),
                lbs_transform_with_remainder,
            )
        else:
            rig.set_basis_map(self.basis_map)
            for name, _before, after in self._proposal_entries(forward_proposal):
                rig.set_basis(name, after)
            forward_points = rig.read_samples(lbs_transform_with_remainder)

            rig.set_basis_map(self.basis_map)
            for name, _before, after in self._proposal_entries(backward_proposal):
                rig.set_basis(name, after)
            backward_points = rig.read_samples(lbs_transform_with_remainder)
        return forward_points, backward_points

    def _screen_rank_pair(
            self,
            forward_proposal: PCProposal,
            backward_proposal: PCProposal) -> Optional[Tuple[float, float]]:
        rig = self.screen_rig
        if rig is not None and hasattr(rig, "probe_pair_subpoints") and len(self._screen_ib) > 0:
            self._sync_screen_rig_to_current()
            forward_sub, backward_sub = rig.probe_pair_subpoints(
                self._proposal_entries(forward_proposal),
                self._proposal_entries(backward_proposal),
                self._screen_ib,
                lbs_transform_with_remainder,
            )
            return (
                self._screen_rank_for_subpoints(forward_sub),
                self._screen_rank_for_subpoints(backward_sub),
            )
        point_pair = self._screen_points_pair(
            forward_proposal, backward_proposal)
        if point_pair is None:
            return None
        forward_points, backward_points = point_pair
        return (
            self._screen_rank_for_points(forward_points),
            self._screen_rank_for_points(backward_points),
        )

    def _screen_metric_pair(
            self,
            forward_proposal: PCProposal,
            backward_proposal: PCProposal) -> Optional[Tuple[PCMetric, PCMetric]]:
        cache_key = self._pair_cache_key(
            forward_proposal, backward_proposal)
        cached = self._screen_metric_pair_cache.get(cache_key)
        if cached is not None:
            return cached
        rig = self.screen_rig
        if rig is not None and hasattr(rig, "probe_pair_subpoints") and len(self._screen_ib) > 0:
            self._sync_screen_rig_to_current()
            forward_sub, backward_sub = rig.probe_pair_subpoints(
                self._proposal_entries(forward_proposal),
                self._proposal_entries(backward_proposal),
                self._screen_ib,
                lbs_transform_with_remainder,
            )
            forward_arr = np.asarray(forward_sub, dtype=float)
            backward_arr = np.asarray(backward_sub, dtype=float)
            if (self.backend is not None
                    and getattr(self.backend, "is_gpu", False)
                    and hasattr(self.backend, "nearest_transient")):
                try:
                    forward_d_b, _ = self.backend.nearest(
                        self._screen_a_points, forward_arr)
                    forward_d_a, _ = self.backend.nearest_transient(
                        forward_arr, self._screen_a_points)
                    backward_d_b, _ = self.backend.nearest(
                        self._screen_a_points, backward_arr)
                    backward_d_a, _ = self.backend.nearest_transient(
                        backward_arr, self._screen_a_points)
                    forward_metric = _finite_metric(
                        metric_from_bidirectional_distances(
                            forward_d_b, forward_d_a, self.tau))
                    backward_metric = _finite_metric(
                        metric_from_bidirectional_distances(
                            backward_d_b, backward_d_a, self.tau))
                except Exception:
                    forward_metric = overlap_metric(
                        self._screen_a_points,
                        forward_arr,
                        self._screen_nn_a,
                        self._nn_factory(forward_arr),
                        self.tau,
                    )
                    backward_metric = overlap_metric(
                        self._screen_a_points,
                        backward_arr,
                        self._screen_nn_a,
                        self._nn_factory(backward_arr),
                        self.tau,
                    )
            else:
                forward_metric = overlap_metric(
                    self._screen_a_points,
                    forward_arr,
                    self._screen_nn_a,
                    self._nn_factory(forward_arr),
                    self.tau,
                )
                backward_metric = overlap_metric(
                    self._screen_a_points,
                    backward_arr,
                    self._screen_nn_a,
                    self._nn_factory(backward_arr),
                    self.tau,
                )
            out = (forward_metric, backward_metric)
            self._screen_metric_pair_cache[cache_key] = out
            return out
        point_pair = self._screen_points_pair(
            forward_proposal, backward_proposal)
        if point_pair is None:
            return None
        forward_points, backward_points = point_pair
        forward_metric = self._screen_metric_for_points(forward_points)
        backward_metric = self._screen_metric_for_points(backward_points)
        out = (forward_metric, backward_metric)
        self._screen_metric_pair_cache[cache_key] = out
        return out

    def _sync_screen_rig_to_current(self) -> None:
        rig = self.screen_rig
        if rig is None or not self._screen_rig_needs_sync:
            return
        rig.set_basis_map(self.basis_map)
        rig.refresh_pose()
        rig.current = np.asarray(self.b_points, dtype=np.float64).copy()
        rig._dirty = set()
        self._screen_rig_needs_sync = False

    def recompute_current_metric(self, update_best: bool = False) -> PCMetric:
        self._set_b_points(self.read_samples())
        self._touch_committed_state()
        self.current_metric = self._full_metric()
        if update_best and self.current_metric.f1 > self.best_f1 + EPS:
            self.best_f1 = self.current_metric.f1
            self.best_step = self.step_count
            self.best_snapshot = self._snapshot_state()
            self.best_reward = max(self.best_reward, 0.0)
        return self.current_metric

    def _bone_pivot(self, name: str) -> np.ndarray:
        return np.asarray(self.bone_point_provider(name, "pivot"), dtype=float)

    def _channel_matrix(self, name: str) -> np.ndarray:
        try:
            return np.asarray(self.bone_point_provider(name, "chan_to_local"), dtype=float)
        except Exception:
            return np.identity(3, dtype=float)

    def _unlocked_channels(self, spec: PCBoneSpec) -> Dict[str, Tuple[bool, bool, bool]]:
        return {
            TF_ROTATION: tuple(not v for v in spec.lock_rotation),
            TF_SCALE: tuple(not v for v in spec.lock_scale),
            TF_LOCATION: tuple(not v for v in spec.lock_location),
        }

    def _search_axis(self, spec: PCBoneSpec, tf_type: str, override: Optional[int] = None) -> Optional[int]:
        unlocked = self._unlocked_channels(spec)[tf_type]
        axes = [axis for axis in range(3) if unlocked[axis]]
        if not axes:
            return None
        if override in axes:
            return int(override)
        key = (spec.name, tf_type)
        cursor = self.axis_cursors.get(key, 0)
        return axes[cursor % len(axes)]

    def _isolate_deform_axis(self, spec: PCBoneSpec, tf_type: str, before: np.ndarray, after: np.ndarray, axis: int) -> np.ndarray:
        if tf_type == TF_SCALE:
            s0 = basis_scale(before)
            ratio = np.divide(basis_scale(after), s0, out=np.ones(3, dtype=float), where=s0 > EPS)
            isolated = np.ones(3, dtype=float)
            isolated[axis] = ratio[axis]
            return compose_basis(before[:3, 3], basis_rotation_matrix(before), s0 * isolated)
        chan = self._channel_matrix(spec.name)
        if tf_type == TF_LOCATION:
            values = chan.T @ (after[:3, 3] - before[:3, 3])
            isolated = np.zeros(3, dtype=float)
            isolated[axis] = values[axis]
            return compose_basis(before[:3, 3] + chan @ isolated, basis_rotation_matrix(before), basis_scale(before))
        delta = basis_rotation_matrix(after) @ basis_rotation_matrix(before).T
        delta_chan = chan.T @ delta @ chan
        if spec.rotation_mode == "QUATERNION":
            values = mat3_to_rotvec(delta_chan)
            isolated = np.zeros(3, dtype=float)
            isolated[axis] = values[axis]
            isolated_delta = rotvec_to_mat3(isolated)
        else:
            values = mat3_to_euler_xyz(delta_chan)
            isolated = np.zeros(3, dtype=float)
            isolated[axis] = values[axis]
            isolated_delta = euler_xyz_to_mat3(isolated)
        return compose_basis(before[:3, 3], chan @ isolated_delta @ chan.T @ basis_rotation_matrix(before), basis_scale(before))

    def _parameter_step(self, key: Tuple[str, str], initial: float, low: float, high: float) -> float:
        if key not in self.step_sizes:
            self.step_sizes[key] = float(np.clip(initial, low, high))
            self.step_size_limits[key] = (float(low), float(high))
            self.step_size_max_seen[key] = self.step_sizes[key]
        return self.step_sizes[key]

    def _adapt_parameter_step(self, proposal: PCProposal, accepted: bool) -> None:
        key = (proposal.bone_name, proposal.tf_type)
        low, high = self.step_size_limits.get(key, (1e-4, 1e6))
        current = self.step_sizes.get(key, 1.0)
        if accepted:
            current = min(high, current * 1.5)
            self.step_reject_streak[key] = 0
        else:
            current = max(low, current * 0.5)
            self.step_reject_streak[key] = self.step_reject_streak.get(key, 0) + 1
            spec = self.bones.get(proposal.bone_name)
            if spec is not None:
                unlocked = [i for i, on in enumerate(self._unlocked_channels(spec)[proposal.tf_type]) if on]
                if unlocked:
                    cursor = self.axis_cursors.get(key, 0)
                    try:
                        position = unlocked.index(proposal.axis) if proposal.axis is not None else cursor
                    except ValueError:
                        position = cursor
                    self.axis_cursors[key] = (position + 1) % len(unlocked)
        self.step_sizes[key] = current
        self.step_size_max_seen[key] = max(self.step_size_max_seen.get(key, current), current)

    def _candidate_scale_factors(
            self, spec: PCBoneSpec, tf_type: str,
            plateau_refine: bool = False) -> Tuple[float, ...]:
        scales: List[float] = [1.0, 0.5, 0.25, 2.0]
        if not plateau_refine:
            return tuple(scales)
        key = (spec.name, tf_type)
        current = max(self._current_step_size(spec, tf_type), EPS)
        max_seen = max(current, self.step_size_max_seen.get(key, current))
        streak = int(self.axis_f1_plateau_streak.get(key, 0))
        if streak <= 0 and current >= max_seen * 0.75:
            return tuple(scales)
        revisit = float(np.clip(max_seen / current, 2.0, 8.0))
        if all(abs(revisit - value) > 1e-9 for value in scales):
            scales.append(revisit)
        if streak >= 3:
            wider = float(np.clip(revisit * 2.0, 4.0, 8.0))
            if all(abs(wider - value) > 1e-9 for value in scales):
                scales.append(wider)
        return tuple(scales)

    def _observe_axis_f1_progress(
            self, proposal: PCProposal,
            accepted: bool, f1_delta: float) -> None:
        if not accepted:
            return
        key = (proposal.bone_name, proposal.tf_type)
        delta = float(f1_delta)
        if delta > EPS:
            self.axis_f1_plateau_streak[key] = 0
        elif delta >= -EPS:
            self.axis_f1_plateau_streak[key] = (
                self.axis_f1_plateau_streak.get(key, 0) + 1)
        else:
            self.axis_f1_plateau_streak[key] = 0

    def _proposal_axis_sign(self, proposal: PCProposal) -> int:
        if proposal.axis is None:
            return 1
        components = self._proposal_channel_components(proposal)
        value = float(components[int(proposal.axis)])
        return -1 if value < 0.0 else 1

    def _high_overlap_escape_probe(
            self,
            proposal: PCProposal,
            baseline: PCMetric,
            reward_fn: Callable[[PCMetric, PCMetric], float],
            improves_fn: Callable[[PCMetric, PCMetric], bool]) -> bool:
        if proposal.axis is None:
            return False
        spec = self.bones.get(proposal.bone_name)
        if spec is None:
            return False
        direction = self._proposal_axis_sign(proposal)
        max_reward = -float("inf")
        for probe_direction in (direction, -direction):
            for scale in (0.25, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0):
                candidate = self._axis_probe_proposal(
                    spec, proposal.tf_type, int(proposal.axis), probe_direction,
                    scale=float(scale))
                metric = self._probe_metric(candidate, self._full_metric)
                reward = float(reward_fn(metric, baseline))
                max_reward = max(max_reward, reward)
                if metric.f1 > baseline.f1 + EPS:
                    return True
                if improves_fn(metric, baseline) and reward > 5e-7:
                    return True
        return False

    def _influence_world_points(self, spec: PCBoneSpec) -> Tuple[np.ndarray, np.ndarray]:
        idx = np.asarray(spec.influence_indices, dtype=np.int64)
        weights = np.asarray(spec.influence_weights, dtype=np.float64)
        if len(idx) == 0:
            return np.zeros((0, 3), dtype=float), np.zeros(0, dtype=float)
        points = self.b_points[idx]
        if len(points) > self.config.residual_samples:
            keep = np.linspace(
                0, len(points) - 1, int(self.config.residual_samples),
                dtype=np.int64)
            points = points[keep]
            weights = weights[keep]
        return points.copy(), weights.copy()

    def _minibatch_active(self) -> bool:
        return int(self.config.minibatch_size) > 0

    def _small_exact_search_active(self) -> bool:
        return (
            not self._minibatch_active()
            and len(self.a_points) >= 1000
            and sum(1 for spec in self.bones.values() if spec.enabled) <= 2
        )

    def _minibatch_metric(self, ia: np.ndarray, ib: np.ndarray) -> PCMetric:
        ia = np.asarray(ia, dtype=np.int64)
        ib = np.asarray(ib, dtype=np.int64)
        q_a = self.a_points[ia] if len(ia) else self.a_points[:0]
        q_b = self.b_points[ib] if len(ib) else self.b_points[:0]
        if self.backend is not None and hasattr(self.backend, "nearest"):
            d_b, _ = self.backend.nearest(self._minibatch_ref_a_points, q_b)
            d_a, _ = self.backend.nearest(self._minibatch_ref_b_points, q_a)
        else:
            d_b, _ = brute_force_nn(self._minibatch_ref_a_points)(q_b)
            d_a, _ = brute_force_nn(self._minibatch_ref_b_points)(q_a)
        precision = float(np.mean(d_b <= self.tau)) if len(d_b) else 0.0
        recall = float(np.mean(d_a <= self.tau)) if len(d_a) else 0.0
        denom = precision + recall
        f1 = 2.0 * precision * recall / denom if denom > EPS else 0.0
        score = soft_overlap_score(d_b, d_a, self.tau)
        chamfer = float(0.5 * ((float(np.mean(d_b)) if len(d_b) else float("inf")) + (float(np.mean(d_a)) if len(d_a) else float("inf"))))
        return PCMetric(
            f1=f1, precision=precision, recall=recall,
            chamfer=chamfer, score=score)

    def _sample_minibatch_indices(self) -> Tuple[np.ndarray, np.ndarray]:
        batch = max(1, int(self.config.minibatch_size))
        if len(self.a_points) <= batch:
            ia = np.arange(len(self.a_points), dtype=np.int64)
        else:
            ia = np.sort(self.rng.choice(len(self.a_points), batch, replace=False)).astype(np.int64)
        if len(self.b_points) <= batch:
            ib = np.arange(len(self.b_points), dtype=np.int64)
        else:
            ib = np.sort(self.rng.choice(len(self.b_points), batch, replace=False)).astype(np.int64)
        return ia, ib

    def _axis_probe_proposal(
            self, spec: PCBoneSpec, tf_type: str, axis: int,
            sign: int, scale: float = 1.0) -> PCProposal:
        before = self.basis_map.get(spec.name, np.identity(4, dtype=float))
        step = self._current_step_size(spec, tf_type) * float(scale)
        if tf_type == TF_ROTATION:
            chan = self._channel_matrix(spec.name)
            vector = np.zeros(3, dtype=float)
            vector[axis] = float(sign) * step
            if spec.rotation_mode == "QUATERNION":
                local_delta = rotvec_to_mat3(vector)
            else:
                local_delta = euler_xyz_to_mat3(vector)
            after = compose_basis(
                before[:3, 3],
                chan @ local_delta @ chan.T @ basis_rotation_matrix(before),
                basis_scale(before))
        elif tf_type == TF_SCALE:
            factors = np.ones(3, dtype=float)
            factors[axis] = max(MIN_SCALE_ABS, 1.0 + float(sign) * step)
            after = compose_basis(
                before[:3, 3], basis_rotation_matrix(before),
                basis_scale(before) * factors)
        else:
            chan = self._channel_matrix(spec.name)
            move = np.zeros(3, dtype=float)
            move[axis] = float(sign) * step
            after = compose_basis(
                before[:3, 3] + chan @ move,
                basis_rotation_matrix(before), basis_scale(before))
        return PCProposal(spec.name, spec.kind, axis, before, after, tf_type)

    def _probe_metric(
            self, proposal: PCProposal,
            metric_fn: Callable[[], PCMetric]) -> PCMetric:
        baseline_points = np.asarray(self.b_points, dtype=float).copy()
        baseline_nn_b = self.nn_b
        baseline_minibatch = (
            self._minibatch_ref_b_points.copy()
            if self._minibatch_active() else None
        )
        self._apply_proposal_group(proposal, use_after=True)
        try:
            points = np.asarray(self.read_samples(), dtype=float)
            if not np.isfinite(points).all():
                return invalid_metric()
            self._set_b_points(points)
            return _finite_metric(metric_fn())
        except Exception:
            return invalid_metric()
        finally:
            self._apply_proposal_group(proposal, use_after=False)
            if self.restore_sample_cache is not None:
                self._restore_b_points_cache(
                    baseline_points, baseline_nn_b, baseline_minibatch)
                try:
                    self.restore_sample_cache(baseline_points)
                except Exception:
                    self._set_b_points(self.read_samples())
            else:
                self._set_b_points(self.read_samples())

    def _probe_metric_pair(
            self,
            forward_proposal: PCProposal,
            backward_proposal: PCProposal,
            metric_fn: Callable[[], PCMetric]) -> Tuple[PCMetric, PCMetric]:
        cache_key = self._pair_cache_key(
            forward_proposal, backward_proposal) + (
                self._metric_fn_cache_key(metric_fn),)
        cached = self._exact_metric_pair_cache.get(cache_key)
        if cached is not None:
            return cached
        baseline_points = np.asarray(self.b_points, dtype=float).copy()
        baseline_nn_b = self.nn_b
        baseline_minibatch = (
            self._minibatch_ref_b_points.copy()
            if self._minibatch_active() else None
        )
        self._apply_proposal_group(forward_proposal, use_after=True)
        try:
            forward_points = np.asarray(self.read_samples(), dtype=float)
            if not np.isfinite(forward_points).all():
                forward_metric = invalid_metric()
            else:
                self._set_b_points(forward_points)
                forward_metric = _finite_metric(metric_fn())

            self._apply_proposal_group(backward_proposal, use_after=True)
            backward_points = np.asarray(self.read_samples(), dtype=float)
            if not np.isfinite(backward_points).all():
                backward_metric = invalid_metric()
            else:
                self._set_b_points(backward_points)
                backward_metric = _finite_metric(metric_fn())
        except Exception:
            forward_metric = invalid_metric()
            backward_metric = invalid_metric()
        finally:
            self._apply_proposal_group(forward_proposal, use_after=False)
            if self.restore_sample_cache is not None:
                self._restore_b_points_cache(
                    baseline_points, baseline_nn_b, baseline_minibatch)
                try:
                    self.restore_sample_cache(baseline_points)
                except Exception:
                    self._set_b_points(self.read_samples())
            else:
                self._set_b_points(self.read_samples())
        out = (forward_metric, backward_metric)
        self._exact_metric_pair_cache[cache_key] = out
        return out

    def _step_small_exact_search(
            self, stage_hops: int = 0) -> Optional[PCStepResult]:
        if (self.current_metric.f1 >= 0.98
                and self._high_overlap_no_f1_accepts >=
                self.high_overlap_accept_patience):
            return None
        current_spec = self._pick_bone()
        if current_spec is None:
            return None
        ordered_names = list(self._bone_curriculum_order)
        if not ordered_names:
            enabled_specs = [spec for spec in self.bones.values() if spec.enabled]
        else:
            start = min(self._bone_curriculum_index, len(ordered_names) - 1)
            ordered_names = ordered_names[start:] + ordered_names[:start]
            enabled_specs = [
                self.bones[name] for name in ordered_names
                if name in self.bones and self.bones[name].enabled
            ]
        if not enabled_specs:
            return None
        reward_fn = overlap_first_reward
        improves_fn = overlap_first_improves
        probe_spec = enabled_specs[0]
        weights = self.schedule.weights_for(self._unlocked_channels(probe_spec))
        while not weights and self.schedule.stage < 4:
            self.schedule.advance_stage()
            weights = self.schedule.weights_for(self._unlocked_channels(probe_spec))
        if not weights:
            return None
        if self.schedule.stage >= 3:
            tf_types = sorted(weights, key=weights.get, reverse=True)
        else:
            tf_types = [max(weights, key=weights.get)]
        before_metric = self.current_metric
        high_overlap_refine = before_metric.f1 >= 0.98
        plateau_signal = (
            self._bone_curriculum_no_gain >= 2 or
            any(
                self.axis_f1_plateau_streak.get((spec.name, tf_type), 0) >= 2
                for spec in enabled_specs for tf_type in tf_types
            )
        )
        plateau_refine_step = (
            high_overlap_refine and plateau_signal and
            (self.step_count % 10 == 0)
        )
        tail_stop_step = high_overlap_refine and self.step_count >= 20

        best_positive = None
        best_positive_metric = None
        best_positive_forward = None
        best_positive_forward_metric = None
        best_positive_backward = None
        best_positive_backward_metric = None
        best_overall = None
        best_overall_metric = None
        best_overall_forward = None
        best_overall_forward_metric = None
        best_overall_backward = None
        best_overall_backward_metric = None
        any_f1_positive = False

        def consider_pair(
                forward_proposal: PCProposal, forward_metric: PCMetric,
                backward_proposal: PCProposal,
                backward_metric: PCMetric) -> None:
            nonlocal best_positive, best_positive_metric
            nonlocal best_positive_forward, best_positive_forward_metric
            nonlocal best_positive_backward, best_positive_backward_metric
            nonlocal best_overall, best_overall_metric
            nonlocal best_overall_forward, best_overall_forward_metric
            nonlocal best_overall_backward, best_overall_backward_metric
            nonlocal any_f1_positive

            forward_reward = reward_fn(forward_metric, before_metric)
            backward_reward = reward_fn(backward_metric, before_metric)
            if (forward_metric.f1 > before_metric.f1 + EPS
                    or backward_metric.f1 > before_metric.f1 + EPS):
                any_f1_positive = True

            chosen_proposal = forward_proposal
            chosen_metric = forward_metric
            chosen_reward = forward_reward
            if backward_reward > chosen_reward:
                chosen_proposal = backward_proposal
                chosen_metric = backward_metric
                chosen_reward = backward_reward

            if (best_overall_metric is None
                    or chosen_reward > reward_fn(
                        best_overall_metric, before_metric)):
                best_overall = chosen_proposal
                best_overall_metric = chosen_metric
                best_overall_forward = forward_proposal
                best_overall_forward_metric = forward_metric
                best_overall_backward = backward_proposal
                best_overall_backward_metric = backward_metric

            positive_candidates = []
            if improves_fn(forward_metric, before_metric):
                positive_candidates.append(
                    (forward_reward, forward_proposal, forward_metric))
            if improves_fn(backward_metric, before_metric):
                positive_candidates.append(
                    (backward_reward, backward_proposal, backward_metric))
            if not positive_candidates:
                return
            positive_candidates.sort(key=lambda item: item[0], reverse=True)
            top_reward, top_proposal, top_metric = positive_candidates[0]
            if (best_positive_metric is None
                    or top_reward > reward_fn(
                        best_positive_metric, before_metric)):
                best_positive = top_proposal
                best_positive_metric = top_metric
                best_positive_forward = forward_proposal
                best_positive_forward_metric = forward_metric
                best_positive_backward = backward_proposal
                best_positive_backward_metric = backward_metric

        fallback_screen_topk = 3 if before_metric.f1 >= 0.97 else 2

        def evaluate_specs(
                specs: List[PCBoneSpec],
                tf_type_order: Optional[List[str]] = None) -> bool:
            nonlocal best_positive, best_positive_metric
            order = list(tf_type_order) if tf_type_order is not None else list(tf_types)
            starting_reward = (
                reward_fn(best_positive_metric, before_metric)
                if best_positive_metric is not None else -float("inf"))
            for tf_type in order:
                # First pass: use the normal analytic proposal path for the
                # current transform type and stop descending the priority list
                # once we find a real positive move.
                for spec in specs:
                    proposal = self._proposal_for_spec(spec, tf_type)
                    if proposal is None:
                        continue
                    backward_proposal = PCProposal(
                        bone_name=proposal.bone_name,
                        kind=proposal.kind,
                        axis=proposal.axis,
                        basis_before=proposal.basis_before,
                        basis_after=opposite_proposal_basis(proposal),
                        tf_type=proposal.tf_type,
                    )
                    forward_metric, backward_metric = self._probe_metric_pair(
                        proposal, backward_proposal, self._full_metric)
                    consider_pair(
                        proposal, forward_metric, backward_proposal,
                        backward_metric)
                    if (best_positive_metric is not None
                            and reward_fn(best_positive_metric, before_metric)
                            > starting_reward + EPS):
                        return True

                # Fallback only when the analytic path found no positive
                # direction for the current transform type.
                pair_candidates = []
                single_candidates: List[Tuple[float, PCProposal]] = []
                pair_items: List[Tuple[PCProposal, PCProposal]] = []
                use_batched_rank = (
                    not high_overlap_refine
                    and self.backend is not None
                    and hasattr(self.backend, "score_batch")
                    and self.screen_rig is not None
                    and len(self._screen_ib) > 0
                )
                screen_baseline_rank = (
                    self._screen_rank_for_points(self.b_points)
                    if use_batched_rank else 0.0
                )
                screen_baseline = (
                    None if use_batched_rank
                    else self._screen_metric_for_points(self.b_points)
                )
                for spec in specs:
                    local_scales = self._candidate_scale_factors(
                        spec, tf_type, plateau_refine_step)
                    unlocked = self._unlocked_channels(spec)[tf_type]
                    for axis in range(3):
                        if not unlocked[axis]:
                            continue
                        for scale in local_scales:
                            forward_proposal = self._axis_probe_proposal(
                                spec, tf_type, axis, 1, scale=scale)
                            backward_proposal = self._axis_probe_proposal(
                                spec, tf_type, axis, -1, scale=scale)
                            pair_items.append(
                                (forward_proposal, backward_proposal))
                if use_batched_rank:
                    batched_scores = self._screen_rank_batch_for_pairs(pair_items)
                    if batched_scores is not None:
                        for (forward_proposal, backward_proposal), (
                                forward_rank,
                                backward_rank) in zip(pair_items, batched_scores):
                            single_candidates.append((
                                float(forward_rank - screen_baseline_rank),
                                forward_proposal,
                            ))
                            single_candidates.append((
                                float(backward_rank - screen_baseline_rank),
                                backward_proposal,
                            ))
                    else:
                        use_batched_rank = False
                        screen_baseline = self._screen_metric_for_points(
                            self.b_points)
                if not use_batched_rank:
                    for forward_proposal, backward_proposal in pair_items:
                        screen_pair = self._screen_metric_pair(
                            forward_proposal, backward_proposal)
                        if screen_pair is None:
                            pair_candidates.append((
                                0.0,
                                forward_proposal,
                                backward_proposal,
                            ))
                        else:
                            forward_screen, backward_screen = screen_pair
                            pair_candidates.append((
                                max(
                                    reward_fn(forward_screen, screen_baseline),
                                    reward_fn(backward_screen, screen_baseline),
                                ),
                                forward_proposal,
                                backward_proposal,
                            ))
                if use_batched_rank:
                    single_candidates.sort(
                        key=lambda item: item[0], reverse=True)
                    if not single_candidates:
                        continue
                    exact_topk = min(
                        len(single_candidates),
                        4 if before_metric.f1 >= 0.95 else 3,
                    )
                    exact_candidates = single_candidates[:exact_topk]
                    for (_screen_reward, proposal) in exact_candidates:
                        metric = self._probe_metric(
                            proposal, self._full_metric)
                        opposite = PCProposal(
                            bone_name=proposal.bone_name,
                            kind=proposal.kind,
                            axis=proposal.axis,
                            basis_before=proposal.basis_before,
                            basis_after=opposite_proposal_basis(proposal),
                            tf_type=proposal.tf_type,
                        )
                        consider_pair(
                            proposal, metric,
                            opposite, invalid_metric())
                else:
                    pair_candidates.sort(key=lambda item: item[0], reverse=True)
                    if not pair_candidates:
                        continue
                    screen_topk = fallback_screen_topk
                    if plateau_refine_step:
                        screen_topk = min(len(pair_candidates), max(screen_topk, 8))
                    exact_candidates = pair_candidates[:screen_topk]
                    for (_screen_reward,
                         forward_proposal,
                         backward_proposal) in exact_candidates:
                        forward_metric, backward_metric = self._probe_metric_pair(
                            forward_proposal, backward_proposal,
                            self._full_metric)
                        consider_pair(
                            forward_proposal, forward_metric,
                            backward_proposal, backward_metric)
                if (best_positive_metric is not None
                        and reward_fn(best_positive_metric, before_metric)
                        > starting_reward + EPS):
                    return True
            return False

        def search_other_specs_for_f1_gain(specs: List[PCBoneSpec]) -> bool:
            for tf_type in tf_types:
                for spec in specs:
                    proposal = self._proposal_for_spec(spec, tf_type)
                    if proposal is None:
                        continue
                    backward_proposal = PCProposal(
                        bone_name=proposal.bone_name,
                        kind=proposal.kind,
                        axis=proposal.axis,
                        basis_before=proposal.basis_before,
                        basis_after=opposite_proposal_basis(proposal),
                        tf_type=proposal.tf_type,
                    )
                    forward_metric, backward_metric = self._probe_metric_pair(
                        proposal, backward_proposal, self._full_metric)
                    consider_pair(
                        proposal, forward_metric, backward_proposal,
                        backward_metric)
                    if ((forward_metric.f1 > before_metric.f1 + EPS)
                            or (backward_metric.f1 > before_metric.f1 + EPS)):
                        return True

                for spec in specs:
                    local_scales = self._candidate_scale_factors(
                        spec, tf_type, True)
                    unlocked = self._unlocked_channels(spec)[tf_type]
                    for axis in range(3):
                        if not unlocked[axis]:
                            continue
                        for scale in local_scales:
                            forward_proposal = self._axis_probe_proposal(
                                spec, tf_type, axis, 1, scale=scale)
                            backward_proposal = self._axis_probe_proposal(
                                spec, tf_type, axis, -1, scale=scale)
                            forward_metric, backward_metric = self._probe_metric_pair(
                                forward_proposal, backward_proposal,
                                self._full_metric)
                            consider_pair(
                                forward_proposal, forward_metric,
                                backward_proposal, backward_metric)
                            if ((forward_metric.f1 > before_metric.f1 + EPS)
                                    or (backward_metric.f1 > before_metric.f1 + EPS)):
                                return True
            return False

        primary_specs = enabled_specs[:1]
        evaluate_specs(primary_specs)
        if (high_overlap_refine
                and len(enabled_specs) > 1
                and (best_positive_metric is None
                     or best_positive_metric.f1 <= before_metric.f1 + EPS)):
            evaluate_specs(enabled_specs[1:])
        if (best_positive is not None
                and best_positive_metric is not None
                and before_metric.f1 >= 0.90
                and before_metric.f1 < 0.98
                and best_positive_metric.f1 <= before_metric.f1 + EPS
                and reward_fn(best_positive_metric, before_metric) <= 1e-4):
            alt_tf_types = [t for t in TF_TYPES if t not in tf_types]
            if alt_tf_types:
                evaluate_specs(primary_specs, alt_tf_types)
        if best_positive is None and plateau_refine_step and len(enabled_specs) > 1:
            evaluate_specs(enabled_specs[1:])

        if best_positive is not None and best_positive_metric is not None:
            if (plateau_refine_step
                    and len(enabled_specs) > 1
                    and best_positive_metric.f1 <= before_metric.f1 + EPS):
                if search_other_specs_for_f1_gain(enabled_specs[1:]):
                    pass
            if (tail_stop_step
                    and best_positive_metric.f1 <= before_metric.f1 + EPS
                    and not any_f1_positive):
                return None
            if (high_overlap_refine
                    and best_positive_metric.f1 <= before_metric.f1 + EPS
                    and reward_fn(best_positive_metric, before_metric) <= 5e-7
                    and not self._high_overlap_escape_probe(
                        best_positive, before_metric, reward_fn, improves_fn)):
                return None
            self._apply_proposal_group(best_positive, use_after=True)
            self._set_b_points(self.read_samples())
            self._commit_proposal_group(best_positive, already_applied=True)
            self.current_metric = best_positive_metric
            if best_positive_metric.f1 > before_metric.f1 + EPS:
                self._high_overlap_no_f1_accepts = 0
            elif self.current_metric.f1 >= 0.98:
                self._high_overlap_no_f1_accepts += 1
            else:
                self._high_overlap_no_f1_accepts = 0
            self._clear_proposal_reject_cycles(best_positive)
            self._remember_group_momentum(best_positive)
            self._observe_axis_f1_progress(
                best_positive, True,
                float(best_positive_metric.f1 - before_metric.f1))
            self._update_bone_curriculum(
                reward_fn(best_positive_metric, before_metric), accepted=True,
                f1_gain=float(best_positive_metric.f1 - before_metric.f1))
            self._adapt_parameter_step(best_positive, accepted=True)
            self._store_debug_payload(
                before_metric,
                best_positive_forward, best_positive_forward_metric,
                best_positive_backward, best_positive_backward_metric,
                best_positive, True, reward_fn=reward_fn)
            return self._make_step_result(
                best_positive, True, before_metric, best_positive_metric,
                reward_fn=reward_fn)

        if stage_hops < 4 and self.schedule.stage < 4:
            previous_stage = self.schedule.stage
            self.schedule.advance_stage()
            if self.schedule.stage != previous_stage:
                return self._step_small_exact_search(stage_hops + 1)

        if best_overall is None or best_overall_metric is None:
            return None
        self.current_metric = before_metric
        if self.current_metric.f1 < 0.98:
            self._high_overlap_no_f1_accepts = 0
        self._register_rejected_bone_attempt(best_overall)
        self._clear_group_momentum(best_overall)
        self._adapt_parameter_step(best_overall, accepted=False)
        self._store_debug_payload(
            before_metric,
            best_overall_forward, best_overall_forward_metric,
            best_overall_backward, best_overall_backward_metric,
            best_overall, False, reward_fn=reward_fn)
        return self._make_step_result(
            best_overall, False, before_metric, best_overall_metric,
            display_metric=before_metric, reward_fn=reward_fn)

    def _pick_bone(self) -> Optional[PCBoneSpec]:
        enabled = [b for b in self.bones.values() if b.enabled]
        if not enabled:
            return None
        signature = (self.schedule.stage, tuple(sorted((b.name, int(len(b.influence_indices))) for b in enabled)))
        if signature != self._bone_curriculum_signature:
            self._bone_curriculum_signature = signature
            self._bone_curriculum_order = [
                b.name for b in sorted(
                    enabled, key=lambda b: (-len(b.influence_indices), b.name))
            ]
            self._bone_curriculum_index = 0
            self._bone_curriculum_no_gain = 0
            self._bone_curriculum_pass = 0
        if not self._bone_curriculum_order:
            return None
        index = min(self._bone_curriculum_index, len(self._bone_curriculum_order) - 1)
        spec = self.bones[self._bone_curriculum_order[index]]
        self.bone_pick_counts[spec.name] = self.bone_pick_counts.get(spec.name, 0) + 1
        self.bone_last_pick_step[spec.name] = self.step_count
        return spec

    def _update_bone_gain(self, name: str, gain: float) -> None:
        ema = self.bone_gain_ema.get(name, 0.0)
        ema = ema * 0.9 + 0.1 * float(gain)
        self.bone_gain_ema[name] = ema

    def _update_bone_curriculum(
            self, gain: float, accepted: bool = True,
            f1_gain: Optional[float] = None) -> None:
        if not accepted or not self._bone_curriculum_order:
            return
        current_name = self._bone_curriculum_order[min(
            self._bone_curriculum_index, len(self._bone_curriculum_order) - 1)]
        current = self.bones[current_name]
        self._update_bone_gain(current.name, gain)
        meaningful_f1_gain = EPS
        effective_f1_gain = (
            float(f1_gain) if f1_gain is not None else float(gain))
        if effective_f1_gain > meaningful_f1_gain:
            self._bone_curriculum_no_gain = 0
            self.bone_no_gain_streak[current.name] = 0
            return
        self._bone_curriculum_no_gain += 1
        self.bone_no_gain_streak[current.name] = self._bone_curriculum_no_gain
        if self._bone_curriculum_no_gain >= self.bone_curriculum_patience:
            if self._bone_curriculum_index < len(self._bone_curriculum_order) - 1:
                self._bone_curriculum_index += 1
            self._bone_curriculum_no_gain = 0

    def _clear_bone_reject_cycle(self, bone_name: str, tf_type: str) -> None:
        self.bone_reject_axes.pop((bone_name, tf_type), None)

    def _register_rejected_bone_attempt(self, proposal: PCProposal) -> None:
        if not self._bone_curriculum_order or proposal.axis is None:
            return
        spec = self.bones.get(proposal.bone_name)
        if spec is None:
            return
        unlocked = self._unlocked_channels(spec)[proposal.tf_type]
        axis_count = sum(1 for flag in unlocked if flag)
        if axis_count <= 0:
            return
        key = (proposal.bone_name, proposal.tf_type)
        tried = self.bone_reject_axes.setdefault(key, set())
        tried.add(int(proposal.axis))
        if len(tried) < axis_count:
            return
        tried.clear()
        current_name = self._bone_curriculum_order[min(
            self._bone_curriculum_index, len(self._bone_curriculum_order) - 1)]
        if current_name != proposal.bone_name:
            return
        self._bone_curriculum_no_gain += 1
        self.bone_no_gain_streak[current_name] = self._bone_curriculum_no_gain
        if self._bone_curriculum_no_gain >= self.bone_curriculum_patience:
            if self._bone_curriculum_index < len(self._bone_curriculum_order) - 1:
                self._bone_curriculum_index += 1
            self._bone_curriculum_no_gain = 0

    def _clear_proposal_reject_cycles(self, proposal: PCProposal) -> None:
        for name, _before, _after in self._proposal_entries(proposal):
            self._clear_bone_reject_cycle(name, proposal.tf_type)

    def _current_step_size(self, spec: PCBoneSpec, tf_type: str) -> float:
        key = (spec.name, tf_type)
        if tf_type == TF_ROTATION:
            initial = math.radians(max(0.1, float(self.config.max_angle_deg)))
            low = math.radians(0.1)
            high = math.radians(45.0)
        elif tf_type == TF_SCALE:
            initial = max(1e-3, float(self.config.max_scale_delta))
            low = 1e-3
            high = 1.0
        else:
            initial = max(1e-4, self._a_diag * float(self.config.max_translation_ratio))
            low = max(1e-5, initial * 0.1)
            high = self._a_diag
        return self._parameter_step(key, initial=initial, low=low, high=high)

    def _proposal_for_spec(self, spec: PCBoneSpec, tf_type: str, axis_override: Optional[int] = None) -> Optional[PCProposal]:
        if not spec.enabled:
            return None
        unlocked = self._unlocked_channels(spec)[tf_type]
        axes = [axis for axis in range(3) if unlocked[axis]]
        if not axes:
            return None
        before = self.basis_map.get(spec.name, np.identity(4, dtype=float))
        key = (spec.name, tf_type)
        if key in self.proposal_momentum:
            delta = self.proposal_momentum[key]
            axis = self.proposal_momentum_axis.get(key)
            after = apply_transform_delta(tf_type, before, delta)
            return PCProposal(spec.name, spec.kind, axis, before, after, tf_type)
        raw_idx = np.asarray(spec.influence_indices, dtype=np.int64)
        raw_weights = np.asarray(spec.influence_weights, dtype=np.float64)
        if len(raw_idx) > self.config.residual_samples:
            keep = np.linspace(0, len(raw_idx) - 1, int(self.config.residual_samples), dtype=np.int64)
            raw_idx = raw_idx[keep]
            raw_weights = raw_weights[keep]
        points = self.b_points[raw_idx]
        weights = raw_weights
        if len(points) == 0:
            axis = self._search_axis(spec, tf_type, axis_override)
            if axis is None:
                return None
            step = self._current_step_size(spec, tf_type)
            if tf_type == TF_ROTATION:
                probe = np.zeros(3, dtype=float)
                probe[axis] = step
                chan = self._channel_matrix(spec.name)
                delta = rotvec_to_mat3(probe)
                after = compose_basis(before[:3, 3], chan @ delta @ chan.T @ basis_rotation_matrix(before), basis_scale(before))
            elif tf_type == TF_SCALE:
                factors = np.ones(3, dtype=float)
                factors[axis] = 1.0 + step
                after = compose_basis(before[:3, 3], basis_rotation_matrix(before), basis_scale(before) * factors)
            else:
                chan = self._channel_matrix(spec.name)
                move = np.zeros(3, dtype=float)
                move[axis] = step
                after = compose_basis(before[:3, 3] + chan @ move, basis_rotation_matrix(before), basis_scale(before))
            return PCProposal(spec.name, spec.kind, axis, before, after, tf_type)

        if len(self.a_points) == len(self.b_points):
            targets = self.a_points[np.clip(raw_idx, 0, max(0, len(self.a_points) - 1))]
        else:
            target_idx = np.asarray(self.nn_a(points)[1], dtype=np.int64)
            targets = self.a_points[target_idx]
        pivot = self._bone_pivot(spec.name)
        chan = self._channel_matrix(spec.name)
        if tf_type == TF_LOCATION:
            current_centroid = np.average(points, axis=0, weights=weights)
            target_centroid = np.average(targets, axis=0, weights=weights)
            delta_world = target_centroid - current_centroid
            delta_chan = chan.T @ delta_world
            fallback = self._search_axis(spec, tf_type, axis_override)
            if fallback is None:
                return None
            if np.linalg.norm(delta_chan) < EPS:
                axis = fallback
                delta_chan = np.zeros(3, dtype=float)
                delta_chan[axis] = self._current_step_size(spec, tf_type)
            else:
                key = (spec.name, tf_type)
                axis = fallback if self.step_reject_streak.get(key, 0) > 0 else int(np.argmax(np.abs(delta_chan)))
                value = delta_chan[axis]
                step = self._current_step_size(spec, tf_type)
                delta_chan = np.zeros(3, dtype=float)
                delta_chan[axis] = value if abs(value) > EPS else step
            after = compose_basis(before[:3, 3] + chan @ delta_chan, basis_rotation_matrix(before), basis_scale(before))
            return PCProposal(spec.name, spec.kind, axis, before, after, tf_type)

        if tf_type == TF_SCALE:
            current = (points - pivot[None, :]) @ chan.T
            target = (targets - pivot[None, :]) @ chan.T
            cur_rms = np.sqrt(np.maximum(np.average(current * current, axis=0, weights=weights), EPS))
            tar_rms = np.sqrt(np.maximum(np.average(target * target, axis=0, weights=weights), EPS))
            ratio = np.divide(tar_rms, cur_rms, out=np.ones(3, dtype=float), where=cur_rms > EPS)
            fallback = self._search_axis(spec, tf_type, axis_override)
            if fallback is None:
                return None
            if np.linalg.norm(ratio - 1.0) < EPS:
                axis = fallback
                value = 0.0
            else:
                key = (spec.name, tf_type)
                axis = fallback if self.step_reject_streak.get(key, 0) > 0 else int(np.argmax(np.abs(ratio - 1.0)))
                value = ratio[axis] - 1.0
            step = self._current_step_size(spec, tf_type)
            factors = np.ones(3, dtype=float)
            factors[axis] = 1.0 + (value if abs(value) > EPS else step)
            after = compose_basis(before[:3, 3], basis_rotation_matrix(before), basis_scale(before) * factors)
            return PCProposal(spec.name, spec.kind, axis, before, after, tf_type)

        delta_world = kabsch_rotation(points, targets, weights, pivot=pivot)
        delta_chan = chan.T @ delta_world @ chan
        if spec.rotation_mode == "QUATERNION":
            values = mat3_to_rotvec(delta_chan)
        else:
            values = mat3_to_euler_xyz(delta_chan)
        fallback = self._search_axis(spec, tf_type, axis_override)
        if fallback is None:
            return None
        if np.linalg.norm(values) < EPS:
            axis = fallback
            values = np.zeros(3, dtype=float)
            values[axis] = self._current_step_size(spec, tf_type)
        else:
            key = (spec.name, tf_type)
            axis = fallback if self.step_reject_streak.get(key, 0) > 0 else int(np.argmax(np.abs(values)))
            step = self._current_step_size(spec, tf_type)
            value = values[axis]
            values = np.zeros(3, dtype=float)
            values[axis] = value if abs(value) > EPS else step
        if spec.rotation_mode == "QUATERNION":
            reduced = rotvec_to_mat3(values)
        else:
            reduced = euler_xyz_to_mat3(values)
        after = compose_basis(before[:3, 3], chan @ reduced @ chan.T @ basis_rotation_matrix(before), basis_scale(before))
        return PCProposal(spec.name, spec.kind, axis, before, after, tf_type)

    @staticmethod
    def _synchronize_linked_magnitudes(proposal: PCProposal) -> None:
        if not proposal.linked or proposal.tf_type != TF_ROTATION:
            return
        primary_angle = float(np.linalg.norm(mat3_to_rotvec(proposal.basis_after[:3, :3])))
        if primary_angle < EPS:
            return
        for linked in proposal.linked:
            rot = basis_rotation_matrix(linked.basis_after)
            vec = mat3_to_rotvec(rot)
            n = float(np.linalg.norm(vec))
            if n < EPS:
                continue
            vec = vec / n * primary_angle
            linked.basis_after = compose_basis(
                linked.basis_after[:3, 3],
                rotvec_to_mat3(vec),
                basis_scale(linked.basis_after),
            )

    @staticmethod
    def _proposal_entries(proposal: PCProposal) -> List[Tuple[str, np.ndarray, np.ndarray]]:
        return [(proposal.bone_name, proposal.basis_before, proposal.basis_after)] + [
            (item.bone_name, item.basis_before, item.basis_after) for item in proposal.linked
        ]

    def _apply_proposal_group(self, proposal: PCProposal, use_after: bool = True) -> None:
        for name, before, after in self._proposal_entries(proposal):
            basis = after if use_after else before
            self.basis_map[name] = np.asarray(basis, dtype=float).copy()
            if self.bones.get(name, None) is not None and self.bones[name].enabled:
                self.apply_basis(name, basis)

    def _commit_proposal_group(self, proposal: PCProposal,
                               already_applied: bool = False) -> None:
        if not already_applied:
            self._apply_proposal_group(proposal, use_after=True)
            self._set_b_points(self.read_samples())
        for name, _before, after in self._proposal_entries(proposal):
            self.deltas.append((self.step_count, name, np.asarray(after, dtype=float).copy()))
        if self.step_count % max(1, int(self.config.snapshot_interval)) == 0:
            self.snapshots.append((self.step_count, self._snapshot_state()))
        max_history = max(1, int(self.config.max_history_steps))
        if len(self.deltas) > max_history:
            retained = self.deltas[-max_history:]
            floor_step = int(retained[0][0])
            floor_state = self._state_at(floor_step)
            new_snapshots = [
                (snap_step, snap_state)
                for snap_step, snap_state in self.snapshots
                if snap_step != floor_step
            ]
            new_snapshots.append((floor_step, floor_state))
            new_snapshots.sort(key=lambda item: item[0])
            self.snapshots = new_snapshots
            self.deltas = [d for d in self.deltas if d[0] > floor_step]
            self._history_floor_step = max(self._history_floor_step, floor_step)
        self._touch_committed_state()

    def _clear_group_momentum(self, proposal: PCProposal) -> None:
        for name, _before, _after in self._proposal_entries(proposal):
            key = (name, proposal.tf_type)
            self.proposal_momentum.pop(key, None)
            self.proposal_momentum_axis.pop(key, None)

    def _remember_group_momentum(self, proposal: PCProposal) -> None:
        for name, before, after in self._proposal_entries(proposal):
            key = (name, proposal.tf_type)
            self.proposal_momentum[key] = basis_transform_delta(proposal.tf_type, before, after)
            self.proposal_momentum_axis[key] = proposal.axis

    def _proposal_channel_components(self, proposal: PCProposal) -> Tuple[float, float, float]:
        before = proposal.basis_before
        after = proposal.basis_after
        spec = self.bones.get(proposal.bone_name)
        if proposal.tf_type == TF_SCALE:
            s0 = basis_scale(before)
            ratio = np.divide(basis_scale(after), s0, out=np.ones(3, dtype=float), where=s0 > EPS)
            values = (ratio - 1.0) * 100.0
        elif proposal.tf_type == TF_LOCATION:
            values = after[:3, 3] - before[:3, 3]
            if spec is not None:
                values = self._channel_matrix(spec.name).T @ values
        else:
            delta = basis_rotation_matrix(after) @ basis_rotation_matrix(before).T
            if spec is not None:
                delta = self._channel_matrix(spec.name).T @ delta @ self._channel_matrix(spec.name)
            values = np.degrees(mat3_to_rotvec(delta)) if spec is None or spec.rotation_mode == "QUATERNION" else np.degrees(mat3_to_euler_xyz(delta))
        return tuple(float(v) for v in values)

    @staticmethod
    def _metric_dict(metric: PCMetric) -> Dict[str, float]:
        return {
            "f1": float(metric.f1),
            "precision": float(metric.precision),
            "recall": float(metric.recall),
            "chamfer": float(metric.chamfer),
            "score": (float(metric.score)
                      if metric.score is not None and
                      math.isfinite(float(metric.score))
                      else float("nan")),
        }

    def _store_debug_payload(
            self,
            before_metric: PCMetric,
            forward_proposal: PCProposal,
            forward_metric: PCMetric,
            backward_proposal: PCProposal,
            backward_metric: PCMetric,
            chosen_proposal: PCProposal,
            accepted: bool,
            reward_fn: Callable[[PCMetric, PCMetric], float] = metric_reward) -> None:
        key = (chosen_proposal.bone_name, chosen_proposal.tf_type)
        chosen_metric = (forward_metric if chosen_proposal is forward_proposal
                         else backward_metric)
        self.last_debug_payload = {
            "step": int(self.step_count),
            "phase": self.schedule.phase_name,
            "stage": int(self.schedule.stage),
            "bone_name": chosen_proposal.bone_name,
            "tf_type": chosen_proposal.tf_type,
            "axis": (int(chosen_proposal.axis)
                     if chosen_proposal.axis is not None else None),
            "accepted": bool(accepted),
            "before": self._metric_dict(before_metric),
            "forward": {
                "delta_components": tuple(
                    float(v) for v in self._proposal_channel_components(
                        forward_proposal)),
                "metric": self._metric_dict(forward_metric),
                "reward": float(reward_fn(forward_metric, before_metric)),
            },
            "backward": {
                "delta_components": tuple(
                    float(v) for v in self._proposal_channel_components(
                        backward_proposal)),
                "metric": self._metric_dict(backward_metric),
                "reward": float(reward_fn(backward_metric, before_metric)),
            },
            "chosen": {
                "delta_components": tuple(
                    float(v) for v in self._proposal_channel_components(
                        chosen_proposal)),
                "reward": float(reward_fn(chosen_metric, before_metric)),
            },
            "step_size": float(self.step_sizes.get(key, float("nan"))),
            "reject_streak": int(self.step_reject_streak.get(key, 0)),
            "axis_cursor": int(self.axis_cursors.get(key, 0)),
        }

    def _make_step_result(
            self,
            proposal: PCProposal,
            accepted: bool,
            before_metric: PCMetric,
            trial_metric: PCMetric,
            display_metric: Optional[PCMetric] = None,
            reward_fn: Callable[[PCMetric, PCMetric], float] = metric_reward) -> PCStepResult:
        reward_value = reward_fn(trial_metric, before_metric)
        score_before = _fit_score(before_metric)
        score_after = _fit_score(trial_metric)
        result = PCStepResult(
            step=self.step_count,
            accepted=accepted,
            bone_name=proposal.bone_name,
            tf_type=proposal.tf_type,
            metric=(trial_metric if accepted else
                    (display_metric if display_metric is not None
                     else before_metric)),
            axis=proposal.axis,
            delta_components=self._proposal_channel_components(proposal),
            linked_count=len(proposal.linked),
            f1_delta=float(trial_metric.f1 - before_metric.f1),
            chamfer_delta=float(before_metric.chamfer - trial_metric.chamfer),
            reward=float(reward_value),
            applied_names=tuple(name for name, _before, _after in self._proposal_entries(proposal)),
            score_delta=(float(score_after - score_before)
                         if score_before is not None and score_after is not None
                         else 0.0),
        )
        self.last_step_result = result
        return result

    def _observe_phase_convergence(self, accepted: bool) -> None:
        if not accepted:
            return
        if self.config.phase_eval_interval > 0 and self.step_count % int(self.config.phase_eval_interval) != 0:
            return
        self.schedule.observe_overlap(self.current_metric.f1)

    def _step_with_metric(self, proposal: PCProposal, metric_fn: Callable[[], PCMetric]) -> PCStepResult:
        before_metric = self.current_metric

        self._apply_proposal_group(proposal, use_after=True)
        self._set_b_points(self.read_samples())
        trial_metric = metric_fn()
        if metric_improves(trial_metric, before_metric):
            self._commit_proposal_group(proposal, already_applied=True)
            self.current_metric = trial_metric
            self._clear_bone_reject_cycle(proposal.bone_name, proposal.tf_type)
            self._remember_group_momentum(proposal)
            self._observe_axis_f1_progress(
                proposal, True, float(trial_metric.f1 - before_metric.f1))
            self._update_bone_curriculum(metric_reward(trial_metric, before_metric), accepted=True)
            self._adapt_parameter_step(proposal, accepted=True)
            self._store_debug_payload(
                before_metric, proposal, trial_metric, proposal, trial_metric,
                proposal, True)
            return self._make_step_result(proposal, True, before_metric, trial_metric)

        opposite = PCProposal(
            bone_name=proposal.bone_name,
            kind=proposal.kind,
            axis=proposal.axis,
            basis_before=proposal.basis_before,
            basis_after=opposite_proposal_basis(proposal),
            tf_type=proposal.tf_type,
            linked=[
                PCLinkedBasis(
                    item.bone_name,
                    item.basis_before,
                    opposite_basis(
                        proposal.tf_type, item.basis_before, item.basis_after))
                for item in proposal.linked
            ],
        )
        self._apply_proposal_group(opposite, use_after=True)
        self._set_b_points(self.read_samples())
        opposite_metric = metric_fn()
        if metric_improves(opposite_metric, before_metric):
            self._commit_proposal_group(opposite, already_applied=True)
            self.current_metric = opposite_metric
            self._clear_bone_reject_cycle(opposite.bone_name, opposite.tf_type)
            self._remember_group_momentum(opposite)
            self._observe_axis_f1_progress(
                opposite, True, float(opposite_metric.f1 - before_metric.f1))
            self._update_bone_curriculum(metric_reward(opposite_metric, before_metric), accepted=True)
            self._adapt_parameter_step(opposite, accepted=True)
            self._store_debug_payload(
                before_metric, proposal, trial_metric, opposite,
                opposite_metric, opposite, True)
            return self._make_step_result(opposite, True, before_metric, opposite_metric)

        self._apply_proposal_group(opposite, use_after=False)
        self._set_b_points(self.read_samples())
        self.current_metric = before_metric
        self._clear_group_momentum(proposal)
        self._adapt_parameter_step(proposal, accepted=False)
        best_proposal = proposal
        best_metric = trial_metric
        if metric_reward(opposite_metric, before_metric) > metric_reward(
                trial_metric, before_metric):
            best_proposal = opposite
            best_metric = opposite_metric
        self._register_rejected_bone_attempt(best_proposal)
        self._store_debug_payload(
            before_metric, proposal, trial_metric, opposite, opposite_metric,
            best_proposal, False)
        return self._make_step_result(
            best_proposal, False, before_metric, best_metric,
            display_metric=before_metric)

    def _step_exact(self, proposal: PCProposal) -> PCStepResult:
        return self._step_with_metric(proposal, self._full_metric)

    def _step_minibatch(self, proposal: PCProposal) -> PCStepResult:
        ia, ib = self._sample_minibatch_indices()
        return self._step_with_metric(
            proposal, lambda ia=ia, ib=ib: self._minibatch_metric(ia, ib))

    def propose(self) -> Optional[PCProposal]:
        spec = self._pick_bone()
        if spec is None:
            return None
        if self.schedule.stage < 4:
            weights = self.schedule.weights_for(self._unlocked_channels(spec))
            while not weights and self.schedule.stage < 4:
                self.schedule.advance_stage()
                weights = self.schedule.weights_for(self._unlocked_channels(spec))
            if not weights:
                return None
            tf_type = max(weights, key=weights.get)
        else:
            weights = self.schedule.weights_for(self._unlocked_channels(spec))
            if not weights:
                return None
            types = list(weights.keys())
            probs = np.array([weights[t] for t in types], dtype=float)
            tf_type = types[int(self.rng.choice(len(types), p=probs))]
        proposal = self._proposal_for_spec(spec, tf_type)
        if proposal is None:
            return None
        if self.schedule.stage >= 4 and spec.mirror_name:
            mirror = self.bones.get(spec.mirror_name)
            if mirror is not None and mirror.enabled and any(self._unlocked_channels(mirror)[tf_type]):
                mirror_proposal = self._proposal_for_spec(mirror, tf_type, axis_override=proposal.axis)
                if mirror_proposal is not None:
                    proposal.linked.append(PCLinkedBasis(mirror.name, mirror_proposal.basis_before, mirror_proposal.basis_after))
                    self._synchronize_linked_magnitudes(proposal)
        return proposal

    def step(self) -> Optional[PCStepResult]:
        self.step_count += 1
        if self._small_exact_search_active():
            result = self._step_small_exact_search()
        else:
            proposal = self.propose()
            if proposal is None:
                self.step_count -= 1
                return None
            result = self._step_minibatch(proposal) if self._minibatch_active() else self._step_exact(proposal)
        if result is None:
            self.step_count -= 1
            return None
        if self.config.phase_eval_interval > 0 and self.step_count % int(self.config.phase_eval_interval) == 0:
            self.schedule.observe_overlap(self.current_metric.f1)
        self.metrics.append(float(self.current_metric.f1))
        interval = int(self.config.full_eval_interval)
        if interval <= 0 or self.step_count < interval or self.step_count % interval == 0:
            if self.current_metric.f1 > self.best_f1 + EPS:
                self.best_f1 = self.current_metric.f1
                self.best_step = self.step_count
                self.best_snapshot = self._snapshot_state()
            self.best_reward = max(self.best_reward, result.reward)
        return result

    def _state_at(self, step: int) -> Dict[str, np.ndarray]:
        step = int(max(0, step))
        exact_snapshot = self._snapshot_at_exact_step(step)
        if exact_snapshot is not None:
            return exact_snapshot
        if step < self._history_floor_step:
            raise ValueError(
                f"步骤 {step} 的非快照历史已被压缩，最早可恢复的非快照步为 {self._history_floor_step}")
        snap_step = 0
        state = self.snapshots[0][1]
        for s, snap in self.snapshots:
            if s <= step and s >= snap_step:
                snap_step = s
                state = snap
        out = {k: v.copy() for k, v in state.items()}
        for s, name, basis in self.deltas:
            if s > snap_step and s <= step:
                out[name] = np.asarray(basis, dtype=float).copy()
        return out

    def seek(self, step: int) -> int:
        state = self._state_at(step)
        self._apply_state(state)
        self.current_metric = self._full_metric()
        return self.step_count

    def truncate_after(self, step: int) -> None:
        step = int(max(0, step))
        self.deltas = [d for d in self.deltas if d[0] <= step]
        self.snapshots = [s for s in self.snapshots if s[0] <= step]
        if not self.snapshots or self.snapshots[-1][0] != step:
            self.snapshots.append((step, self._state_at(step)))
        self.step_count = step
        self._apply_state(self._state_at(step))
        self.current_metric = self._full_metric()
        self.metrics = self.metrics[:step]
        self._history_floor_step = min(self._history_floor_step, step)
        if self.best_step > step:
            self.best_step = step
            self.best_f1 = self.current_metric.f1
            self.best_snapshot = self._snapshot_state()

    def history_total(self) -> int:
        return self.step_count

    def jump_to_best(self) -> int:
        if self.best_snapshot is not None:
            self._apply_state(self.best_snapshot)
            self.current_metric = self._full_metric()
        return self.best_step


def _spatial_sample_indices(points: np.ndarray, max_count: int) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64)
    max_count = max(0, min(int(len(points)), int(max_count)))
    if max_count <= 0:
        return np.zeros(0, dtype=np.int64)
    if len(points) <= max_count:
        return np.arange(len(points), dtype=np.int64)
    p_min = points.min(axis=0)
    extent = points.max(axis=0) - p_min
    max_extent = float(extent.max())
    if max_extent <= 1e-12:
        return np.array([0], dtype=np.int64)

    def representatives(cell_size: float) -> np.ndarray:
        cells = np.floor((points - p_min) / cell_size).astype(np.int64)
        _, first = np.unique(cells, axis=0, return_index=True)
        return np.sort(first.astype(np.int64))

    low = max_extent * 1e-12
    high = max_extent * 2.0
    best = representatives(high)
    for _ in range(12):
        mid = 0.5 * (low + high)
        candidate = representatives(mid)
        if len(candidate) > max_count:
            low = mid
        else:
            best = candidate
            high = mid
    return best
