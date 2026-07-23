# -*- coding: utf-8 -*-
"""点云姿态匹配：独立工具面板（挂在工具集主面板末尾）。"""
import bpy

from . import pc_operators


class PC_UL_BoneList(bpy.types.UIList):
    """骨骼列表：勾选框 + 名称 + 类型图标 + 锁定摘要。"""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            return
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        bone_icon = 'BONE_DATA' if item.kind == 'deform' else 'CONSTRAINT_BONE'
        row.label(text=item.name, icon=bone_icon)
        if item.has_constraints:
            row.label(text="", icon='CONSTRAINT')
        if item.lock_info:
            sub = row.row()
            sub.scale_x = 0.9
            sub.label(text=item.lock_info)


class PC_PT_MainPanel(bpy.types.Panel):
    bl_label = "点云姿态匹配（实验）"
    bl_idname = "VIEW3D_PT_Herta_PC_Main_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_Toolkit_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 100

    def draw(self, context):
        layout = self.layout
        props = context.scene.pc_props

        # 状态行
        status_box = layout.box()
        row = status_box.row()
        row.label(text=f"状态: {props.status_text}",
                  icon='PLAY' if pc_operators._state == 'running' else 'PAUSE')
        if props.perf_info:
            status_box.label(text=props.perf_info, icon='SETTINGS')

        self._draw_target(layout, context, props)
        self._draw_bones(layout, context, props)
        self._draw_run(layout, context, props)
        self._draw_history(layout, context, props)
        self._draw_result(layout, context, props)

    # ------------------------------------------------------------------
    def _draw_target(self, layout, context, props):
        box = layout.box()
        box.label(text="目标与采样", icon='MESH_DATA')
        col = box.column(align=True)
        col.prop(props, "a_object")
        col.prop(props, "b_armature")
        col.prop(props, "sample_count")
        row = col.row(align=True)
        row.prop(props, "threshold_mode", expand=True)
        if props.threshold_mode == 'MANUAL':
            col.prop(props, "threshold")
        else:
            tau = pc_operators._cache.tau if pc_operators._cache else 0.0
            col.label(text=f"匹配阈值 = {tau:.4f}（自动）")
        if pc_operators._cache:
            cache = pc_operators._cache
            col.label(text=f"缓存: A {len(cache.a_points)} 点 / B {len(cache.b_rest_points)} 点 / "
                           f"{len(cache.b_parts)} 网格 / {len(cache.bones)} 骨骼")
        col.operator(pc_operators.PC_OT_BuildCache.bl_idname,
                     text="初始化/重建缓存", icon='FILE_REFRESH')

    def _draw_bones(self, layout, context, props):
        box = layout.box()
        enabled = sum(1 for it in props.bone_list if it.enabled)
        box.label(text=f"骨骼列表（已启用 {enabled}/{len(props.bone_list)}）", icon='ARMATURE_DATA')
        row = box.row(align=True)
        row.operator(pc_operators.PC_OT_RefreshBoneList.bl_idname, text="刷新", icon='FILE_REFRESH')
        row.operator(pc_operators.PC_OT_SelectPoseBones.bl_idname, text="按姿态选中")
        row.operator(pc_operators.PC_OT_EnableDeformOnly.bl_idname, text="仅变形骨")
        box.template_list("PC_UL_BoneList", "", props, "bone_list",
                          props, "bone_list_index", rows=6)

    def _draw_run(self, layout, context, props):
        box = layout.box()
        box.label(text="运行控制", icon='PLAY')
        row = box.row(align=True)
        if pc_operators._state == 'running':
            row.operator(pc_operators.PC_OT_Pause.bl_idname, text="暂停", icon='PAUSE')
        elif pc_operators._state == 'paused':
            row.operator(pc_operators.PC_OT_Resume.bl_idname, text="继续", icon='PLAY')
        else:
            row.operator(pc_operators.PC_OT_Start.bl_idname, text="开始", icon='PLAY')
        row.operator(pc_operators.PC_OT_Stop.bl_idname, text="停止", icon='SNAP_FACE')
        step100 = row.operator(
            pc_operators.PC_OT_StepOnce.bl_idname,
            text="单步×100",
            icon='FORWARD')
        step100.count = 100
        row.operator(pc_operators.PC_OT_RecomputeCurrentOverlap.bl_idname,
                     text="计算当前重合率", icon='FILE_REFRESH')
        box.label(text=f"当前场景重合率: {props.debug_overlap_text}", icon='INFO')

        col = box.column(align=True)
        col.prop(props, "steps_per_tick")
        col.prop(props, "tick_interval")
        col.prop(props, "learning_rate")
        col.prop(props, "max_angle_deg")
        col.prop(props, "max_scale_delta")
        col.prop(props, "max_translation")

        col.separator()
        col.label(text="性能加速:")
        col.prop(props, "backend_mode", text="")
        col.prop(props, "use_headless")
        col.prop(props, "use_fast_lbs")
        col.prop(props, "use_approximate_fallback")
        col.prop(props, "minibatch_size")
        col.prop(props, "full_eval_interval")
        col.prop(props, "phase_eval_interval")
        col.prop(props, "phase_plateau_delta")
        col.prop(props, "phase_plateau_checks")
        col.prop(props, "approximate_realign_interval")

        sub = box.box()
        sub.label(text="联合微调先验（最终阶段）:")
        row = sub.row(align=True)
        row.prop(props, "prior_rotation", text="旋转")
        row.prop(props, "prior_scale", text="缩放")
        row.prop(props, "prior_location", text="位移")

        sub2 = box.box()
        sub2.label(text=f"当前阶段: {props.sched_phase}")
        sub2.label(text=props.sched_phase_status)
        sub2.separator()
        sub2.label(text=props.last_move_summary, icon='BONE_DATA')
        if props.last_move_detail:
            sub2.label(text=props.last_move_detail)
        sub2.separator()
        sub2.label(text=props.bone_curriculum_status)
        if props.bone_curriculum_detail:
            sub2.label(text=props.bone_curriculum_detail)
        sub2.label(text="当前调度权重 / 残差:")
        row = sub2.row(align=True)
        row.label(text=f"旋 {props.sched_w_rot:.2f}")
        row.label(text=f"缩 {props.sched_w_scale:.2f}")
        row.label(text=f"移 {props.sched_w_loc:.2f}")
        sub2.label(text=f"Chamfer {props.residual_mean:.5f}")

    def _draw_history(self, layout, context, props):
        box = layout.box()
        box.label(text="历史回放", icon='TIME')
        col = box.column(align=True)
        col.prop(props, "history_cursor", slider=True)
        col.label(text=f"步 {props.history_view_step} / 共 {props.history_total}")
        col.label(text=f"当前重合率: {props.cur_f1 * 100:.2f}%  (第 {props.cur_step} 步)")
        col.label(text=f"最佳重合率: {props.best_f1 * 100:.2f}%  (第 {props.best_step} 步)")
        row = box.row(align=True)
        row.operator(pc_operators.PC_OT_JumpBest.bl_idname, text="跳到最佳", icon='SOLO_ON')
        row.operator(pc_operators.PC_OT_TruncateAndResume.bl_idname, text="从此截断续跑")
        row.operator(pc_operators.PC_OT_ClearHistory.bl_idname, text="清空", icon='TRASH')

    def _draw_result(self, layout, context, props):
        box = layout.box()
        box.label(text="结果", icon='KEYFRAME')
        box.operator(pc_operators.PC_OT_KeyframeCurrentPose.bl_idname,
                     text="为当前姿态打关键帧", icon='KEY_HLT')
        box.label(text="提示：姿态直接写在骨架上；历史为运行时内存，重开文件需重建缓存。",
                  icon='INFO')


pc_panel_list = (
    PC_UL_BoneList,
    PC_PT_MainPanel,
)
