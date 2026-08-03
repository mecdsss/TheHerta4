import bpy
from bpy.props import IntProperty, StringProperty, BoolProperty, CollectionProperty

from .anim_driver_base import (
    ANIM_DRIVER_INPUT_SOCKET_NAME,
    ANIM_DRIVER_OUTPUT_SOCKET_NAME,
    SSMTNode_AnimDriver_Base,
)
from .variable_registry import normalize_variable_name


def _resolve_shapekey_variable(item):
    custom = getattr(item, 'custom_variable_name', '').strip()
    if custom:
        if custom.startswith('$'):
            custom = custom[1:]
        return f"${custom}"
    assigned = getattr(item, 'assigned_variable_name', '').strip()
    if assigned:
        if assigned.startswith('$'):
            assigned = assigned[1:]
        return f"${assigned}"
    return ""


class ShapeKeyAnimItem(bpy.types.PropertyGroup):
    name: StringProperty(
        name="形态键名称",
        description="形态键的名称",
        default="",
    )

    variable: StringProperty(
        name="变量名",
        description="从形态键配置节点解析到的变量名（如 $Freq_Shape）",
        default="",
    )

    frame_values_csv: StringProperty(
        name="帧数值",
        description="各帧的数值，逗号分隔（用于显示）",
        default="",
    )


class SSMT_UL_ShapeKeyAnimItems(bpy.types.UIList):
    bl_idname = "SSMT_UL_SHAPEKEY_ANIM_ITEMS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'SHAPEKEY_DATA' if item.name else 'ERROR'
            row.prop(item, "name", text="", icon=icon_val, emboss=False)
            if item.variable:
                row.label(text=item.variable, icon='VIEWZOOM')
            if item.frame_values_csv:
                row.label(text=item.frame_values_csv, icon='INFO')


