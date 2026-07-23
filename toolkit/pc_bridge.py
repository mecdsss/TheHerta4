# -*- coding: utf-8 -*-
"""点云姿态匹配 bpy 适配层。

本模块是唯一 import bpy/mathutils 的层：
- 评估网格读取（depsgraph + foreach_get，numpy 批量）
- 姿态骨骼读写与空间换算（matrix_basis 直写，不经 ops、不产生 undo）
- KD 树最近邻提供者
- 骨骼规格提取（轴向锁定 / 旋转模式 / 约束 / 顶点组影响表）
"""
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import bpy
import numpy as np
from mathutils import Matrix, Vector, kdtree

from . import pc_engine


@dataclass
class PCMeshPart:
    """B 侧一个蒙皮网格的采样信息。"""
    obj_name: str
    sample_vert_indices: np.ndarray      # 采样的顶点下标（该网格局部）
    vert_count: int


@dataclass
class PCCache:
    """初始化时构建的匹配缓存。"""
    a_obj_name: str
    arm_obj_name: str
    a_points: np.ndarray                 # A 采样点云（世界坐标）
    a_diag: float                        # A 包围盒对角线长度
    tau: float                           # 点云占用体素边长
    b_parts: List[PCMeshPart]
    b_rest_points: np.ndarray            # B 初始采样点云（世界坐标）
    bones: List[pc_engine.PCBoneSpec]
    nn_a: pc_engine.NNProvider
    a_matrix_world: Tuple[float, ...]    # 失效检测用
    arm_matrix_world: Tuple[float, ...]
    bone_count: int


def _matrix_to_tuple(m: Matrix) -> Tuple[float, ...]:
    return tuple(round(v, 6) for row in m for v in row)


