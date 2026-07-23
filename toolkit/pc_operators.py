# -*- coding: utf-8 -*-
"""点云姿态匹配：操作符与计时器状态机。

状态机：idle -> running <-> paused -> stopped（保留历史可回退）。
会话单例存模块级变量（numpy 数据无法进 Scene 属性）。
计时器 tick：
  - running：执行 steps_per_tick 步 -> 更新显示属性 -> 刷新视图；
  - 任意状态：处理待定位 seek（进度条去抖）。
防护：对象删除/改名、matrix_world 变动 -> 自动暂停；tick 内 try/except；
addon unregister 时 shutdown() 注销计时器。
"""
import math
import time
import json
from dataclasses import replace
from pathlib import Path
from typing import Optional

import bpy
import numpy as np

from . import pc_backend
from . import pc_bridge
from . import pc_engine
from . import pc_virtualrig
from . import pc_worker

# ---------------------------------------------------------------------------
# 模块级会话状态
# ---------------------------------------------------------------------------

_session: Optional[pc_engine.PCFitSession] = None
_cache: Optional[pc_bridge.PCCache] = None
_backend = None                            # pc_backend 计算后端（NN 加速）
_lbs: Optional[pc_bridge.PCLBSData] = None  # 快速蒙皮缓存（构建期自验证）
_vrig = None                               # 虚拟骨架（headless 高速模式）
_state: str = 'idle'                      # idle | running | paused | stopped
_timer_registered: bool = False
_pending_seek_step: Optional[int] = None  # 进度条请求的目标步（去抖）
_cursor_follow_guard: bool = False        # 运行中回写游标时防止 update 回调递归
_last_result_text: str = ""
_worker = None                               # 后台近似迭代 worker
_latest_result = None                        # 主线程最后消费到的 step 结果快照
_last_viewport_sync_at: float = 0.0
_lab_sync_file = Path(__file__).resolve().parents[1] / '.dbg' / 'pc_iteration_lab_state.js'


def request_seek_from_cursor(cursor: float) -> None:
    """pc_properties 的 history_cursor update 回调入口（只记录，不执行）。"""
    global _pending_seek_step
    if _cursor_follow_guard:
        return
    if _session is None:
        return
    total = _session.history_total()
    if total <= 0:
        return
    target = int(round(max(0.0, min(1.0, cursor)) * total))
    target = _session.nearest_recoverable_step(target)
    _pending_seek_step = target
    try:
        props = _props(bpy.context)
        props.history_view_step = target
        props.status_text = f"正在定位第 {target} 步"
    except Exception:
        pass
    _ensure_timer()


def _props(context) -> object:
    return context.scene.pc_props


