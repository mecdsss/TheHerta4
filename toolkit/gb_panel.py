# -*- coding: utf-8 -*-
"""高斯权重球：独立工具面板（挂在工具集主面板末尾，排在点云姿态匹配之后）。"""
import bpy

from . import gb_operators


class GB_PT_MainPanel(bpy.types.Panel):
    bl_label = "高斯权重球（实验）"
    bl_idname = "VIEW3D_PT_Herta_GB_Main_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_Toolkit_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 101

    def draw(self, context):
        layout = self.layout
        props = context.scene.gb_props

        sessions = gb_operators._sessions
        running = bool(sessions)

        # 状态行
        status_box = layout.box()
        row = status_box.row()
        row.label(text=f"状态: {'编辑中' if running else '未开始'}",
                  icon='PLAY' if running else 'PAUSE')
        if running:
            status_box.label(text=f"共 {len(sessions)} 个会话", icon='GROUP_VERTEX')

        self._draw_session(layout, context, props, sessions, running)
        if running:
            self._draw_balls(layout, context, sessions)
            self._draw_preview(layout, context, props)
        self._draw_tips(layout, context)

    # ------------------------------------------------------------------
    def _draw_session(self, layout, context, props, sessions, running):
        box = layout.box()
        box.label(text="会话", icon='GROUP_VERTEX')

        # 新建入口（始终可用，支持同时开多个不同调试物体的会话）
        box.label(text="选中一个 Source/Target 调试物体开始新会话", icon='QUESTION')
        box.operator(gb_operators.GB_OT_StartFromDebug.bl_idname,
                     text="从选中调试物体创建高斯球", icon='ADD')

        if running:
            col = box.column(align=True)
            col.prop(props, "normalize_on_confirm")

            # 会话列表：勾选 = 参与确认写入；行首图标 = 设为活动会话
            list_box = box.box()
            list_box.label(text="勾选要写入的会话（按勾选顺序写入）:", icon='CHECKMARK')
            for session_id in gb_operators._sorted_sessions():
                session = sessions[session_id]
                row = list_box.row(align=True)

                # 设为活动会话的按钮（高亮当前活动）
                is_active = (session_id == gb_operators._active_session_id)
                act_op = row.operator(
                    gb_operators.GB_OT_SetActiveSession.bl_idname,
                    text="",
                    icon='RADIOBUT_ON' if is_active else 'RADIOBUT_OFF',
                    emboss=False)
                act_op.session_id = session_id

                # 会话标签
                mode_label = "源→目标" if session.mode == 'source' else "目标自身"
                row.label(text=f"{session.vg_name} ({mode_label}, "
                               f"{len(session.ball_names)}球/{len(session.targets)}目标)",
                          icon='GROUP_VERTEX')

                # 勾选写入
                sel_op = row.operator(
                    gb_operators.GB_OT_ToggleSessionSelect.bl_idname,
                    text="",
                    icon='CHECKBOX_HLT' if session.selected else 'CHECKBOX_DEHLT',
                    emboss=False)
                sel_op.session_id = session_id

            n_sel = len(gb_operators._selected_sessions())
            row = box.row(align=True)
            row.operator(gb_operators.GB_OT_Confirm.bl_idname,
                         text=f"确认并写入权重 ({n_sel})", icon='CHECKMARK')
            row.operator(gb_operators.GB_OT_Cancel.bl_idname,
                         text="取消活动会话", icon='X')

        row = box.row(align=True)
        row.operator(gb_operators.GB_OT_CleanupOrphans.bl_idname,
                     text="清理残留球体", icon='TRASH')

    def _draw_balls(self, layout, context, sessions):
        box = layout.box()
        box.label(text="高斯球", icon='META_BALL')
        row = box.row(align=True)
        row.operator(gb_operators.GB_OT_AddBall.bl_idname,
                     text="添加球", icon='ADD')
        row.operator(gb_operators.GB_OT_RemoveBall.bl_idname,
                     text="删除活动球", icon='REMOVE')

        # 活动会话的球列表
        active_session = gb_operators.get_active_session()
        if active_session is not None:
            box.label(text=f"活动会话 '{active_session.vg_name}'："
                      f"共 {len(active_session.ball_names)} 个球（重叠区取最大值合并）")
        else:
            box.label(text="（活动会话已失效，点击会话行首图标切换）")

        active = context.active_object
        in_any = active is not None and any(
            active.name in s.ball_names for s in sessions.values())
        if in_any and hasattr(active, "gb_ball"):
            sub = box.box()
            sub.label(text=f"活动球: {active.name}", icon='RADIOBUT_ON')
            col = sub.column(align=True)
            col.prop(active.gb_ball, "use_source_sampling")
            if active.gb_ball.use_source_sampling:
                col.prop(active.gb_ball, "strength", slider=True,
                         text="强度倍率")
                sub.label(text="采样场：球内目标取最近源顶点的原始权重，"
                          "保留真实分布、不穿透网格", icon='INFO')
            else:
                col.prop(active.gb_ball, "strength", slider=True,
                         text="中心强度")
                col.prop(active.gb_ball, "falloff_k", slider=True)
                col.prop(active.gb_ball, "use_surface_propagation")
            col.prop(active.gb_ball, "enabled")
            row = sub.row(align=True)
            row.label(text="设半径:")
            for radius in (0.01, 0.02, 0.05, 0.1):
                op = row.operator(gb_operators.GB_OT_SetRadius.bl_idname,
                                  text=f"{radius:g}")
                op.radius = radius
        else:
            box.label(text="在视口选中一个高斯球可调参数", icon='INFO')

        box.label(text="提示: 视口中移动/旋转/缩放球体；球面即权重≈0边界，",
                  icon='INFO')
        box.label(text="非均匀缩放可得椭球（形状调整）。")

    def _draw_preview(self, layout, context, props):
        box = layout.box()
        box.label(text="预览", icon='HIDE_OFF')
        col = box.column(align=True)
        col.prop(props, "heat_opacity", slider=True)
        col.prop(props, "tick_interval")
        col.prop(props, "xray_preview")
        col.prop(props, "only_nearest_island")

        # 每个会话 × 每个目标的覆盖统计
        sessions = gb_operators._sessions
        for session_id in gb_operators._sorted_sessions():
            session = sessions[session_id]
            for tcache in session.targets.values():
                if tcache.preview_info:
                    box.label(
                        text=f"[{session.vg_name} → {tcache.name}] {tcache.preview_info}",
                        icon='INFO')

    def _draw_tips(self, layout, context):
        box = layout.box()
        box.label(text="说明", icon='HELP')
        col = box.column()
        col.scale_y = 0.8
        col.label(text="· 预览为热力图，确认前不写入真实权重；")
        col.label(text="· 可同时开多个会话，热力图叠加显示；")
        col.label(text="· 勾选多个会话按勾选顺序依次写入+规格化；")
        col.label(text="· 写入后在球位置生成绿方块(Source)/黄球(Target)；")
        col.label(text="· v1 用基础网格位置（不含骨骼/形态键变形）。")


gb_panel_list = (
    GB_PT_MainPanel,
)
