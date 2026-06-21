import bpy
from bpy.props import IntProperty, StringProperty, BoolProperty, CollectionProperty

from .anim_driver_base import SSMTNode_AnimDriver_Base


class TriggerTargetItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="变量名",
        description="触发时要设置的变量名称（如 $myTrigger）",
        default="",
    )

    trigger_value: StringProperty(
        name="赋值",
        description="触发时将变量设置为此值",
        default="1",
    )


class SSMT_UL_TriggerTargets(bpy.types.UIList):
    bl_idname = "SSMT_UL_TRIGGER_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)
            row.prop(item, "trigger_value", text="")


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


class SSMT_OT_TriggerTargetRefresh(bpy.types.Operator):
    bl_idname = "ssmt.trigger_target_refresh"
    bl_label = "刷新触发变量列表"
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
        node.target_list.clear()
        for var_name in downstream_vars:
            item = node.target_list.add()
            item.variable_name = var_name
        node.target_list_active = 0
        return {'FINISHED'}


class SSMTNode_AnimDriver_Trigger(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_Trigger'
    bl_label = '计时触发'
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
        name="默认播放",
        description="节点默认处于播放状态",
        default=True,
    )

    custom_paused_var: StringProperty(
        name="暂停变量",
        description="自定义暂停状态变量名（留空自动分配）",
        default="",
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', "链输入")
        self.inputs.new('SSMTSocketAnimDriver', "时间输入")
        self.inputs.new('SSMTSocketAnimDriver', "驱动输入")
        self.outputs.new('SSMTSocketAnimDriver', "链输出")
        self.width = 300
        self._assign_next_available_index()
        self._ensure_paused_variable_name("trigger_paused")

    def copy(self, node):
        self._assign_next_available_index()
        self.custom_paused_var = ""
        self._ensure_paused_variable_name("trigger_paused")

    def draw_buttons(self, context, layout):
        safe_idx = self._read_safe_index()
        box = layout.box()
        box.label(text=f"索引: {safe_idx}", icon='LINENUMBERS_ON')

        row = box.row(align=True)
        row.label(text="触发变量:", icon='VIEWZOOM')
        op = row.operator("ssmt.trigger_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.trigger_target_remove", text="", icon='REMOVE')
        op.node_name = self.name
        op = row.operator("ssmt.trigger_target_refresh", text="", icon='FILE_REFRESH')
        op.node_name = self.name

        if self.target_list:
            box.template_list(
                "SSMT_UL_TRIGGER_TARGETS", "",
                self, "target_list",
                self, "target_list_active",
                rows=max(2, min(len(self.target_list), 6)),
            )
        else:
            box.label(text="点击刷新按钮自动获取下游变量", icon='INFO')

        box.separator()
        row = box.row(align=True)
        row.prop(self, "default_paused", text="默认播放")
        if self.custom_paused_var.strip():
            row.prop(self, "custom_paused_var", text="")
        else:
            row.label(text=f"$trigger_paused{safe_idx}")

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

    def generate_ini_segment(self, connected_nodes=None) -> str:
        idx = self._read_safe_index()

        runtime = self._find_runtime_node()
        playback_rate = runtime.playback_rate if runtime else 1

        paused_state = self._resolve_default_play_state(self.default_paused)
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
                val = item.trigger_value.strip()
                if not val:
                    val = "1"
                target_assignments.append(f"        {target} = {val}")

        if not target_assignments:
            target_assignments.append("        $trigger_target = 1")

        lines = [
            "[Constants]",
            self._format_global_assignment(f"$speed_auto{idx}", playback_rate, persist=True),
            "; 切换速度（由运行时间的播放速率控制）",
            self._format_global_assignment(paused_var, paused_state, persist=True),
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
                    SSMTNode_AnimDriver_Base.migrate_default_play_state_flag(node)
                    SSMTNode_AnimDriver_Base._migrate_controlled_sockets(node)
                    if not node.custom_paused_var:
                        node._ensure_indexed_paused_variable_name("trigger_paused")
                except Exception:
                    pass


classes = (
    TriggerTargetItem,
    SSMT_UL_TriggerTargets,
    SSMT_OT_TriggerTargetAdd,
    SSMT_OT_TriggerTargetRemove,
    SSMT_OT_TriggerTargetRefresh,
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
