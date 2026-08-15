import bpy
from bpy.props import IntProperty, StringProperty, BoolProperty, EnumProperty, CollectionProperty

from .anim_driver_base import (
    ANIM_DRIVER_INPUT_SOCKET_NAME,
    ANIM_DRIVER_OUTPUT_SOCKET_NAME,
    SSMTNode_AnimDriver_Base,
)


def _combine_conditions(conditions, joiner: str) -> str:
    clauses = [f"({var} {op} {val})" for var, op, val in conditions]
    return f" {joiner} ".join(clauses)


def _combine_conditions_with_or(conditions) -> str:
    # 兼容入口：OR 组合
    return _combine_conditions(conditions, "||")


class ConditionItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="条件变量",
        description="要判断的变量名称",
        default="",
    )

    comparison_op: EnumProperty(
        name="比较运算符",
        description="比较运算符",
        items=[
            ('==', '==', '等于'),
            ('!=', '!=', '不等于'),
            ('>', '>', '大于'),
            ('<', '<', '小于'),
            ('>=', '>=', '大于等于'),
            ('<=', '<=', '小于等于'),
        ],
        default='==',
    )

    compare_value: StringProperty(
        name="比较值",
        description="要比较的值",
        default="1",
    )


class CondTriggerTargetItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="变量名",
        description="条件满足时要设置的变量名称（如 $myTrigger）",
        default="",
    )

    trigger_value: StringProperty(
        name="赋值",
        description="条件满足时将变量设置为此值",
        default="1",
    )


class CondTriggerElseTargetItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="变量名",
        description="条件不满足时要设置的变量名称（如 $myTrigger），留空跳过",
        default="",
    )

    trigger_value: StringProperty(
        name="赋值",
        description="条件不满足时将变量设置为此值",
        default="1",
    )


class SSMT_UL_Conditions(bpy.types.UIList):
    bl_idname = "SSMT_UL_CONDITIONS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)
            row.prop(item, "comparison_op", text="")
            row.prop(item, "compare_value", text="")


class SSMT_UL_CondTriggerTargets(bpy.types.UIList):
    bl_idname = "SSMT_UL_COND_TRIGGER_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)
            row.prop(item, "trigger_value", text="")


class SSMT_UL_CondTriggerElseTargets(bpy.types.UIList):
    bl_idname = "SSMT_UL_COND_TRIGGER_ELSE_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)
            row.prop(item, "trigger_value", text="")


class SSMT_OT_ConditionAdd(bpy.types.Operator):
    bl_idname = "ssmt.condition_add"
    bl_label = "添加条件"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.condition_list.add()
        item.variable_name = "$cond_var"
        item.comparison_op = '=='
        item.compare_value = "1"
        node.condition_list_active = len(node.condition_list) - 1
        return {'FINISHED'}


class SSMT_OT_ConditionRemove(bpy.types.Operator):
    bl_idname = "ssmt.condition_remove"
    bl_label = "删除条件"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.condition_list_active
        if 0 <= idx < len(node.condition_list):
            node.condition_list.remove(idx)
            node.condition_list_active = min(idx, len(node.condition_list) - 1)
        return {'FINISHED'}


class SSMT_OT_CondTriggerTargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.cond_trigger_target_add"
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


class SSMT_OT_CondTriggerTargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.cond_trigger_target_remove"
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


class SSMT_OT_CondTriggerTargetRefresh(bpy.types.Operator):
    bl_idname = "ssmt.cond_trigger_target_refresh"
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


class SSMT_OT_CondTriggerElseTargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.cond_trigger_else_target_add"
    bl_label = "添加不满足触发变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.else_target_list.add()
        item.variable_name = "$else_trigger_target"
        node.else_target_list_active = len(node.else_target_list) - 1
        return {'FINISHED'}


class SSMT_OT_CondTriggerElseTargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.cond_trigger_else_target_remove"
    bl_label = "删除不满足触发变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.else_target_list_active
        if 0 <= idx < len(node.else_target_list):
            node.else_target_list.remove(idx)
            node.else_target_list_active = min(idx, len(node.else_target_list) - 1)
        return {'FINISHED'}


class SSMT_OT_CondTriggerElseTargetRefresh(bpy.types.Operator):
    bl_idname = "ssmt.cond_trigger_else_target_refresh"
    bl_label = "刷新不满足触发变量列表"
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
        node.else_target_list.clear()
        for var_name in downstream_vars:
            item = node.else_target_list.add()
            item.variable_name = var_name
        node.else_target_list_active = 0
        return {'FINISHED'}


