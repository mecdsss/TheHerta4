# -*- coding: utf-8 -*-
"""高斯权重球：多会话状态、操作符、计时器与 GPU 热力图绘制。

架构：
- _sessions: session_id -> _GBSession，多个会话（不同调试物体/顶点组）可并存。
- 每个会话持有一组目标物体（source 模式 = 与调试父同源的全部匹配目标；
  target 模式 = 目标物体自身），每个目标有独立的热力图批与网格岛缓存，
  叠加层同时显示在所有目标上；确认时同名顶点组写入全部目标并分别规格化。
- 确认时可勾选多个会话，按勾选顺序依次写入；写入的组临时 lock_weight，
  统一规格化时不受影响，再恢复锁状态。
- 确认后为每个会话创建与顶点组匹配节点一致的调试标记物体：
  Source 模式 -> 绿色方块（无连接），Target 模式 -> 黄色球。
- 预览期不写入任何真实权重；确认/取消/注销的清理都汇入 _cleanup_session()。
"""
import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader

from . import gb_core
from ..utils.log_utils import LOG

# ---------------------------------------------------------------------------
# 模块级会话状态
# ---------------------------------------------------------------------------

_state = "idle"            # 'idle' | 'active'（存在任意会话即 active）
_dirty = True              # 全局重算标记（全局参数变化时置位）
_timer_registered = False
_draw_handler = None

_sessions = {}             # session_id -> _GBSession
_next_session_id = 1
_active_session_id = None
_select_counter = 0        # 勾选顺序计数器（确认按此顺序写入）


class _GBTargetCache:
    """会话内单个目标物体的运行时缓存（热力图/岛/顶点坐标）。"""

    def __init__(self, name):
        self.name = name
        self.verts_world = None       # (N,3) 目标基础网格世界坐标
        self.tri_indices = None       # (M,3) 三角面索引
        self.edge_verts = None        # (E,2) 边顶点（岛计算备用）
        self.island_ids = None        # (N,) 岛 ID（懒计算）
        self.island_count = 0
        self.colors = None            # (N,4) 热力图颜色缓冲
        self.matrix_sig = None        # 目标 matrix_world 签名
        self.batch = None             # GPUBatch（主层，深度测试）
        self.ghost_batch = None       # GPUBatch（幽灵层，无深度测试，透视用）
        self.positions = None         # (N,3) 热力图位置（法线偏移后）
        self.preview_info = ""


class _GBSession:
    """一个高斯球会话（一个顶点组 + N 个球 + 1..K 个目标物体）。"""

    def __init__(self, session_id):
        self.id = session_id
        self.mode = ""                # 'source' | 'target'
        self.vg_name = ""
        self.source_key = frozenset()  # 解析后的源物体名集合（去重比较用）
        self.source_info = ""         # 面板展示用
        self.debug_parent_name = ""   # 顶点组匹配节点的调试父 Empty（标记挂到它下面）
        self.session_root_name = ""   # GB_Session_* 根 Empty
        self.ball_names = []          # 球 Empty 名列表
        self.source_positions = None  # (M,3) 源顶点组非零权重顶点世界坐标（采样场用）
        self.source_weights = None    # (M,) 与 source_positions 对应的原始权重
        self.targets = {}             # target_name -> _GBTargetCache（保持插入序）
        self.matrix_signatures = {}   # 球名 -> matrix_world 签名
        self.selected = False         # 面板勾选（多选确认用）
        self.select_order = 0         # 勾选顺序（0=未勾选）

    def target_names(self):
        return list(self.targets.keys())

    @property
    def primary_target_name(self):
        return next(iter(self.targets), "")


def _sync_state():
    global _state
    _state = "active" if _sessions else "idle"


def mark_dirty():
    global _dirty
    _dirty = True


def get_active_session():
    """活动会话：优先取活动物体所属会话，否则取上次活动的会话。"""
    global _active_session_id
    obj = bpy.context.active_object
    if obj is not None:
        sid = obj.get("gb_session_id")
        if sid in _sessions:
            _active_session_id = sid
    return _sessions.get(_active_session_id)


def _selected_sessions():
    """按勾选顺序返回勾选的会话列表。"""
    return sorted(
        (s for s in _sessions.values() if s.selected),
        key=lambda s: s.select_order)


def _sorted_sessions():
    """按创建顺序返回全部会话 ID 列表（面板遍历用）。"""
    return sorted(_sessions.keys())


# ---------------------------------------------------------------------------
# 数据读取 helpers
# ---------------------------------------------------------------------------

def _mesh_vertices_world(obj):
    """基础网格顶点世界坐标（foreach_get + matrix_world）。"""
    count = len(obj.data.vertices)
    arr = np.empty(count * 3, dtype=np.float64)
    obj.data.vertices.foreach_get("co", arr)
    co = arr.reshape(count, 3)
    mw = np.array(obj.matrix_world, dtype=np.float64)
    return co @ mw[:3, :3].T + mw[:3, 3]


