from collections import deque

import bpy
from bpy.props import IntProperty, StringProperty, CollectionProperty
from bpy.types import NodeSocket, Node

from .node_base import SSMTNodeBase


class DrivenVariableItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="驱动变量",
        description="要驱动的变量名称（如 $myVar）",
        default="",
    )


class SSMT_UL_DrivenVariables(bpy.types.UIList):
    bl_idname = "SSMT_UL_DRIVEN_VARIABLES"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)


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

    @classmethod
    def poll(cls, ntree):
        if ntree.bl_idname != 'SSMTBlueprintTreeType':
            return False
        return ntree.get("is_animation_driver", False)

    def generate_ini_segment(self, connected_nodes=None) -> str:
        raise NotImplementedError("子类必须实现 generate_ini_segment 方法")

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
    SSMT_UL_DrivenVariables,
    SSMT_OT_DrivenVariableAdd,
    SSMT_OT_DrivenVariableRemove,
    SSMTSocketAnimDriver,
    SSMTNode_AnimDriver_Base,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)