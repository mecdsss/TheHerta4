import bpy
from bpy.props import FloatProperty, StringProperty, BoolProperty

from .anim_driver_base import SSMTNode_AnimDriver_Base


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
        name="驱动变量",
        description="要驱动的变量名称（如 $myVar）",
        default="",
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
        name="默认暂停",
        description="节点默认处于暂停状态",
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

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', "链输入")
        self.inputs.new('SSMTSocketAnimDriver', "时间输入")
        self.inputs.new('SSMTSocketAnimDriver', "驱动输入")
        self.outputs.new('SSMTSocketAnimDriver', "链输出")
        self.outputs.new('SSMTSocketAnimDriver', "时间输出")
        self.width = 300
        self._assign_next_available_index()
        self.custom_paused_var = f"$animation_paused{self.auto_index}"

    def copy(self, node):
        self._assign_next_available_index()
        self.custom_paused_var = f"$animation_paused{self.auto_index}"

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
        box = layout.box()
        row = box.row(align=True)
        row.label(text=f"索引: {self.auto_index}", icon='LINENUMBERS_ON')
        row.prop(self, "use_float_interval", text="浮点", icon='IPO_BEZIER')
        box.prop(self, "frame_start")
        box.prop(self, "frame_end")
        box.prop(self, "driven_variable")

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
            row.label(text=f"$animation_paused{self.auto_index}")

        row = box.row(align=True)
        row.prop(self, "reverse_playback", text="反向", icon='ARROW_LEFTRIGHT')
        if self._get_next_node_in_chain() is not None:
            row.label(text="循环已禁用（下游有节点）", icon='FILE_REFRESH')
        else:
            row.prop(self, "loop_playback", text="循环", icon='FILE_REFRESH')

        box.separator()
        box.label(text="输入说明:", icon='INFO')
        if self.inputs.get("时间输入") and self.inputs["时间输入"].is_linked:
            box.label(text="  [时间输入] 已连接", icon='TIME')
        else:
            box.label(text="  [时间输入] 未连接", icon='ERROR')
        if self.inputs.get("驱动输入") and self.inputs["驱动输入"].is_linked:
            box.label(text="  [驱动输入] 已连接", icon='KEYFRAME')
        else:
            box.label(text="  [驱动输入] 未连接", icon='SNAP_FACE')
        if self.outputs.get("时间输出") and self.outputs["时间输出"].is_linked:
            box.label(text="  [时间输出] 已连接（传递到下一节点）", icon='FORWARD')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        self._ensure_valid_index()
        idx = self.auto_index
        var = self.driven_variable.strip()
        if not var:
            var = "$driven_var"
        elif not var.startswith('$'):
            var = f"${var}"

        runtime = self._find_runtime_node()
        playback_rate = runtime.playback_rate if runtime else 1

        paused_state = 1 if self.default_paused else 0
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
        init_var = self.frame_end if self.reverse_playback else self.frame_start

        lines = [
            "[Constants]",
            f"global $speed_auto{idx} = {playback_rate}",
            "; 切换速度（由运行时间的播放速率控制）",
            f"global $frameStart{idx} = {self.frame_start}",
            "; 起始帧",
            f"global $frameEnd{idx} = {self.frame_end}",
            "; 结束帧",
            f"global $direction{idx} = {init_direction}",
            "; 播放方向（1=正向，-1=反向）",
            f"global {paused_var} = {paused_state}",
            "; 暂停状态",
            "[Present]",
            f"if {paused_var} == 1",
            f"    if $swapvar % $speed_auto{idx} == 0",
            f"        if $direction{idx} == 1",
            f"            if {var} < $frameEnd{idx}",
            f"                {var} = {var} + {interval_str}",
            "            else",
            f"                $direction{idx} = -1",
            "            endif",
            "        else",
            f"            if {var} > $frameStart{idx}",
            f"                {var} = {var} - {interval_str}",
            "            else",
        ]

        if can_loop:
            lines.extend([
                f"                $direction{idx} = 1",
                "            endif",
                "        endif",
                "    endif",
                "endif",
            ])
        elif has_next_in_chain:
            next_paused = self._get_next_paused_var()
            if next_paused:
                lines.extend([
                    f"                {paused_var} = 0",
                    f"                {next_paused} = 1",
                    f"                $direction{idx} = 1",
                    "            endif",
                    "        endif",
                    "    endif",
                    "endif",
                ])
            else:
                lines.extend([
                    f"                {paused_var} = 0",
                    f"                $direction{idx} = 1",
                    "            endif",
                    "        endif",
                    "    endif",
                    "endif",
                ])
        else:
            lines.extend([
                f"                {paused_var} = 0",
                f"                $direction{idx} = 1",
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
                    SSMTNode_AnimDriver_Base._migrate_play_sockets(node)
                    if not node.custom_paused_var:
                        node.custom_paused_var = f"$animation_paused{node.auto_index}"
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