def _read_vg_weights(obj, vg_name):
    """读取物体某顶点组的逐顶点权重 (N,)；组不存在返回 None。"""
    vg = obj.vertex_groups.get(vg_name)
    if vg is None:
        return None
    weights = np.zeros(len(obj.data.vertices), dtype=np.float64)
    for i, v in enumerate(obj.data.vertices):
        for g in v.groups:
            if g.group == vg.index:
                weights[i] = g.weight
                break
    return weights


def _read_source_weights(debug_parent, vg_name):
    """解析并读取权重来源数据（支持合集匹配的临时合并物体与多物体合集）。

    与顶点组匹配节点的解析语义一致（复用其 get_debug_* 函数）：
    临时合并物体优先；否则取源合集的全部网格物体（或单个源物体），
    聚合所有含该顶点组的物体的权重（世界坐标，与临时合并物体语义一致）。

    Returns:
        (positions, weights, desc)；失败时 weights=None，desc 为失败原因。
    """
    from ..blueprint.node_vertex_group_match import (
        get_debug_runtime_source_object, get_debug_source_objects)

    runtime_obj = get_debug_runtime_source_object(debug_parent)
    if runtime_obj is not None:
        weights = _read_vg_weights(runtime_obj, vg_name)
        if weights is not None:
            return (_mesh_vertices_world(runtime_obj), weights,
                    f"临时合并物体 {runtime_obj.name}")

    source_objects = get_debug_source_objects(debug_parent)
    if not source_objects:
        return None, None, "找不到权重来源物体（runtime/源物体/源合集均不存在）"

    sources = []
    names = []
    for obj in source_objects:
        w = _read_vg_weights(obj, vg_name)
        if w is None:
            continue
        sources.append((_mesh_vertices_world(obj), w))
        names.append(obj.name)

    if not sources:
        obj_names = "、".join(o.name for o in source_objects[:4])
        if len(source_objects) > 4:
            obj_names += " 等"
        return None, None, f"来源物体（{obj_names}）上不存在顶点组 '{vg_name}'"

    positions, weights = gb_core.combine_weight_sources(sources)
    if len(names) > 1:
        desc = f"源合集合并 {len(names)} 个物体"
    else:
        desc = f"源物体 {names[0]}"
    return positions, weights, desc


def _resolve_target_names(debug_parent):
    """解析与调试父物体同源（源物体集合一致）的全部匹配目标物体名。

    同源性 = get_debug_source_objects 解析出的源物体名集合相同。
    注意不能比较 vgtp_source_collection 名字：快速匹配每次按目标名创建
    不同的源合集（VGMatchSources_{target}），即使里面装的是同一批源物体，
    按键相等分组会永远失败。

    源侧物体（源物体/源合集成员/临时合并物体）永远不会成为写入目标——
    从机制上杜绝权重被写到原物体上。

    Returns:
        list[str]：去重后的目标物体名列表（当前调试父的目标排第一）。
    """
    source_names = _source_names_for(debug_parent)
    own_target = debug_parent.get("vgtp_target_name", "")

    # 源无法解析时（已被 StartFromDebug 前置拦截，这里只是兜底）不跨调试父分组
    if not source_names:
        return [own_target] if own_target else []

    names = []

    def _accept(name):
        if name and name not in names and name not in source_names:
            names.append(name)

    _accept(own_target)
    for obj in bpy.data.objects:
        if obj is debug_parent or obj.type != 'EMPTY':
            continue
        if not obj.get("vgtp_target_name"):
            continue
        if _source_names_for(obj) != source_names:
            continue
        _accept(obj.get("vgtp_target_name", ""))
    return names


def _source_names_for(debug_parent):
    """解析调试父物体的源侧物体名集合（源物体/源合集成员 + 临时合并物体）。"""
    from ..blueprint.node_vertex_group_match import get_debug_source_objects
    names = {o.name for o in get_debug_source_objects(debug_parent)}
    runtime_name = debug_parent.get("vgtp_runtime_source_object", "")
    if runtime_name:
        names.add(runtime_name)
    return names


def _collect_balls(session):
    """返回会话当前存活的球物体列表（并清理失效名字）。"""
    balls = []
    for name in session.ball_names:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            balls.append(obj)
    if len(balls) != len(session.ball_names):
        session.ball_names = [o.name for o in balls]
    return balls


# ---------------------------------------------------------------------------
# 权重场计算（预览与确认共用；每个目标独立计算）
# ---------------------------------------------------------------------------

def _ensure_islands(tcache, target, props):
    """选项开启时懒计算岛 ID。"""
    if not props.only_nearest_island or tcache.island_ids is not None:
        return
    # 防御：edge_verts 正常由 _build_target_batch 初始化，但本函数不应
    # 隐式依赖它必须先被调用——此处自行补齐，避免 None 引用。
    if tcache.edge_verts is None:
        edges = np.empty(len(target.data.edges) * 2, dtype=np.int64)
        target.data.edges.foreach_get("vertices", edges)
        tcache.edge_verts = edges.reshape(-1, 2)
    tcache.island_ids = gb_core.compute_island_ids(
        len(target.data.vertices), tcache.edge_verts)
    tcache.island_count = len(set(tcache.island_ids.tolist()))


