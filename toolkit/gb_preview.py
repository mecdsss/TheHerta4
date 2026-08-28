# -*- coding: utf-8 -*-
"""高斯权重球：真实热力图预览与生命周期辅助（纯 numpy，不依赖 bpy，可直接 pytest）。

与 gb_core 一样保持无 bpy 依赖，把"方向标签 / 脏检测签名 / 均匀缩放判定 /
变形位置评估守卫 / 测地快速路径 / 单球贡献选择 / 可写性判定"等可测逻辑集中在此，
由 gb_operators 在真实 Blender 数据上调用。测试直接 importlib 加载本文件即可。

设计要点（对应 t1 调研 NU1-NU6）：
- 方向标签：会话方向在面板上明确显示（源→目标 / 目标自身 / 目标→源合集）。
- 签名：拓扑 / 顶点位置 / 骨骼姿态 / 形态键值 的稳定哈希，用于 tick 的脏检测，
  避免持续拖动反复重建批次与重算场。
- 变形位置守卫：depsgraph 评估网格顶点数与基础网格不一致时回退基础网格
  （顶点索引一一对应性守卫，与顶点组匹配节点 use_shape_key 链路一致）。
- 测地快速路径：均匀缩放球复用世界坐标邻接表（surface_distances_uniform_scale），
  与逐球重建局部邻接表的 geodesic_field 结果一致但省去每球每目标一次 O(E) 构建。
- 单球贡献：预览模式区分"组合权重(max 合并)"与"单个球贡献"。
"""
import hashlib

import numpy as np

from . import gb_core
from . import gb_resolve

# ---------------------------------------------------------------------------
# 方向标签（面板/会话模型共用；t2 反向写入设置 direction="reverse"）
# ---------------------------------------------------------------------------

#: 会话方向取值与 gb_resolve 共享（DIRECTION_FORWARD/SELF/REVERSE）


def direction_label(mode: str = "", direction: str = "") -> str:
    """会话方向标签（面板行首显示）。

    Args:
        mode: 现有会话模式 'source' | 'target'（兼容旧会话无 direction 字段）。
        direction: gb_resolve 方向常量
            'forward' | 'self' | 'reverse'。

    Returns:
        str：如 "源→目标" / "目标自身" / "目标→源合集"。
    """
    if direction == gb_resolve.DIRECTION_REVERSE:
        return "目标→源合集"
    if direction in (gb_resolve.DIRECTION_SELF,):
        return "目标自身"
    if mode == "source" or direction == gb_resolve.DIRECTION_FORWARD:
        return "源→目标"
    if mode == "target":
        return "目标自身"
    return "方向未知"


# ---------------------------------------------------------------------------
# 脏检测签名（拓扑 / 位置 / 姿态 / 形态键）
# ---------------------------------------------------------------------------

def hash_state(arrays, decimals: int = 4, token: str = "") -> str:
    """多个 float64 数组的稳定 blake2b 签名（脏检测用）。

    相同数值（按 decimals 舍入）得到相同签名；不依赖数据布局顺序。
    token 为附加字符串种子（如网格数据块身份），参与哈希但不必是数值。
    """
    h = hashlib.blake2b(digest_size=16)
    if token:
        h.update(token.encode("utf-8"))
        h.update(b"\x00")
    for a in arrays:
        arr = np.asarray(a, dtype=np.float64)
        if arr.size:
            h.update(np.round(arr, decimals).astype("<f8").tobytes())
        h.update(b"\x00")
    return h.hexdigest()


def topo_signature(vert_count: int, edge_verts) -> str:
    """网格拓扑签名：顶点数 + 边数组。

    顶点数或边集变化（拓扑改变/顶点数改变）都会改变签名；模版评估结果
    与基础网格一致时，签名可作为评估缓存有效期的依据。
    """
    return hash_state([np.asarray([int(vert_count)], dtype=np.float64),
                       np.asarray(edge_verts, dtype=np.int64)])