class SSMTNode_AnimDriver_ConditionalTrigger(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_ConditionalTrigger'
    bl_label = '条件触发'
    bl_icon = 'ORIENTATION_CURSOR'

    condition_list: CollectionProperty(
        type=ConditionItem,
        name="条件列表",
    )

    condition_list_active: IntProperty(
        name="当前条件",
        default=0,
    )

    logic_operator: EnumProperty(
        name="逻辑运算符",
        description="多个条件之间的逻辑关系",
        items=[
            ('AND', '&& (全部满足)', '所有条件都必须满足'),
            ('OR', '|| (任一满足)', '任一条件满足即可'),
        ],
        default='AND',
    )

    target_list: CollectionProperty(
        type=CondTriggerTargetItem,
        name="触发变量列表",
    )

    target_list_active: IntProperty(
        name="当前触发变量",
        default=0,
    )

    else_target_list: CollectionProperty(
        type=CondTriggerElseTargetItem,
        name="不满足触发变量列表",
    )

    else_target_list_active: IntProperty(
        name="当前不满足触发变量",
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
        self.inputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_INPUT_SOCKET_NAME)
        self.outputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_OUTPUT_SOCKET_NAME)
        self.width = 350
        self._assign_next_available_index()
        self._ensure_paused_variable_name("cond_trigger_paused")

    def copy(self, node):
        self._assign_next_available_index()
        self.custom_paused_var = ""
        self._ensure_paused_variable_name("cond_trigger_paused")

    def draw_buttons(self, context, layout):
        safe_idx = self._read_safe_index()
        box = layout.box()
        box.label(text=f"索引: {safe_idx}", icon='LINENUMBERS_ON')

        # 逻辑运算符
        box.prop(self, "logic_operator")

        # 条件列表
        box.separator()
        row = box.row(align=True)
        row.label(text="条件列表:", icon='SCRIPT')
        op = row.operator("ssmt.condition_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.condition_remove", text="", icon='REMOVE')
        op.node_name = self.name

        if self.condition_list:
            box.template_list(
                "SSMT_UL_CONDITIONS", "",
                self, "condition_list",
                self, "condition_list_active",
                rows=max(2, min(len(self.condition_list), 6)),
            )
        else:
            box.label(text="点击 + 添加条件", icon='INFO')

        # 触发变量列表
        box.separator()
        row = box.row(align=True)
        row.label(text="触发变量(满足):", icon='VIEWZOOM')
        op = row.operator("ssmt.cond_trigger_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.cond_trigger_target_remove", text="", icon='REMOVE')
        op.node_name = self.name
        op = row.operator("ssmt.cond_trigger_target_refresh", text="", icon='FILE_REFRESH')
        op.node_name = self.name

        if self.target_list:
            box.template_list(
                "SSMT_UL_COND_TRIGGER_TARGETS", "",
                self, "target_list",
                self, "target_list_active",
                rows=max(2, min(len(self.target_list), 6)),
            )
        else:
            box.label(text="点击刷新按钮自动获取下游变量", icon='INFO')

        # 不满足触发变量列表
        box.separator()
        row = box.row(align=True)
        row.label(text="触发变量(不满足):", icon='ORPHAN_DATA')
        op = row.operator("ssmt.cond_trigger_else_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.cond_trigger_else_target_remove", text="", icon='REMOVE')
        op.node_name = self.name
        op = row.operator("ssmt.cond_trigger_else_target_refresh", text="", icon='FILE_REFRESH')
        op.node_name = self.name

        if self.else_target_list:
            box.template_list(
                "SSMT_UL_COND_TRIGGER_ELSE_TARGETS", "",
                self, "else_target_list",
                self, "else_target_list_active",
                rows=max(2, min(len(self.else_target_list), 6)),
            )
        else:
            box.label(text="不满足条件时触发的变量（留空跳过）", icon='INFO')

        # 暂停变量
        box.separator()
        row = box.row(align=True)
        row.prop(self, "default_paused", text="默认播放")
        if self.custom_paused_var.strip():
            row.prop(self, "custom_paused_var", text="")
        else:
            row.label(text=f"$cond_trigger_paused{safe_idx}")

        # 连接状态
        box.separator()
        box.label(text="连接状态:", icon='INFO')
        if self._has_linked_input():
            box.label(text="  [链输入] 已连接", icon='KEYFRAME')
        else:
            box.label(text="  [链输入] 未连接", icon='SNAP_FACE')
        if self._has_linked_output():
            box.label(text="  [链输出] 已连接（传递到下一节点）", icon='FORWARD')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        idx = self._read_safe_index()

        paused_state = self._resolve_default_play_state(self.default_paused)
        paused_var = self.custom_paused_var.strip()
        if not paused_var:
            paused_var = f"$cond_trigger_paused{idx}"
        elif not paused_var.startswith('$'):
            paused_var = f"${paused_var}"

        # 收集条件
        conditions = []
        for item in self.condition_list:
            var = item.variable_name.strip()
            if not var:
                continue
            if not var.startswith('$'):
                var = f"${var}"
            val = item.compare_value.strip()
            if not val:
                val = "1"
            conditions.append((var, item.comparison_op, val))

        # 收集触发目标
        target_assignments = []
        for item in self.target_list:
            target = item.variable_name.strip()
            if target:
                if not target.startswith('$'):
                    target = f"${target}"
                val = item.trigger_value.strip()
                if not val:
                    val = "1"
                target_assignments.append((target, val))

        if not target_assignments:
            target_assignments.append(("$trigger_target", "1"))

        # 收集不满足条件时的触发目标（留空则跳过 else 分支）
        else_target_assignments = []
        for item in self.else_target_list:
            target = item.variable_name.strip()
            if target:
                if not target.startswith('$'):
                    target = f"${target}"
                val = item.trigger_value.strip()
                if not val:
                    val = "1"
                else_target_assignments.append((target, val))

        lines = [
            "[Constants]",
            self._format_global_assignment(paused_var, paused_state, persist=True),
            "; 暂停状态",
        ]

        # 电平触发：每帧独立判定，无状态锁存
        # - paused==1 且条件成立 → 执行满足目标
        # - paused==1 且条件不成立 → 执行不满足目标（若有）
        # 两条分支各自独立判定、互不牵连；paused==0 时整段跳过
        lines.append("[Present]")
        lines.append(f"if {paused_var} == 1")

        if not conditions:
            # 无条件：paused==1 视为满足 → 满足目标，否则执行不满足目标
            for target, val in target_assignments:
                lines.append(f"    {target} = {val}")
            if else_target_assignments:
                lines.append("else")
                for target, val in else_target_assignments:
                    lines.append(f"    {target} = {val}")
            lines.append("endif")
        else:
            joiner = "&&" if self.logic_operator == 'AND' else "||"
            condition_expr = _combine_conditions(conditions, joiner)
            lines.append(f"    if {condition_expr}")
            for target, val in target_assignments:
                lines.append(f"        {target} = {val}")
            if else_target_assignments:
                lines.append("    else")
                for target, val in else_target_assignments:
                    lines.append(f"        {target} = {val}")
            lines.append("    endif")
            lines.append("endif")

        return "\n".join(lines)