def _compute_merged_field(session, tcache, target, props):
    """对会话全部启用球计算某个目标上的合并权重场 (N,)。"""
    balls = [b for b in _collect_balls(session) if b.gb_ball.enabled]
    verts = tcache.verts_world
    if not balls or verts is None:
        return np.zeros(0, dtype=np.float64)

    _ensure_islands(tcache, target, props)
    fields = []
    for ball in balls:
        mw = np.array(ball.matrix_world, dtype=np.float64)
        # 采样场模式：球内目标顶点取最近源顶点的原始权重（保留真实分布）；
        # 源点云缺失（退化场景）时回退解析高斯，避免权重全 0
        if (ball.gb_ball.use_source_sampling
                and session.source_positions is not None
                and session.source_positions.shape[0] > 0):
            field = gb_core.sampled_field(
                verts, session.source_positions, session.source_weights,
                mw, strength_scale=ball.gb_ball.strength)
        elif (getattr(ball.gb_ball, "use_surface_propagation", True)
                and tcache.edge_verts is not None):
            # 沿表面传播：权重从接触点沿网格表面扩散，不穿透到背面/对侧
            field = gb_core.geodesic_field(
                verts, mw, ball.gb_ball.strength, ball.gb_ball.falloff_k,
                tcache.edge_verts)
        else:
            field = gb_core.gaussian_field(
                verts, mw, ball.gb_ball.strength, ball.gb_ball.falloff_k)
        if props.only_nearest_island and tcache.island_ids is not None:
            bound = gb_core.nearest_island(
                tcache.island_ids, verts, ball.matrix_world.translation)
            field = gb_core.mask_field_to_island(
                field, tcache.island_ids, bound)
        fields.append(field)
    return gb_core.merge_fields_max(fields)


# ---------------------------------------------------------------------------
# GPU 热力图（每会话 × 每目标一个批，叠加层同时显示）
# ---------------------------------------------------------------------------

def _build_target_batch(tcache, target):
    """构建目标的三角批；颜色缓冲随后按帧更新。"""
    mesh = target.data
    mesh.calc_loop_triangles()
    tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", tris)
    tcache.tri_indices = tris.reshape(-1, 3)

    edges = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", edges)
    tcache.edge_verts = edges.reshape(-1, 2)

    verts = tcache.verts_world
    # 法线方向微偏移防 z-fighting
    normals = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("normal", normals)
    normals = normals.reshape(-1, 3)
    mw = np.array(target.matrix_world, dtype=np.float64)
    world_normals = normals @ mw[:3, :3].T
    norms = np.linalg.norm(world_normals, axis=1, keepdims=True)
    norms[norms < 1e-12] = 1.0
    world_normals = world_normals / norms
    tcache.positions = verts + world_normals * 1e-3
    tcache.matrix_sig = tuple(np.array(target.matrix_world).reshape(-1))

    tcache.colors = np.zeros((len(verts), 4), dtype=np.float32)
    _rebuild_batch(tcache)


# 幽灵层透明度倍率（透视层，用于看到被模型自身遮挡的背面权重）
GHOST_ALPHA_FACTOR = 0.3


def _rebuild_batch(tcache):
    if tcache.positions is None or tcache.tri_indices is None:
        return
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    pos = tcache.positions.astype(np.float32)
    indices = tcache.tri_indices.astype(np.int32)
    tcache.batch = batch_for_shader(
        shader, 'TRIS',
        {"pos": pos, "color": tcache.colors},
        indices=indices,
    )
    # 幽灵层：同一几何，alpha 压暗，绘制时关闭深度测试实现透视
    ghost_colors = np.array(tcache.colors, copy=True)
    ghost_colors[:, 3] *= GHOST_ALPHA_FACTOR
    tcache.ghost_batch = batch_for_shader(
        shader, 'TRIS',
        {"pos": pos, "color": ghost_colors},
        indices=indices,
    )


def _update_heatmap_colors(session, tcache, target, props):
    field = _compute_merged_field(session, tcache, target, props)
    if field.shape[0] == 0:
        return
    tcache.colors = gb_core.weights_to_colors(
        field, props.heat_opacity).astype(np.float32)
    _rebuild_batch(tcache)
    covered = int(np.count_nonzero(field > gb_core.EPS_WEIGHT))
    tcache.preview_info = (
        f"覆盖顶点 {covered}/{field.shape[0]}，"
        f"最大权重 {float(field.max()):.3f}")
    # 采样场诊断：球内源点数（帮助确认球是否罩住了源模型的权重区域）
    if (session.source_positions is not None
            and session.source_positions.shape[0] > 0):
        src_in_balls = 0
        for ball in _collect_balls(session):
            if not ball.gb_ball.enabled or not ball.gb_ball.use_source_sampling:
                continue
            s_local = gb_core._to_ball_local(
                session.source_positions, ball.matrix_world)
            if s_local is not None:
                d2 = np.einsum("ij,ij->i", s_local, s_local)
                src_in_balls += int(np.count_nonzero(d2 < 1.0))
        tcache.preview_info += f"，球内源点 {src_in_balls}"


