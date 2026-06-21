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
        max=9999,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', "链输入")
        self.outputs.new('SSMTSocketAnimDriver', "链输出")
        self.width = 300
        self._assign_next_available_index()

    def draw_buttons(self, context, layout):
        layout.prop(self, "fps")
        layout.prop(self, "playback_rate")

    def generate_ini_segment(self, connected_nodes=None) -> str:
        return f"""[Constants]
global persist $fps = {self.fps}
; 固定帧率
global persist $swapvar = 0
; 当前帧索引（整数）
[Present]
; 基于系统时间的自动计算（每帧执行）
$swapvar = (time * $fps) // 1"""



_load_handler_registered = False


@bpy.app.handlers.persistent
def _runtime_load_handler(dummy):
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'SSMTBlueprintTreeType':
            continue
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_AnimDriver_Runtime':
                try:
                    SSMTNode_AnimDriver_Base._migrate_base_sockets(node)
                except Exception:
                    pass


classes = (
    SSMTNode_AnimDriver_Runtime,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_runtime_load_handler)
        _load_handler_registered = True


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_runtime_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
