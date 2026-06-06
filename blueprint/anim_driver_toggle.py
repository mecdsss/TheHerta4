import bpy
from bpy.props import IntProperty, StringProperty, CollectionProperty

from .anim_driver_base import SSMTNode_AnimDriver_Base


class ToggleTargetItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="暂停变量",
        description="要控制的暂停变量名称（如 $animation_paused1）",
        default="",
    )


class SSMT_UL_ToggleTargets(bpy.types.UIList):
    bl_idname = "SSMT_UL_TOGGLE_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)


class SSMT_OT_ToggleTargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.toggle_target_add"
    bl_label = "添加暂停变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.pause_target_list.add()
        item.variable_name = "$animation_paused1"
        node.pause_target_active = len(node.pause_target_list) - 1
        return {'FINISHED'}


class SSMT_OT_ToggleTargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.toggle_target_remove"
    bl_label = "删除暂停变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.pause_target_active
        if 0 <= idx < len(node.pause_target_list):
            node.pause_target_list.remove(idx)
            node.pause_target_active = min(idx, len(node.pause_target_list) - 1)
        return {'FINISHED'}


class SSMT_OT_ToggleTargetRefresh(bpy.types.Operator):
    bl_idname = "ssmt.toggle_target_refresh"
    bl_label = "刷新暂停变量列表"
    bl_description = "自动获取下游所有索引播放和往返播放节点的暂停变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        downstream_vars = node._collect_downstream_pause_vars()
        node.pause_target_list.clear()
        for var_name in downstream_vars:
            item = node.pause_target_list.add()
            item.variable_name = var_name
        node.pause_target_active = 0
        return {'FINISHED'}


class SSMTNode_AnimDriver_Toggle(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_Toggle'
    bl_label = '动画驱动开关'
    bl_icon = 'KEYFRAME'

    key_binding: StringProperty(
        name="快捷键",
        description="快捷键绑定（如 no_modifiers k）",
        default="no_modifiers k",
    )

    toggle_values: StringProperty(
        name="开关值",
        description="开关循环的数值，用空格或逗号分隔（如 0,1 或 0 0 1）",
        default="0,1",
    )

    comment: StringProperty(
        name="备注",
        description="生成到 KeyToggle 段落中的注释。",
        default="",
    )

    pause_target_list: CollectionProperty(
        type=ToggleTargetItem,
        name="暂停变量列表",
    )

    pause_target_active: IntProperty(
        name="当前暂停变量",
        default=0,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', "链输入")
        self.outputs.new('SSMTSocketAnimDriver', "链输出")
        self.width = 300
        self._assign_next_available_index()

    def copy(self, node):
        self._assign_next_available_index()

    def draw_buttons(self, context, layout):
        safe_idx = self._read_safe_index()
        box = layout.box()
        box.label(text=f"索引: {safe_idx}", icon='LINENUMBERS_ON')
        box.prop(self, "key_binding")
        box.prop(self, "toggle_values")
        box.prop(self, "comment", text="备注")

        box.separator()
        row = box.row(align=True)
        row.label(text="暂停变量:", icon='VIEWZOOM')
        op = row.operator("ssmt.toggle_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.toggle_target_remove", text="", icon='REMOVE')
        op.node_name = self.name
        op = row.operator("ssmt.toggle_target_refresh", text="", icon='FILE_REFRESH')
        op.node_name = self.name

        if self.pause_target_list:
            box.template_list(
                "SSMT_UL_TOGGLE_TARGETS", "",
                self, "pause_target_list",
                self, "pause_target_active",
                rows=max(2, min(len(self.pause_target_list), 6)),
            )
        else:
            box.label(text="点击刷新按钮自动获取下游变量", icon='INFO')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        idx = self._read_safe_index()

        key = self.key_binding.strip() or "no_modifiers k"

        target_vars = []
        for item in self.pause_target_list:
            var = item.variable_name.strip()
            if var:
                if not var.startswith('$'):
                    var = f"${var}"
                target_vars.append(var)

        if not target_vars:
            downstream_vars = self._collect_downstream_pause_vars()
            if downstream_vars:
                target_vars = downstream_vars
            else:
                target_vars = [f"$animation_paused{idx}"]

        raw = self.toggle_values.strip() or "0 1"
        values = ",".join(raw.replace(",", " ").split())
        toggle_vals = values if values else "0,1"
        comment = self.comment.strip()

        lines = [
            f"[KeyToggle_Anim{idx}]",
            "condition = $active0 == 1",
            f"key = {key}",
            "type = cycle",
        ]
        if comment:
            lines.insert(1, f"; {comment}")
        for var in target_vars:
            lines.append(f"{var} = {toggle_vals}")

        return "\n".join(lines)


_load_handler_registered = False


@bpy.app.handlers.persistent
def _toggle_load_handler(dummy):
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'SSMTBlueprintTreeType':
            continue
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_AnimDriver_Toggle':
                try:
                    SSMTNode_AnimDriver_Base._migrate_base_sockets(node)
                except Exception:
                    pass


classes = (
    ToggleTargetItem,
    SSMT_UL_ToggleTargets,
    SSMT_OT_ToggleTargetAdd,
    SSMT_OT_ToggleTargetRemove,
    SSMT_OT_ToggleTargetRefresh,
    SSMTNode_AnimDriver_Toggle,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_toggle_load_handler)
        _load_handler_registered = True


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_toggle_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