def _draw_heatmap():
    if _state != "active":
        return
    props = getattr(bpy.context.scene, "gb_props", None)
    xray = bool(getattr(props, "xray_preview", True)) if props else True
    try:
        shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        gpu.state.blend_set('ALPHA')
        if xray:
            # 第一遍：幽灵层，关闭深度测试——被遮挡的背面权重以低透明度透视可见
            gpu.state.depth_test_set('NONE')
            for session in _sessions.values():
                for tcache in session.targets.values():
                    if tcache.ghost_batch is not None:
                        try:
                            tcache.ghost_batch.draw(shader)
                        except Exception:
                            pass
        # 第二遍：主层，正常深度测试——可见表面的权重以全强度显示
        gpu.state.depth_test_set('LESS_EQUAL')
        for session in _sessions.values():
            for tcache in session.targets.values():
                if tcache.batch is not None:
                    try:
                        tcache.batch.draw(shader)
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        try:
            gpu.state.blend_set('NONE')
            gpu.state.depth_test_set('NONE')
        except Exception:
            pass


def _register_draw():
    global _draw_handler
    if _draw_handler is None:
        _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_heatmap, (), 'WINDOW', 'POST_VIEW')


def _unregister_draw():
    global _draw_handler
    if _draw_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        except Exception:
            pass
        _draw_handler = None


# ---------------------------------------------------------------------------
# 计时器
# ---------------------------------------------------------------------------

def _ball_matrix_sig(ball):
    return tuple(round(v, 6) for v in np.array(ball.matrix_world).reshape(-1))


def gb_tick():
    """轮询全部会话：目标存活校验 + 矩阵签名比对，必要时逐目标重算热力图。"""
    global _dirty
    if _state != "active":
        return None

    context = bpy.context
    props = getattr(context.scene, "gb_props", None)
    if props is None:
        return 0.2

    interval = max(0.02, float(props.tick_interval))
    global_need = _dirty
    _dirty = False

    for session in list(_sessions.values()):
        # 目标存活检查：被删目标从会话移除；没有目标则取消会话
        for tname in list(session.targets.keys()):
            if bpy.data.objects.get(tname) is None:
                LOG.warning(
                    f"[GB] 目标物体 '{tname}' 已删除，从会话 "
                    f"'{session.vg_name}' 移除")
                dead = session.targets.pop(tname)
                dead.batch = None
                dead.ghost_batch = None
        if not session.targets:
            LOG.warning(f"[GB] 会话 '{session.vg_name}' 没有可用目标，自动取消")
            _cleanup_session(session.id)
            continue

        # 球矩阵签名（变化则全部目标都需要重算）
        need = global_need
        sigs = {}
        for ball in _collect_balls(session):
            sig = _ball_matrix_sig(ball)
            sigs[ball.name] = sig
            if session.matrix_signatures.get(ball.name) != sig:
                need = True
        if set(session.matrix_signatures) - set(sigs):
            need = True
        session.matrix_signatures = sigs

        # 逐目标：目标矩阵签名 + 重算
        for tname, tcache in session.targets.items():
            target = bpy.data.objects.get(tname)
            t_need = need
            t_sig = tuple(round(v, 6) for v in
                          np.array(target.matrix_world).reshape(-1))
            if t_sig != tcache.matrix_sig:
                t_need = True
                try:
                    _build_target_batch(tcache, target)  # 目标移动：重建位置缓冲
                except Exception as e:
                    LOG.warning(f"[GB] 重建热力图失败 '{tname}': {e}")
                    continue
            if t_need:
                try:
                    _update_heatmap_colors(session, tcache, target, props)
                except Exception as e:
                    LOG.warning(f"[GB] 权重场重算失败 '{tname}': {e}")

    props.status_text = (
        f"{len(_sessions)} 个会话编辑中" if _sessions else "未开始")
    _tag_redraw()
    return interval


def _ensure_timer():
    global _timer_registered
    if not _timer_registered:
        bpy.app.timers.register(gb_tick, first_interval=0.1, persistent=False)
        _timer_registered = True


def _remove_timer():
    global _timer_registered
    if _timer_registered:
        try:
            bpy.app.timers.unregister(gb_tick)
        except Exception:
            pass
        _timer_registered = False


def _tag_redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 会话清理（所有退出路径的唯一出口）
# ---------------------------------------------------------------------------

def _cleanup_session(session_id=None):
    """清理指定会话；session_id=None 时清理全部会话。"""
    global _active_session_id
    ids = list(_sessions) if session_id is None else [session_id]

    for sid in ids:
        session = _sessions.get(sid)
        if session is None:
            continue
        for name in list(session.ball_names):
            obj = bpy.data.objects.get(name)
            if obj is not None:
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except Exception:
                    pass
        root = bpy.data.objects.get(session.session_root_name)
        if root is not None:
            try:
                bpy.data.objects.remove(root, do_unlink=True)
            except Exception:
                pass
        for tcache in session.targets.values():
            tcache.batch = None
            tcache.ghost_batch = None
        del _sessions[sid]
        if _active_session_id == sid:
            _active_session_id = None

    _sync_state()
    if not _sessions:
        _remove_timer()
        _unregister_draw()
        props = getattr(bpy.context.scene, "gb_props", None)
        if props is not None:
            props.status_text = "未开始"
    _tag_redraw()


