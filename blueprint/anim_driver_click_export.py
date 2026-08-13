import bpy
from bpy.props import IntProperty, StringProperty, CollectionProperty

from .anim_driver_base import (
    ANIM_DRIVER_INPUT_SOCKET_NAME,
    ANIM_DRIVER_OUTPUT_SOCKET_NAME,
    SSMTNode_AnimDriver_Base,
)
from .node_postprocess_draginteraction import (
    DEFAULT_MOD_NAMESPACE,
    MAX_ZONES,
    is_postprocess_node_on_export_chain,
)
from .variable_registry import normalize_variable_name


class ClickExportTargetItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="受控变量",
        description="要写入区域点击次数的变量名；与物体切换节点的变量名一致即可驱动该切换（可从切换节点复制其预分配变量名）",
        default="",
    )


class SSMT_UL_ClickExportTargets(bpy.types.UIList):
    bl_idname = "SSMT_UL_CLICK_EXPORT_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)


class SSMT_OT_ClickExportTargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.click_export_target_add"
    bl_label = "添加受控变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.click_target_list.add()
        item.variable_name = ""
        node.click_target_active = len(node.click_target_list) - 1
        return {'FINISHED'}


class SSMT_OT_ClickExportTargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.click_export_target_remove"
    bl_label = "删除受控变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.click_target_active
        if 0 <= idx < len(node.click_target_list):
            node.click_target_list.remove(idx)
            node.click_target_active = min(idx, len(node.click_target_list) - 1)
        return {'FINISHED'}


class SSMT_OT_ClickExportCycleRefresh(bpy.types.Operator):
    bl_idname = "ssmt.click_export_cycle_refresh"
    bl_label = "刷新循环档数"
    bl_description = "按受控变量列表查找对应物体切换节点，取其选项数的最大值作为循环档数"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        best, matched = node._compute_cycle_from_swaps()
        node.cycle_length = best
        if matched:
            self.report({'INFO'}, f"已获取循环档数 {best}（匹配 {matched} 个物体切换节点）")
        else:
            self.report({'WARNING'}, "未匹配到物体切换节点，循环档数已置 0（跟随形态键推导）")
        return {'FINISHED'}


