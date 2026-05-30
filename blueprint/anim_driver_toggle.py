import bpy
from bpy.props import IntProperty, StringProperty

from .anim_driver_base import SSMTNode_AnimDriver_Base


class SSMTNode_AnimDriver_Toggle(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_Toggle'
    bl_label = '动画驱动开关'
    bl_icon = 'KEYFRAME'

    key_binding: StringProperty(
        name="快捷键",
        description="快捷键绑定（如 no_modifiers k）",
        default="no_modifiers k",
    )

    manual_paused_var: StringProperty(
        name="暂停变量",
        description="手动指定要控制的暂停变量（留空自动从上游节点获取）",
        default="",
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', "Input")
        self.outputs.new('SSMTSocketAnimDriver', "Output")
        self.width = 300
        self._assign_auto_index()

    def copy(self, node):
        self._assign_auto_index()

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.label(text=f"索引: {self.auto_index}", icon='LINENUMBERS_ON')
        box.prop(self, "key_binding")

        if self.manual_paused_var.strip():
            box.prop(self, "manual_paused_var", text="暂停变量")
        else:
            box.label(text="暂停变量: 自动获取")

    def generate_ini_segment(self, connected_nodes=None) -> str:
        self._ensure_valid_index()
        idx = self.auto_index

        target_paused = self.manual_paused_var.strip()
        if target_paused:
            if not target_paused.startswith('$'):
                target_paused = f"${target_paused}"
        elif connected_nodes:
            for cn in connected_nodes:
                if hasattr(cn, 'custom_paused_var'):
                    val = cn.custom_paused_var.strip()
                    if val:
                        if not val.startswith('$'):
                            val = f"${val}"
                        target_paused = val
                        break

        if not target_paused:
            target_paused = f"$animation_paused{idx}"

        key = self.key_binding.strip() or "no_modifiers k"

        return f"""[KeyToggle_Anim{idx}]
condition = $active{idx} == 1
key = {key}
type = cycle
{target_paused} = 0,1"""


classes = (
    SSMTNode_AnimDriver_Toggle,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
