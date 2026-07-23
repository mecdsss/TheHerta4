from decimal import Decimal, InvalidOperation

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from .anim_driver_base import SSMTNode_AnimDriver_Base


class AccumulativeConditionItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="条件变量",
        description="要判断的变量名称",
        default="",
    )

    comparison_op: EnumProperty(
        name="比较运算符",
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
        default="1",
    )

    increment_value: StringProperty(
        name="累计值",
        description="此条件每次满足时增加的数值",
        default="0.1",
    )


class AccumulativeTargetItem(bpy.types.PropertyGroup):
    threshold_value: StringProperty(
        name="阈值",
        description="累计值达到此数值时执行本行操作",
        default="1",
    )

    variable_name: StringProperty(
        name="变量名",
        description="达到阈值时要设置的变量名称",
        default="",
    )

    trigger_value: StringProperty(
        name="赋值",
        description="达到阈值时将变量设置为此值",
        default="1",
    )


class SSMT_UL_AccumulativeConditions(bpy.types.UIList):
    bl_idname = "SSMT_UL_ACCUMULATIVE_CONDITIONS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "variable_name", text="", icon='VIEWZOOM' if item.variable_name else 'ERROR')
            row.prop(item, "comparison_op", text="")
            row.prop(item, "compare_value", text="")
            row.prop(item, "increment_value", text="+", emboss=True)


class SSMT_UL_AccumulativeTargets(bpy.types.UIList):
    bl_idname = "SSMT_UL_ACCUMULATIVE_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "threshold_value", text="")
            row.prop(item, "variable_name", text="", icon='VIEWZOOM' if item.variable_name else 'ERROR')
            row.prop(item, "trigger_value", text="")


def _get_active_node(context, node_name):
    tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
    if not tree:
        return None
    return tree.nodes.get(node_name) if node_name else tree.nodes.active


class SSMT_OT_AccumulativeConditionAdd(bpy.types.Operator):
    bl_idname = "ssmt.accumulative_condition_add"
    bl_label = "添加累计条件"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        node = _get_active_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}
        item = node.condition_list.add()
        item.variable_name = "$cond_var"
        item.comparison_op = '=='
        item.compare_value = "1"
        item.increment_value = "0.1"
        node.condition_list_active = len(node.condition_list) - 1
        return {'FINISHED'}


class SSMT_OT_AccumulativeConditionRemove(bpy.types.Operator):
    bl_idname = "ssmt.accumulative_condition_remove"
    bl_label = "删除累计条件"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        node = _get_active_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}
        idx = node.condition_list_active
        if 0 <= idx < len(node.condition_list):
            node.condition_list.remove(idx)
            node.condition_list_active = min(idx, len(node.condition_list) - 1)
        return {'FINISHED'}


class SSMT_OT_AccumulativeTargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.accumulative_target_add"
    bl_label = "添加触发变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        node = _get_active_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}
        item = node.target_list.add()
        item.threshold_value = "1"
        item.variable_name = "$trigger_target"
        node.target_list_active = len(node.target_list) - 1
        return {'FINISHED'}


class SSMT_OT_AccumulativeTargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.accumulative_target_remove"
    bl_label = "删除触发变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        node = _get_active_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}
        idx = node.target_list_active
        if 0 <= idx < len(node.target_list):
            node.target_list.remove(idx)
            node.target_list_active = min(idx, len(node.target_list) - 1)
        return {'FINISHED'}


class SSMT_OT_AccumulativeTargetRefresh(bpy.types.Operator):
    bl_idname = "ssmt.accumulative_target_refresh"
    bl_label = "刷新触发变量列表"
    bl_description = "自动获取下游播放节点的暂停变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        node = _get_active_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}
        node.target_list.clear()
        for variable_name in node._collect_downstream_pause_vars():
            item = node.target_list.add()
            item.variable_name = variable_name
        node.target_list_active = 0
        return {'FINISHED'}