class SSMTNode_AnimDriver_ClickExport(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_ClickExport'
    bl_label = '点击计数导出'
    bl_icon = 'DRIVER'

    click_zone_id: IntProperty(
        name="绑定区域编号",
        description="拖拽交互节点区域空物体列表中的稳定区域 ID；仅命中模式下按住左键/X 点击该区域递增计数",
        default=0, min=0, max=MAX_ZONES - 1,
    )

    cycle_length: IntProperty(
        name="循环档数",
        description="该区域点击次数的循环长度（0..档数-1）。0=跟随形态键点击档位推导；点右侧刷新按物体切换节点选项数自动获取（多个变量取最大值）。会同步改变该区域形态键点击档位的循环长度",
        default=0, min=0, max=64,
    )

    click_target_list: CollectionProperty(
        type=ClickExportTargetItem,
        name="受控变量列表",
    )

    click_target_active: IntProperty(
        name="当前受控变量",
        default=0,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_INPUT_SOCKET_NAME)
        self.outputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_OUTPUT_SOCKET_NAME)
        self.width = 300

    # ------------------------------------------------------------------
    # 数据收集
    # ------------------------------------------------------------------

    def _iter_target_vars(self):
        """归一化受控变量列表：['$a', '$b']，过滤空项并保序去重。"""
        result = []
        seen = set()
        for item in self.click_target_list:
            name = normalize_variable_name(getattr(item, "variable_name", "") or "")
            if not name:
                continue
            var = f"${name}"
            if var in seen:
                continue
            seen.add(var)
            result.append(var)
        return result

    def _find_anim_owner_trees(self):
        """回溯引用本动画驱动树的主蓝图树列表。"""
        anim_tree = getattr(self, "id_data", None)
        if anim_tree is None:
            return []
        owners = []
        for tree in bpy.data.node_groups:
            if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
                continue
            if tree.get("is_animation_driver"):
                continue
            for node in getattr(tree, "nodes", None) or []:
                if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_AnimDriver":
                    continue
                if getattr(node, "mute", False):
                    continue
                if not is_postprocess_node_on_export_chain(tree, node):
                    continue
                if str(getattr(node, "blueprint_name", "") or "") != anim_tree.name:
                    continue
                owners.append(tree)
                break
        return owners

    def _find_drag_drive_nodes(self):
        """在关联主树中查找开启形态键驱动的拖拽交互节点。"""
        candidates = []
        for tree in self._find_anim_owner_trees():
            for candidate in getattr(tree, "nodes", None) or []:
                if getattr(candidate, "bl_idname", "") == "SSMTNode_PostProcess_DragInteraction" \
                        and not getattr(candidate, "mute", False) \
                        and is_postprocess_node_on_export_chain(tree, candidate) \
                        and getattr(candidate, "enable_shapekey_drive", False):
                    candidates.append(candidate)
        return candidates

    def _find_drag_drive_node(self):
        candidates = self._find_drag_drive_nodes()
        return candidates[0] if len(candidates) == 1 else None

    def _compute_cycle_from_swaps(self):
        """按受控变量查找物体切换节点，返回 (最大选项数, 匹配到的节点数)。"""
        best = 0
        matched = 0
        for var in self._iter_target_vars():
            target = var.lstrip("$")
            for tree in bpy.data.node_groups:
                if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
                    continue
                for node in getattr(tree, "nodes", None) or []:
                    if getattr(node, "bl_idname", "") != "SSMTNode_ObjectSwap" or getattr(node, "mute", False):
                        continue
                    candidates = set()
                    custom = str(getattr(node, "custom_var_name", "") or "").strip().lstrip("$")
                    if custom:
                        candidates.add(custom)
                    assigned = str(getattr(node, "assigned_variable_name", "") or "").strip().lstrip("$")
                    if assigned:
                        candidates.add(assigned)
                    if target not in candidates:
                        continue
                    try:
                        count = int(getattr(node, "input_slot_count", 0) or 0)
                    except Exception:
                        count = 0
                    if count > 0:
                        matched += 1
                        best = max(best, count)
        return best, matched

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.prop(self, "click_zone_id")
        row = box.row(align=True)
        row.prop(self, "cycle_length")
        op = row.operator("ssmt.click_export_cycle_refresh", text="", icon='FILE_REFRESH')
        op.node_name = self.name

        box.separator()
        row = box.row(align=True)
        row.label(text="受控变量:", icon='VIEWZOOM')
        op = row.operator("ssmt.click_export_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.click_export_target_remove", text="", icon='REMOVE')
        op.node_name = self.name

        if self.click_target_list:
            box.template_list(
                "SSMT_UL_CLICK_EXPORT_TARGETS", "",
                self, "click_target_list",
                self, "click_target_active",
                rows=max(2, min(len(self.click_target_list), 6)),
            )
        else:
            box.label(text="添加受控变量（与物体切换节点变量名一致即联动）", icon='INFO')

        drag_nodes = self._find_drag_drive_nodes()
        if not drag_nodes:
            box.label(text="警告: 未找到同模组开启形态键驱动的拖拽交互节点", icon='ERROR')
        elif len(drag_nodes) > 1:
            box.label(text="警告: 找到多个拖拽所有者，请只保留一个", icon='ERROR')

    # ------------------------------------------------------------------
    # INI 段生成
    # ------------------------------------------------------------------

    def generate_ini_segment(self, connected_nodes=None) -> str:
        target_vars = self._iter_target_vars()
        if not target_vars:
            return ""
        drag_node = self._find_drag_drive_node()
        if drag_node is None:
            return ""
        try:
            ns = drag_node._resolve_namespace("")
        except Exception:
            ns = DEFAULT_MOD_NAMESPACE
        try:
            zone = int(getattr(self, "click_zone_id", 0) or 0)
        except (TypeError, ValueError):
            return ""
        if not 0 <= zone < MAX_ZONES:
            return ""
        lines = [
            "[Present]",
            "; 点击计数导出：boot 且冷启动播种完成后，每帧把绑定区域的点击次数写入受控变量",
            "; （点击次数是持久状态，不做模式门控；播种完成前不发布，防止缓冲清零态冲刷 persist 变量）",
            f"if $ssmtdrag_booted_{ns} == 1 && $ssmtdrag_seed_pending_{ns} == 0",
        ]
        for var in target_vars:
            lines.append(f"\tstore = {var}, ResourceDragShapeKeyClickCountF_{ns}, {zone}")
        lines.append("endif")
        return "\n".join(lines)


classes = (
    ClickExportTargetItem,
    SSMT_UL_ClickExportTargets,
    SSMT_OT_ClickExportTargetAdd,
    SSMT_OT_ClickExportTargetRemove,
    SSMT_OT_ClickExportCycleRefresh,
    SSMTNode_AnimDriver_ClickExport,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