def shutdown():
    """插件注销时调用：清理全部会话、timer 与 draw handler。"""
    _cleanup_session()
    _remove_timer()
    _unregister_draw()


# ---------------------------------------------------------------------------
# 调试标记（与顶点组匹配节点的调试物体样式一致）
# ---------------------------------------------------------------------------

def _create_debug_marker(session, context):
    """确认写入后，在高斯球位置创建调试标记物体。

    样式与顶点组匹配节点一致（复用其材质与自定义属性约定）：
    - Source 模式：绿色方块（无连接），名称 Source_{组名}
    - Target 模式：黄色球，名称 Target_{组名}
    多球会话取启用球位置的平均；挂到原调试父 Empty 下（若仍存在）。
    """
    import bmesh
    from ..blueprint.node_vertex_group_match import SSMTNode_VertexGroupMatch

    balls = [b for b in _collect_balls(session) if b.gb_ball.enabled]
    if not balls:
        balls = _collect_balls(session)
    if balls:
        loc = np.mean(
            [np.array(b.matrix_world.translation) for b in balls], axis=0)
    else:
        loc = np.zeros(3)

    if session.mode == "source":
        marker_name = f"Source_{session.vg_name}"
        mesh = bpy.data.meshes.new(marker_name + "_mesh")
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=0.006)
        bm.to_mesh(mesh)
        bm.free()
        mat_name, color = "VGTP_Debug_Green", (0.0, 1.0, 0.2, 1.0)
    else:
        marker_name = f"Target_{session.vg_name}"
        mesh = bpy.data.meshes.new(marker_name + "_mesh")
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8,
                                  radius=(0.005 / 2))
        bm.to_mesh(mesh)
        bm.free()
        mat_name, color = "VGTP_Debug_Yellow", (1.0, 1.0, 0.0, 1.0)

    mat = SSMTNode_VertexGroupMatch.get_or_create_debug_material(mat_name, color)
    marker = bpy.data.objects.new(marker_name, mesh)
    marker.location = loc
    marker.data.materials.append(mat)
    marker["original_vg_name"] = session.vg_name
    marker["is_connected"] = False
    marker["gb_marker"] = True
    marker.show_name = True

    parent = bpy.data.objects.get(session.debug_parent_name)
    if parent is not None:
        marker.parent = parent
    context.scene.collection.objects.link(marker)
    return marker


# ---------------------------------------------------------------------------
# 写入 + 规格化
# ---------------------------------------------------------------------------

def _write_session_weights(session, context, props):
    """把会话合并场写入全部目标物体的同名顶点组。

    Returns:
        (written_vg_by_target, messages, success_count)
        written_vg_by_target: target_name -> [vg_name]
    """
    written = {}
    messages = []
    success = 0

    for tname, tcache in session.targets.items():
        target = bpy.data.objects.get(tname)
        if target is None:
            messages.append(f"'{tname}': 目标已删除，跳过")
            continue

        field = _compute_merged_field(session, tcache, target, props)
        if field.shape[0] == 0:
            messages.append(f"'{tname}': 没有启用的高斯球，跳过")
            continue

        vg = target.vertex_groups.get(session.vg_name)
        if vg is None:
            vg = target.vertex_groups.new(name=session.vg_name)

        count = 0
        for i in range(field.shape[0]):
            w = float(field[i])
            if w > gb_core.EPS_WEIGHT:
                vg.add([i], min(1.0, w), 'REPLACE')
                count += 1

        if count == 0:
            messages.append(f"'{tname}': 权重场为空（球都在范围外？），跳过")
            continue

        written.setdefault(tname, []).append(session.vg_name)
        messages.append(f"'{tname}': 写入 {count} 顶点")
        success += 1

    return written, messages, success


def _normalize_preserving(context, target, vg_names):
    """规格化目标物体权重，同时保护刚写入的组不被缩放。

    通过临时 lock_weight 锁定写入组（normalize_all 不改动锁定组），
    规格化完成后恢复原来的锁状态。
    """
    vgs = [target.vertex_groups.get(n) for n in vg_names]
    vgs = [v for v in vgs if v is not None]
    if not vgs:
        return
    prev_locks = {v.name: v.lock_weight for v in vgs}
    try:
        for v in vgs:
            v.lock_weight = True
        target.vertex_groups.active_index = vgs[0].index
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
        try:
            bpy.ops.object.vertex_group_normalize_all(lock_active=False)
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')
    finally:
        for v in vgs:
            if v.name in prev_locks:
                v.lock_weight = prev_locks[v.name]


# ---------------------------------------------------------------------------
# 操作符
# ---------------------------------------------------------------------------

