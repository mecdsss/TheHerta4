from collections import deque
import time

import bpy
from bpy.props import IntProperty, StringProperty, CollectionProperty
from bpy.types import NodeSocket, Node

from .node_base import SSMTNodeBase
from .variable_registry import allocate_continuous_shapekey_index_variable_name, mark_variable_name_used, normalize_variable_name


class DrivenVariableItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="驱动变量",
        description="要驱动的变量名称（如 $myVar）",
        default="",
    )


class ContinuousShapeKeyItem(bpy.types.PropertyGroup):
    shape_key_name: StringProperty(
        name="形态键名称",
        description="连续形态键名称",
        default="",
    )

    variable_name: StringProperty(
        name="导出变量",
        description="对应的预分配导出变量",
        default="",
    )


class SSMT_UL_DrivenVariables(bpy.types.UIList):
    bl_idname = "SSMT_UL_DRIVEN_VARIABLES"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)


class SSMT_UL_ContinuousShapeKeys(bpy.types.UIList):
    bl_idname = "SSMT_UL_CONTINUOUS_SHAPEKEYS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'SHAPEKEY_DATA' if item.shape_key_name else 'ERROR'
            row.label(text=item.shape_key_name or "<未命名>", icon=icon_val)
            row.label(text=item.variable_name or "<未匹配变量>", icon='VIEWZOOM')


class SSMT_OT_DrivenVariableAdd(bpy.types.Operator):
    bl_idname = "ssmt.driven_variable_add"
    bl_label = "添加驱动变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.driven_variable_list.add()
        item.variable_name = "$driven_var"
        node.driven_variable_list_active = len(node.driven_variable_list) - 1
        return {'FINISHED'}


class SSMT_OT_DrivenVariableRemove(bpy.types.Operator):
    bl_idname = "ssmt.driven_variable_remove"
    bl_label = "删除驱动变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.driven_variable_list_active
        if 0 <= idx < len(node.driven_variable_list):
            node.driven_variable_list.remove(idx)
            node.driven_variable_list_active = min(idx, len(node.driven_variable_list) - 1)
        return {'FINISHED'}


class SSMT_OT_ContinuousShapeKeyRefresh(bpy.types.Operator):
    bl_idname = "ssmt.continuous_shapekey_refresh"
    bl_label = "刷新连续形态键"
    bl_description = "按物体上的形态键顺序自动读取连续形态键，并匹配父级形态键配置节点中的预分配变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        if not hasattr(node, "continuous_target_object") or not hasattr(node, "continuous_shape_key_items"):
            self.report({'WARNING'}, "当前节点不支持连续形态键刷新")
            return {'CANCELLED'}

        obj_name = str(getattr(node, "continuous_target_object", "") or "").strip()
        if not obj_name:
            self.report({'WARNING'}, "请先指定目标物体")
            return {'CANCELLED'}

        obj = bpy.data.objects.get(obj_name)
        if not obj or getattr(obj, "type", "") != "MESH":
            self.report({'WARNING'}, f"目标物体 '{obj_name}' 不存在或不是网格物体")
            return {'CANCELLED'}

        key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
        if not key_blocks or len(key_blocks) <= 1:
            self.report({'WARNING'}, f"物体 '{obj_name}' 没有可用的非 Basis 形态键")
            return {'CANCELLED'}

        var_map = SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map(tree)
        item_count, missing_count = SSMTNode_AnimDriver_Base._rebuild_continuous_shape_key_items(
            node,
            key_blocks,
            var_map,
        )

        if item_count <= 0:
            self.report({'WARNING'}, "未找到可用的连续形态键")
            return {'CANCELLED'}

        if missing_count > 0:
            self.report({'WARNING'}, f"已导入 {item_count} 个连续形态键，其中 {missing_count} 个未匹配到预分配变量")
        else:
            self.report({'INFO'}, f"已导入 {item_count} 个连续形态键，并完成预分配变量匹配")
        return {'FINISHED'}