_load_handler_registered = False


@bpy.app.handlers.persistent
def _cond_trigger_load_handler(dummy):
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'SSMTBlueprintTreeType':
            continue
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_AnimDriver_ConditionalTrigger':
                try:
                    SSMTNode_AnimDriver_Base.migrate_default_play_state_flag(node)
                    SSMTNode_AnimDriver_Base._migrate_dynamic_sockets(node)
                    if not node.custom_paused_var:
                        node._ensure_indexed_paused_variable_name("cond_trigger_paused")
                except Exception:
                    pass


classes = (
    ConditionItem,
    CondTriggerTargetItem,
    CondTriggerElseTargetItem,
    SSMT_UL_Conditions,
    SSMT_UL_CondTriggerTargets,
    SSMT_UL_CondTriggerElseTargets,
    SSMT_OT_ConditionAdd,
    SSMT_OT_ConditionRemove,
    SSMT_OT_CondTriggerTargetAdd,
    SSMT_OT_CondTriggerTargetRemove,
    SSMT_OT_CondTriggerTargetRefresh,
    SSMT_OT_CondTriggerElseTargetAdd,
    SSMT_OT_CondTriggerElseTargetRemove,
    SSMT_OT_CondTriggerElseTargetRefresh,
    SSMTNode_AnimDriver_ConditionalTrigger,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_cond_trigger_load_handler)
        _load_handler_registered = True


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_cond_trigger_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
