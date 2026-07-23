# -*- coding: utf-8 -*-
"""虚拟骨架（纯 numpy，不依赖 bpy）：headless 高速迭代的核心。

动机：迭代慢的根源不是最近邻计算，而是每步
`view_layer.update()` 触发全 depsgraph 重估 + 视口重绘。
本模块把骨骼层级、姿态矩阵递推、LBS 蒙皮全部搬进 numpy，
迭代循环完全脱离 Blender；每 tick 批量迭代后只同步一次视口。

姿态矩阵递推（无约束变形骨路径）：
    root:  pose[b] = matrix_local[b] @ basis[b]
    child: pose[b] = pose[parent] @ (matrix_local[parent]⁻¹ @ matrix_local[b]) @ basis[b]
不继承旋转/缩放、连接骨平移、约束等差异由构建期扰动验证兜底
（验证失败则回退实时评估模式，见 pc_bridge.validate_virtual_rig）。
"""
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np


def _safe_inv3(m: np.ndarray) -> np.ndarray:
    """Return an inverse for channel mapping; fall back to a pseudoinverse."""
    m = np.asarray(m, dtype=np.float64)
    try:
        return np.linalg.inv(m)
    except np.linalg.LinAlgError:
        return np.linalg.pinv(m, rcond=1e-8)