def pose_signature(bone_matrices) -> str:
    """骨骼姿态签名：全部骨骼矩阵（world/local 均可，调用方决定）哈希。

    bone_matrices: list[(4,4) ndarray]；空列表返回固定串（无骨骼）。
    """
    if not bone_matrices:
        return "no-bones"
    return hash_state([np.asarray(m, dtype=np.float64) for m in bone_matrices])


def shapekey_signature(values) -> str:
    """形态键值签名：非基础形态键当前 value 列表。

    values: list[float]（bpy 侧取 key_blocks[1:] 的 value）；空列表（无形态键）
    返回固定串。
    """
    if not values:
        return "no-shapekeys"
    return hash_state([np.asarray(values, dtype=np.float64)])


# ---------------------------------------------------------------------------
# 变形位置评估守卫（复用顶点组匹配节点 use_shape_key 范式）
# ---------------------------------------------------------------------------

def evaluation_decision(base_count: int, evaluated_count) -> tuple:
    """决定是否采用评估（变形后）顶点位置。

    Args:
        base_count: 基础网格顶点数（原始可写顶点组索引空间）。
        evaluated_count: 评估网格顶点数；None 表示评估失败。

    Returns:
        (use_evaluated: bool, message: str)
        evaluated_count == base_count → (True, "")；
        不一致 → (False, 说明)，调用方回退基础网格（索引一一对应性被破坏）。
    """
    if evaluated_count is None:
        return False, "变形网格评估失败，回退基础网格位置"
    if int(evaluated_count) == int(base_count):
        return True, ""
    return False, (
        f"变形网格顶点数({evaluated_count})≠基础网格({base_count})，"
        f"顶点索引无法一一对应，回退基础网格位置")


def geometry_mix_value(use_evaluated: bool) -> float:
    """把是否使用评估位置编码进几何签名（数值层）。"""
    return 1.0 if use_evaluated else 0.0


# ---------------------------------------------------------------------------
# 均匀缩放判定（决定能否走测地快速路径）
# ---------------------------------------------------------------------------

def is_uniform_scale(scale, rel_tol: float = 1e-4) -> bool:
    """均匀缩放判定：|sx-sy|,|sy-sz| ≤ rel_tol*max(|s|,1e-6)。

    均匀缩放球可复用世界坐标邻接表做沿表面传播；非均匀（椭球）必须回退
    逐球局部邻接表的 geodesic_field。
    """
    s = np.asarray(scale, dtype=np.float64).reshape(-1)
    if s.size < 3:
        return True
    s = s[:3]
    bound = rel_tol * max(float(np.max(np.abs(s))), 1e-6)
    return bool(np.allclose(s, s[0], rtol=0.0, atol=bound))


# ---------------------------------------------------------------------------
# 测地快速路径（均匀缩放球复用世界邻接表）
# ---------------------------------------------------------------------------

def geodesic_field_fast(world_pts, adjacency, center, scale,
                        strength, falloff_k, allowed_mask=None):
    """均匀缩放球的沿表面传播权重场（快速路径）。

    与 gb_core.geodesic_field 同形（w = strength*exp(-k*d²)，d≥1 为 0），
    但邻接表是世界坐标预构建的（与 draginteraction 的
    surface_distances_uniform_scale 一致）：球局部距离 = 世界沿表面距离 / scale。

    Args:
        world_pts: (N,3) 目标顶点世界坐标。
        adjacency: gb_core.build_surface_adjacency(world_pts, edge_verts) 预构建。
        center: (3,) 球心世界坐标。
        scale: 球均匀缩放（球半径）。
        strength / falloff_k: 场参数。
        allowed_mask: 可选 (N,) bool；None = 全部允许。
    """
    pts = np.asarray(world_pts, dtype=np.float64).reshape(-1, 3)
    n = pts.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    d = gb_core.surface_distances_uniform_scale(
        adjacency, pts, center, float(scale), allowed_mask=allowed_mask)
    field = float(strength) * np.exp(-float(falloff_k) * d * d)
    field[d >= 1.0] = 0.0
    return field