def _json_float(value, default=0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    return out if math.isfinite(out) else float(default)


def _lab_sync_payload(context) -> dict:
    props = _props(context)
    session = _session
    result = _latest_result if _latest_result is not None else getattr(
        session, 'last_step_result', None)
    debug = getattr(session, 'last_debug_payload', {}) if session is not None else {}
    return {
        "generated_at": time.time(),
        "state": _state,
        "step": int(props.cur_step),
        "history_total": int(props.history_total),
        "current_metric": {
            "f1": _json_float(props.cur_f1),
            "best_f1": _json_float(props.best_f1),
            "best_step": int(props.best_step),
            "chamfer": _json_float(props.residual_mean),
            "debug_overlap_text": props.debug_overlap_text,
        },
        "phase": {
            "name": props.sched_phase,
            "status": props.sched_phase_status,
            "weights": {
                "rotation": _json_float(props.sched_w_rot),
                "scale": _json_float(props.sched_w_scale),
                "location": _json_float(props.sched_w_loc),
            },
        },
        "bone": {
            "curriculum_status": props.bone_curriculum_status,
            "curriculum_detail": props.bone_curriculum_detail,
        },
        "last_move": {
            "summary": props.last_move_summary,
            "detail": props.last_move_detail,
            "bone_name": getattr(result, 'bone_name', ''),
            "tf_type": getattr(result, 'tf_type', ''),
            "accepted": bool(getattr(result, 'accepted', False)),
            "axis": getattr(result, 'axis', None),
            "delta_components": [
                _json_float(v) for v in getattr(
                    result, 'delta_components', (0.0, 0.0, 0.0))
            ],
            "f1_delta": _json_float(getattr(result, 'f1_delta', 0.0)),
            "chamfer_delta": _json_float(
                getattr(result, 'chamfer_delta', 0.0)),
            "reward": _json_float(getattr(result, 'reward', 0.0)),
            "score_delta": _json_float(getattr(result, 'score_delta', 0.0)),
            "linked_count": int(getattr(result, 'linked_count', 0)),
        },
        "debug": debug,
    }


def _write_lab_sync(context) -> None:
    try:
        _lab_sync_file.parent.mkdir(parents=True, exist_ok=True)
        payload = _lab_sync_payload(context)
        blob = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        _lab_sync_file.write_text(
            f"window.__PC_ITERATION_LAB_STATE__ = {blob};\n",
            encoding='utf-8')
    except Exception:
        pass


def _selected_bone_names(context, arm) -> set[str]:
    """Return the active armature's selection using Blender 5 context APIs."""
    if getattr(context, 'object', None) is not arm:
        return set()
    if arm.mode == 'POSE':
        bones = context.selected_pose_bones
    elif arm.mode == 'EDIT':
        bones = context.selected_editable_bones
    else:
        return set()
    return {bone.name for bone in bones or ()}


def _make_config(props) -> pc_engine.PCFitConfig:
    return pc_engine.PCFitConfig(
        sample_count=props.sample_count,
        threshold=props.threshold if props.threshold_mode == 'MANUAL' else 0.0,
        max_angle_deg=props.max_angle_deg,
        max_scale_delta=props.max_scale_delta,
        max_translation_ratio=props.max_translation,
        learning_rate=props.learning_rate,
        prior_rotation=props.prior_rotation,
        prior_scale=props.prior_scale,
        prior_location=props.prior_location,
        snapshot_interval=props.snapshot_interval,
        max_history_steps=props.max_history,
        minibatch_size=props.minibatch_size,
        full_eval_interval=props.full_eval_interval,
        phase_eval_interval=props.phase_eval_interval,
        phase_plateau_delta=props.phase_plateau_delta,
        phase_plateau_checks=props.phase_plateau_checks,
    )


def _apply_enabled_flags() -> None:
    """把面板骨骼列表的勾选状态同步到会话骨骼规格。"""
    global _session
    if _session is None:
        return
    ctx = bpy.context
    try:
        props = _props(ctx)
        flags = {item.name: item.enabled for item in props.bone_list}
        headless = _vrig is not None and _props(ctx).use_headless
        for name, spec in _session.bones.items():
            if name in flags:
                spec.enabled = bool(flags[name])
            if headless and not _vrig.has_bone(name):
                spec.enabled = False
    except Exception:
        pass


def _movement_display(result) -> tuple[str, str]:
    if result is None or not getattr(result, 'bone_name', ''):
        return "最近一步: 等待第一次迭代", ""
    tf_type = getattr(result, 'tf_type', '')
    tf_name = {
        pc_engine.TF_ROTATION: "旋转",
        pc_engine.TF_SCALE: "缩放",
        pc_engine.TF_LOCATION: "位移",
    }.get(tf_type, tf_type or "变换")
    accepted = bool(getattr(result, 'accepted', False))
    verdict = "已接受" if accepted else "已回退"
    summary = (f"第 {int(result.step)} 步 | {verdict} | "
               f"{result.bone_name} | {tf_name}")
    values = tuple(float(v) for v in getattr(
        result, 'delta_components', (0.0, 0.0, 0.0)))
    if tf_type == pc_engine.TF_ROTATION:
        parts = [f"{axis} {value:+.4f}°"
                 for axis, value in zip("XYZ", values)]
    elif tf_type == pc_engine.TF_SCALE:
        parts = [f"{axis} {value:+.4f}%"
                 for axis, value in zip("XYZ", values)]
    else:
        parts = [f"{axis} {value:+.6f}"
                 for axis, value in zip("XYZ", values)]
    linked = int(getattr(result, 'linked_count', 0))
    if linked:
        parts.append(f"镜像联动 {linked} 根")
    f1_delta = float(getattr(result, 'f1_delta', 0.0))
    chamfer_delta = float(getattr(result, 'chamfer_delta', 0.0))
    reward = float(getattr(result, 'reward', 0.0))
    score_delta = float(getattr(result, 'score_delta', 0.0))
    parts.append(f"F1 {f1_delta * 100.0:+.6f}%")
    parts.append(f"Chamfer收益 {chamfer_delta:+.6g}")
    parts.append(f"总奖励 {reward:+.6g}")
    if not accepted:
        parts.append("实际变化 0")
    return summary, " | ".join(parts)


def _update_display_props(context) -> None:
    """把会话状态写入显示属性（面板只读展示）。"""
    global _cursor_follow_guard
    if _session is None:
        return
    props = _props(context)
    result = _latest_result
    movement_result = result if result is not None else \
        getattr(_session, 'last_step_result', None)
    if result is not None:
        props.cur_step = int(result.step)
        props.cur_f1 = float(result.metric_f1)
        props.best_f1 = float(max(0.0, result.best_f1))
        props.best_step = int(result.best_step)
        props.history_total = max(int(result.step), _session.history_total())
    else:
        props.cur_step = _session.step_count
        props.cur_f1 = float(_session.current_metric.f1)
        props.best_f1 = float(max(0.0, _session.best_f1))
        props.best_step = _session.best_step
        props.history_total = _session.history_total()
    props.last_move_summary, props.last_move_detail = \
        _movement_display(movement_result)
    w = _session.schedule.current_weights_display()
    props.sched_w_rot = w.get(pc_engine.TF_ROTATION, 0.0)
    props.sched_w_scale = w.get(pc_engine.TF_SCALE, 0.0)
    props.sched_w_loc = w.get(pc_engine.TF_LOCATION, 0.0)
    props.sched_phase = _session.schedule.phase_name
    if _session.schedule.stage >= 4:
        props.sched_phase_status = "镜像联动与全部变换已开放"
    elif _session.schedule.phase_best_f1 < 0.0:
        props.sched_phase_status = "等待首次固定验证"
    else:
        props.sched_phase_status = (
            f"阶段最佳F1 {_session.schedule.phase_best_f1:.6f} | "
            f"无明显收益 {_session.schedule.phase_plateau_count}/"
            f"{_session.schedule.plateau_checks}")
    if _session._bone_curriculum_order:
        index = min(_session._bone_curriculum_index,
                    len(_session._bone_curriculum_order) - 1)
        bone_name = _session._bone_curriculum_order[index]
        coverage = len(_session.bones[bone_name].influence_indices)
        props.bone_curriculum_status = (
            f"骨骼 {index + 1}/{len(_session._bone_curriculum_order)}: {bone_name}")
        props.bone_curriculum_detail = (
            f"覆盖 {coverage} 点 | 无收益 "
            f"{_session._bone_curriculum_no_gain}/"
            f"{_session.bone_curriculum_patience}")
    else:
        props.bone_curriculum_status = "等待迭代"
        props.bone_curriculum_detail = ""
    props.residual_mean = float(_session.current_metric.chamfer) \
        if math.isfinite(_session.current_metric.chamfer) else 0.0
    # 运行中游标跟随当前步（置防重入标志避免触发 seek 请求）
    if _state == 'running':
        _cursor_follow_guard = True
        try:
            total = max(1, _session.history_total())
            props.history_cursor = _session.step_count / total
            props.history_view_step = _session.step_count
        finally:
            _cursor_follow_guard = False
    _write_lab_sync(context)


def _ensure_timer() -> None:
    global _timer_registered
    if not _timer_registered:
        bpy.app.timers.register(_timer_tick, first_interval=0.05, persistent=False)
        _timer_registered = True


def _stop_timer() -> None:
    global _timer_registered
    if _timer_registered:
        try:
            bpy.app.timers.unregister(_timer_tick)
        except Exception:
            pass
        _timer_registered = False


def _seek_history(context, target: int) -> None:
    """暂停计算并查看历史姿态；保留完整历史长度供前后拖动。"""
    global _state, _latest_result
    target = int(max(0, min(target, _session.history_total())))
    target = _session.nearest_recoverable_step(target)
    _state = 'paused'
    try:
        if _worker is not None:
            _worker.pause()
            item = _worker.seek(target)
        else:
            _session.seek(target)
            item = pc_worker.PCWorkerResult(
                step=target,
                metric_f1=float(_session.current_metric.f1),
                metric_chamfer=float(_session.current_metric.chamfer),
                best_step=_session.best_step,
                best_f1=float(max(0.0, _session.best_f1)),
                basis_map=_session._snapshot_state())
    except ValueError as exc:
        props = _props(context)
        props.status_text = str(exc)
        return
    _latest_result = item
    _session.basis_map = {k: v.copy() for k, v in item.basis_map.items()}
    _refresh_after_state_change(context)
    _update_display_props(context)
    props = _props(context)
    props.history_view_step = target
    props.status_text = f"已暂停并查看第 {target} 步"


def _timer_tick():
    """计时器主循环；返回 None 表示停止重注册。"""
    global _state, _pending_seek_step, _timer_registered, _last_viewport_sync_at
    _timer_registered = False  # 每次触发后需要重新注册
    t_tick0 = time.perf_counter()
    try:
        context = bpy.context
        if _session is None or _cache is None:
            return None
        props = _props(context)

        # 缓存失效防护（对象删除/改名/物体变换）
        invalid_reason = pc_bridge.validate_cache(_cache)
        if invalid_reason is not None:
            _state = 'paused'
            if _worker is not None:
                _worker.pause()
            props.status_text = f"场景已变化：{invalid_reason}"
        elif _pending_seek_step is not None:
            target = _pending_seek_step
            _pending_seek_step = None
            _seek_history(context, target)
        elif _state == 'running':
            _apply_enabled_flags()
            interval = max(0.005, float(props.tick_interval))
            headless = _vrig is not None and props.use_headless
            if headless and _worker is not None:
                t_consume0 = time.perf_counter()
                item = _worker.latest_result()
                if item is not None:
                    globals()['_latest_result'] = item
                    if item.basis_map:
                        _session.basis_map = {k: v.copy()
                                              for k, v in item.basis_map.items()}
                    if item.changed_bases:
                        for name, basis in item.changed_bases.items():
                            _session.basis_map[name] = basis.copy()
                    now = time.perf_counter()
                    if item.changed_bases and (
                            now - _last_viewport_sync_at >= max(0.05, interval)):
                        t_sync0 = time.perf_counter()
                        _sync_viewport(
                            context, changed_bases=item.changed_bases,
                            evaluate=False)
                        _last_viewport_sync_at = time.perf_counter()
                        print(f"[TheHerta4][PC][Perf] viewport_sync = {(_last_viewport_sync_at - t_sync0) * 1000.0:.2f} ms")
                    if _lbs is not None and not _lbs.valid:
                        interval_steps = max(1, int(props.approximate_realign_interval))
                        if item.step % interval_steps == 0:
                            t_realign0 = time.perf_counter()
                            exact_b = pc_bridge.read_b_samples(_cache, context)
                            nn_b = _session._nn_factory(exact_b)
                            exact_metric = pc_engine.overlap_metric(
                                _session.a_points, exact_b, _session.nn_a, nn_b, _session.tau)
                            _worker.publish_exact_metric(
                                exact_metric.f1, exact_metric.chamfer,
                                exact_metric.score)
                            print(f"[TheHerta4][PC][Perf] exact_realign = {(time.perf_counter() - t_realign0) * 1000.0:.2f} ms")
                            item.metric_f1 = float(exact_metric.f1)
                            item.metric_chamfer = float(exact_metric.chamfer)
                            item.latest_metric_exact = True
                            if exact_metric.f1 > float(item.best_f1):
                                item.best_f1 = float(exact_metric.f1)
                                item.best_step = int(item.step)
                if _worker.error:
                    _state = 'paused'
                    props.status_text = f"后台线程异常: {_worker.error}"
            else:
                # 非 headless 仍走旧同步路径
                n_steps = max(1, int(props.steps_per_tick))
                for _ in range(n_steps):
                    result = _session.step()
                    if result is None:
                        _state = 'paused'
                        props.status_text = "无可迭代目标：检查骨骼勾选"
                        break
                _refresh_after_state_change(context)
            _update_display_props(context)
            print(f"[TheHerta4][PC][Perf] tick_total = {(time.perf_counter() - t_tick0) * 1000.0:.2f} ms")
            if _state == 'running':
                props.status_text = "迭代中"
                bpy.app.timers.register(_timer_tick, first_interval=interval, persistent=False)
                _timer_registered = True
                return None

        # 非运行态保持低频 tick 以响应进度条拖动
        if _session is not None:
            bpy.app.timers.register(_timer_tick, first_interval=0.1, persistent=False)
            _timer_registered = True
    except Exception as exc:  # 异常必暂停，绝不留孤儿计时器
        _state = 'paused'
        try:
            _props(bpy.context).status_text = f"异常暂停: {exc}"
        except Exception:
            pass
        print(f"[TheHerta4] 点云匹配计时器异常: {exc}")
    return None


def _sync_viewport(context, changed_bases=None, evaluate: bool = True) -> None:
    """headless 批量迭代后：把当前 basis_map 一次性写入姿态骨骼并刷新视口。"""
    if _session is None or _cache is None:
        return
    arm = bpy.data.objects.get(_cache.arm_obj_name)
    if arm is None:
        return
    source = changed_bases if changed_bases else _session.basis_map
    for name, basis in source.items():
        pb = arm.pose.bones.get(name)
        if pb is not None:
            pc_bridge.set_basis(pb, basis)
    pc_bridge.update_view(context, evaluate=evaluate)


def _refresh_after_state_change(context) -> None:
    """seek/jump/truncate/clear 后统一刷新：headless 用视口同步，否则全量刷新。"""
    if _vrig is not None and _props(context).use_headless:
        _sync_viewport(context, evaluate=True)
    else:
        pc_bridge.update_view(context)


def _stop_worker(context=None, reason: str = "") -> bool:
    global _worker
    if _worker is None:
        return True
    stopped = _worker.stop()
    if stopped:
        _worker = None
        return True
    message = "后台线程未能在 1 秒内停止"
    if reason:
        message = f"{message}（{reason}）"
    try:
        if context is not None:
            _props(context).status_text = message
    except Exception:
        pass
    print(f"[TheHerta4] {message}")
    return False


def _close_backend() -> None:
    global _backend
    backend = _backend
    _backend = None
    if backend is None:
        return
    close = getattr(backend, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:
            print(f"[TheHerta4] 关闭 backend 失败: {exc}")


def shutdown() -> None:
    """addon unregister 时调用：注销计时器、清空会话。"""
    global _session, _cache, _state, _pending_seek_step, _backend, _lbs, _vrig, _worker, _latest_result
    _stop_timer()
    if not _stop_worker(reason="shutdown"):
        _state = 'paused'
        return
    _session = None
    _cache = None
    _close_backend()
    _lbs = None
    _vrig = None
    _worker = None
    _latest_result = None
    _state = 'idle'
    _pending_seek_step = None


# ---------------------------------------------------------------------------
# 操作符
# ---------------------------------------------------------------------------

class PC_OT_RefreshBoneList(bpy.types.Operator):
    """从骨架重建骨骼列表（保留已有勾选状态）"""
    bl_idname = "toolkit.pc_refresh_bone_list"
    bl_label = "从骨架刷新"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = _props(context)
        arm = props.b_armature
        if arm is None or arm.type != 'ARMATURE':
            self.report({'WARNING'}, "请先选择骨架B")
            return {'CANCELLED'}

        old_flags = {item.name: item.enabled for item in props.bone_list}
        props.bone_list.clear()
        mesh_objs = pc_bridge.find_skinned_meshes(arm)
        vg_names = set()
        for obj in mesh_objs:
            for vg in obj.vertex_groups:
                vg_names.add(vg.name)

        for pb in arm.pose.bones:
            item = props.bone_list.add()
            item.name = pb.name
            has_constraints = len(pb.constraints) > 0
            has_weights = pb.name in vg_names and pb.bone.use_deform
            item.kind = 'deform' if (has_weights and not has_constraints) else 'controller'
            item.has_constraints = has_constraints
            item.lock_info = pc_bridge.lock_info_text(pb)
            item.enabled = old_flags.get(pb.name, True)

        props.bone_list_index = 0
        self.report({'INFO'}, f"已刷新 {len(arm.pose.bones)} 根骨骼")
        return {'FINISHED'}


class PC_OT_SelectPoseBones(bpy.types.Operator):
    """按姿态模式当前选中的骨骼勾选列表项"""
    bl_idname = "toolkit.pc_select_pose_bones"
    bl_label = "按姿态选中"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = _props(context)
        arm = props.b_armature
        if arm is None:
            self.report({'WARNING'}, "请先选择骨架B")
            return {'CANCELLED'}
        selected = _selected_bone_names(context, arm)
        if not selected:
            self.report({'WARNING'}, "姿态/编辑模式下没有选中的骨骼")
            return {'CANCELLED'}
        count = 0
        for item in props.bone_list:
            item.enabled = item.name in selected
            if item.enabled:
                count += 1
        self.report({'INFO'}, f"已勾选 {count} 根选中骨骼")
        return {'FINISHED'}


class PC_OT_EnableDeformOnly(bpy.types.Operator):
    """仅勾选变形骨（取消控制器骨）"""
    bl_idname = "toolkit.pc_enable_deform_only"
    bl_label = "仅变形骨"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = _props(context)
        for item in props.bone_list:
            item.enabled = (item.kind == 'deform')
        return {'FINISHED'}


class PC_OT_BuildCache(bpy.types.Operator):
    """初始化/重建点云缓存（作废历史与会话）"""
    bl_idname = "toolkit.pc_build_cache"
    bl_label = "初始化/重建缓存"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _session, _cache, _state, _backend, _lbs, _vrig, _worker, _latest_result, _last_viewport_sync_at
        props = _props(context)
        if props.a_object is None or props.b_armature is None:
            self.report({'WARNING'}, "请先指定模型A与骨架B")
            return {'CANCELLED'}

        if not _stop_worker(context, reason="rebuild cache"):
            self.report({'ERROR'}, "后台线程仍在运行，请稍后重试")
            return {'CANCELLED'}
        _close_backend()

        _state = 'idle'
        t_build0 = time.perf_counter()
        try:
            cfg = _make_config(props)
            _cache = pc_bridge.build_cache(props.a_object, props.b_armature, cfg, context)
            print(f"[TheHerta4][PC][Perf] build_cache = {(time.perf_counter() - t_build0) * 1000.0:.2f} ms")
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        # 计算后端 + 快速蒙皮缓存（LBS 构建期自验证，不通过自动回退全量评估）
        requested_backend = 'TORCH' if props.backend_mode == 'AUTO' else props.backend_mode
        _backend, backend_info = pc_backend.select_backend(requested_backend)
        if hasattr(_backend, "warmup"):
            try:
                _backend.warmup()
            except Exception:
                pass
        if (not getattr(_backend, 'is_gpu', False)) and cfg.minibatch_size > 0:
            cfg.minibatch_size = min(int(cfg.minibatch_size), 256)
        _lbs = None
        if props.use_fast_lbs:
            try:
                perturb = [item.name for item in props.bone_list
                           if item.enabled and item.kind == 'deform']
                t_lbs0 = time.perf_counter()
                _lbs = pc_bridge.build_lbs(_cache, context, perturb_bones=perturb)
                print(f"[TheHerta4][PC][Perf] build_lbs = {(time.perf_counter() - t_lbs0) * 1000.0:.2f} ms")
            except Exception as exc:
                print(f"[TheHerta4] LBS 缓存构建失败，回退全量评估: {exc}")
                _lbs = None

        # 虚拟骨架（headless 高速模式）：
        # 1) LBS 严格验证通过 -> 正常启用
        # 2) 若仅因 Preserve Volume/约束扰动验证失败，但用户允许近似回退 ->
        #    仍启用近似高速模式，并在每个 tick 后做一次真实校正
        _vrig = None
        vrig_available = False
        vrig_approximate = False
        if props.use_headless and _lbs is not None and (_lbs.valid or (props.use_approximate_fallback and len(_lbs.bone_names) > 0)):
            try:
                arm = bpy.data.objects.get(_cache.arm_obj_name)
                t_vrig0 = time.perf_counter()
                _vrig = pc_bridge.build_virtual_rig_from_cache(_cache, _lbs, arm)
                print(f"[TheHerta4][PC][Perf] build_virtual_rig = {(time.perf_counter() - t_vrig0) * 1000.0:.2f} ms")
                if _lbs.valid:
                    test_bones = [item.name for item in props.bone_list
                                  if item.enabled and item.kind == 'deform'
                                  and _vrig.has_bone(item.name)][:3]
                    ok, note = pc_bridge.validate_virtual_rig(_vrig, arm, context, test_bones)
                    if not ok:
                        print(f"[TheHerta4] 虚拟骨架验证失败，回退实时评估: {note}")
                        _vrig = None
                    else:
                        vrig_available = True
                else:
                    vrig_available = True
                    vrig_approximate = True
                    print(f"[TheHerta4][PC] {_lbs.note}")
            except Exception as exc:
                print(f"[TheHerta4] 虚拟骨架构建失败，回退实时评估: {exc}")
                _vrig = None

        session = self._create_session(context, _cache, cfg)
        if session is None:
            self.report({'WARNING'}, "没有可用的骨骼（请先刷新骨骼列表并勾选）")
            return {'CANCELLED'}
        _session = session
        _latest_result = None
        _worker = None
        _last_viewport_sync_at = 0.0
        runtime_mode_note = getattr(session, 'runtime_mode_note', '')
        runtime_nn_note = getattr(session, 'runtime_nn_note', '')
        enabled_count = sum(1 for spec in _session.bones.values() if spec.enabled)
        use_worker_headless = (
            _vrig is not None
            and props.use_headless
            and not (enabled_count <= 2 and _lbs is not None and not _lbs.valid)
        )
        if use_worker_headless:
            chunk = 1 if enabled_count <= 2 else max(8, int(props.steps_per_tick))
            _worker = pc_worker.PCWorkerController(
                _session, steps_per_chunk=chunk)
        props.cur_step = 0
        props.history_total = 0
        props.history_cursor = 0.0
        props.cur_f1 = float(session.current_metric.f1)
        props.best_f1 = 0.0
        props.best_step = 0
        props.status_text = (
            f"缓存就绪: A {len(_cache.a_points)}点 / B {len(_cache.b_rest_points)}点"
            f" / τ={_cache.tau:.4f} / 网格{len(_cache.b_parts)}个")
        if not props.use_fast_lbs:
            lbs_note = "已关闭"
        elif _lbs is not None and _lbs.valid:
            lbs_note = "启用"
        elif _vrig is not None and vrig_approximate:
            lbs_note = "近似启用（周期真实校正）"
        else:
            detail = f"（{_lbs.note}）" if (_lbs is not None and _lbs.note) else ""
            lbs_note = f"回退全量评估{detail}"
            if _lbs is not None and _lbs.note:
                print(f"[TheHerta4] LBS 回退原因: {_lbs.note}")
        if _vrig is not None and vrig_approximate:
            mode_note = "近似高速+周期真实校正"
        elif _vrig is not None:
            mode_note = "高速(虚拟骨架)"
        elif _lbs is not None and _lbs.valid:
            mode_note = "LBS加速"
        elif _lbs is not None and (not _lbs.valid) and props.use_approximate_fallback:
            mode_note = "全量评估（可切换近似高速）"
        else:
            mode_note = "全量评估"
        if runtime_mode_note:
            mode_note = runtime_mode_note
        perf_parts = [
            f"后端: {backend_info}",
            f"快速蒙皮: {lbs_note}",
            f"模式: {mode_note}",
        ]
        if runtime_nn_note:
            perf_parts.append(f"ExactNN: {runtime_nn_note}")
        props.perf_info = " | ".join(perf_parts)

        # 完整诊断日志直接打到 Blender 控制台，避免面板截断
        print("[TheHerta4][PC] ===== 点云匹配缓存诊断 =====")
        print(f"[TheHerta4][PC] A: {getattr(props.a_object, 'name', None)} | B: {getattr(props.b_armature, 'name', None)}")
        print(f"[TheHerta4][PC] SampleCount=requested:{props.sample_count} / actual:A={len(_cache.a_points)},B={len(_cache.b_rest_points)} | Tau={_cache.tau:.6f} | MeshParts={len(_cache.b_parts)}")
        mirror_pair_count = sum(
            1 for spec in _cache.bones if spec.mirror_name) // 2
        print(f"[TheHerta4][PC] SpatialMirrorPairs={mirror_pair_count}")
        print(f"[TheHerta4][PC] Backend = {backend_info}")
        print(f"[TheHerta4][PC] FastLBS = {lbs_note}")
        if _lbs is not None and _lbs.valid:
            print("[TheHerta4][PC] LBS = exact")
        elif _lbs is not None and _vrig is not None and vrig_approximate:
            print(f"[TheHerta4][PC] LBS = approximate | {_lbs.note}")
        elif _lbs is not None:
            print(f"[TheHerta4][PC] LBS = unavailable | {_lbs.note}")
        else:
            print("[TheHerta4][PC] LBS = None")
        if _vrig is not None:
            print("[TheHerta4][PC] VirtualRig = enabled")
        else:
            print("[TheHerta4][PC] VirtualRig = disabled / fallback realtime")
        print(f"[TheHerta4][PC] Mode = {mode_note}")
        if runtime_nn_note:
            print(f"[TheHerta4][PC] ExactNN = {runtime_nn_note}")
        runtime_screen_note = getattr(_session, "runtime_screen_note", "")
        if runtime_screen_note:
            print(f"[TheHerta4][PC] ScreenRank = {runtime_screen_note}")
        print("[TheHerta4][PC] ==============================")

        self.report({'INFO'}, "点云缓存已构建")
        return {'FINISHED'}

    def _create_session(self, context, cache, cfg) -> Optional[pc_engine.PCFitSession]:
        props = _props(context)
        enabled_flags = {item.name: item.enabled for item in props.bone_list}
        bones = []
        enabled_count = sum(1 for enabled in enabled_flags.values() if enabled)
        candidate_headless = _vrig is not None and props.use_headless
        lbs_index = ({name: i for i, name in enumerate(_lbs.bone_names)}
                     if _lbs is not None else {})
        for cached_spec in cache.bones:
            spec = replace(cached_spec)
            bone_index = lbs_index.get(spec.name)
            if bone_index is not None:
                rows = _lbs.bone_to_rows.get(bone_index)
                if rows is not None and len(rows) > 0:
                    # A parent bone moves all descendant-influenced rows, not
                    # only vertices directly weighted to its own group.
                    spec.influence_indices = np.asarray(rows, dtype=np.int64).copy()
                    spec.influence_weights = np.ones(len(rows), dtype=np.float64)
            spec.enabled = enabled_flags.get(spec.name, True)
            if candidate_headless and not _vrig.has_bone(spec.name):
                spec.enabled = False
            bones.append(spec)
        if not any(b.enabled for b in bones):
            return None
        enabled_count = sum(1 for b in bones if b.enabled)
        force_realtime_exact = (
            candidate_headless
            and enabled_count <= 2
            and _lbs is not None
            and not _lbs.valid
        )
        subset_lbs_exact = False
        subset_lbs_note = ""
        subset_vrig_exact = False
        subset_vrig_note = ""
        headless = candidate_headless and not force_realtime_exact
        if enabled_count <= 2 and int(cfg.minibatch_size) > 0:
            cfg.minibatch_size = 0
            print(f"[TheHerta4][PC] enabled bones = {enabled_count}; force exact metric per step")
        arm = bpy.data.objects.get(cache.arm_obj_name)
        if arm is None:
            return None
        if force_realtime_exact:
            print("[TheHerta4][PC] enabled bones <= 2 and approximate LBS invalid; use realtime exact Blender evaluation for convergence")
            try:
                enabled_names = [b.name for b in bones if b.enabled]
                subset_lbs_exact, subset_lbs_note = pc_bridge.validate_lbs_subset(
                    cache, _lbs, arm, context, enabled_names, cache.tau)
            except Exception as exc:
                subset_lbs_exact = False
                subset_lbs_note = f"选中骨骼子集 LBS 验证失败: {exc}"
            if subset_lbs_exact:
                print(f"[TheHerta4][PC] {subset_lbs_note}; use sampled LBS for exact-session reads")
                if False and _vrig is not None:
                    try:
                        subset_vrig_exact, subset_vrig_note = (
                            pc_bridge.validate_virtual_rig_subset(
                                cache, _vrig, arm, context,
                                enabled_names, cache.tau))
                    except Exception as exc:
                        subset_vrig_exact = False
                        subset_vrig_note = (
                            f"选中骨骼子集 VirtualRig 验证失败: {exc}")
                    if subset_vrig_exact:
                        print(f"[TheHerta4][PC] {subset_vrig_note}; use virtual-rig exact-session reads")
                    elif subset_vrig_note:
                        print(f"[TheHerta4][PC] {subset_vrig_note}")
            elif subset_lbs_note:
                print(f"[TheHerta4][PC] {subset_lbs_note}")
        elif headless and _lbs is not None and not _lbs.valid:
            for spec in bones:
                bone_index = lbs_index.get(spec.name)
                if bone_index is None:
                    continue
                rows = _lbs.bone_to_rows.get(bone_index)
                if rows is not None and len(rows) > 0:
                    # Approximate virtual-rig mode ignores Blender constraints,
                    # so weighted controller bones can use analytic deform proposals.
                    spec.kind = 'deform'

        fast_vrig_exact = (
            force_realtime_exact and subset_vrig_exact and _vrig is not None)
        if headless or fast_vrig_exact:
            # headless 高速：迭代全程纯 numpy（无 bpy），每 tick 批量后同步一次视口
            def apply_basis(name: str, basis: np.ndarray) -> None:
                if fast_vrig_exact:
                    pc_bridge.apply_basis_to_armature(arm, name, basis)
                _vrig.set_basis(name, basis)

            def read_samples() -> np.ndarray:
                return _vrig.read_samples(pc_engine.lbs_transform_with_remainder)

            def restore_sample_cache(points: np.ndarray) -> None:
                dirty = set(getattr(_vrig, "_dirty", set()))
                _vrig.refresh_pose()
                if dirty:
                    rows = np.unique(np.concatenate([
                        _vrig.bone_to_rows[i]
                        for i in dirty if i in _vrig.bone_to_rows
                    ])) if any(i in _vrig.bone_to_rows for i in dirty) \
                        else np.zeros(0, dtype=np.int64)
                    if len(rows) > 0:
                        _vrig.current[rows] = np.asarray(
                            points, dtype=np.float64)[rows]
                _vrig._dirty = set()

            provider = _vrig.provider()
        else:
            changed: set = set()  # 本步被写入 basis 的骨骼名（LBS 增量重算用）

            descendant_names = None
            if subset_lbs_exact and _lbs is not None:
                descendant_index = pc_bridge._bone_descendant_closure(
                    arm, _lbs.bone_names)
                descendant_names = {
                    _lbs.bone_names[i]: {
                        _lbs.bone_names[j] for j in indices
                    }
                    for i, indices in descendant_index.items()
                }

            pending_refresh: set = set()

            def apply_basis(name: str, basis: np.ndarray) -> None:
                pc_bridge.apply_basis_to_armature(arm, name, basis)
                changed.add(name)

            def read_samples() -> np.ndarray:
                names = set(changed)
                if pending_refresh:
                    names.update(pending_refresh)
                if not names:
                    return pc_bridge.lbs_read(
                        cache, _lbs, None, bpy.context,
                        allow_invalid=subset_lbs_exact)
                changed.clear()
                pending_refresh.clear()
                if subset_lbs_exact and descendant_names:
                    expanded = set()
                    for name in names:
                        expanded.update(descendant_names.get(name, {name}))
                    names = expanded
                return pc_bridge.lbs_read(
                    cache, _lbs, names, bpy.context,
                    allow_invalid=False if subset_lbs_exact else subset_lbs_exact)

            def restore_sample_cache(points: np.ndarray) -> None:
                if changed:
                    pending_refresh.update(changed)
                    changed.clear()

            provider = pc_bridge.bone_point_provider_factory(arm)
        current_b_points = (
            _vrig.read_samples(pc_engine.lbs_transform_with_remainder)
            if (headless or fast_vrig_exact) else pc_bridge.lbs_read(
                cache, _lbs, None, context,
                allow_invalid=subset_lbs_exact))
        session_backend = _backend
        runtime_nn_note = ""
        runtime_mode_note = ""
        runtime_screen_note = ""
        screen_rig = None
        if force_realtime_exact:
            # Keep the dual-bone exact convergence path on the stable CPU
            # nearest-neighbor providers for now. Use the fast SciPy-backed
            # CPU implementation instead of Blender's per-query KDTree loop so
            # exact convergence stays stable without paying Python-level NN
            # overhead on every candidate evaluation.
            exact_backend = pc_backend.NumpyBackend()
            nn_a = exact_backend.nearest_provider(cache.a_points)
            nn_factory = exact_backend.nearest_provider
            if (_backend is not None
                    and getattr(_backend, "is_gpu", False)
                    and hasattr(_backend, "nearest_provider")):
                runtime_nn_note = (
                    f"proposal:A-side {exact_backend.name} | "
                    f"metric:B-side {exact_backend.name} "
                    f"(GPU backend {_backend.name} available; "
                    f"exact metric pinned to CPU for stability)")
            else:
                runtime_nn_note = exact_backend.name
            runtime_mode_note = "双骨实时 exact（稳定收敛）"
            screen_rig = _vrig
            session_backend = _backend
            if fast_vrig_exact:
                runtime_mode_note = "双骨 VirtualRig exact（稳定收敛）"
            if (_backend is not None
                    and hasattr(_backend, "score_batch")
                    and screen_rig is not None):
                runtime_screen_note = (
                    f"coarse ranking: {_backend.name} batched screen-score search")
        elif _backend is not None and hasattr(_backend, "nearest_provider"):
            # Dual-bone exact mode is sensitive to the A-side nearest-neighbor
            # mapping used by proposal generation. Keep that mapping on the
            # stable CPU cache, while still allowing the backend to accelerate
            # the dynamic B-side metric provider.
            nn_a = _backend.nearest_provider(cache.a_points)
            nn_factory = _backend.nearest_provider
        else:
            nn_a = cache.nn_a
            nn_factory = pc_bridge.make_nn_provider

        session = pc_engine.PCFitSession(
            bones=bones,
            a_points=cache.a_points,
            b_points=current_b_points,
            nn_a=nn_a,
            config=cfg,
            apply_basis=apply_basis,
            read_samples=read_samples,
            bone_point_provider=provider,
            tau=cache.tau,
            basis_map=(_vrig.basis_map()
                        if headless
                        else {s.name: pc_bridge.get_basis(arm.pose.bones[s.name])
                              for s in bones}),
            nn_factory=nn_factory,
            backend=session_backend,
            screen_rig=screen_rig,
            restore_sample_cache=restore_sample_cache,
        )
        session.runtime_mode_note = runtime_mode_note
        session.runtime_nn_note = runtime_nn_note
        session.runtime_screen_note = runtime_screen_note
        # 初始指标
        session.current_metric = pc_engine.overlap_metric(
            session.a_points, session.b_points, session.nn_a, session.nn_b, session.tau)
        return session


class PC_OT_Start(bpy.types.Operator):
    """开始迭代（每步刷新视图与重合率）"""
    bl_idname = "toolkit.pc_start"
    bl_label = "开始"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _state, _latest_result
        global _state, _latest_result
        if _session is None:
            self.report({'WARNING'}, "请先初始化缓存")
            return {'CANCELLED'}
        props = _props(context)
        _state = 'running'
        if _worker is not None: _worker.start()
        props.status_text = "迭代中"
        _ensure_timer()
        return {'FINISHED'}


class PC_OT_Pause(bpy.types.Operator):
    """暂停迭代"""
    bl_idname = "toolkit.pc_pause"
    bl_label = "暂停"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _state, _latest_result
        _state = 'paused'
        if _worker is not None: _worker.pause()
        _props(context).status_text = "已暂停"
        _ensure_timer()  # 保持低频 tick 以响应进度条
        return {'FINISHED'}


class PC_OT_Resume(bpy.types.Operator):
    """继续迭代"""
    bl_idname = "toolkit.pc_resume"
    bl_label = "继续"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _state, _latest_result
        if _session is None:
            self.report({'WARNING'}, "请先初始化缓存")
            return {'CANCELLED'}
        _state = 'running'
        _latest_result = None
        if _worker is not None: _worker.resume()
        _props(context).status_text = "迭代中"
        _ensure_timer()
        return {'FINISHED'}


class PC_OT_Stop(bpy.types.Operator):
    """停止迭代（保留历史可回退）"""
    bl_idname = "toolkit.pc_stop"
    bl_label = "停止"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _state
        _state = 'stopped'
        if _worker is not None: _worker.pause()
        _props(context).status_text = "已停止（历史保留）"
        _ensure_timer()
        return {'FINISHED'}


class PC_OT_StepOnce(bpy.types.Operator):
    """同步执行若干步（调试用）"""
    bl_idname = "toolkit.pc_step_once"
    bl_label = "单步"
    bl_options = {'REGISTER'}

    count: bpy.props.IntProperty(name="步数", default=1, min=1, max=10000)

    def execute(self, context):
        global _state, _latest_result
        if _session is None:
            self.report({'WARNING'}, "请先初始化缓存")
            return {'CANCELLED'}
        if _worker is not None:
            _worker.pause()
            if not _worker.pause_and_wait():
                self.report({'ERROR'}, "后台线程未能及时暂停")
                return {'CANCELLED'}
        _state = 'paused'
        _latest_result = None
        _apply_enabled_flags()
        t_step0 = time.perf_counter()
        done = 0
        for _ in range(self.count):
            if _session.step() is None:
                break
            done += 1
        _refresh_after_state_change(context)
        _update_display_props(context)
        print(f"[TheHerta4][PC][Perf] step_once_total = {(time.perf_counter() - t_step0) * 1000.0:.2f} ms | steps = {done}")
        self.report({'INFO'}, f"已执行 {done} 步")
        return {'FINISHED'}


class PC_OT_RecomputeCurrentOverlap(bpy.types.Operator):
    """计算 Blender 当前姿态的重合率（调试用，不推进历史）"""
    bl_idname = "toolkit.pc_recompute_overlap"
    bl_label = "计算当前重合率"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _state, _latest_result
        if _cache is None:
            self.report({'WARNING'}, "请先初始化缓存")
            return {'CANCELLED'}
        if _worker is not None:
            _worker.pause()
            if not _worker.pause_and_wait():
                self.report({'ERROR'}, "后台线程未能及时暂停")
                return {'CANCELLED'}
        _state = 'paused'

        invalid_reason = pc_bridge.validate_cache(_cache)
        if invalid_reason is not None:
            self.report({'WARNING'}, invalid_reason)
            _props(context).status_text = f"场景已变化：{invalid_reason}"
            return {'CANCELLED'}

        current_b = pc_bridge.read_b_samples(_cache, context)
        if _backend is not None and hasattr(_backend, "nearest_provider"):
            nn_a = _backend.nearest_provider(_cache.a_points)
            nn_b = _backend.nearest_provider(current_b)
        else:
            nn_a = _cache.nn_a
            nn_b = pc_bridge.make_nn_provider(current_b)
        a_voxel_keys = pc_engine.occupied_voxel_keys(_cache.a_points, _cache.tau)
        b_voxel_keys = pc_engine.occupied_voxel_keys(current_b, _cache.tau)
        voxel_stats = pc_engine.voxel_overlap_key_stats(a_voxel_keys, b_voxel_keys)
        metric = pc_engine.overlap_metric(
            _cache.a_points, current_b, nn_a, nn_b, _cache.tau,
            a_voxel_keys=a_voxel_keys)
        _latest_result = None
        props = _props(context)
        props.cur_f1 = float(metric.f1)
        props.residual_mean = float(metric.chamfer) if math.isfinite(metric.chamfer) else 0.0
        fit_score = metric.score if metric.score is not None else metric.f1
        props.debug_overlap_text = (
            f"{metric.f1 * 100.0:.2f}%"
            f" | fit {fit_score * 100.0:.2f}%"
            f" | strict voxel {voxel_stats.f1 * 100.0:.2f}%"
            f" | P {metric.precision * 100.0:.2f}%"
            f" R {metric.recall * 100.0:.2f}%"
            f" | tau {_cache.tau:.5f}"
            f" | vox {voxel_stats.intersection}/{voxel_stats.a_count}/{voxel_stats.b_count}"
            f" | Chamfer {metric.chamfer:.5f}")
        props.status_text = (
            f"当前场景姿态重合率 {metric.f1 * 100.0:.2f}%"
            f" | P {metric.precision * 100.0:.2f}%"
            f" | R {metric.recall * 100.0:.2f}%")
        return {'FINISHED'}


class PC_OT_JumpBest(bpy.types.Operator):
    """跳到最佳重合率步"""
    bl_idname = "toolkit.pc_jump_best"
    bl_label = "跳到最佳"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if _session is None:
            self.report({'WARNING'}, "请先初始化缓存")
            return {'CANCELLED'}
        step = _session.jump_to_best() if _worker is None else (_worker.seek(_session.best_step).step)
        _refresh_after_state_change(context)
        _update_display_props(context)
        props = _props(context)
        props.history_view_step = step
        total = max(1, _session.history_total())
        global _cursor_follow_guard
        _cursor_follow_guard = True
        try:
            props.history_cursor = step / total
        finally:
            _cursor_follow_guard = False
        props.status_text = f"已跳到最佳步 {step}"
        return {'FINISHED'}


class PC_OT_TruncateAndResume(bpy.types.Operator):
    """在当前游标处截断后续历史并继续迭代"""
    bl_idname = "toolkit.pc_truncate_resume"
    bl_label = "从此截断并继续"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _state, _latest_result
        if _session is None:
            self.report({'WARNING'}, "请先初始化缓存")
            return {'CANCELLED'}
        props = _props(context)
        k = _session.nearest_recoverable_step(int(props.history_view_step))
        try:
            if _worker is None:
                _session.seek(k)
                _session.truncate_after(k)
            else:
                _worker.pause()
                _latest_result = _worker.truncate_after(k)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            props.status_text = str(exc)
            return {'CANCELLED'}
        _refresh_after_state_change(context)
        _update_display_props(context)
        _state = 'running'
        _latest_result = None
        if _worker is not None:
            _worker.resume()
        props.status_text = f"已从第 {k} 步截断并继续"
        _ensure_timer()
        return {'FINISHED'}


class PC_OT_ClearHistory(bpy.types.Operator):
    """清空历史（回到初始姿态）"""
    bl_idname = "toolkit.pc_clear_history"
    bl_label = "清空历史"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _state
        if _session is None:
            self.report({'WARNING'}, "请先初始化缓存")
            return {'CANCELLED'}
        _state = 'idle'
        if _worker is None:
            _session.seek(0)
            _session.truncate_after(0)
        else:
            _worker.pause()
            _worker.truncate_after(0)
        _refresh_after_state_change(context)
        _update_display_props(context)
        _props(context).status_text = "历史已清空"
        return {'FINISHED'}


class PC_OT_KeyframeCurrentPose(bpy.types.Operator):
    """为启用骨骼在当前帧插入关键帧（保留结果）"""
    bl_idname = "toolkit.pc_keyframe_pose"
    bl_label = "为当前姿态打关键帧"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if _session is None or _cache is None:
            self.report({'WARNING'}, "请先初始化缓存")
            return {'CANCELLED'}
        arm = bpy.data.objects.get(_cache.arm_obj_name)
        if arm is None:
            self.report({'ERROR'}, "骨架 B 已被删除或改名，请重建缓存")
            return {'CANCELLED'}
        frame = context.scene.frame_current
        count = 0
        for item in _props(context).bone_list:
            if not item.enabled:
                continue
            pb = arm.pose.bones.get(item.name)
            if pb is None:
                continue
            pb.keyframe_insert(data_path="location", frame=frame)
            if pb.rotation_mode == 'QUATERNION':
                pb.keyframe_insert(data_path="rotation_quaternion", frame=frame)
            else:
                pb.keyframe_insert(data_path="rotation_euler", frame=frame)
            pb.keyframe_insert(data_path="scale", frame=frame)
            count += 1
        self.report({'INFO'}, f"已为 {count} 根骨骼在第 {frame} 帧打关键帧")
        return {'FINISHED'}


pc_operators_list = (
    PC_OT_RefreshBoneList,
    PC_OT_SelectPoseBones,
    PC_OT_EnableDeformOnly,
    PC_OT_BuildCache,
    PC_OT_Start,
    PC_OT_Pause,
    PC_OT_Resume,
    PC_OT_Stop,
    PC_OT_StepOnce,
    PC_OT_RecomputeCurrentOverlap,
    PC_OT_JumpBest,
    PC_OT_TruncateAndResume,
    PC_OT_ClearHistory,
    PC_OT_KeyframeCurrentPose,
)