class GB_OT_StartFromDebug(bpy.types.Operator):
    """从选中的顶点组匹配调试物体（Source 方块 / Target 黄球）创建高斯球会话。
    Source 模式自动关联同源的全部匹配目标，权重将写入所有目标物体。"""
    bl_idname = "toolkit.gb_start_from_debug"
    bl_label = "从选中调试物体创建高斯球"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        global _next_session_id, _active_session_id, _dirty
        obj = context.active_object
        props = context.scene.gb_props

        vg_name = obj.get("original_vg_name")
        parent = obj.parent
        if not vg_name or parent is None or not parent.get("vgtp_target_name"):
            self.report({'ERROR'},
                        "请选中一个顶点组匹配调试物体（Source_*/Target_*，"
                        "由顶点组匹配节点勾选'创建调试物体'生成）")
            return {'CANCELLED'}

        own_target = bpy.data.objects.get(parent["vgtp_target_name"])
        if own_target is None or own_target.type != 'MESH':
            self.report({'ERROR'}, "调试物体记录的目标物体已不存在")
            return {'CANCELLED'}

        source_key = frozenset(_source_names_for(parent))

        if obj.name.startswith("Source_"):
            mode = "source"
            positions, weights, source_desc = _read_source_weights(parent, vg_name)
            if weights is None:
                self.report({'ERROR'}, source_desc)
                return {'CANCELLED'}
            target_names = _resolve_target_names(parent)
        elif obj.name.startswith("Target_"):
            mode = "target"
            weights = _read_vg_weights(own_target, vg_name)
            if weights is None:
                self.report({'ERROR'},
                            f"目标物体 '{own_target.name}' 上不存在顶点组 '{vg_name}'")
                return {'CANCELLED'}
            positions = _mesh_vertices_world(own_target)
            source_desc = f"目标物体自身 {own_target.name}"
            target_names = [own_target.name]
        else:
            self.report({'ERROR'}, "调试物体名须以 Source_ 或 Target_ 开头")
            return {'CANCELLED'}

        # 目标集合：过滤出仍存在的网格物体
        targets = []
        for name in target_names:
            t = bpy.data.objects.get(name)
            if t is not None and t.type == 'MESH':
                targets.append(t)
        if not targets:
            self.report({'ERROR'}, "同源的目标物体均已不存在")
            return {'CANCELLED'}

        # 会话去重：同源同组（source）或同目标同组（target）只允许一个会话
        for s in _sessions.values():
            if s.vg_name != vg_name:
                continue
            if mode == "source" and s.mode == "source" and s.source_key == source_key:
                self.report({'ERROR'},
                            f"顶点组 '{vg_name}' 已有进行中的会话")
                return {'CANCELLED'}
            if mode == "target" and s.mode == "target" \
                    and own_target.name in s.targets:
                self.report({'ERROR'},
                            f"顶点组 '{vg_name}' 已有进行中的会话")
                return {'CANCELLED'}

        stats = gb_core.compute_vg_stats(positions, weights)
        params = gb_core.initial_ball_params(
            stats, fallback_location=tuple(obj.matrix_world.translation))

        session = _GBSession(_next_session_id)
        _next_session_id += 1

        # 会话根 + 第一个球
        root = bpy.data.objects.new(f"GB_Session_{vg_name}", None)
        root.empty_display_type = 'PLAIN_AXES'
        root["gb_session_root"] = True
        root["gb_session_id"] = session.id
        context.scene.collection.objects.link(root)

        ball = bpy.data.objects.new(f"GB_{vg_name}_001", None)
        ball.empty_display_type = 'SPHERE'
        ball.empty_display_size = 1.0
        ball.parent = root
        ball.matrix_world = _compose_ball_matrix(
            params["location"], params["radius"])
        ball["gb_vg_name"] = vg_name
        ball["gb_session_id"] = session.id
        ball.gb_ball.strength = min(1.0, max(0.0, params["strength"]))
        ball.gb_ball.use_source_sampling = True
        # 衰减系数也从原始权重分布拟合（无有效样本时回退默认值）
        ball.gb_ball.falloff_k = gb_core.estimate_falloff_k(
            positions, weights, stats["centroid"],
            params["radius"], stats["max_weight"])
        context.scene.collection.objects.link(ball)

        # 会话状态（含逐目标缓存）
        session.mode = mode
        session.vg_name = vg_name
        session.source_key = source_key
        session.source_info = source_desc
        session.debug_parent_name = parent.name
        session.session_root_name = root.name
        session.ball_names = [ball.name]
        # 源点云：该顶点组所有权重 > ε 的源顶点（采样场模式的权重来源）
        mask = weights > gb_core.EPS_WEIGHT
        session.source_positions = np.asarray(
            positions, dtype=np.float64)[mask]
        session.source_weights = np.asarray(weights, dtype=np.float64)[mask]
        try:
            for t in targets:
                tcache = _GBTargetCache(t.name)
                tcache.verts_world = _mesh_vertices_world(t)
                _build_target_batch(tcache, t)
                session.targets[t.name] = tcache
        except Exception as e:
            bpy.data.objects.remove(ball, do_unlink=True)
            bpy.data.objects.remove(root, do_unlink=True)
            self.report({'ERROR'}, f"热力图初始化失败: {e}")
            return {'CANCELLED'}

        _sessions[session.id] = session
        _active_session_id = session.id
        _sync_state()
        _dirty = True

        props.status_text = f"{len(_sessions)} 个会话编辑中"

        _register_draw()
        _ensure_timer()
        _tag_redraw()

        if len(targets) > 1:
            names_text = "、".join(t.name for t in targets[:5])
            if len(targets) > 5:
                names_text += " 等"
            target_note = f"，关联 {len(targets)} 个目标（{names_text}）"
        else:
            target_note = f"，目标 {targets[0].name}"
        self.report({'INFO'},
                    f"已为顶点组 '{vg_name}' 创建高斯球{target_note}，"
                    f"视口中移动/缩放球体即可实时预览权重")
        return {'FINISHED'}