@dataclass
class PCVirtualRig:
    """纯 numpy 虚拟骨架：basis -> 姿态矩阵 -> LBS 采样点云。"""
    bone_names: List[str]
    parents: np.ndarray              # (B,) int64，-1 = 根骨
    local_mats: np.ndarray           # (B,4,4) 绑定姿态 matrix_local（骨架空间）
    rel_mats: np.ndarray             # (B,4,4) root=local；child=local[parent]⁻¹ @ local
    rest_inv: np.ndarray             # (B,4,4) local 的逆
    topo_order: np.ndarray           # (B,) 父先子后的计算顺序
    arm_mw: np.ndarray               # (4,4) 骨架世界矩阵（会话期不变）

    rest_arm: np.ndarray             # (N,3) 绑定姿态采样点（骨架空间）
    topk_w: np.ndarray               # (N,K) 归一化权重
    topk_idx: np.ndarray             # (N,K) 骨骼下标（-1=空槽）
    bone_to_rows: Dict[int, np.ndarray]  # 骨骼（含后代）-> 受影响采样行
    subtree_topo: List[np.ndarray] = field(default_factory=list)
    name_to_index: Dict[str, int] = field(default_factory=dict)

    basis: np.ndarray = field(default_factory=lambda: np.zeros((0, 4, 4)))
    pose_mats: np.ndarray = field(default_factory=lambda: np.zeros((0, 4, 4)))
    current: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    _dirty: set = field(default_factory=set)

    # ------------------------------------------------------------------
    def index_of(self, name: str) -> int:
        return self.name_to_index[name]

    def has_bone(self, name: str) -> bool:
        return name in self.name_to_index

    def set_basis(self, name: str, mat4: np.ndarray) -> None:
        """写入某骨骼 basis（engine apply_basis 回调）。"""
        i = self.index_of(name)
        self.basis[i] = np.asarray(mat4, dtype=np.float64)
        self._dirty.add(i)

    def get_basis(self, name: str) -> np.ndarray:
        return self.basis[self.index_of(name)].copy()

    def refresh_pose(self) -> None:
        """按需重算全部姿态矩阵（B 很小，全量比维护脏链更省）。"""
        if not self._dirty:
            return
        order = self.topo_order
        if self.subtree_topo:
            affected = np.zeros(len(self.bone_names), dtype=bool)
            for bone_index in self._dirty:
                affected[self.subtree_topo[int(bone_index)]] = True
            order = self.topo_order[affected[self.topo_order]]
        for b in order:
            p = int(self.parents[b])
            if p < 0:
                self.pose_mats[b] = self.local_mats[b] @ self.basis[b]
            else:
                self.pose_mats[b] = self.pose_mats[p] @ self.rel_mats[b] @ self.basis[b]

    def _capture_probe_state(
            self,
            touched_names: List[str],
    ) -> Tuple[Dict[str, np.ndarray], np.ndarray, np.ndarray, Optional[np.ndarray], np.ndarray]:
        saved_basis = {
            name: self.basis[self.name_to_index[name]].copy()
            for name in touched_names
        }
        affected = np.zeros(len(self.bone_names), dtype=bool)
        dirty_row_parts: List[np.ndarray] = []
        for name in touched_names:
            bone_index = self.name_to_index[name]
            if self.subtree_topo:
                affected[self.subtree_topo[bone_index]] = True
            else:
                affected[bone_index] = True
            rows = self.bone_to_rows.get(bone_index)
            if rows is not None and len(rows) > 0:
                dirty_row_parts.append(rows)
        affected_indices = self.topo_order[affected[self.topo_order]]
        dirty_rows = (
            np.unique(np.concatenate(dirty_row_parts))
            if dirty_row_parts else np.zeros(0, dtype=np.int64)
        )
        baseline_rows = (
            self.current[dirty_rows].copy() if len(dirty_rows) > 0 else None
        )
        saved_pose = self.pose_mats[affected_indices].copy()
        return saved_basis, affected_indices, dirty_rows, baseline_rows, saved_pose

    def _restore_probe_state(
            self,
            saved_basis: Dict[str, np.ndarray],
            affected_indices: np.ndarray,
            dirty_rows: np.ndarray,
            baseline_rows: Optional[np.ndarray],
            saved_pose: np.ndarray,
    ) -> None:
        for name, mat in saved_basis.items():
            self.basis[self.name_to_index[name]] = mat
        if len(affected_indices) > 0:
            self.pose_mats[affected_indices] = saved_pose
        if baseline_rows is not None and len(dirty_rows) > 0:
            self.current[dirty_rows] = baseline_rows
        self._dirty = set()

    def read_samples(self, lbs_transform) -> np.ndarray:
        """重算脏骨骼影响的采样行，返回 (N,3) 世界坐标缓冲。"""
        if not self._dirty:
            return self.current
        self.refresh_pose()
        deltas = self.pose_mats @ self.rest_inv
        rows = np.unique(np.concatenate(
            [self.bone_to_rows[i] for i in self._dirty if i in self.bone_to_rows]
        )) if self._dirty else np.zeros(0, dtype=np.int64)
        if len(rows) > 0:
            self.current[rows] = (self.arm_mw[:3, :3] @ lbs_transform(
                self.rest_arm[rows], deltas,
                self.topk_w[rows], self.topk_idx[rows]).T).T + self.arm_mw[:3, 3]
        self._dirty = set()
        return self.current

    def read_subsamples(self, rows: np.ndarray, lbs_transform) -> np.ndarray:
        """Read only requested rows for temporary screen probes."""
        sub_rows = np.asarray(rows, dtype=np.int64)
        if len(sub_rows) == 0:
            return np.zeros((0, 3), dtype=np.float64)
        if not self._dirty:
            return self.current[sub_rows]
        self.refresh_pose()
        deltas = self.pose_mats @ self.rest_inv
        dirty_rows = np.unique(np.concatenate(
            [self.bone_to_rows[i] for i in self._dirty if i in self.bone_to_rows]
        )) if self._dirty else np.zeros(0, dtype=np.int64)
        if len(dirty_rows) > 0:
            target_rows = np.intersect1d(
                sub_rows, dirty_rows, assume_unique=False)
            if len(target_rows) > 0:
                self.current[target_rows] = (self.arm_mw[:3, :3] @ lbs_transform(
                    self.rest_arm[target_rows], deltas,
                    self.topk_w[target_rows], self.topk_idx[target_rows]).T).T + self.arm_mw[:3, 3]
        self._dirty = set()
        return self.current[sub_rows]

    def provider(self, lbs_transform=None):
        """生成骨点提供者（pivot/通道轴，骨架空间系，语义与 pc_bridge 一致）。"""
        def _provider(name: str, attr: str) -> np.ndarray:
            self.refresh_pose()
            i = self.index_of(name)
            pm = self.pose_mats[i]
            if attr == 'pivot':
                world = self.arm_mw @ pm
                return world[:3, 3].copy()
            pl3 = pm[:3, :3]
            if attr in ('rot_cols', 'loc_cols'):
                cols = pl3.copy()
                for c in range(3):
                    n = float(np.linalg.norm(cols[:, c]))
                    if n > 1e-12:
                        cols[:, c] /= n
                return cols
            if attr == 'scale_rows':
                rows = pl3.copy()
                for r in range(3):
                    n = float(np.linalg.norm(rows[r, :]))
                    if n > 1e-12:
                        rows[r, :] /= n
                return rows
            if attr == 'chan_to_local':
                return _safe_inv3(pl3)
            return np.identity(3)
        return _provider

    def basis_map(self) -> Dict[str, np.ndarray]:
        return {name: self.basis[i].copy() for i, name in enumerate(self.bone_names)}

    def set_basis_map(self, basis_map: Dict[str, np.ndarray]) -> None:
        for name, mat in basis_map.items():
            i = self.name_to_index.get(name)
            if i is not None:
                self.basis[i] = np.asarray(mat, dtype=np.float64)
                self._dirty.add(i)

    def probe_pair_points(
        self,
        forward_entries: List[Tuple[str, np.ndarray, np.ndarray]],
        backward_entries: List[Tuple[str, np.ndarray, np.ndarray]],
        lbs_transform,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Probe two candidate states and restore the rig baseline afterwards.

        The caller must first synchronize ``basis`` / ``pose_mats`` / ``current``
        to the desired baseline state. This method then only touches the bones
        referenced by the proposals instead of rebuilding the entire basis map
        for every candidate probe.
        """
        touched_names: List[str] = []
        seen: set[str] = set()
        for name, _before, _after in list(forward_entries) + list(backward_entries):
            if name in self.name_to_index and name not in seen:
                seen.add(name)
                touched_names.append(name)
        if not touched_names:
            baseline = np.asarray(self.current, dtype=np.float64).copy()
            return baseline, baseline.copy()

        (saved,
         affected_indices,
         dirty_rows,
         baseline_rows,
         saved_pose) = self._capture_probe_state(touched_names)

        def _set_entries(entries: List[Tuple[str, np.ndarray, np.ndarray]]) -> None:
            for name, _before, after in entries:
                i = self.name_to_index.get(name)
                if i is None:
                    continue
                self.basis[i] = np.asarray(after, dtype=np.float64)
                self._dirty.add(i)

        def _restore_baseline() -> None:
            for name, mat in saved.items():
                i = self.name_to_index[name]
                self.basis[i] = mat
                self._dirty.add(i)

        _set_entries(forward_entries)
        forward_points = self.read_samples(lbs_transform).copy()

        self._restore_probe_state(
            saved, affected_indices, dirty_rows, baseline_rows, saved_pose)
        _set_entries(backward_entries)
        backward_points = self.read_samples(lbs_transform).copy()

        self._restore_probe_state(
            saved, affected_indices, dirty_rows, baseline_rows, saved_pose)
        return forward_points, backward_points

    def probe_pair_subpoints(
        self,
        forward_entries: List[Tuple[str, np.ndarray, np.ndarray]],
        backward_entries: List[Tuple[str, np.ndarray, np.ndarray]],
        rows: np.ndarray,
        lbs_transform,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Probe two candidate states but only materialize selected sample rows."""
        sub_rows = np.asarray(rows, dtype=np.int64)
        if len(sub_rows) == 0:
            return (
                np.zeros((0, 3), dtype=np.float64),
                np.zeros((0, 3), dtype=np.float64),
            )
        touched_names: List[str] = []
        seen: set[str] = set()
        for name, _before, _after in list(forward_entries) + list(backward_entries):
            if name in self.name_to_index and name not in seen:
                seen.add(name)
                touched_names.append(name)
        if not touched_names:
            baseline = np.asarray(self.current, dtype=np.float64)
            return baseline[sub_rows].copy(), baseline[sub_rows].copy()

        (saved,
         affected_indices,
         dirty_rows,
         baseline_rows,
         saved_pose) = self._capture_probe_state(touched_names)

        def _set_entries(entries: List[Tuple[str, np.ndarray, np.ndarray]]) -> None:
            for name, _before, after in entries:
                i = self.name_to_index.get(name)
                if i is None:
                    continue
                self.basis[i] = np.asarray(after, dtype=np.float64)
                self._dirty.add(i)

        def _restore_baseline() -> None:
            for name, mat in saved.items():
                i = self.name_to_index[name]
                self.basis[i] = mat
                self._dirty.add(i)

        _set_entries(forward_entries)
        forward_points = self.read_subsamples(sub_rows, lbs_transform).copy()

        self._restore_probe_state(
            saved, affected_indices, dirty_rows, baseline_rows, saved_pose)
        _set_entries(backward_entries)
        backward_points = self.read_subsamples(sub_rows, lbs_transform).copy()

        self._restore_probe_state(
            saved, affected_indices, dirty_rows, baseline_rows, saved_pose)
        return forward_points, backward_points


def build_virtual_rig(
    bone_names: List[str],
    parents: Dict[str, Optional[str]],
    local_mats: Dict[str, np.ndarray],
    rest_arm: np.ndarray,
    topk_w: np.ndarray,
    topk_idx: np.ndarray,
    bone_to_rows: Dict[int, np.ndarray],
    arm_mw: np.ndarray,
    basis_map: Optional[Dict[str, np.ndarray]] = None,
) -> PCVirtualRig:
    """由桥接层提取的纯数据构建虚拟骨架。"""
    b_count = len(bone_names)
    index = {name: i for i, name in enumerate(bone_names)}
    par = np.full(b_count, -1, dtype=np.int64)
    for name, p in parents.items():
        if p is not None and p in index and name in index:
            par[index[name]] = index[p]

    local = np.zeros((b_count, 4, 4))
    rest_inv = np.zeros((b_count, 4, 4))
    for i, name in enumerate(bone_names):
        local[i] = np.asarray(local_mats[name], dtype=np.float64)
        rest_inv[i] = np.linalg.inv(local[i])

    rel = np.zeros((b_count, 4, 4))
    for i in range(b_count):
        p = int(par[i])
        rel[i] = local[i] if p < 0 else rest_inv[p] @ local[i]

    # 拓扑序（父先子后）：按深度排序
    depth = np.zeros(b_count, dtype=np.int64)
    for i in range(b_count):
        d, cur, guard = 0, i, 0
        while int(par[cur]) >= 0 and guard <= b_count:
            cur = int(par[cur])
            d += 1
            guard += 1
        depth[i] = d
    topo = np.argsort(depth, kind='stable')
    topo_list = topo.tolist()
    children: List[List[int]] = [[] for _ in range(b_count)]
    for child in range(b_count):
        parent = int(par[child])
        if parent >= 0:
            children[parent].append(child)
    subtree_topo: List[np.ndarray] = []
    for root in range(b_count):
        stack = [root]
        seen: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(children[cur])
        subtree_topo.append(np.asarray(
            [b for b in topo_list if b in seen], dtype=np.int64))

    basis = np.zeros((b_count, 4, 4))
    for i, name in enumerate(bone_names):
        basis[i] = np.identity(4) if basis_map is None or name not in basis_map \
            else np.asarray(basis_map[name], dtype=np.float64)

    rig = PCVirtualRig(
        bone_names=list(bone_names), parents=par, local_mats=local,
        rel_mats=rel, rest_inv=rest_inv, topo_order=topo,
        arm_mw=np.asarray(arm_mw, dtype=np.float64),
        rest_arm=np.asarray(rest_arm, dtype=np.float64),
        topk_w=np.asarray(topk_w, dtype=np.float64),
        topk_idx=np.asarray(topk_idx, dtype=np.int64),
        bone_to_rows={int(k): np.asarray(v, dtype=np.int64) for k, v in bone_to_rows.items()},
        subtree_topo=subtree_topo,
        name_to_index=index,
        basis=basis,
        pose_mats=np.zeros((b_count, 4, 4)),
        current=np.zeros(len(rest_arm) if len(rest_arm) else 0),
    )
    rig._dirty = set(range(b_count))
    rig.current = np.zeros((len(rest_arm), 3))
    # 构建即刷新姿态矩阵，保证新建 rig 处于一致状态
    rig.refresh_pose()
    return rig


def validate_pose_recursion(
    rig: PCVirtualRig,
    test_bones: List[str],
    read_blender_pose: Callable[[str], np.ndarray],
    apply_blender_basis: Callable[[str, np.ndarray], None],
    rng_seed: int = 0,
    tol: float = 1e-4,
) -> Tuple[bool, str]:
    """扰动验证：对若干骨骼施加同一随机 basis 旋转，
    分别用虚拟骨架递推和 Blender 评估求姿态矩阵，比较全部骨骼。

    read_blender_pose(name) -> (4,4) Blender 侧姿态矩阵（调用方负责先 update）
    apply_blender_basis(name, basis4x4)（调用方负责 update 与恢复）
    """
    if not test_bones:
        return True, "无可验证骨骼（跳过）"
    rng = np.random.default_rng(rng_seed)
    saved = {n: rig.get_basis(n) for n in test_bones}
    try:
        for n in test_bones:
            axis = rng.normal(size=3)
            axis /= (np.linalg.norm(axis) + 1e-12)
            ang = math.radians(20.0)
            k = axis
            rot = (math.cos(ang) * np.identity(3)
                   + math.sin(ang) * np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
                   + (1.0 - math.cos(ang)) * np.outer(k, k))
            basis = saved[n].copy()
            new_rot = rot @ basis[:3, :3]
            new_basis = basis.copy()
            new_basis[:3, :3] = new_rot
            rig.set_basis(n, new_basis)
            apply_blender_basis(n, new_basis)
        rig.refresh_pose()
        worst = 0.0
        worst_bone = ""
        for i, name in enumerate(rig.bone_names):
            ref = np.asarray(read_blender_pose(name), dtype=np.float64)
            dev = float(np.max(np.abs(rig.pose_mats[i] - ref)))
            if dev > worst:
                worst, worst_bone = dev, name
        if worst > tol:
            return False, f"骨骼 '{worst_bone}' 姿态递推偏差 {worst:.5f} 超阈值（特殊继承/连接/约束）"
        return True, f"验证通过（最大偏差 {worst:.2e}）"
    finally:
        for n, b in saved.items():
            rig.set_basis(n, b)
            apply_blender_basis(n, b)
        rig.refresh_pose()