class SSMTNode_AnimDriver_AccumulativeTrigger(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_AccumulativeTrigger'
    bl_label = '累计触发'
    bl_icon = 'SORTTIME'

    condition_list: CollectionProperty(type=AccumulativeConditionItem, name="累计条件")
    condition_list_active: IntProperty(name="当前累计条件", default=0)
    target_list: CollectionProperty(type=AccumulativeTargetItem, name="触发变量列表")
    target_list_active: IntProperty(name="当前触发变量", default=0)

    accumulator_variable: StringProperty(
        name="累计变量",
        description="保存当前累计值的变量名称；留空时自动分配",
        default="",
    )

    default_paused: BoolProperty(
        name="默认播放",
        description="节点默认处于累计状态",
        default=True,
    )

    custom_paused_var: StringProperty(
        name="暂停变量",
        description="控制累计状态的变量名称；留空时自动分配",
        default="",
    )

    def _mark_play_state_migrated(self):
        migration_key = SSMTNode_AnimDriver_Base.PLAY_STATE_MIGRATION_KEY
        try:
            self[migration_key] = True
        except (AttributeError, TypeError):
            setattr(self, migration_key, True)

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', "链输入")
        self.inputs.new('SSMTSocketAnimDriver', "时间输入")
        self.inputs.new('SSMTSocketAnimDriver', "驱动输入")
        self.outputs.new('SSMTSocketAnimDriver', "链输出")
        self.width = 420
        self._assign_next_available_index()
        self._ensure_paused_variable_name("accumulative_trigger_paused")
        self._mark_play_state_migrated()

    def copy(self, node):
        self._assign_next_available_index()
        self.custom_paused_var = ""
        self.accumulator_variable = ""
        self._ensure_paused_variable_name("accumulative_trigger_paused")
        self._mark_play_state_migrated()

    def draw_buttons(self, context, layout):
        safe_idx = self._read_safe_index()
        box = layout.box()
        box.label(text=f"索引: {safe_idx}", icon='LINENUMBERS_ON')

        row = box.row(align=True)
        row.label(text="累计条件:", icon='ORIENTATION_CURSOR')
        op = row.operator("ssmt.accumulative_condition_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.accumulative_condition_remove", text="", icon='REMOVE')
        op.node_name = self.name
        if self.condition_list:
            box.template_list(
                "SSMT_UL_ACCUMULATIVE_CONDITIONS", "", self, "condition_list",
                self, "condition_list_active", rows=max(2, min(len(self.condition_list), 6)),
            )
        else:
            box.label(text="添加条件并为每个条件设置累计值", icon='INFO')

        box.separator()
        row = box.row(align=True)
        row.prop(self, "accumulator_variable", text="累计变量")
        if not self.accumulator_variable.strip():
            row.label(text=f"$accumulator{safe_idx}")

        box.separator()
        row = box.row(align=True)
        row.label(text="阈值动作:", icon='VIEWZOOM')
        op = row.operator("ssmt.accumulative_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.accumulative_target_remove", text="", icon='REMOVE')
        op.node_name = self.name
        op = row.operator("ssmt.accumulative_target_refresh", text="", icon='FILE_REFRESH')
        op.node_name = self.name
        if self.target_list:
            box.template_list(
                "SSMT_UL_ACCUMULATIVE_TARGETS", "", self, "target_list",
                self, "target_list_active", rows=max(2, min(len(self.target_list), 6)),
            )
        else:
            box.label(text="按阈值从小到大添加动作；最高阈值结束周期", icon='INFO')

        box.separator()
        row = box.row(align=True)
        row.prop(self, "default_paused", text="默认播放")
        if self.custom_paused_var.strip():
            row.prop(self, "custom_paused_var", text="")
        else:
            row.label(text=f"$accumulative_trigger_paused{safe_idx}")

        box.separator()
        if self.inputs.get("时间输入") and self.inputs["时间输入"].is_linked:
            box.label(text="[时间输入] 已连接", icon='TIME')
        else:
            box.label(text="[时间输入] 未连接", icon='ERROR')

    @staticmethod
    def _normalize_variable_name(raw_value, fallback):
        variable_name = str(raw_value or "").strip() or fallback
        return variable_name if variable_name.startswith('$') else f"${variable_name}"

    @staticmethod
    def _group_threshold_targets(target_list):
        groups = []
        groups_by_value = {}
        all_numeric = True
        for item in target_list:
            variable_name = str(item.variable_name or "").strip()
            if not variable_name:
                continue
            threshold = str(item.threshold_value or "").strip() or "1"
            group = groups_by_value.get(threshold)
            if group is None:
                try:
                    numeric_value = Decimal(threshold)
                except InvalidOperation:
                    numeric_value = None
                    all_numeric = False
                else:
                    if not numeric_value.is_finite():
                        numeric_value = None
                        all_numeric = False
                group = {
                    "threshold": threshold,
                    "numeric_value": numeric_value,
                    "targets": [],
                }
                groups_by_value[threshold] = group
                groups.append(group)
            group["targets"].append((
                SSMTNode_AnimDriver_AccumulativeTrigger._normalize_variable_name(variable_name, ""),
                str(item.trigger_value or "").strip() or "1",
            ))

        if not groups:
            return [{
                "threshold": "1",
                "numeric_value": Decimal("1"),
                "targets": [("$trigger_target", "1")],
            }]
        if all_numeric:
            groups.sort(key=lambda group: group["numeric_value"])
        return groups

    def generate_ini_segment(self, connected_nodes=None) -> str:
        idx = self._read_safe_index()
        runtime = self._find_runtime_node()
        playback_rate = runtime.playback_rate if runtime else 1
        paused_var = self._normalize_variable_name(
            self.custom_paused_var, f"$accumulative_trigger_paused{idx}"
        )
        accumulator_var = self._normalize_variable_name(
            self.accumulator_variable, f"$accumulator{idx}"
        )

        conditions = []
        for item in self.condition_list:
            variable_name = str(item.variable_name or "").strip()
            if not variable_name:
                continue
            variable_name = self._normalize_variable_name(variable_name, "")
            compare_value = str(item.compare_value or "").strip() or "1"
            increment_value = str(item.increment_value or "").strip() or "0.1"
            conditions.append((variable_name, item.comparison_op, compare_value, increment_value))

        threshold_groups = self._group_threshold_targets(self.target_list)
        intermediate_groups = threshold_groups[:-1]
        final_group = threshold_groups[-1]

        lines = [
            "[Constants]",
            self._format_global_assignment(f"$speed_auto{idx}", playback_rate, persist=True),
            "; 累计频率（由运行时间的播放速率控制）",
            self._format_global_assignment(
                paused_var, self._resolve_default_play_state(self.default_paused), persist=True
            ),
            "; 累计状态（1=运行，0=暂停）",
            self._format_global_assignment(accumulator_var, 0, persist=True),
            "; 当前累计值",
        ]

        for group_index, _group in enumerate(intermediate_groups, 1):
            lines.append(self._format_global_assignment(
                f"$accumulative_threshold_state{idx}_{group_index}", 0, persist=True
            ))
            lines.append("; 本周期内的阈值触发状态")

        lines.extend([
            "[Present]",
            f"if {paused_var} == 1",
            f"    if $swapvar % $speed_auto{idx} == 0",
        ])

        for variable_name, comparison_op, compare_value, increment_value in conditions:
            lines.extend([
                f"        if {variable_name} {comparison_op} {compare_value}",
                f"            {accumulator_var} = {accumulator_var} + {increment_value}",
                "        endif",
            ])

        for group_index, group in enumerate(intermediate_groups, 1):
            state_var = f"$accumulative_threshold_state{idx}_{group_index}"
            lines.extend([
                f"        if {state_var} == 0",
                f"            if {accumulator_var} >= {group['threshold']}",
            ])
            for variable_name, trigger_value in group["targets"]:
                lines.append(f"                {variable_name} = {trigger_value}")
            lines.extend([
                f"                {state_var} = 1",
                "            endif",
                "        endif",
            ])

        lines.append(f"        if {accumulator_var} >= {final_group['threshold']}")
        for variable_name, trigger_value in final_group["targets"]:
            lines.append(f"            {variable_name} = {trigger_value}")
        lines.extend([
            f"            {accumulator_var} = 0",
        ])
        for group_index, _group in enumerate(intermediate_groups, 1):
            lines.append(f"            $accumulative_threshold_state{idx}_{group_index} = 0")
        lines.extend([
            "        endif",
            "    endif",
            "endif",
        ])
        return "\n".join(lines)


_load_handler_registered = False


@bpy.app.handlers.persistent
def _accumulative_trigger_load_handler(dummy):
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'SSMTBlueprintTreeType':
            continue
        for node in tree.nodes:
            if node.bl_idname != 'SSMTNode_AnimDriver_AccumulativeTrigger':
                continue
            try:
                SSMTNode_AnimDriver_Base.migrate_default_play_state_flag(node)
                SSMTNode_AnimDriver_Base._migrate_controlled_sockets(node)
                if not node.custom_paused_var:
                    node._ensure_indexed_paused_variable_name("accumulative_trigger_paused")
            except Exception:
                pass


classes = (
    AccumulativeConditionItem,
    AccumulativeTargetItem,
    SSMT_UL_AccumulativeConditions,
    SSMT_UL_AccumulativeTargets,
    SSMT_OT_AccumulativeConditionAdd,
    SSMT_OT_AccumulativeConditionRemove,
    SSMT_OT_AccumulativeTargetAdd,
    SSMT_OT_AccumulativeTargetRemove,
    SSMT_OT_AccumulativeTargetRefresh,
    SSMTNode_AnimDriver_AccumulativeTrigger,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_accumulative_trigger_load_handler)
        _load_handler_registered = True


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_accumulative_trigger_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