_picking_continuous_target_node_name = None
_picking_continuous_target_tree_name = None
_PICK_CONTINUOUS_TARGET_TIMEOUT_SECONDS = 30.0


class SSMT_OT_StartPickContinuousTargetObject(bpy.types.Operator):
    bl_idname = "ssmt.start_pick_continuous_target_object"
    bl_label = "Pick Continuous Target Object"
    bl_description = "点击后在3D视图中选择一个目标物体，写入连续形态键模式的目标物体"

    node_name: StringProperty(default="")

    def execute(self, context):
        global _picking_continuous_target_node_name, _picking_continuous_target_tree_name

        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            self.report({'WARNING'}, "无法获取当前动画驱动蓝图")
            return {'CANCELLED'}

        _picking_continuous_target_node_name = self.node_name
        _picking_continuous_target_tree_name = tree.name
        self.report({'INFO'}, "请在3D视图中点击选择一个网格物体")
        bpy.ops.ssmt.pick_continuous_target_object_modal('INVOKE_DEFAULT')
        return {'FINISHED'}


class SSMT_OT_PickContinuousTargetObjectModal(bpy.types.Operator):
    bl_idname = "ssmt.pick_continuous_target_object_modal"
    bl_label = "Pick Continuous Target Object"
    bl_options = {'REGISTER', 'INTERNAL'}

    def _finish(self, context, status, clear_globals=True):
        global _picking_continuous_target_node_name, _picking_continuous_target_tree_name

        timer = getattr(self, "_timer", None)
        if timer is not None:
            context.window_manager.event_timer_remove(timer)
            self._timer = None

        if clear_globals:
            _picking_continuous_target_node_name = None
            _picking_continuous_target_tree_name = None

        return status

    @staticmethod
    def _get_current_selected_object(context):
        active_obj = getattr(getattr(context, "view_layer", None), "objects", None)
        active_obj = getattr(active_obj, "active", None)
        if active_obj and active_obj in getattr(context, "selected_objects", []):
            return active_obj

        selected_objects = getattr(context, "selected_objects", []) or []
        if selected_objects:
            return selected_objects[0]
        return None

    def _try_apply_selected_object(self, context):
        global _picking_continuous_target_node_name, _picking_continuous_target_tree_name

        current_obj = self._get_current_selected_object(context)
        if current_obj is None:
            return None

        if current_obj == self._last_selected_obj and current_obj in self._initial_selected_objs:
            return None

        tree = getattr(getattr(bpy, "data", None), "node_groups", {}).get(_picking_continuous_target_tree_name)
        if tree is None:
            self.report({'WARNING'}, "动画驱动蓝图已失效，已取消吸管选择")
            return self._finish(context, {'CANCELLED'})

        node = tree.nodes.get(_picking_continuous_target_node_name)
        if node is None:
            self.report({'WARNING'}, "目标驱动节点已不存在，已取消吸管选择")
            return self._finish(context, {'CANCELLED'})

        if getattr(current_obj, "type", "") != "MESH":
            self.report({'WARNING'}, f"物体 '{current_obj.name}' 不是网格物体")
            return self._finish(context, {'CANCELLED'})

        node.continuous_target_object = current_obj.name
        self.report({'INFO'}, f"已选择目标物体: {current_obj.name}")
        return self._finish(context, {'FINISHED'})

    def invoke(self, context, event):
        if not _picking_continuous_target_node_name:
            return {'CANCELLED'}

        self._initial_selected_objs = set(getattr(context, "selected_objects", []) or [])
        self._last_selected_obj = self._get_current_selected_object(context)
        self._started_at = time.monotonic()
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if not _picking_continuous_target_node_name or not _picking_continuous_target_tree_name:
            return self._finish(context, {'CANCELLED'}, clear_globals=False)

        if time.monotonic() - self._started_at > _PICK_CONTINUOUS_TARGET_TIMEOUT_SECONDS:
            self.report({'WARNING'}, "吸管选择超时，已自动取消")
            return self._finish(context, {'CANCELLED'})

        if event.type in {'ESC', 'RIGHTMOUSE'}:
            return self._finish(context, {'CANCELLED'})

        if event.type == 'TIMER':
            result = self._try_apply_selected_object(context)
            if result is not None:
                return result
            return {'RUNNING_MODAL'}

        if event.type in {
            'LEFTMOUSE',
            'MIDDLEMOUSE',
            'MOUSEMOVE',
            'WHEELUPMOUSE',
            'WHEELDOWNMOUSE',
            'TRACKPADPAN',
            'TRACKPADZOOM',
        }:
            return {'PASS_THROUGH'}

        return {'RUNNING_MODAL'}