def _compose_ball_matrix(location, radius):
    import mathutils
    radius = max(float(radius), gb_core.MIN_RADIUS)
    return (mathutils.Matrix.Translation(location)
            @ mathutils.Matrix.Scale(radius, 4))


class GB_OT_ToggleSessionSelect(bpy.types.Operator):
    """勾选/取消勾选一个会话（确认时按勾选顺序依次写入）"""
    bl_idname = "toolkit.gb_toggle_session_select"
    bl_label = "勾选会话"

    session_id: bpy.props.IntProperty()

    def execute(self, context):
        global _select_counter
        session = _sessions.get(self.session_id)
        if session is None:
            return {'CANCELLED'}
        if session.selected:
            session.selected = False
            session.select_order = 0
        else:
            _select_counter += 1
            session.selected = True
            session.select_order = _select_counter
        return {'FINISHED'}


class GB_OT_SetActiveSession(bpy.types.Operator):
    """把某个会话设为面板中的活动会话"""
    bl_idname = "toolkit.gb_set_active_session"
    bl_label = "设为活动会话"

    session_id: bpy.props.IntProperty()

    def execute(self, context):
        global _active_session_id
        if self.session_id in _sessions:
            _active_session_id = self.session_id
            root = bpy.data.objects.get(
                _sessions[self.session_id].session_root_name)
            if root is not None:
                bpy.ops.object.select_all(action='DESELECT')
                root.select_set(True)
                context.view_layer.objects.active = root
        return {'FINISHED'}


class GB_OT_AddBall(bpy.types.Operator):
    """为活动会话再添加一个高斯球（同组多球，权重按 max 合并）"""
    bl_idname = "toolkit.gb_add_ball"
    bl_label = "添加高斯球"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _state == "active"

    def execute(self, context):
        session = get_active_session()
        if session is None:
            self.report({'ERROR'}, "没有活动会话")
            return {'CANCELLED'}

        balls = _collect_balls(session)
        if balls:
            template = context.active_object if (
                context.active_object in balls) else balls[-1]
            loc = template.matrix_world.translation.copy()
            loc.x += 0.02
            radius = max(float(template.scale.x), gb_core.MIN_RADIUS)
            strength = template.gb_ball.strength
            falloff = template.gb_ball.falloff_k
        else:
            target = bpy.data.objects.get(session.primary_target_name)
            loc = target.matrix_world.translation.copy() if target else None
            radius = gb_core.DEFAULT_RADIUS
            strength = 1.0
            falloff = gb_core.DEFAULT_FALLOFF_K

        idx = len(session.ball_names) + 1
        ball = bpy.data.objects.new(f"GB_{session.vg_name}_{idx:03d}", None)
        ball.empty_display_type = 'SPHERE'
        ball.empty_display_size = 1.0
        root = bpy.data.objects.get(session.session_root_name)
        ball.parent = root
        ball.matrix_world = _compose_ball_matrix(loc, radius)
        ball["gb_vg_name"] = session.vg_name
        ball["gb_session_id"] = session.id
        ball.gb_ball.strength = strength
        ball.gb_ball.falloff_k = falloff
        ball.gb_ball.use_source_sampling = (
            template.gb_ball.use_source_sampling if balls else True)
        context.scene.collection.objects.link(ball)
        session.ball_names.append(ball.name)

        context.view_layer.objects.active = ball
        ball.select_set(True)
        mark_dirty()
        self.report({'INFO'},
                    f"已为 '{session.vg_name}' 添加高斯球"
                    f"（当前 {len(session.ball_names)} 个）")
        return {'FINISHED'}


class GB_OT_SetRadius(bpy.types.Operator):
    """把活动高斯球设为指定的均匀半径（写均匀缩放）"""
    bl_idname = "toolkit.gb_set_radius"
    bl_label = "设置半径"
    bl_options = {'REGISTER', 'UNDO'}

    radius: bpy.props.FloatProperty(
        name="半径", default=0.02, min=1e-4,
        description="高斯球的均匀半径（球面 = 权重≈0 的边界）")

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (_state == "active" and obj is not None
                and obj.get("gb_session_id") in _sessions)

    def execute(self, context):
        r = max(float(self.radius), gb_core.MIN_RADIUS)
        context.active_object.scale = (r, r, r)
        mark_dirty()
        return {'FINISHED'}


