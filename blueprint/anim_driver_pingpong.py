import bpy
from bpy.props import IntProperty, FloatProperty, StringProperty, BoolProperty

from .anim_driver_base import SSMTNode_AnimDriver_Base


class SSMTNode_AnimDriver_PingPong(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_PingPong'
    bl_label = '往返播放'
    bl_icon = 'PLAY'

    frame_start: IntProperty(
        name="起始帧",
        description="动画播放的起始帧",
        default=0,
    )

    frame_end: IntProperty(
        name="结束帧",
        description="动画播放的结束帧",
        default=14,
    )

    driven_variable: StringProperty(
        name="驱动变量",
        description="要驱动的变量名称（如 $myVar）",
        default="",
    )

    play_interval: FloatProperty(
        name="播放间隔",
        description="每帧驱动的变量增量（0~1，1=每帧+1，0.5=每帧+0.5）",
        default=1.0,
        min=0.0,
        max=1.0,
        step=0.001,
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
        description="播放完一轮（正向→反向→起点）后自动重新开始",
        default=False,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', "Input")
        self.outputs.new('SSMTSocketAnimDriver', "Output")
        self.width = 300
        self._assign_auto_index()
        self.custom_paused_var = f"$animation_paused{self.auto_index}"

    def copy(self, node):
        self._assign_auto_index()
        self.custom_paused_var = f"$animation_paused{self.auto_index}"

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.label(text=f"索引: {self.auto_index}", icon='LINENUMBERS_ON')
        box.prop(self, "frame_start")
        box.prop(self, "frame_end")
        box.prop(self, "driven_variable")
        box.prop(self, "play_interval")

        box.separator()
        row = box.row(align=True)
        row.prop(self, "default_paused", text="默认播放")
        if self.custom_paused_var.strip():
            row.prop(self, "custom_paused_var", text="")
        else:
            row.label(text=f"$animation_paused{self.auto_index}")

        row = box.row(align=True)
        row.prop(self, "loop_playback", text="循环", icon='FILE_REFRESH')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        self._ensure_valid_index()
        idx = self.auto_index
        var = self.driven_variable.strip()
        if not var:
            var = "$driven_var"
        elif not var.startswith('$'):
            var = f"${var}"

        playback_rate = 1
        if connected_nodes:
            for cn in connected_nodes:
                if hasattr(cn, 'playback_rate'):
                    playback_rate = cn.playback_rate
                    break

        paused_state = 1 if self.default_paused else 0
        paused_var = self.custom_paused_var.strip()
        if not paused_var:
            paused_var = f"$animation_paused{idx}"
        elif not paused_var.startswith('$'):
            paused_var = f"${paused_var}"

        lines = [
            "[Constants]",
            f"global $speed_auto{idx} = {playback_rate}",
            "; 切换速度（由运行时间的播放速率控制）",
            f"global $frameStart{idx} = {self.frame_start}",
            "; 起始帧",
            f"global $frameEnd{idx} = {self.frame_end}",
            "; 结束帧",
            f"global $direction{idx} = 1",
            "; 播放方向（1=正向，-1=反向）",
            f"global {paused_var} = {paused_state}",
            "; 暂停状态",
            "[Present]",
            f"if {paused_var} == 1",
            f"    if $swapvar % $speed_auto{idx} == 0",
            f"        if $direction{idx} == 1",
            f"            if {var} < $frameEnd{idx}",
            f"                {var} = {var} + {self.play_interval:.3f}",
            "            else",
            f"                $direction{idx} = -1",
            "            endif",
            "        else",
            f"            if {var} > $frameStart{idx}",
            f"                {var} = {var} - {self.play_interval:.3f}",
            "            else",
        ]

        if not self.loop_playback:
            lines.append(f"                {paused_var} = 0")

        lines.extend([
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