class SSMT_OT_ContinuousShapeKeyRemove(bpy.types.Operator):
    bl_idname = "ssmt.continuous_shapekey_remove"
    bl_label = "删除连续形态键"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node or not hasattr(node, "continuous_shape_key_items"):
            return {'CANCELLED'}

        idx = int(getattr(node, "continuous_shape_key_items_active", -1))
        if 0 <= idx < len(node.continuous_shape_key_items):
            node.continuous_shape_key_items.remove(idx)
            node.continuous_shape_key_items_active = min(idx, len(node.continuous_shape_key_items) - 1)
            return {'FINISHED'}

        self.report({'WARNING'}, "当前没有可删除的连续形态键")
        return {'CANCELLED'}


class SSMT_OT_ContinuousShapeKeyClear(bpy.types.Operator):
    bl_idname = "ssmt.continuous_shapekey_clear"
    bl_label = "清空连续形态键"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node or not hasattr(node, "continuous_shape_key_items"):
            return {'CANCELLED'}

        while len(node.continuous_shape_key_items) > 0:
            node.continuous_shape_key_items.remove(len(node.continuous_shape_key_items) - 1)
        node.continuous_shape_key_items_active = 0
        return {'FINISHED'}


class SSMTSocketAnimDriver(NodeSocket):
    bl_idname = 'SSMTSocketAnimDriver'
    bl_label = 'Anim Driver Socket'

    def draw_color(self, context, node):
        return (0.2, 0.7, 0.6, 1.0)

    def draw(self, context, layout, node, text):
        layout.label(text=text)


