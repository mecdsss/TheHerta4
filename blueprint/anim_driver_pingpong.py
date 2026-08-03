import bpy
from bpy.props import FloatProperty, StringProperty, BoolProperty, IntProperty, CollectionProperty

from .anim_driver_base import (
    ANIM_DRIVER_INPUT_SOCKET_NAME,
    ANIM_DRIVER_OUTPUT_SOCKET_NAME,
    SSMTNode_AnimDriver_Base,
    DrivenVariableItem,
    ContinuousShapeKeyItem,
)


class SSMTNode_AnimDriver_PingPong(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_PingPong'
    bl_label = '往返播放'
    bl_icon = 'PLAY'

    frame_start: FloatProperty(
        name="起始数值",
        description="动画播放的起始数值",
        default=0.0,
        precision=3,
    )

    frame_end: FloatProperty(
        name="结束数值",
        description="动画播放的结束数值",
        default=14.0,
        precision=3,
    )

    driven_variable: StringProperty(
        name="驱动变量(旧)",
        description="旧版驱动变量（已迁移到列表）",
        default="",
        options={'HIDDEN'},
    )

    driven_variable_list: CollectionProperty(
        type=DrivenVariableItem,
        name="驱动变量列表",
    )

    driven_variable_list_active: IntProperty(
        name="当前驱动变量",
        default=0,
    )

    play_total_duration: FloatProperty(
        name="播放总时长",
        description="往返播放的总时长（秒），系统自动计算播放间隔",
        default=1.0,
        min=0.001,
        max=999.0,
        step=1.0,
        precision=3,
    )

    default_paused: BoolProperty(
        name="默认播放",
        description="节点默认处于播放状态",
        default=True,
    )

    custom_paused_var: StringProperty(
        name="暂停变量",
        description="自定义暂停状态变量名（留空自动分配 $animation_paused{N}）",
        default="",
    )

    loop_playback: BoolProperty(
        name="循环播放",
        description="播放完一轮（正向→反向→起点）后自动重新开始（中间节点强制禁用）",
        default=False,
    )

    hold_end_value: BoolProperty(
        name="保持结束值",
        description="播放完成后保持当前值不变，不重置状态（仅在未启用循环时生效）",
        default=False,
    )

    reverse_playback: BoolProperty(
        name="反向播放",
        description="反向播放，变量从结束数值递减到起始数值",
        default=False,
    )

    use_float_interval: BoolProperty(
        name="浮点计算",
        description="开启时播放间隔按浮点计算，关闭时取整为整数",
        default=True,
    )

    use_continuous_shapekey_mode: BoolProperty(
        name="连续形态键",
        description="开启后，驱动变量改为按目标物体上的连续形态键顺序自动映射到预分配变量",
        default=False,
    )

    continuous_target_object: StringProperty(
        name="目标物体",
        description="用于读取连续形态键顺序的目标物体名称",
        default="",
    )

    continuous_shape_key_prefix_filter: StringProperty(
        name="前缀过滤",
        description="刷新连续形态键时仅保留指定前缀的形态键，留空则不过滤",
        default="",
    )

    continuous_shape_key_items: CollectionProperty(
        type=ContinuousShapeKeyItem,
        name="连续形态键列表",
    )

    continuous_shape_key_items_active: IntProperty(
        name="当前连续形态键",
        default=0,
    )

    def _get_driven_vars(self):
        """获取所有驱动变量名列表（自动补$前缀）"""
        if getattr(self, "use_continuous_shapekey_mode", False):
            return [self._get_continuous_primary_var()]

        vars_list = []
        for item in self.driven_variable_list:
            var = item.variable_name.strip()
            if var:
                if not var.startswith('$'):
                    var = f"${var}"
                vars_list.append(var)
        # 向后兼容：如果列表为空但旧字段有值
        if not vars_list:
            old_var = self.driven_variable.strip()
            if old_var:
                if not old_var.startswith('$'):
                    old_var = f"${old_var}"
                vars_list.append(old_var)
        if not vars_list:
            vars_list.append("$driven_var")
        return vars_list

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_INPUT_SOCKET_NAME)
        self.outputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_OUTPUT_SOCKET_NAME)
        self.width = 300
        self._assign_next_available_index()
        self._ensure_paused_variable_name("animation_paused")
        self.assigned_continuous_index_variable_name = ""
        self.custom_continuous_index_variable_name = ""
        self.continuous_index_var_initialized = False
        self._ensure_continuous_index_variable_name(context=context)
        self._ensure_initial_visible_continuous_index_variable_name(context=context)

    def copy(self, node):
        self._assign_next_available_index()
        self.custom_paused_var = ""
        self._ensure_paused_variable_name("animation_paused")
        self.assigned_continuous_index_variable_name = ""
        self.custom_continuous_index_variable_name = ""
        self.continuous_index_var_initialized = False
        self._ensure_continuous_index_variable_name()
        self._ensure_initial_visible_continuous_index_variable_name()

    def _compute_play_interval(self):
        runtime = self._find_runtime_node()
        fps = runtime.fps if runtime else 30
        playback_rate = runtime.playback_rate if runtime else 1
        total_steps_one_way = abs(self.frame_end - self.frame_start)
        total_steps = 2 * total_steps_one_way
        total_frames = 2 * (total_steps_one_way + 1)
        if self.play_total_duration <= 0 or total_steps <= 0:
            return 1.0, total_frames, fps, playback_rate
        effective_ticks = self.play_total_duration * fps / playback_rate
        if effective_ticks <= 0:
            return 1.0, total_frames, fps, playback_rate
        interval = total_steps / effective_ticks
        return interval, total_frames, fps, playback_rate

    def draw_buttons(self, context, layout):
        safe_idx = self._read_safe_index()
        box = layout.box()
        box.label(text="当前模式: 往返播放", icon='PLAY')
        row = box.row(align=True)
        row.label(text=f"索引: {safe_idx}", icon='LINENUMBERS_ON')
        row.prop(self, "use_float_interval", text="浮点", icon='IPO_BEZIER')
        box.prop(self, "frame_start")
        box.prop(self, "frame_end")

        box.prop(self, "use_continuous_shapekey_mode", text="连续形态键模式", icon='SHAPEKEY_DATA')

        if getattr(self, "use_continuous_shapekey_mode", False):
            self._draw_continuous_shape_key_controls(box)
        else:
            row = box.row(align=True)
            row.label(text="驱动变量:", icon='VIEWZOOM')
            op = row.operator("ssmt.driven_variable_add", text="", icon='ADD')
            op.node_name = self.name
            op = row.operator("ssmt.driven_variable_remove", text="", icon='REMOVE')
            op.node_name = self.name

            if self.driven_variable_list:
                box.template_list(
                    "SSMT_UL_DRIVEN_VARIABLES", "",
                    self, "driven_variable_list",
                    self, "driven_variable_list_active",
                    rows=max(2, min(len(self.driven_variable_list), 6)),
                )
            else:
                box.label(text="点击 + 添加驱动变量", icon='INFO')

        interval, total_frames, fps, playback_rate = self._compute_play_interval()

        box.prop(self, "play_total_duration")
        info_col = box.column(align=True)
        info_col.label(text=f"总帧数: {total_frames} (往返)")
        if self.use_float_interval:
            info_col.label(text=f"播放间隔: {interval:.4f}")
        else:
            info_col.label(text=f"播放间隔: {interval:.4f} (取整为 {max(1, int(interval))})")
        info_col.label(text=f"帧率: {fps} | 速率: {playback_rate}")

        box.separator()
        row = box.row(align=True)
        row.prop(self, "default_paused", text="默认播放")
        if self.custom_paused_var.strip():
            row.prop(self, "custom_paused_var", text="")
        else:
            row.label(text=f"$animation_paused{safe_idx}")

        row = box.row(align=True)
        row.prop(self, "reverse_playback", text="反向", icon='ARROW_LEFTRIGHT')
        if self._get_next_node_in_chain() is not None:
            row.label(text="循环已禁用（下游有节点）", icon='FILE_REFRESH')
        else:
            row.prop(self, "loop_playback", text="循环", icon='FILE_REFRESH')

        if not self.loop_playback:
            box.prop(self, "hold_end_value", text="保持结束值", icon='KEYFRAME')
        else:
            box.label(text="保持结束值（禁用）", icon='KEYFRAME')

        box.separator()
        box.label(text="连接状态:", icon='INFO')
        if self._has_linked_input():
            box.label(text="  [链输入] 已连接", icon='KEYFRAME')
        else:
            box.label(text="  [链输入] 未连接", icon='SNAP_FACE')
        if self._find_runtime_node() is not None:
            box.label(text="  [运行时间] 已连接", icon='TIME')
        else:
            box.label(text="  [运行时间] 未连接", icon='ERROR')
        if self._has_linked_output():
            box.label(text="  [链输出] 已连接（传递到下一节点）", icon='FORWARD')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        idx = self._read_safe_index()
        driven_vars = self._get_driven_vars()
        primary_var = driven_vars[0]
        continuous_mode = getattr(self, "use_continuous_shapekey_mode", False)
        continuous_entries = self._get_continuous_shape_key_entries() if continuous_mode else []
        hold_end_value = bool(getattr(self, "hold_end_value", False))

        runtime = self._find_runtime_node()
        playback_rate = runtime.playback_rate if runtime else 1

        paused_state = self._resolve_default_play_state(self.default_paused)
        paused_var = self.custom_paused_var.strip()
        if not paused_var:
            paused_var = f"$animation_paused{idx}"
        elif not paused_var.startswith('$'):
            paused_var = f"${paused_var}"

        interval, _, _, _ = self._compute_play_interval()
        interval_str = f"{interval:.4f}" if self.use_float_interval else str(max(1, int(interval)))

        has_next_in_chain = self._get_next_node_in_chain() is not None
        can_loop = not has_next_in_chain and self.loop_playback
        init_direction = -1 if self.reverse_playback else 1

        lines = [
            "[Constants]",
            self._format_global_assignment(f"$speed_auto{idx}", playback_rate, persist=True),
            "; 切换速度（由运行时间的播放速率控制）",
            self._format_global_assignment(f"$frameStart{idx}", self.frame_start, persist=True),
            "; 起始帧",
            self._format_global_assignment(f"$frameEnd{idx}", self.frame_end, persist=True),
            "; 结束帧",
            self._format_global_assignment(f"$direction{idx}", init_direction, persist=True),
            "; 播放方向（1=正向，-1=反向）",
            self._format_global_assignment(paused_var, paused_state, persist=True),
            "; 暂停状态",
        ]
        if continuous_mode:
            lines.extend([
                self._format_global_assignment(primary_var, self._get_continuous_primary_initial_value(), persist=True),
                "; 连续形态键索引变量",
            ])
        lines.extend([
            "[Present]",
            f"if {paused_var} == 1",
            f"    if $swapvar % $speed_auto{idx} == 0",
            f"        if $direction{idx} == 1",
            f"            if {primary_var} < $frameEnd{idx}",
        ])

        # 正向：所有驱动变量递增
        for var in driven_vars:
            lines.append(f"                {var} = {var} + {interval_str}")

        lines.append(f"                if {primary_var} > $frameEnd{idx}")
        for var in driven_vars:
            lines.append(f"                    {var} = $frameEnd{idx}")
        lines.append("                endif")
        if continuous_mode and continuous_entries:
            self._append_continuous_shape_key_mapping_lines(lines, primary_var, indent="                ")

        lines.append("            else")

        # 正向到达边界：翻转方向
        lines.append(f"                $direction{idx} = -1")

        lines.extend([
            "            endif",
            "        else",
            f"            if {primary_var} > $frameStart{idx}",
        ])

        # 反向：所有驱动变量递减
        for var in driven_vars:
            lines.append(f"                {var} = {var} - {interval_str}")

        lines.append(f"                if {primary_var} < $frameStart{idx}")
        for var in driven_vars:
            lines.append(f"                    {var} = $frameStart{idx}")
        lines.append("                endif")
        if continuous_mode and continuous_entries:
            self._append_continuous_shape_key_mapping_lines(lines, primary_var, indent="                ")

        lines.append("            else")

        if can_loop:
            lines.append(f"                $direction{idx} = 1")
            lines.extend([
                "            endif",
                "        endif",
                "    endif",
                "endif",
            ])
        elif has_next_in_chain:
            next_paused_vars = self._get_all_next_paused_vars()
            if next_paused_vars:
                lines.append(f"                {paused_var} = 0")
                for next_paused in next_paused_vars:
                    lines.append(f"                {next_paused} = 1")
                if not hold_end_value:
                    lines.append(f"                $direction{idx} = 1")
                lines.extend([
                    "            endif",
                    "        endif",
                    "    endif",
                    "endif",
                ])
            else:
                lines.append(f"                {paused_var} = 0")
                if not hold_end_value:
                    lines.append(f"                $direction{idx} = 1")
                lines.extend([
                    "            endif",
                    "        endif",
                    "    endif",
                    "endif",
                ])
        else:
            lines.append(f"                {paused_var} = 0")
            if not hold_end_value:
                lines.append(f"                $direction{idx} = 1")
            lines.extend([
                "            endif",
                "        endif",
                "    endif",
                "endif",
            ])

        return "\n".join(lines)


_load_handler_registered = False


@bpy.app.handlers.persistent
def _pingpong_load_handler(dummy):
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'SSMTBlueprintTreeType':
            continue
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_AnimDriver_PingPong':
                try:
                    SSMTNode_AnimDriver_Base.migrate_default_play_state_flag(node)
                    SSMTNode_AnimDriver_Base._migrate_dynamic_sockets(node)
                    if not node.custom_paused_var:
                        node._ensure_indexed_paused_variable_name("animation_paused")
                    if getattr(node, "use_continuous_shapekey_mode", False):
                        node.continuous_index_var_initialized = False
                        node._ensure_initial_visible_continuous_index_variable_name()
                    # 迁移旧 driven_variable 到 driven_variable_list
                    old_var = node.driven_variable.strip()
                    if old_var and len(node.driven_variable_list) == 0:
                        item = node.driven_variable_list.add()
                        item.variable_name = old_var
                        node.driven_variable = ""
                except Exception:
                    pass


classes = (
    SSMTNode_AnimDriver_PingPong,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_pingpong_load_handler)
        _load_handler_registered = True


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_pingpong_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