# ---------------------------------------------------------------------------
# 单球贡献 / 组合权重选择
# ---------------------------------------------------------------------------

def pick_single_or_merged(per_ball_fields, ball_names, single_ball_name=None):
    """按预览模式选择展示的权重场。

    Args:
        per_ball_fields: list[(ball_name, (N,) ndarray)]，与 _compute_merged_field
            的逐球结果一致（已含岛掩码，未合并）。
        ball_names: 会话全部球名（含禁用球，用于校验名字归属）。
        single_ball_name: 单球预览模式指定的球名；None 或无此球 → 组合。

    Returns:
        (field (N,), display_ball (str|None), note (str))
        display_ball 为 None 表示组合权重（max 合并）；note 为附加说明。
    """
    if not per_ball_fields:
        return np.zeros(0, dtype=np.float64), None, ""
    if single_ball_name is not None:
        for name, f in per_ball_fields:
            if name == single_ball_name:
                return np.asarray(f, dtype=np.float64), name, ""
        # 指定球存在但被禁用/不在本次计算列表 → 回退组合并说明
        if single_ball_name in ball_names:
            field = gb_core.merge_fields_max([f for _, f in per_ball_fields])
            return field, None, "（指定球已停用，显示组合权重）"
        return gb_core.merge_fields_max([f for _, f in per_ball_fields]), None, ""
    field = gb_core.merge_fields_max([f for _, f in per_ball_fields])
    if len(per_ball_fields) > 1:
        return field, None, f"（{len(per_ball_fields)} 球 max 合并）"
    return field, None, ""


# ---------------------------------------------------------------------------
# 可写性判定与轻量反馈
# ---------------------------------------------------------------------------

def not_writable_reason(obj) -> str:
    """对象不可写原因；"" 表示可写。

    覆盖：物体/网格数据来自链接库（library 覆盖不可直接改顶点组）、
    物体无网格数据。其余情况视为可写（顶点组可自动创建）。
    """
    if obj is None:
        return "对象不存在"
    if getattr(obj, "library", None) is not None:
        return f"'{obj.name}' 来自链接库（library），不可直接写入顶点组"
    data = getattr(obj, "data", None)
    if data is None or getattr(getattr(data, "library", None), "name", None):
        if data is not None and getattr(data, "library", None) is not None:
            return f"'{obj.name}' 的网格数据来自链接库，不可直接写入"
    if getattr(obj, "type", "") != "MESH":
        return f"'{obj.name}' 不是网格物体，不可写入顶点组"
    return ""


def matched_edge_feedback(is_connected, matched_count) -> str:
    """无匹配边反馈：区分"未匹配（可补权）"与正常。

    Args:
        is_connected: 调试物体 is_connected（匹配节点画的边）。
        matched_count: 调试父 vgtp_matched_count（本目标匹配总数）。

    Returns:
        str：非空时需要提示用户（不阻断操作）。
    """
    if matched_count is not None and int(matched_count) <= 0:
        return "该顶点组/目标无任何匹配边——权重将按同名组写入（未匹配补权路径）"
    if not is_connected:
        return "该调试物体无匹配边（未连接的源组）——按同名组写入目标集合"
    return ""


def eval_capability_feedback(obj) -> str:
    """该物体是否具备变形评估能力（骨骼/形态键）；空串 = 普通基础网格。"""
    if obj is None:
        return "对象不存在"
    has_sk = False
    data = getattr(obj, "data", None)
    if data is not None:
        shape_keys = getattr(data, "shape_keys", None)
        has_sk = bool(shape_keys and getattr(shape_keys, "key_blocks", None))
    has_arm = False
    for mod in getattr(obj, "modifiers", ()) or ():
        if getattr(mod, "type", "") == "ARMATURE":
            has_arm = True
            break
    if has_sk or has_arm:
        return ""
    return "该对象无骨骼/形态键修改器，评估位置与基础网格一致（仅矩阵变换参与）"