class SSMTNode_AnimDriver_Base(SSMTNodeBase):
    bl_idname = 'SSMTNode_AnimDriver_Base'
    bl_label = 'AnimDriver Base'

    auto_index: bpy.props.IntProperty(
        name="自动索引",
        default=0,
        options={'HIDDEN'},
    )

    continuous_index_var_initialized: bpy.props.BoolProperty(
        name="Continuous Index Variable Initialized",
        default=False,
        options={'HIDDEN'},
    )

    def _ensure_initial_visible_continuous_index_variable_name(self, context=None):
        if getattr(self, "continuous_index_var_initialized", False):
            return False

        assigned_name = self._ensure_continuous_index_variable_name(context=context)
        if not assigned_name:
            return False

        if str(getattr(self, "custom_continuous_index_variable_name", "") or "").strip():
            self.continuous_index_var_initialized = True
            return False

        self.continuous_index_var_initialized = True
        self.custom_continuous_index_variable_name = assigned_name
        return True

    def update_continuous_index_variable_name(self, context):
        if self._ensure_initial_visible_continuous_index_variable_name(context=context):
            return
        normalized = normalize_variable_name(self.custom_continuous_index_variable_name)
        if normalized != str(self.custom_continuous_index_variable_name or "").strip().lstrip("$"):
            self.custom_continuous_index_variable_name = normalized
            return
        if normalized:
            mark_variable_name_used(normalized, context=context)
        self._ensure_continuous_index_variable_name(context=context)
        self.update_node_width([
            getattr(self, "custom_continuous_index_variable_name", ""),
            getattr(self, "assigned_continuous_index_variable_name", ""),
        ])

    custom_continuous_index_variable_name: bpy.props.StringProperty(
        name="连续索引变量",
        description="连续形态键模式的主索引变量名；创建时会自动填入预分配变量名，可直接复制或手动修改。",
        default="",
        update=update_continuous_index_variable_name,
    )

    assigned_continuous_index_variable_name: bpy.props.StringProperty(
        name="Assigned Continuous Index Variable Name",
        description="Preallocated primary variable name for continuous shape key mode.",
        default="",
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, ntree):
        if ntree.bl_idname != 'SSMTBlueprintTreeType':
            return False
        return ntree.get("is_animation_driver", False)

    def generate_ini_segment(self, connected_nodes=None) -> str:
        raise NotImplementedError("子类必须实现 generate_ini_segment 方法")

    @staticmethod
    def _resolve_shapekey_variable(item):
        custom = str(getattr(item, 'custom_variable_name', '') or '').strip()
        if custom:
            return custom[1:] if custom.startswith('$') else custom
        assigned = str(getattr(item, 'assigned_variable_name', '') or '').strip()
        if assigned:
            return assigned[1:] if assigned.startswith('$') else assigned
        return ""

    @staticmethod
    def _find_parent_shapekey_variable_map(current_tree) -> dict:
        result = {}
        for tree in getattr(getattr(bpy, "data", None), "node_groups", []):
            if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
                continue
            if tree.get("is_animation_driver"):
                continue

            has_ref = False
            for node in getattr(tree, "nodes", []):
                if getattr(node, "bl_idname", "") == 'SSMTNode_PostProcess_AnimDriver':
                    if getattr(node, 'blueprint_name', '') == getattr(current_tree, "name", ""):
                        has_ref = True
                        break
            if not has_ref:
                continue

            for node in getattr(tree, "nodes", []):
                if getattr(node, "bl_idname", "") != 'SSMTNode_PostProcess_ShapeKey':
                    continue
                for item in getattr(node, 'shapekey_variable_items', []):
                    name = str(getattr(item, 'shape_key_name', '') or '').strip()
                    resolved = SSMTNode_AnimDriver_Base._resolve_shapekey_variable(item)
                    if name and resolved:
                        result[name] = f"${resolved}"
        return result

    @staticmethod
    def _rebuild_continuous_shape_key_items(node, key_blocks, var_map) -> tuple[int, int]:
        prefix_filter = str(getattr(node, "continuous_shape_key_prefix_filter", "") or "").strip()
        shape_key_names = []
        for index, key_block in enumerate(key_blocks):
            key_name = str(getattr(key_block, "name", "") or "").strip()
            if not key_name:
                continue
            if index == 0 or key_name.lower() == "basis":
                continue
            if prefix_filter and not key_name.startswith(prefix_filter):
                continue
            shape_key_names.append(key_name)

        while len(node.continuous_shape_key_items) > 0:
            node.continuous_shape_key_items.remove(len(node.continuous_shape_key_items) - 1)

        missing_count = 0
        for key_name in shape_key_names:
            item = node.continuous_shape_key_items.add()
            item.shape_key_name = key_name
            item.variable_name = var_map.get(key_name, "")
            if not item.variable_name:
                missing_count += 1

        return len(shape_key_names), missing_count

    def _get_continuous_primary_var(self):
        assigned_name = self._ensure_continuous_index_variable_name()
        custom_name = normalize_variable_name(getattr(self, "custom_continuous_index_variable_name", "") or "")
        resolved_name = custom_name or assigned_name
        return f"${resolved_name}"

    def _get_continuous_shape_key_entries(self):
        result = []
        for offset, item in enumerate(getattr(self, "continuous_shape_key_items", [])):
            key_name = str(getattr(item, "shape_key_name", "") or "").strip()
            variable_name = str(getattr(item, "variable_name", "") or "").strip()
            if not key_name or not variable_name:
                continue
            if not variable_name.startswith('$'):
                variable_name = f"${variable_name}"
            result.append((offset, key_name, variable_name))
        return result

    def _append_continuous_shape_key_mapping_lines(self, lines, primary_var, indent="        "):
        entries = self._get_continuous_shape_key_entries()
        if not entries:
            return

        frame_start = float(getattr(self, "frame_start", 0.0) or 0.0)
        lines.append(f"{indent}; --- 连续形态键映射 ---")
        for offset, shape_key_name, variable_name in entries:
            threshold = frame_start + offset
            lines.append(f"{indent}{variable_name} = {primary_var} - {threshold}")
            lines.append(f"{indent}if {variable_name} < 0")
            lines.append(f"{indent}    {variable_name} = 0")
            lines.append(f"{indent}endif")
            lines.append(f"{indent}if {variable_name} > 1")
            lines.append(f"{indent}    {variable_name} = 1")
            lines.append(f"{indent}endif")
            lines.append(f"{indent}; {shape_key_name}")

    def _get_continuous_primary_initial_value(self):
        frame_start = float(getattr(self, "frame_start", 0.0) or 0.0)
        frame_end = float(getattr(self, "frame_end", frame_start) or frame_start)
        reverse_playback = bool(getattr(self, "reverse_playback", False))
        return frame_end if reverse_playback else frame_start

    def _draw_continuous_shape_key_controls(self, box):
        row = box.row(align=True)
        row.prop_search(self, "continuous_target_object", bpy.data, "objects", text="目标物体", icon='OBJECT_DATA')
        op = row.operator("ssmt.start_pick_continuous_target_object", text="", icon='EYEDROPPER')
        op.node_name = self.name
        target_object_name = str(getattr(self, "continuous_target_object", "") or "").strip()
        if target_object_name:
            op = row.operator("ssmt.select_node_object", text="", icon='RESTRICT_SELECT_OFF')
            op.object_name = target_object_name

        box.prop(self, "continuous_shape_key_prefix_filter", text="前缀过滤")

        box.prop(self, "custom_continuous_index_variable_name", text="索引变量")
        assigned_name = normalize_variable_name(getattr(self, "assigned_continuous_index_variable_name", "") or "")
        if not str(getattr(self, "custom_continuous_index_variable_name", "") or "").strip() and assigned_name:
            box.label(text=f"预分配变量: ${assigned_name}", icon='INFO')

        row = box.row(align=True)
        op = row.operator("ssmt.continuous_shapekey_refresh", text="刷新", icon='FILE_REFRESH')
        op.node_name = self.name
        op = row.operator("ssmt.continuous_shapekey_remove", text="", icon='REMOVE')
        op.node_name = self.name
        op = row.operator("ssmt.continuous_shapekey_clear", text="", icon='TRASH')
        op.node_name = self.name

        if self.continuous_shape_key_items:
            box.template_list(
                "SSMT_UL_CONTINUOUS_SHAPEKEYS", "",
                self, "continuous_shape_key_items",
                self, "continuous_shape_key_items_active",
                rows=max(2, min(len(self.continuous_shape_key_items), 6)),
            )
            box.label(text="可刷新后按需删减，用于保留当前动画段所需的连续形态键", icon='INFO')
        else:
            box.label(text="指定物体后点击刷新，自动读取连续形态键和预分配变量", icon='INFO')

    def _get_indexed_nodes(self, tree):
        result = []
        for n in tree.nodes:
            try:
                _ = n.auto_index
                result.append(n)
            except Exception:
                pass
        return result

    def _assign_auto_index(self):
        tree = self.id_data
        if not tree:
            self.auto_index = 1
            return
        all_indexed = sorted(
            self._get_indexed_nodes(tree),
            key=lambda n: n.name
        )
        for i, n in enumerate(all_indexed, 1):
            n.auto_index = i

    def _ensure_valid_index(self):
        tree = self.id_data
        if not tree:
            return
        all_indexed = self._get_indexed_nodes(tree)
        indices = [n.auto_index for n in all_indexed]
        if len(set(indices)) != len(indices) or any(i <= 0 for i in indices):
            sorted_nodes = sorted(all_indexed, key=lambda n: n.name)
            for i, n in enumerate(sorted_nodes, 1):
                n.auto_index = i

    def _read_safe_index(self):
        """只读方式获取 auto_index，不会写入 ID 属性。
        用于 draw 回调等不允许写入的上下文。"""
        tree = self.id_data
        if not tree:
            return self.auto_index or 1
        all_indexed = self._get_indexed_nodes(tree)
        indices = [n.auto_index for n in all_indexed]
        if len(set(indices)) != len(indices) or any(i <= 0 for i in indices):
            # 索引无效，按名称排序计算本节点应有的索引
            sorted_nodes = sorted(all_indexed, key=lambda n: n.name)
            for i, n in enumerate(sorted_nodes, 1):
                if n == self:
                    return i
            return 1
        return self.auto_index

    def _assign_next_available_index(self):
        self.auto_index = 1
        tree = self.id_data
        if not tree:
            return
        existing = {n.auto_index for n in self._get_indexed_nodes(tree) if n != self}
        while self.auto_index in existing:
            self.auto_index += 1

    def _ensure_continuous_index_variable_name(self, context=None):
        owned_names = (
            getattr(self, "assigned_continuous_index_variable_name", ""),
            getattr(self, "custom_continuous_index_variable_name", ""),
        )
        assigned_name = normalize_variable_name(getattr(self, "assigned_continuous_index_variable_name", "") or "")
        if not assigned_name:
            assigned_name = allocate_continuous_shapekey_index_variable_name(
                preferred=getattr(self, "custom_continuous_index_variable_name", ""),
                context=context,
                owned_names=owned_names,
            )
            self.assigned_continuous_index_variable_name = assigned_name

        custom_name = normalize_variable_name(getattr(self, "custom_continuous_index_variable_name", "") or "")
        if not custom_name:
            self.custom_continuous_index_variable_name = assigned_name
        return assigned_name

    def update(self):
        if getattr(self, "use_continuous_shapekey_mode", False):
            self._ensure_initial_visible_continuous_index_variable_name()

    def _get_chain_links(self):
        tree = self.id_data
        if not tree:
            return [], []
        upstream = []
        downstream = []
        for link in tree.links:
            if link.to_node == self and link.to_socket.name == '链输入':
                upstream.append(link.from_node)
            if link.from_node == self and link.from_socket.name == '链输出':
                downstream.append(link.to_node)
        return upstream, downstream

    def _get_chain_position(self):
        upstream, downstream = self._get_chain_links()
        if upstream and downstream:
            return 'intermediate'
        elif upstream and not downstream:
            return 'last'
        elif not upstream and downstream:
            return 'first'
        else:
            return 'alone'

    def _is_intermediate_play_node(self):
        return self._get_chain_position() == 'intermediate'

    def _is_last_in_chain(self):
        return self._get_chain_position() == 'last'

    def _get_next_node_in_chain(self):
        _, downstream = self._get_chain_links()
        return downstream[0] if downstream else None

    def _get_next_paused_var(self):
        next_node = self._get_next_node_in_chain()
        if next_node and hasattr(next_node, 'custom_paused_var'):
            var = next_node.custom_paused_var.strip()
            if var:
                if not var.startswith('$'):
                    var = f"${var}"
                return var
        return None

    def _is_play_node(self, node):
        """判断节点是否为播放节点（索引播放、往返播放、形态键动画序列等）"""
        return (hasattr(node, 'driven_variable') or hasattr(node, 'driven_variable_list')) and hasattr(node, 'custom_paused_var')

    def _collect_upstream_play_pause_vars(self):
        tree = self.id_data
        if not tree:
            return []
        result = []
        visited = {self.name}
        queue = deque([self])
        while queue:
            current = queue.popleft()
            for link in tree.links:
                if link.to_node == current and getattr(link.to_socket, 'bl_idname', '') == 'SSMTSocketAnimDriver':
                    upstream_node = link.from_node
                    if upstream_node.name not in visited:
                        visited.add(upstream_node.name)
                        if self._is_play_node(upstream_node):
                            var = upstream_node.custom_paused_var.strip()
                            if var:
                                if not var.startswith('$'):
                                    var = f"${var}"
                                if var not in result:
                                    result.append(var)
                        queue.append(upstream_node)
        return result

    def _collect_downstream_pause_vars(self):
        tree = self.id_data
        if not tree:
            return []
        result = []
        visited = {self.name}
        queue = deque([self])
        while queue:
            current = queue.popleft()
            for link in tree.links:
                if link.from_node == current and getattr(link.from_socket, 'bl_idname', '') == 'SSMTSocketAnimDriver':
                    downstream_node = link.to_node
                    if downstream_node.name not in visited:
                        visited.add(downstream_node.name)
                        if self._is_play_node(downstream_node):
                            var = downstream_node.custom_paused_var.strip()
                            if var:
                                if not var.startswith('$'):
                                    var = f"${var}"
                                if var not in result:
                                    result.append(var)
                        queue.append(downstream_node)
        return result

    def _find_runtime_node(self):
        tree = self.id_data
        if not tree:
            return None
        for link in tree.links:
            if link.to_node == self and link.to_socket.name == '时间输入':
                node = link.from_node
                if hasattr(node, 'fps') and hasattr(node, 'playback_rate'):
                    return node
        visited = {self.name}
        queue = deque([self])
        while queue:
            current = queue.popleft()
            if hasattr(current, 'fps') and hasattr(current, 'playback_rate'):
                return current
            for link in tree.links:
                if link.to_node == current and link.to_socket.name == '时间输入':
                    upstream_node = link.from_node
                    if upstream_node.name not in visited:
                        visited.add(upstream_node.name)
                        queue.append(upstream_node)
        visited = {self.name}
        queue = deque([self])
        while queue:
            current = queue.popleft()
            if hasattr(current, 'fps') and hasattr(current, 'playback_rate'):
                return current
            for link in tree.links:
                if link.to_node == current and link.to_socket.name == '链输入':
                    upstream_node = link.from_node
                    if upstream_node.name not in visited:
                        visited.add(upstream_node.name)
                        queue.append(upstream_node)
        return None

    @staticmethod
    def _migrate_play_sockets(node):
        required_inputs = ["链输入", "时间输入", "驱动输入"]
        required_outputs = ["链输出", "时间输出"]
        SSMTNode_AnimDriver_Base._migrate_node_sockets(node, required_inputs, required_outputs)

    @staticmethod
    def _migrate_base_sockets(node):
        required_inputs = ["链输入"]
        required_outputs = ["链输出"]
        SSMTNode_AnimDriver_Base._migrate_node_sockets(node, required_inputs, required_outputs)

    @staticmethod
    def _migrate_controlled_sockets(node):
        required_inputs = ["链输入", "时间输入", "驱动输入"]
        required_outputs = ["链输出"]
        SSMTNode_AnimDriver_Base._migrate_node_sockets(node, required_inputs, required_outputs)

    @staticmethod
    def _migrate_node_sockets(node, required_inputs, required_outputs):
        existing_input_names = {s.name for s in node.inputs}
        existing_output_names = {s.name for s in node.outputs}
        for name in required_inputs:
            if name not in existing_input_names:
                node.inputs.new('SSMTSocketAnimDriver', name)
        for name in required_outputs:
            if name not in existing_output_names:
                node.outputs.new('SSMTSocketAnimDriver', name)


classes = (
    DrivenVariableItem,
    ContinuousShapeKeyItem,
    SSMT_UL_DrivenVariables,
    SSMT_UL_ContinuousShapeKeys,
    SSMT_OT_DrivenVariableAdd,
    SSMT_OT_DrivenVariableRemove,
    SSMT_OT_ContinuousShapeKeyRefresh,
    SSMT_OT_StartPickContinuousTargetObject,
    SSMT_OT_PickContinuousTargetObjectModal,
    SSMT_OT_ContinuousShapeKeyRemove,
    SSMT_OT_ContinuousShapeKeyClear,
    SSMTSocketAnimDriver,
    SSMTNode_AnimDriver_Base,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