def _eval_world_verts(obj: bpy.types.Object, depsgraph) -> np.ndarray:
    """读取物体评估后网格的世界坐标顶点（N x 3）。"""
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        count = len(mesh.vertices)
        if count == 0:
            return np.zeros((0, 3))
        arr = np.empty(count * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", arr)
        arr = arr.reshape(count, 3)
        mw = np.array(eval_obj.matrix_world, dtype=np.float64)
        return arr @ mw[:3, :3].T + mw[:3, 3]
    finally:
        eval_obj.to_mesh_clear()


def _thin_indices(count: int, sample_count: int) -> np.ndarray:
    """均匀抽稀顶点下标。"""
    if sample_count <= 0 or count <= 0:
        return np.zeros(0, dtype=np.int64)
    if count <= sample_count:
        return np.arange(count)
    return np.unique(np.linspace(0, count - 1, sample_count).astype(np.int64))


def _bounded_sample_count(vertex_count: int, requested: int) -> int:
    """独立限制单侧点云大小，不要求 A/B 数量相同。"""
    return max(0, min(int(vertex_count), int(requested)))


def _spatial_sample_indices(points: np.ndarray, max_count: int) -> np.ndarray:
    """体素化抽取空间代表点；结果只受几何分布影响，不依赖顶点顺序。"""
    points = np.asarray(points, dtype=np.float64)
    max_count = _bounded_sample_count(len(points), max_count)
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
        _keys, first = np.unique(cells, axis=0, return_index=True)
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


def make_nn_provider(points: np.ndarray) -> pc_engine.NNProvider:
    """用 mathutils.kdtree 构建最近邻提供者。"""
    pts = np.asarray(points, dtype=np.float64)
    kd = kdtree.KDTree(len(pts))
    for i, p in enumerate(pts):
        kd.insert(Vector((p[0], p[1], p[2])), i)
    kd.balance()

    def _query(x: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        x = np.asarray(x, dtype=np.float64)
        dists = np.empty(len(x), dtype=np.float64)
        idxs = np.empty(len(x), dtype=np.int64)
        for i, p in enumerate(x):
            _co, index, dist = kd.find(Vector((p[0], p[1], p[2])))
            dists[i] = dist if dist is not None else float('inf')
            idxs[i] = index if index is not None else -1
        return dists, idxs

    return _query


def find_skinned_meshes(arm_obj: bpy.types.Object) -> List[bpy.types.Object]:
    """查找所有 Armature 修改器指向该骨架的网格物体。"""
    out: List[bpy.types.Object] = []
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object == arm_obj:
                out.append(obj)
                break
    return out


def _build_bone_specs(arm_obj: bpy.types.Object,
                      mesh_objs: List[bpy.types.Object],
                      global_sample_indices: Dict[str, np.ndarray],
                      sample_vert_indices: Dict[str, np.ndarray]) -> List[pc_engine.PCBoneSpec]:
    """构建骨骼规格列表（变形骨含采样级影响表；带约束或无权重者判 controller）。"""
    # 每根骨 -> (全局采样下标, 权重)
    bone_influence: Dict[str, List[Tuple[int, float]]] = {
        b.name: [] for b in arm_obj.data.bones
    }
    for mesh_obj in mesh_objs:
        vg_index = {vg.index: vg.name for vg in mesh_obj.vertex_groups}
        sample_set = sample_vert_indices.get(mesh_obj.name)
        gidx_of_vert = global_sample_indices.get(mesh_obj.name)
        if sample_set is None or gidx_of_vert is None:
            continue
        # 顶点下标 -> 全局采样下标
        vert_to_gsample = {int(v): int(g) for v, g in zip(sample_set, gidx_of_vert)}
        base_mesh = mesh_obj.data
        for vert in base_mesh.vertices:
            g = vert_to_gsample.get(vert.index)
            if g is None:
                continue
            for grp in vert.groups:
                if grp.weight < 0.01:
                    continue
                name = vg_index.get(grp.group)
                if name in bone_influence:
                    bone_influence[name].append((g, float(grp.weight)))

    specs: List[pc_engine.PCBoneSpec] = []
    vg_names_all = set()
    for mesh_obj in mesh_objs:
        vg_names_all.update(vg.name for vg in mesh_obj.vertex_groups)

    for pb in arm_obj.pose.bones:
        has_constraints = len(pb.constraints) > 0
        has_weights = pb.name in vg_names_all and len(bone_influence.get(pb.name, [])) > 0
        kind = 'deform' if (has_weights and not has_constraints) else 'controller'
        pairs = bone_influence.get(pb.name, [])
        if pairs and kind == 'deform':
            indices = np.array([p[0] for p in pairs], dtype=np.int64)
            weights = np.array([p[1] for p in pairs], dtype=np.float64)
        else:
            indices = np.array([], dtype=np.int64)
            weights = np.array([], dtype=np.float64)
        specs.append(pc_engine.PCBoneSpec(
            name=pb.name,
            enabled=True,
            kind=kind,
            rotation_mode=pb.rotation_mode,
            lock_rotation=tuple(bool(x) for x in pb.lock_rotation),
            lock_scale=tuple(bool(x) for x in pb.lock_scale),
            lock_location=tuple(bool(x) for x in pb.lock_location),
            has_constraints=has_constraints,
            influence_indices=indices,
            influence_weights=weights,
        ))
    rest_segments = {
        bone.name: (np.asarray(bone.head_local, dtype=np.float64),
                    np.asarray(bone.tail_local, dtype=np.float64))
        for bone in arm_obj.data.bones
    }
    mirror_pairs = pc_engine.detect_mirror_pairs(rest_segments)
    for spec in specs:
        spec.mirror_name = mirror_pairs.get(spec.name)
    return specs


def build_cache(a_obj: bpy.types.Object, arm_obj: bpy.types.Object,
                cfg: pc_engine.PCFitConfig, context) -> PCCache:
    """构建匹配缓存（A/B 采样点云、骨骼规格、KD 树）。"""
    depsgraph = context.evaluated_depsgraph_get()

    a_all = _eval_world_verts(a_obj, depsgraph)
    if len(a_all) == 0:
        raise ValueError(f"模型 A '{a_obj.name}' 没有顶点")

    mesh_objs = find_skinned_meshes(arm_obj)
    if not mesh_objs:
        raise ValueError(f"未找到绑定到骨架 '{arm_obj.name}' 的网格物体")

    evaluated_parts = []
    for mesh_obj in mesh_objs:
        verts = _eval_world_verts(mesh_obj, depsgraph)
        if len(verts) > 0:
            evaluated_parts.append((mesh_obj, verts))
    if not evaluated_parts:
        raise ValueError("B 侧蒙皮网格没有顶点")
    mesh_objs = [obj for obj, _verts in evaluated_parts]
    b_vertex_total = sum(len(verts) for _obj, verts in evaluated_parts)
    a_sample_count = _bounded_sample_count(len(a_all), cfg.sample_count)
    b_sample_count = _bounded_sample_count(b_vertex_total, cfg.sample_count)
    if a_sample_count <= 0 or b_sample_count <= 0:
        raise ValueError("A/B 没有可用于匹配的采样点")

    a_idx = _spatial_sample_indices(a_all, a_sample_count)
    a_points = a_all[a_idx]
    a_min = a_points.min(axis=0)
    a_max = a_points.max(axis=0)
    a_diag = float(np.linalg.norm(a_max - a_min))
    tau = cfg.threshold if cfg.threshold > 0.0 else a_diag * 0.01
    all_b_points = np.concatenate(
        [verts for _obj, verts in evaluated_parts], axis=0)
    b_global_indices = _spatial_sample_indices(all_b_points, b_sample_count)

    b_parts: List[PCMeshPart] = []
    b_points_list: List[np.ndarray] = []
    global_sample_indices: Dict[str, np.ndarray] = {}
    sample_vert_indices: Dict[str, np.ndarray] = {}
    offset = 0
    source_offset = 0
    for mesh_obj, verts in evaluated_parts:
        selected = b_global_indices[
            (b_global_indices >= source_offset)
            & (b_global_indices < source_offset + len(verts))]
        idx = selected - source_offset
        source_offset += len(verts)
        b_parts.append(PCMeshPart(obj_name=mesh_obj.name,
                                  sample_vert_indices=idx,
                                  vert_count=len(verts)))
        b_points_list.append(verts[idx])
        sample_vert_indices[mesh_obj.name] = idx
        global_sample_indices[mesh_obj.name] = np.arange(offset, offset + len(idx))
        offset += len(idx)
    b_rest = np.concatenate(b_points_list, axis=0)

    bones = _build_bone_specs(arm_obj, mesh_objs, global_sample_indices, sample_vert_indices)

    return PCCache(
        a_obj_name=a_obj.name,
        arm_obj_name=arm_obj.name,
        a_points=a_points,
        a_diag=a_diag,
        tau=float(tau),
        b_parts=b_parts,
        b_rest_points=b_rest,
        bones=bones,
        nn_a=make_nn_provider(a_points),
        a_matrix_world=_matrix_to_tuple(a_obj.matrix_world),
        arm_matrix_world=_matrix_to_tuple(arm_obj.matrix_world),
        bone_count=len(arm_obj.data.bones),
    )


def read_b_samples(cache: PCCache, context) -> np.ndarray:
    """每步读取 B 当前姿态的采样点云（世界坐标）。"""
    depsgraph = context.evaluated_depsgraph_get()
    out: List[np.ndarray] = []
    for part in cache.b_parts:
        obj = bpy.data.objects.get(part.obj_name)
        if obj is None:
            raise ValueError(f"B 侧网格 '{part.obj_name}' 已被删除")
        verts = _eval_world_verts(obj, depsgraph)
        if len(verts) != part.vert_count:
            raise ValueError(f"B 侧网格 '{part.obj_name}' 顶点数发生变化")
        out.append(verts[part.sample_vert_indices])
    return np.concatenate(out, axis=0)


# ---------------------------------------------------------------------------
# 姿态骨骼读写与空间换算
# ---------------------------------------------------------------------------

def get_basis(pb: bpy.types.PoseBone) -> np.ndarray:
    return np.array(pb.matrix_basis, dtype=np.float64)


def set_basis(pb: bpy.types.PoseBone, basis: np.ndarray) -> None:
    pb.matrix_basis = Matrix(basis.tolist())


def _pose_local(arm_obj: bpy.types.Object, pb: bpy.types.PoseBone) -> Matrix:
    """骨骼局部姿态系（臂骨空间）：arm_world_inv @ pb.matrix。"""
    return arm_obj.matrix_world.inverted() @ pb.matrix


def bone_point_provider_factory(arm_obj: bpy.types.Object) -> pc_engine.BonePointProvider:
    """生成骨点提供者（pivot / 通道轴映射，世界坐标）。"""
    arm_mw = arm_obj.matrix_world
    arm_mw_inv = arm_mw.inverted()

    def _provider(name: str, attr: str) -> np.ndarray:
        pb = arm_obj.pose.bones.get(name)
        if pb is None:
            if attr == 'pivot':
                return np.zeros(3)
            return np.identity(3)
        pl = _pose_local(arm_obj, pb)
        pl3 = pl.to_3x3()
        chan = pl3.inverted()  # 通道轴 -> 骨骼局部系
        if attr == 'pivot':
            world = arm_mw @ pb.head
            return np.array(world, dtype=np.float64)
        if attr == 'rot_cols' or attr == 'loc_cols':
            # 通道轴（世界方向）：pose_local 3x3 的列
            cols = [pl3.col[i].normalized() if pl3.col[i].length > 1e-12 else Vector((1, 0, 0)) for i in range(3)]
            m = Matrix((cols[0], cols[1], cols[2])).transposed()
            return np.array(m, dtype=np.float64)
        if attr == 'scale_rows':
            rows = [pl3.row[i].normalized() if pl3.row[i].length > 1e-12 else Vector((1, 0, 0)) for i in range(3)]
            return np.array(Matrix((rows[0], rows[1], rows[2])), dtype=np.float64)
        if attr == 'chan_to_local':
            try:
                return np.array(chan, dtype=np.float64)
            except Exception:
                return np.linalg.pinv(np.array(pl3, dtype=np.float64), rcond=1e-8)
        return np.identity(3)

    return _provider


def apply_basis_to_armature(arm_obj: bpy.types.Object, name: str, basis: np.ndarray) -> None:
    """把 basis 写入指定姿态骨骼。"""
    pb = arm_obj.pose.bones.get(name)
    if pb is not None:
        set_basis(pb, basis)


def update_view(context, evaluate: bool = True) -> None:
    """请求 3D 视图重绘；evaluate=True 时强制刷新 depsgraph。"""
    if evaluate:
        try:
            context.view_layer.update()
        except Exception:
            pass
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def validate_cache(cache: PCCache) -> Optional[str]:
    """失效检测：返回 None 表示有效，否则返回原因。"""
    a_obj = bpy.data.objects.get(cache.a_obj_name)
    arm_obj = bpy.data.objects.get(cache.arm_obj_name)
    if a_obj is None:
        return "模型 A 已被删除或改名"
    if arm_obj is None:
        return "骨架 B 已被删除或改名"
    if _matrix_to_tuple(a_obj.matrix_world) != cache.a_matrix_world:
        return "模型 A 的物体变换发生变化，请重建缓存"
    if _matrix_to_tuple(arm_obj.matrix_world) != cache.arm_matrix_world:
        return "骨架 B 的物体变换发生变化，请重建缓存"
    if len(arm_obj.data.bones) != cache.bone_count:
        return "骨骼数量发生变化，请重建缓存"
    for part in cache.b_parts:
        if bpy.data.objects.get(part.obj_name) is None:
            return f"B 侧网格 '{part.obj_name}' 已被删除或改名"
    return None


def lock_info_text(spec) -> str:
    # duck-typing：接受 PCBoneSpec 或 bpy.types.PoseBone
    #（两者均有 lock_rotation/lock_scale/lock_location 属性）
    """骨骼锁定摘要（面板显示用），如 R:--Z S:XYZ L:---。"""
    def _fmt(lock: Tuple[bool, bool, bool]) -> str:
        axes = 'XYZ'
        return ''.join('-' if lock[i] else axes[i] for i in range(3))
    return f"R:{_fmt(spec.lock_rotation)} S:{_fmt(spec.lock_scale)} L:{_fmt(spec.lock_location)}"


# ---------------------------------------------------------------------------
# 自算 LBS 快速蒙皮缓存（替代每步 depsgraph 网格重读）
# ---------------------------------------------------------------------------

LBS_TOPK: int = 4


@dataclass
class PCLBSData:
    """自算蒙皮缓存。valid=False 时 lbs_read 自动回退全量重读。"""
    enabled: bool
    valid: bool
    note: str
    bone_names: List[str]
    rest_arm: np.ndarray                 # (N,3) 基础网格采样点（骨架空间）
    topk_idx: np.ndarray                 # (N,K) 骨骼下标（-1=空槽）
    topk_w: np.ndarray                   # (N,K) 归一化权重
    rest_inv: np.ndarray                 # (B,4,4) 绑定姿态逆矩阵
    pose_mats: np.ndarray                # (B,4,4) 当前姿态矩阵（骨架空间）
    arm_mw: np.ndarray                   # (4,4) 骨架世界矩阵（会话期不变）
    bone_to_rows: Dict[int, np.ndarray]  # 骨骼（含后代）-> 受影响采样行
    current: np.ndarray                  # (N,3) 工作缓冲（世界坐标）


def _bone_descendant_closure(arm_obj: bpy.types.Object,
                             bone_names: List[str]) -> Dict[int, List[int]]:
    """每根骨骼的下标 -> 自身+全部后代的下标闭包。"""
    index = {name: i for i, name in enumerate(bone_names)}
    children: Dict[str, List[str]] = {name: [] for name in bone_names}
    for bone in arm_obj.data.bones:
        if bone.name in index and bone.parent and bone.parent.name in index:
            children[bone.parent.name].append(bone.name)
    closure: Dict[int, List[int]] = {}
    for name in bone_names:
        stack = [name]
        seen = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(children.get(cur, []))
        closure[index[name]] = sorted(index[n] for n in seen)
    return closure


def build_lbs(cache: PCCache, context,
              perturb_bones: Tuple[str, ...] = (),
              tol_rest_ratio: float = 1e-4,
              tol_pose_ratio: float = 1e-3) -> PCLBSData:
    """构建自算蒙皮缓存并做两级验证（失败自动 valid=False）。"""
    invalid = PCLBSData(enabled=True, valid=False, note="未构建",
                        bone_names=[], rest_arm=np.zeros((0, 3)),
                        topk_idx=np.zeros((0, LBS_TOPK), dtype=np.int64),
                        topk_w=np.zeros((0, LBS_TOPK)),
                        rest_inv=np.zeros((0, 4, 4)), pose_mats=np.zeros((0, 4, 4)),
                        arm_mw=np.identity(4), bone_to_rows={},
                        current=np.zeros((0, 3)))

    arm_obj = bpy.data.objects.get(cache.arm_obj_name)
    if arm_obj is None:
        invalid.note = "骨架不存在"
        return invalid

    # 1) 收集骨骼列表（顶点组与骨架骨骼的交集）
    mesh_objs = []
    for part in cache.b_parts:
        obj = bpy.data.objects.get(part.obj_name)
        if obj is None:
            invalid.note = f"网格 '{part.obj_name}' 不存在"
            return invalid
        mesh_objs.append(obj)
    preserve_volume = any(
        mod.type == 'ARMATURE'
        and mod.object == arm_obj
        and mod.show_viewport
        and bool(getattr(mod, 'use_deform_preserve_volume', False))
        for obj in mesh_objs for mod in obj.modifiers)
    vg_names = set()
    for obj in mesh_objs:
        for vg in obj.vertex_groups:
            vg_names.add(vg.name)
    bone_names = sorted(b.name for b in arm_obj.data.bones if b.name in vg_names)
    if not bone_names:
        invalid.note = "无匹配的顶点组骨骼"
        return invalid
    bone_index = {name: i for i, name in enumerate(bone_names)}

    n_samples = len(cache.b_rest_points)
    rest_arm = np.zeros((n_samples, 3))
    topk_idx = np.full((n_samples, LBS_TOPK), -1, dtype=np.int64)
    topk_w = np.zeros((n_samples, LBS_TOPK))

    arm_mw = np.array(arm_obj.matrix_world, dtype=np.float64)
    arm_mw_inv = np.array(arm_obj.matrix_world.inverted(), dtype=np.float64)

    # 2) 基础网格顶点（骨架空间）+ 每采样点 top-K 权重
    offset = 0
    for part, obj in zip(cache.b_parts, mesh_objs):
        base = obj.data
        if len(base.vertices) != part.vert_count:
            invalid.note = f"'{part.obj_name}' 基础网格与评估网格顶点数不一致（存在增减顶点的修改器）"
            return invalid
        count = len(base.vertices)
        arr = np.empty(count * 3, dtype=np.float64)
        base.vertices.foreach_get("co", arr)
        base_verts = arr.reshape(count, 3)
        m_rel = arm_mw_inv @ np.array(obj.matrix_world, dtype=np.float64)
        base_arm = base_verts @ m_rel[:3, :3].T + m_rel[:3, 3]
        idx = part.sample_vert_indices
        rows = np.arange(offset, offset + len(idx))
        rest_arm[rows] = base_arm[idx]
        vg_index = {vg.index: vg.name for vg in obj.vertex_groups}
        for row, vi in zip(rows, idx):
            groups = base.vertices[int(vi)].groups
            cand = []
            for g in groups:
                if g.weight < 1e-4:
                    continue
                name = vg_index.get(g.group)
                bi = bone_index.get(name)
                if bi is not None:
                    cand.append((float(g.weight), bi))
            if not cand:
                continue  # 零权重静态行：保持构建时位置
            cand.sort(key=lambda t: -t[0])
            cand = cand[:LBS_TOPK]
            total = sum(w for w, _ in cand)
            if total < 1e-12:
                continue
            for k, (w, bi) in enumerate(cand):
                topk_idx[row, k] = bi
                topk_w[row, k] = w  # 不归一化：Blender 对权重和<1的剩余部分按恒等处理
        offset += len(idx)

    rest_inv = np.zeros((len(bone_names), 4, 4))
    for i, name in enumerate(bone_names):
        rest_inv[i] = np.array(arm_obj.data.bones[name].matrix_local.inverted(),
                               dtype=np.float64)

    context.view_layer.update()
    pose_mats = np.zeros((len(bone_names), 4, 4))
    for i, name in enumerate(bone_names):
        pose_mats[i] = np.array(arm_obj.pose.bones[name].matrix, dtype=np.float64)

    closure = _bone_descendant_closure(arm_obj, bone_names)
    bone_to_rows: Dict[int, np.ndarray] = {}
    for i in range(len(bone_names)):
        members = closure[i]
        mask = np.isin(topk_idx, members) & (topk_w > 0.0)
        bone_to_rows[i] = np.unique(np.nonzero(mask.any(axis=1))[0])

    def _compute_rows(deltas: np.ndarray, rows: np.ndarray, out: np.ndarray) -> None:
        if len(rows) == 0:
            return
        out[rows] = (arm_mw[:3, :3] @ pc_engine.lbs_transform_with_remainder(
            rest_arm[rows], deltas, topk_w[rows], topk_idx[rows]).T).T + arm_mw[:3, 3]

    current = cache.b_rest_points.copy()
    deltas = pose_mats @ rest_inv
    all_rows = np.arange(n_samples)
    _compute_rows(deltas, all_rows, current)

    diag = float(np.linalg.norm(np.ptp(cache.b_rest_points, axis=0)))
    diag = max(diag, 1e-6)

    # 验证 1：同姿态 LBS vs depsgraph
    dev = float(np.max(np.linalg.norm(current - cache.b_rest_points, axis=1)))
    if dev > tol_rest_ratio * diag:
        invalid.note = f"同姿态偏差 {dev:.5f} 超阈值（可能存在其它形变修改器/保留体积）"
        return invalid

    if preserve_volume:
        # Linear LBS cannot reproduce Blender's dual-quaternion Preserve
        # Volume deformation. Keep the cache for the supported approximate
        # virtual-rig path and avoid an expensive validation that must fail.
        return PCLBSData(
            enabled=True, valid=False,
            note="检测到保留体积蒙皮，使用近似虚拟骨架（周期真实校正）",
            bone_names=bone_names, rest_arm=rest_arm,
            topk_idx=topk_idx, topk_w=topk_w,
            rest_inv=rest_inv, pose_mats=pose_mats,
            arm_mw=arm_mw, bone_to_rows=bone_to_rows,
            current=current)

    # 验证 2：扰动验证（暴露保留体积/双四元数等差异）
    candidates = [n for n in perturb_bones if n in bone_index][:5]
    if candidates:
        saved_basis = {n: get_basis(arm_obj.pose.bones[n]) for n in candidates}
        rng = np.random.default_rng(0)
        try:
            for n in candidates:
                axis = rng.normal(size=3)
                axis = axis / (np.linalg.norm(axis) + 1e-12)
                ang = np.deg2rad(45.0)
                k = axis
                rot = (np.cos(ang) * np.identity(3)
                       + np.sin(ang) * np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
                       + (1.0 - np.cos(ang)) * np.outer(k, k))
                basis = saved_basis[n].copy()
                new_rot = rot @ pc_engine.basis_rotation_matrix(basis)
                set_basis(arm_obj.pose.bones[n],
                          pc_engine.compose_basis(basis[:3, 3], new_rot,
                                                  pc_engine.basis_scale(basis)))
            context.view_layer.update()
            for n in candidates:
                pose_mats[bone_index[n]] = np.array(arm_obj.pose.bones[n].matrix,
                                                    dtype=np.float64)
            deltas = pose_mats @ rest_inv
            _compute_rows(deltas, all_rows, current)
            ref = read_b_samples(cache, context)
            dev = float(np.max(np.linalg.norm(current - ref, axis=1)))
        finally:
            for n in candidates:
                set_basis(arm_obj.pose.bones[n], saved_basis[n])
            context.view_layer.update()
            for n in candidates:
                pose_mats[bone_index[n]] = np.array(arm_obj.pose.bones[n].matrix,
                                                    dtype=np.float64)
            deltas = pose_mats @ rest_inv
            _compute_rows(deltas, all_rows, current)
        if dev > tol_pose_ratio * diag:
            # 保留已构建的近似数据：上层可启用"近似高速 + 周期真实校正"
            return PCLBSData(enabled=True, valid=False,
                             note="检测到约束或特殊继承，使用近似虚拟骨架（周期真实校正）",
                             bone_names=bone_names, rest_arm=rest_arm,
                             topk_idx=topk_idx, topk_w=topk_w,
                             rest_inv=rest_inv, pose_mats=pose_mats,
                             arm_mw=arm_mw, bone_to_rows=bone_to_rows,
                             current=current)

    return PCLBSData(enabled=True, valid=True, note="已验证",
                     bone_names=bone_names, rest_arm=rest_arm,
                     topk_idx=topk_idx, topk_w=topk_w,
                     rest_inv=rest_inv, pose_mats=pose_mats,
                     arm_mw=arm_mw, bone_to_rows=bone_to_rows,
                     current=current)


def lbs_read(cache: PCCache, lbs: Optional[PCLBSData],
             changed: Optional[set], context,
             allow_invalid: bool = False) -> np.ndarray:
    """快速读取 B 采样点云：有效缓存走自算 LBS，否则回退全量重读。

    changed: 本次写入了 basis 的骨骼名集合；None=全部重算；空集=直接返回缓冲。
    """
    if lbs is None or (not lbs.valid and not allow_invalid):
        return read_b_samples(cache, context)
    if changed is not None and len(changed) == 0:
        return lbs.current

    arm_obj = bpy.data.objects.get(cache.arm_obj_name)
    if arm_obj is None:
        return read_b_samples(cache, context)

    context.view_layer.update()
    bone_index = {name: i for i, name in enumerate(lbs.bone_names)}

    if changed is None:
        refresh = set(range(len(lbs.bone_names)))
    else:
        refresh = {bone_index[n] for n in changed if n in bone_index}
        unknown = [n for n in changed if n not in bone_index]
        if unknown:
            # 控制器骨经约束传导影响任意变形骨：全部刷新
            refresh = set(range(len(lbs.bone_names)))

    if not refresh:
        return lbs.current

    pose_refresh = (
        range(len(lbs.bone_names))
        if allow_invalid else refresh)
    for i in pose_refresh:
        lbs.pose_mats[i] = np.array(arm_obj.pose.bones[lbs.bone_names[i]].matrix,
                                    dtype=np.float64)
    rows = np.unique(np.concatenate([lbs.bone_to_rows[i] for i in refresh]))
    deltas = lbs.pose_mats @ lbs.rest_inv
    if len(rows) > 0:
        lbs.current[rows] = (lbs.arm_mw[:3, :3] @ pc_engine.lbs_transform_with_remainder(
            lbs.rest_arm[rows], deltas, lbs.topk_w[rows], lbs.topk_idx[rows]).T).T \
            + lbs.arm_mw[:3, 3]
    return lbs.current


def validate_lbs_subset(
        cache: PCCache,
        lbs: Optional[PCLBSData],
        arm_obj: bpy.types.Object,
        context,
        bone_names: List[str],
        tau: float,
        rot_deg: float = 10.0,
        scale_mul: float = 1.2) -> Tuple[bool, str]:
    """Check whether the invalid LBS cache is still exact for selected bones.

    Some rigs fail the broad perturb validation because of unrelated controller
    or inheritance complexity elsewhere, while a tiny enabled subset still
    matches Blender's evaluated mesh almost exactly. This targeted check lets
    the dual-bone exact path reuse the fast sampled LBS reader when the actual
    selected bones remain faithful.
    """
    if lbs is None or arm_obj is None:
        return False, "LBS/骨架不存在"
    names = [name for name in bone_names if arm_obj.pose.bones.get(name) is not None]
    if not names:
        return False, "无可验证骨骼"

    saved_basis = {name: get_basis(arm_obj.pose.bones[name]) for name in names}
    pose = np.zeros_like(lbs.pose_mats)

    def _approx_points() -> np.ndarray:
        for i, name in enumerate(lbs.bone_names):
            pose[i] = np.array(arm_obj.pose.bones[name].matrix, dtype=np.float64)
        deltas = pose @ lbs.rest_inv
        return (
            lbs.arm_mw[:3, :3] @ pc_engine.lbs_transform_with_remainder(
                lbs.rest_arm, deltas, lbs.topk_w, lbs.topk_idx).T
        ).T + lbs.arm_mw[:3, 3]

    def _max_dev() -> float:
        exact = read_b_samples(cache, context)
        approx = _approx_points()
        return float(np.max(np.linalg.norm(approx - exact, axis=1)))

    devs: List[float] = []
    tol = max(1e-5, float(tau) * 0.05)
    try:
        devs.append(_max_dev())
        for name in names:
            basis = saved_basis[name]
            rot_base = pc_engine.basis_rotation_matrix(basis)
            scale_base = pc_engine.basis_scale(basis)

            rot_x = pc_engine.rotvec_to_mat3(
                np.array([np.deg2rad(rot_deg), 0.0, 0.0], dtype=np.float64))
            set_basis(
                arm_obj.pose.bones[name],
                pc_engine.compose_basis(
                    basis[:3, 3], rot_x @ rot_base, scale_base))
            context.view_layer.update()
            devs.append(_max_dev())
            set_basis(arm_obj.pose.bones[name], basis)
            context.view_layer.update()

            rot_y = pc_engine.rotvec_to_mat3(
                np.array([0.0, np.deg2rad(rot_deg * 2.0), 0.0], dtype=np.float64))
            set_basis(
                arm_obj.pose.bones[name],
                pc_engine.compose_basis(
                    basis[:3, 3], rot_y @ rot_base, scale_base))
            context.view_layer.update()
            devs.append(_max_dev())
            set_basis(arm_obj.pose.bones[name], basis)
            context.view_layer.update()

            scale = scale_base.copy()
            scale[0] *= float(scale_mul)
            set_basis(
                arm_obj.pose.bones[name],
                pc_engine.compose_basis(
                    basis[:3, 3], rot_base, scale))
            context.view_layer.update()
            devs.append(_max_dev())
            set_basis(arm_obj.pose.bones[name], basis)
            context.view_layer.update()

        if len(names) >= 2:
            left = saved_basis[names[0]]
            right = saved_basis[names[1]]
            rot_l = pc_engine.rotvec_to_mat3(
                np.array([0.0, 0.0, np.deg2rad(rot_deg)], dtype=np.float64))
            rot_r = pc_engine.rotvec_to_mat3(
                np.array([0.0, 0.0, -np.deg2rad(rot_deg)], dtype=np.float64))
            set_basis(
                arm_obj.pose.bones[names[0]],
                pc_engine.compose_basis(
                    left[:3, 3], rot_l @ pc_engine.basis_rotation_matrix(left),
                    pc_engine.basis_scale(left)))
            set_basis(
                arm_obj.pose.bones[names[1]],
                pc_engine.compose_basis(
                    right[:3, 3], rot_r @ pc_engine.basis_rotation_matrix(right),
                    pc_engine.basis_scale(right)))
            context.view_layer.update()
            devs.append(_max_dev())
    finally:
        for name, basis in saved_basis.items():
            set_basis(arm_obj.pose.bones[name], basis)
        context.view_layer.update()

    max_dev = max(devs) if devs else float("inf")
    if max_dev <= tol:
        return True, f"选中骨骼子集 LBS 验证通过（max_dev={max_dev:.2e}, tol={tol:.2e}）"
    return False, f"选中骨骼子集 LBS 偏差 {max_dev:.5g} 超阈值 {tol:.5g}"


# ---------------------------------------------------------------------------
# 虚拟骨架（headless 高速迭代）：bpy 侧构建与验证
# ---------------------------------------------------------------------------

def build_virtual_rig_from_cache(cache: PCCache, lbs: PCLBSData,
                                 arm_obj: bpy.types.Object):
    """由缓存 + LBS 数据 + 骨架对象构建纯 numpy 虚拟骨架。"""
    from . import pc_virtualrig
    parents: Dict[str, Optional[str]] = {}
    local_mats: Dict[str, np.ndarray] = {}
    for name in lbs.bone_names:
        bone = arm_obj.data.bones.get(name)
        if bone is None:
            continue
        parents[name] = bone.parent.name if bone.parent else None
        local_mats[name] = np.array(bone.matrix_local, dtype=np.float64)
    usable = [n for n in lbs.bone_names if n in local_mats]
    index = {n: i for i, n in enumerate(lbs.bone_names)}
    keep = [index[n] for n in usable]
    basis_map = {n: get_basis(arm_obj.pose.bones[n]) for n in usable}
    return pc_virtualrig.build_virtual_rig(
        bone_names=usable,
        parents=parents,
        local_mats=local_mats,
        rest_arm=lbs.rest_arm,
        topk_w=lbs.topk_w,
        topk_idx=np.clip(lbs.topk_idx, -1, len(lbs.bone_names) - 1),
        bone_to_rows=lbs.bone_to_rows,
        arm_mw=lbs.arm_mw,
        basis_map=basis_map,
    )


def validate_virtual_rig_subset(
        cache: PCCache,
        vrig,
        arm_obj: bpy.types.Object,
        context,
        bone_names: List[str],
        tau: float,
        rot_deg: float = 10.0,
        scale_mul: float = 1.2) -> Tuple[bool, str]:
    """Validate selected-bone virtual-rig sample reads against exact Blender mesh eval."""
    names = [
        n for n in bone_names
        if arm_obj.pose.bones.get(n) is not None and vrig is not None
        and vrig.has_bone(n)
    ]
    if not names:
        return False, "无可验证 VirtualRig 子集"
    saved_basis = {name: get_basis(arm_obj.pose.bones[name]) for name in names}

    def _set_pair(name: str, basis: np.ndarray) -> None:
        set_basis(arm_obj.pose.bones[name], basis)
        vrig.set_basis(name, basis)

    def _max_dev() -> float:
        exact = read_b_samples(cache, context)
        approx = vrig.read_samples(pc_engine.lbs_transform_with_remainder).copy()
        return float(np.max(np.linalg.norm(approx - exact, axis=1)))

    devs: List[float] = []
    tol = max(1e-4, float(tau) * 0.5)
    try:
        context.view_layer.update()
        devs.append(_max_dev())
        for name in names:
            basis = saved_basis[name]
            rot_base = pc_engine.basis_rotation_matrix(basis)
            scale_base = pc_engine.basis_scale(basis)

            rot_x = pc_engine.rotvec_to_mat3(
                np.array([np.deg2rad(rot_deg), 0.0, 0.0], dtype=np.float64))
            _set_pair(
                name,
                pc_engine.compose_basis(
                    basis[:3, 3], rot_x @ rot_base, scale_base))
            context.view_layer.update()
            devs.append(_max_dev())
            _set_pair(name, basis)
            context.view_layer.update()

            rot_y = pc_engine.rotvec_to_mat3(
                np.array([0.0, np.deg2rad(rot_deg * 2.0), 0.0], dtype=np.float64))
            _set_pair(
                name,
                pc_engine.compose_basis(
                    basis[:3, 3], rot_y @ rot_base, scale_base))
            context.view_layer.update()
            devs.append(_max_dev())
            _set_pair(name, basis)
            context.view_layer.update()

            scale = scale_base.copy()
            scale[0] *= float(scale_mul)
            _set_pair(
                name,
                pc_engine.compose_basis(
                    basis[:3, 3], rot_base, scale))
            context.view_layer.update()
            devs.append(_max_dev())
            _set_pair(name, basis)
            context.view_layer.update()

        if len(names) >= 2:
            left = saved_basis[names[0]]
            right = saved_basis[names[1]]
            rot_l = pc_engine.rotvec_to_mat3(
                np.array([0.0, 0.0, np.deg2rad(rot_deg)], dtype=np.float64))
            rot_r = pc_engine.rotvec_to_mat3(
                np.array([0.0, 0.0, -np.deg2rad(rot_deg)], dtype=np.float64))
            _set_pair(
                names[0],
                pc_engine.compose_basis(
                    left[:3, 3],
                    rot_l @ pc_engine.basis_rotation_matrix(left),
                    pc_engine.basis_scale(left)))
            _set_pair(
                names[1],
                pc_engine.compose_basis(
                    right[:3, 3],
                    rot_r @ pc_engine.basis_rotation_matrix(right),
                    pc_engine.basis_scale(right)))
            context.view_layer.update()
            devs.append(_max_dev())
    finally:
        for name, basis in saved_basis.items():
            _set_pair(name, basis)
        context.view_layer.update()
        vrig.read_samples(pc_engine.lbs_transform_with_remainder)

    max_dev = max(devs) if devs else float("inf")
    if max_dev <= tol:
        return True, (
            f"选中骨骼子集 VirtualRig 验证通过"
            f"（max_dev={max_dev:.2e}, tol={tol:.2e}）")
    return False, (
        f"选中骨骼子集 VirtualRig 偏差 {max_dev:.5g}"
        f" 超阈值 {tol:.5g}")


def validate_virtual_rig(vrig, arm_obj: bpy.types.Object, context,
                         test_bones, tol: float = 1e-4):
    """用真实 Blender 姿态评估验证虚拟骨架递推（扰动法）。

    test_bones 为空时跳过验证（视为通过）。
    """
    from . import pc_virtualrig
    names = [n for n in test_bones if arm_obj.pose.bones.get(n) is not None]
    if not names:
        return True, "无可验证骨骼（跳过）"
    needs_update = [True]

    def read_pose(name: str) -> np.ndarray:
        if needs_update[0]:
            context.view_layer.update()
            needs_update[0] = False
        return np.array(arm_obj.pose.bones[name].matrix, dtype=np.float64)

    def apply_basis(name: str, basis: np.ndarray) -> None:
        pb = arm_obj.pose.bones.get(name)
        if pb is not None:
            set_basis(pb, basis)
            needs_update[0] = True

    ok, note = pc_virtualrig.validate_pose_recursion(
        vrig, names, read_pose, apply_basis, tol=tol)
    # 恢复后确保 Blender 侧回到原姿态
    context.view_layer.update()
    return ok, note
