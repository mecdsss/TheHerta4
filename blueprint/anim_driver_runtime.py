import bpy
from bpy.props import IntProperty

from .anim_driver_base import SSMTNode_AnimDriver_Base


class SSMTNode_AnimDriver_Runtime(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_Runtime'
    bl_label = '运行时间'
    bl_icon = 'TIME'

    fps: IntProperty(
        name="固定帧率",
        description="用于计算帧索引的目标帧率",
        default=30,
        min=1,
        max=144,
    )

    playback_rate: IntProperty(
        name="播放速率",
        description="控制 $swapvar 取模的速率（1=每帧，2=每两帧）",
        default=1,
        min=1,
        max=60,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', "Input")
        self.outputs.new('SSMTSocketAnimDriver', "Output")
        self.width = 300

    def draw_buttons(self, context, layout):
        layout.prop(self, "fps")
        layout.prop(self, "playback_rate")

    def generate_ini_segment(self, connected_nodes=None) -> str:
        return f"""[Constants]
global $fps = {self.fps}
; 固定帧率
global $swapvar = 0
; 当前帧索引
[Present]
; 基于系统时间的自动计算（每帧执行）
$swapvar = time * $fps"""


classes = (
    SSMTNode_AnimDriver_Runtime,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