class SSMT_OT_ShapeKeyAnimAdd(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_add"
    bl_label = "添加形态键"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.shapekey_items.add()
        item.name = ""
        node.shapekey_items_active = len(node.shapekey_items) - 1
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimRemove(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_remove"
    bl_label = "删除形态键"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.shapekey_items_active
        if 0 <= idx < len(node.shapekey_items):
            node.shapekey_items.remove(idx)
            node.shapekey_items_active = min(idx, len(node.shapekey_items) - 1)
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimRefresh(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_refresh"
    bl_label = "刷新形态键变量"
    bl_description = "从上层蓝图中的形态键配置节点读取变量名，自动填充到列表中已有的同名形态键"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def _find_parent_shapekey_map(self, current_tree) -> dict:
        result = {}
        for tree in bpy.data.node_groups:
            if tree.bl_idname != 'SSMTBlueprintTreeType':
                continue
            if tree.get("is_animation_driver"):
                continue
            has_ref = False
            for node in tree.nodes:
                if node.bl_idname == 'SSMTNode_PostProcess_AnimDriver':
                    if getattr(node, 'blueprint_name', '') == current_tree.name:
                        has_ref = True
                        break
            if not has_ref:
                continue
            for node in tree.nodes:
                if node.bl_idname == 'SSMTNode_PostProcess_ShapeKey':
                    if not hasattr(node, 'shapekey_variable_items'):
                        continue
                    for item in node.shapekey_variable_items:
                        name = getattr(item, 'shape_key_name', '').strip()
                        resolved = _resolve_shapekey_variable(item)
                        if name and resolved:
                            result[name] = resolved
        return result

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        var_map = self._find_parent_shapekey_map(tree)
        if not var_map:
            self.report({'INFO'}, "未在父级蓝图中找到形态键配置节点")
            return {'FINISHED'}
        matched = 0
        for item in node.shapekey_items:
            name = item.name.strip()
            if name in var_map:
                if item.variable != var_map[name]:
                    item.variable = var_map[name]
                    matched += 1
        self.report({'INFO'}, f"已刷新 {matched} 个形态键的变量名")
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimSample(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_sample"
    bl_label = "采样帧数值"
    bl_description = "对目标物体采样各形态键在每一帧的数值"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        obj_name = node.target_object.strip()
        obj = bpy.data.objects.get(obj_name)
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, f"目标物体 '{obj_name}' 不存在或不是网格物体")
            return {'CANCELLED'}
        if not obj.data.shape_keys:
            self.report({'ERROR'}, f"物体 '{obj_name}' 没有形态键")
            return {'CANCELLED'}
        start_frame = node.frame_start
        end_frame = node.frame_end
        if start_frame > end_frame:
            self.report({'ERROR'}, "起始帧不能大于结束帧")
            return {'CANCELLED'}
        shapekey_names = [item.name.strip() for item in node.shapekey_items if item.name.strip()]
        if not shapekey_names:
            self.report({'ERROR'}, "形态键列表为空，请先添加形态键名称")
            return {'CANCELLED'}
        current_frame = context.scene.frame_current
        original_values = {}
        for kb in obj.data.shape_keys.key_blocks:
            if kb.name.lower() != 'basis':
                original_values[kb.name] = kb.value
        for item in node.shapekey_items:
            item.frame_values_csv = ""
        for frame in range(start_frame, end_frame + 1):
            context.scene.frame_set(frame)
            for item in node.shapekey_items:
                name = item.name.strip()
                if not name:
                    continue
                kb = obj.data.shape_keys.key_blocks.get(name)
                if kb is None or kb.name.lower() == 'basis':
                    continue
                if item.frame_values_csv:
                    item.frame_values_csv += f",{kb.value:.3f}"
                else:
                    item.frame_values_csv = f"{kb.value:.3f}"
        for kb in obj.data.shape_keys.key_blocks:
            if kb.name in original_values:
                kb.value = original_values[kb.name]
        context.scene.frame_set(current_frame)
        frame_count = end_frame - start_frame + 1
        self.report({'INFO'}, f"已完成采样：{len(shapekey_names)} 个形态键 x {frame_count} 帧")
        return {'FINISHED'}


class SSMTNode_AnimDriver_ShapeKeySequence(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_ShapeKeySequence'
    bl_label = '形态键动画序列'
    bl_icon = 'SHAPEKEY_DATA'

    shapekey_items: CollectionProperty(
        type=ShapeKeyAnimItem,
        name="形态键列表",
    )

    shapekey_items_active: IntProperty(
        name="当前形态键",
        default=0,
    )

    frame_start: IntProperty(
        name="起始帧",
        description="采样的起始帧",
        default=1,
        min=0,
        max=9999,
    )

    frame_end: IntProperty(
        name="结束帧",
        description="采样的结束帧",
        default=30,
        min=0,
        max=9999,
    )

    target_object: StringProperty(
        name="目标物体",
        description="要采样形态键值的物体名称",
        default="",
    )

    driven_variable: StringProperty(
        name="驱动变量",
        description="此序列自动驱动的变量名，映射形态键帧数值到 $Freq_XXX 变量",
        default="",
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

    loop_playback: BoolProperty(
        name="循环播放",
        description="播放到最后一帧后自动从头开始循环",
        default=True,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_INPUT_SOCKET_NAME)
        self.outputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_OUTPUT_SOCKET_NAME)
        self.width = 350
        self._assign_next_available_index()
        self._ensure_paused_variable_name("shapekey_seq_paused")
        self._ensure_sequence_driver_variable_name()

    def copy(self, node):
        self._assign_next_available_index()
        self.custom_paused_var = ""
        self.driven_variable = ""
        self._ensure_paused_variable_name("shapekey_seq_paused")
        self._ensure_sequence_driver_variable_name()

    def _ensure_sequence_driver_variable_name(self):
        current_value = str(getattr(self, "driven_variable", "") or "").strip()
        normalized = normalize_variable_name(current_value)
        if normalized:
            self.driven_variable = f"${normalized}"
            return self.driven_variable
        allocated = self._allocate_unique_anim_driver_variable_name("shapekey_seq")
        self.driven_variable = f"${allocated}"
        return self.driven_variable

    def draw_buttons(self, context, layout):
        safe_idx = self._read_safe_index()
        box = layout.box()
        box.label(text=f"索引: {safe_idx}", icon='LINENUMBERS_ON')

        row = box.row(align=True)
        row.label(text="形态键:", icon='SHAPEKEY_DATA')
        op = row.operator("ssmt.shapekey_anim_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.shapekey_anim_remove", text="", icon='REMOVE')
        op.node_name = self.name

        if self.shapekey_items:
            box.template_list(
                "SSMT_UL_SHAPEKEY_ANIM_ITEMS", "",
                self, "shapekey_items",
                self, "shapekey_items_active",
                rows=max(2, min(len(self.shapekey_items), 6)),
            )

        box.separator()
        row = box.row(align=True)
        op = row.operator("ssmt.shapekey_anim_refresh", text="刷新变量", icon='FILE_REFRESH')
        op.node_name = self.name

        box.separator()
        box.label(text="采样设置:", icon='SETTINGS')
        box.prop(self, "target_object")
        row = box.row(align=True)
        row.prop(self, "frame_start")
        row.prop(self, "frame_end")
        op = row.operator("ssmt.shapekey_anim_sample", text="采样", icon='PLAY')
        op.node_name = self.name

        if any(item.frame_values_csv for item in self.shapekey_items):
            box.separator()
            box.label(text="帧数值一览:", icon='INFO')
            col = box.column(align=True)
            for item in self.shapekey_items:
                if item.name.strip() and item.frame_values_csv:
                    values = item.frame_values_csv.split(",")
                    display = ", ".join(v for v in values)
                    col.label(text=f"  {item.name}: {display}")

        box.separator()
        box.prop(self, "driven_variable")

        row = box.row(align=True)
        row.prop(self, "default_paused", text="默认播放")
        if self.custom_paused_var.strip():
            row.prop(self, "custom_paused_var", text="")
        else:
            row.label(text=f"$shapekey_seq_paused{safe_idx}")

        row = box.row(align=True)
        row.prop(self, "loop_playback", text="循环", icon='FILE_REFRESH')

        box.separator()
        box.label(text="连接状态:", icon='INFO')
        if self._has_linked_input():
            box.label(text="  [链输入] 已连接", icon='KEYFRAME')
        else:
            box.label(text="  [链输入] 未连接", icon='SNAP_FACE')
        if self._find_runtime_node() is not None:
            box.label(text="  [运行时间] 已连接", icon='TIME')
        else:
            box.label(text="  [运行时间] 未连接", icon='ERROR')
        if self._has_linked_output():
            box.label(text="  [链输出] 已连接（传递到下一节点）", icon='FORWARD')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        idx = self._read_safe_index()

        runtime = self._find_runtime_node()
        playback_rate = runtime.playback_rate if runtime else 1

        paused_state = self._resolve_default_play_state(self.default_paused)
        paused_var = self.custom_paused_var.strip()
        if not paused_var:
            paused_var = f"$shapekey_seq_paused{idx}"
        elif not paused_var.startswith('$'):
            paused_var = f"${paused_var}"

        drv = self.driven_variable.strip()
        if not drv:
            drv = f"$shapekey_seq{idx}"
        elif not drv.startswith('$'):
            drv = f"${drv}"

        items_with_data = [
            item for item in self.shapekey_items
            if item.name.strip() and item.variable.strip() and item.frame_values_csv.strip()
        ]

        if not items_with_data:
            return "\n".join([
                "[Constants]",
                self._format_global_assignment(f"$shapekey_seq_frame_count{idx}", 0, persist=True),
                self._format_global_assignment(f"$speed_auto{idx}", playback_rate, persist=True),
                "; 切换速度",
                self._format_global_assignment(paused_var, paused_state, persist=True),
                "; 暂停状态",
            ])

        frame_values = []
        for item in items_with_data:
            parts = item.frame_values_csv.split(",")
            var = item.variable.strip()
            if not var.startswith('$'):
                var = f"${var}"
            frame_values.append((var, parts))

        frame_count = max(len(parts) for _, parts in frame_values)

        lines = [
            "[Constants]",
            self._format_global_assignment(f"$speed_auto{idx}", playback_rate, persist=True),
            "; 切换速度（由运行时间的播放速率控制）",
            self._format_global_assignment(paused_var, paused_state, persist=True),
            "; 暂停状态",
            self._format_global_assignment(drv, 0, persist=True),
            "; 当前动画帧索引（自驱动）",
            self._format_global_assignment(f"$shapekey_seq_frame_count{idx}", frame_count, persist=True),
            "; 总帧数",
            "[Present]",
            f"; 形态键动画序列 - 自驱动 {drv}",
            f"if {paused_var} == 1",
            f"    if $swapvar % $speed_auto{idx} == 0",
            f"        {drv} = {drv} + 1.0",
        ]

        if self.loop_playback:
            lines.append(f"        if {drv} >= $shapekey_seq_frame_count{idx}")
            lines.append(f"            {drv} = 0")
            lines.append("        endif")
        else:
            lines.append(f"        if {drv} >= $shapekey_seq_frame_count{idx}")
            lines.append(f"            {drv} = $shapekey_seq_frame_count{idx} - 1.0")
            lines.append(f"            {paused_var} = 0")
            lines.append("        endif")

        for f_idx in range(frame_count - 1):
            keyword = "else if" if f_idx > 0 else "if"
            lines.append(f"        {keyword} {drv} < {float(f_idx + 1)}")
            for var, parts in frame_values:
                val = parts[f_idx] if f_idx < len(parts) else "0.000"
                lines.append(f"            {var} = {val}")

        keyword = "else if" if frame_count > 1 else "if"
        lines.append(f"        {keyword} {drv} >= 0")
        for var, parts in frame_values:
            val = parts[-1] if parts else "0.000"
            lines.append(f"            {var} = {val}")

        lines.extend([
            "        endif",
            "    endif",
            "endif",
        ])

        return "\n".join(lines)


_load_handler_registered = False


@bpy.app.handlers.persistent
def _shapekey_seq_load_handler(dummy):
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'SSMTBlueprintTreeType':
            continue
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_AnimDriver_ShapeKeySequence':
                try:
                    SSMTNode_AnimDriver_Base.migrate_default_play_state_flag(node)
                    SSMTNode_AnimDriver_Base._migrate_dynamic_sockets(node)
                    if not node.custom_paused_var:
                        node._ensure_indexed_paused_variable_name("shapekey_seq_paused")
                    if not node.driven_variable:
                        node._ensure_sequence_driver_variable_name()
                except Exception:
                    pass


classes = (
    ShapeKeyAnimItem,
    SSMT_UL_ShapeKeyAnimItems,
    SSMT_OT_ShapeKeyAnimAdd,
    SSMT_OT_ShapeKeyAnimRemove,
    SSMT_OT_ShapeKeyAnimRefresh,
    SSMT_OT_ShapeKeyAnimSample,
    SSMTNode_AnimDriver_ShapeKeySequence,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_shapekey_seq_load_handler)
        _load_handler_registered = True


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_shapekey_seq_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