class GB_OT_RemoveBall(bpy.types.Operator):
    """删除当前活动的高斯球"""
    bl_idname = "toolkit.gb_remove_ball"
    bl_label = "删除当前球"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (_state == "active" and obj is not None
                and obj.get("gb_session_id") in _sessions)

    def execute(self, context):
        ball = context.active_object
        session = _sessions.get(ball.get("gb_session_id"))
        if session is None or ball.name not in session.ball_names:
            return {'CANCELLED'}
        session.ball_names.remove(ball.name)
        bpy.data.objects.remove(ball, do_unlink=True)
        mark_dirty()
        remaining = len(session.ball_names)
        self.report({'INFO'},
                    f"已删除，剩余 {remaining} 个球" if remaining
                    else "已删除全部球，可重新添加")
        return {'FINISHED'}


class GB_OT_Confirm(bpy.types.Operator):
    """确认写入：按勾选顺序依次写入勾选的会话（未勾选时写入活动会话）。
    每个会话把同名顶点组写入其全部目标物体；写完后逐目标统一规格化
    （写入的组被锁定保护），并为每个会话创建调试标记物体。"""
    bl_idname = "toolkit.gb_confirm"
    bl_label = "确认并写入权重"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _state == "active"

    def execute(self, context):
        props = context.scene.gb_props
        sessions = _selected_sessions()
        if not sessions:
            active = get_active_session()
            sessions = [active] if active is not None else []
        if not sessions:
            self.report({'ERROR'}, "没有可确认的会话")
            return {'CANCELLED'}

        messages = []
        written_vg_by_target = {}   # target_name -> [vg_name, ...]
        confirmed_ids = []

        for session in sessions:
            written, msgs, success = _write_session_weights(
                session, context, props)
            messages.extend(msgs)
            if success == 0:
                self.report({'WARNING'},
                            f"'{session.vg_name}': 所有目标均未写入")
                continue
            # 创建调试标记（基于高斯球当前位置，样式与匹配节点一致）
            try:
                _create_debug_marker(session, context)
            except Exception as e:
                LOG.warning(f"[GB] 创建调试标记失败: {e}")
            for tname, vgs in written.items():
                written_vg_by_target.setdefault(tname, []).extend(vgs)
            confirmed_ids.append(session.id)

        if not confirmed_ids:
            self.report({'ERROR'}, "没有会话写入成功")
            return {'CANCELLED'}

        # 逐目标统一规格化：写入的组临时锁定，其余组围绕它们缩放
        normalized = False
        if props.normalize_on_confirm:
            for target_name, vg_names in written_vg_by_target.items():
                target = bpy.data.objects.get(target_name)
                if target is None:
                    continue
                try:
                    _normalize_preserving(context, target, vg_names)
                    normalized = True
                except Exception as e:
                    LOG.warning(f"[GB] 规格化失败: {e}")
                    self.report({'WARNING'},
                                f"'{target_name}' 权重已写入但规格化失败: {e}")

        for sid in confirmed_ids:
            _cleanup_session(sid)

        msg = (f"已按顺序写入 {len(confirmed_ids)} 个会话"
               f"（{len(written_vg_by_target)} 个目标）：" + "；".join(messages))
        if normalized:
            msg += "（已规格化，写入组已锁定保护）"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class GB_OT_Cancel(bpy.types.Operator):
    """取消会话：删除勾选的会话（未勾选时取消活动会话），不写入任何权重"""
    bl_idname = "toolkit.gb_cancel"
    bl_label = "取消"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _state == "active"

    def execute(self, context):
        sessions = _selected_sessions()
        if not sessions:
            active = get_active_session()
            sessions = [active] if active is not None else []
        for session in sessions:
            _cleanup_session(session.id)
        self.report({'INFO'}, f"已取消 {len(sessions)} 个会话，未写入任何权重")
        return {'FINISHED'}


class GB_OT_CleanupOrphans(bpy.types.Operator):
    """清理场景中残留的高斯球物体（防崩溃残留；调试标记会保留）"""
    bl_idname = "toolkit.gb_cleanup_orphans"
    bl_label = "清理残留球体"

    def execute(self, context):
        removed = 0
        for obj in list(bpy.data.objects):
            if obj.get("gb_vg_name") or obj.get("gb_session_root"):
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                    removed += 1
                except Exception:
                    pass
        if _sessions:
            _cleanup_session()
        self.report({'INFO'}, f"已清理 {removed} 个残留物体")
        return {'FINISHED'}


gb_operators_list = (
    GB_OT_StartFromDebug,
    GB_OT_ToggleSessionSelect,
    GB_OT_SetActiveSession,
    GB_OT_AddBall,
    GB_OT_SetRadius,
    GB_OT_RemoveBall,
    GB_OT_Confirm,
    GB_OT_Cancel,
    GB_OT_CleanupOrphans,
)
