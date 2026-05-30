import bpy
from bpy.props import IntProperty, StringProperty, BoolProperty, CollectionProperty

from .anim_driver_base import SSMTNode_AnimDriver_Base


class TriggerTargetItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="变量名",
        description="触发时设置为1的变量名称（如 $myTrigger）",
        default="",
    )


class SSMT_UL_TriggerTargets(bpy.types.UIList):
    bl_idname = "SSMT_UL_TRIGGER_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)


class SSMT_OT_TriggerTargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.trigger_target_add"
    bl_label = "添加触发变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.target_list.add()
        item.variable_name = "$trigger_target"
        node.target_list_active = len(node.target_list) - 1
        return {'FINISHED'}


class SSMT_OT_TriggerTargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.trigger_target_remove"
    bl_label = "删除触发变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.target_list_active
        if 0 <= idx < len(node.target_list):
            node.target_list.remove(idx)
            node.target_list_active = min(idx, len(node.target_list) - 1)
        return {'FINISHED'}


class SSMTNode_AnimDriver_Trigger(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_Trigger'
    bl_label = '触发'
    bl_icon = 'TIME'

    target_list: CollectionProperty(
        type=TriggerTargetItem,
        name="触发变量列表",
    )

    target_list_active: IntProperty(
        name="当前触发变量",
        default=0,
    )

    default_paused: BoolProperty(
        name="默认暂停",
        description="节点默认处于暂停状态",
        default=True,
    )

    custom_paused_var: StringProperty(
        name="暂停变量",
        description="自定义暂停状态变量名（留空自动分配）",
        default="",
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', "Input")
        self.outputs.new('SSMTSocketAnimDriver', "Output")
        self.width = 300
        self._assign_auto_index()
        self.custom_paused_var = f"$trigger_paused{self.auto_index}"

    def copy(self, node):
        self._assign_auto_index()
        self.custom_paused_var = f"$trigger_paused{self.auto_index}"

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.label(text=f"索引: {self.auto_index}", icon='LINENUMBERS_ON')

        row = box.row(align=True)
        row.label(text="触发变量:", icon='VIEWZOOM')
        op = row.operator("ssmt.trigger_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.trigger_target_remove", text="", icon='REMOVE')
        op.node_name = self.name

        if self.target_list:
            box.template_list(
                "SSMT_UL_TRIGGER_TARGETS", "",
                self, "target_list",
                self, "target_list_active",
                rows=max(2, min(len(self.target_list), 6)),
            )

        box.separator()
        row = box.row(align=True)
        row.prop(self, "default_paused", text="默认播放")
        if self.custom_paused_var.strip():
            row.prop(self, "custom_paused_var", text="")
        else:
            row.label(text=f"$trigger_paused{self.auto_index}")

    def generate_ini_segment(self, connected_nodes=None) -> str:
        self._ensure_valid_index()
        idx = self.auto_index

        playback_rate = 1
        if connected_nodes:
            for cn in connected_nodes:
                if hasattr(cn, 'playback_rate'):
                    playback_rate = cn.playback_rate
                    break

        paused_state = 1 if self.default_paused else 0
        paused_var = self.custom_paused_var.strip()
        if not paused_var:
            paused_var = f"$trigger_paused{idx}"
        elif not paused_var.startswith('$'):
            paused_var = f"${paused_var}"

        target_assignments = []
        for item in self.target_list:
            target = item.variable_name.strip()
            if target:
                if not target.startswith('$'):
                    target = f"${target}"
                target_assignments.append(f"        {target} = 1")

        if not target_assignments:
            target_assignments.append("        $trigger_target = 1")

        lines = [
            "[Constants]",
            f"global $speed_auto{idx} = {playback_rate}",
            "; 切换速度（由运行时间的播放速率控制）",
            f"global {paused_var} = {paused_state}",
            "; 暂停状态",
            "[Present]",
            f"if {paused_var} == 1",
            f"    if $swapvar % $speed_auto{idx} == 0",
        ]

        lines.extend(target_assignments)
        lines.extend([
            "    endif",
            "endif",
        ])

        return "\n".join(lines)


_load_handler_registered = False


@bpy.app.handlers.persistent
def _trigger_load_handler(dummy):
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'SSMTBlueprintTreeType':
            continue
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_AnimDriver_Trigger':
                try:
                    if not node.custom_paused_var:
                        node.custom_paused_var = f"$trigger_paused{node.auto_index}"
                except Exception:
                    pass


classes = (
    TriggerTargetItem,
    SSMT_UL_TriggerTargets,
    SSMT_OT_TriggerTargetAdd,
    SSMT_OT_TriggerTargetRemove,
    SSMTNode_AnimDriver_Trigger,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_trigger_load_handler)
        _load_handler_registered = True


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_trigger_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
