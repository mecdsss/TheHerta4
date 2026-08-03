import bpy
import os
import re
import struct
import shutil
from collections import OrderedDict

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

from .node_postprocess_base import SSMTNode_PostProcess_Base
from . import deform_chain
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.mod_path_compat import (
    find_base_position_resource_name,
    ensure_resource_alias_section,
    iter_position_buffer_candidates,
)

try:
    from ..toolkit import gb_core
    GB_CORE_AVAILABLE = True
except Exception:
    GB_CORE_AVAILABLE = False


# ---------------------------------------------------------------------------
# 常量与 zone 寄存器映射（与原作 LEWDHAND 完全一致，保留 77-124，115 空档跳过）
# ---------------------------------------------------------------------------

MAX_ZONES = 12

# 每组参数 → (zone0-3 寄存器, zone4-7 寄存器, zone8-11 寄存器)
ZONE_REGISTER_MAP = OrderedDict([
    ("radius",     (77, 78, 103)),
    ("strength",   (79, 80, 106)),
    ("max_offset", (81, 82, 109)),
    ("grabbable",  (99, 100, 112)),
    ("falloff",    (101, 102, 116)),
    ("path",       (119, 120, 121)),
    ("damping",    (122, 123, 124)),
])

# 有 "0=继承回退" 语义的参数（grabbable 例外，始终显式生成）
ZONE_FALLBACK_PARAMS = ("radius", "strength", "max_offset", "falloff", "damping")

SHADER_FILES = (
    "rzm_gs_probe.hlsl",
    "rzm_object_detect.hlsl",
    "rzm_pin_detected.hlsl",
    "rzm_jiggle_screen_state.hlsl",
    "rzm_jiggle_interaction.hlsl",
)
HAND_SHADER_FILES = ("rzm_jiggle_cursor_preview.hlsl", "rzm_jiggle_hand.hlsl", "rzm_jiggle_cursor.hlsl")
# 手部网格/法线资产（二进制，原样复制）
HAND_ASSET_FILES = (
    "HandAction.buf", "HandAction.ib", "HandAction_Normal.buf",
    "HandNoAction.buf", "HandNoAction.ib", "HandNoAction_Normal.buf",
)
# 视口探针着色器（不读角色网格，无 struct VertexAttributes，字节级原样复制）
VIEWPORT_SHADER_FILES = (
    "rzm_viewport_layout_vs.hlsl",
    "rzm_viewport_layout_probe.hlsl",
    "rzm_viewport_layout_decode.hlsl",
)

DEFAULT_VERTEX_STRUCT = (
    "struct VertexAttributes {\n"
    "    float3 position;\n"
    "    float3 normal;\n"
    "    float4 tangent;\n"
    "};"
)

DRAG_TAIL_MARKER = "; --- AUTO-APPENDED DRAG INTERACTION MODULE ---"
MESH_COMMENT_RE = re.compile(r"^;\s*\[mesh:(?P<object_name>[^\]]+)\]", re.IGNORECASE)

# 导出的着色器在 ini 中的引用路径（mod 根 → res/drag_interaction/）
RES_SHADER_DIR = "res/drag_interaction"


# ---------------------------------------------------------------------------
# 空物体区域参数 PropertyGroup
# ---------------------------------------------------------------------------

class SSMT_DragZoneSettings(bpy.types.PropertyGroup):
    """挂在一个 Empty 上的区域参数（画刷 + 拖拽物理）。"""

    # 画刷参数
    brush_strength: bpy.props.FloatProperty(name="画刷强度", default=1.0, min=0.0, max=4.0)
    brush_falloff_k: bpy.props.FloatProperty(name="画刷衰减 k", default=4.6, min=0.1, max=50.0)
    enabled: bpy.props.BoolProperty(name="启用", default=True)

    # 拖拽参数（0 = 继承回退到全局）
    radius: bpy.props.FloatProperty(name="影响半径", default=0.0, min=0.0)
    strength: bpy.props.FloatProperty(name="拖拽强度", default=0.0, min=0.0)
    max_offset: bpy.props.FloatProperty(name="最大位移", default=0.0, min=0.0)
    falloff: bpy.props.FloatProperty(name="衰减", default=0.0, min=0.0)
    damping: bpy.props.FloatProperty(name="阻尼", default=0.0, min=0.0)
    grabbable: bpy.props.BoolProperty(name="可抓取", default=True)


class SSMT_DragZoneRef(bpy.types.PropertyGroup):
    """节点 zone_objects 列表里的一项：指向一个 Empty。"""
    zone_object: bpy.props.PointerProperty(
        name="区域空物体", type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'EMPTY',
    )


# ---------------------------------------------------------------------------
# 空物体管理 operators
# ---------------------------------------------------------------------------

class SSMT_OT_DragZoneAdd(bpy.types.Operator):
    bl_idname = "ssmt.drag_zone_add"
    bl_label = "添加拖拽区域空物体"
    bl_description = "在场景原点创建一个拖拽区域空物体并挂到节点列表"

    node_name: bpy.props.StringProperty()
    node_tree: bpy.props.StringProperty()

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None or len(node.zone_objects) >= MAX_ZONES:
            self.report({'WARNING'}, "节点不存在或区域已达上限 12")
            return {'CANCELLED'}

        empty = bpy.data.objects.new(f"SSMT_DragZone_{len(node.zone_objects)}", None)
        empty.empty_display_type = 'SPHERE'
        empty.empty_display_size = 0.25
        empty.ssmt_drag_zone.radius = 0.5
        context.scene.collection.objects.link(empty)

        item = node.zone_objects.add()
        item.zone_object = empty
        self.report({'INFO'}, f"已创建区域空物体 {empty.name}")
        return {'FINISHED'}


class SSMT_OT_DragZoneRemove(bpy.types.Operator):
    bl_idname = "ssmt.drag_zone_remove"
    bl_label = "移除选中区域"
    bl_description = (
        "从列表移除该区域。SSMT 创建的区域空物体（SSMT_DragZone_*）在不被其他拖拽节点引用时一并删除；"
        "用户手动指定的已有空物体仅移除引用，保留在场景中"
    )

    node_name: bpy.props.StringProperty()
    node_tree: bpy.props.StringProperty()
    index: bpy.props.IntProperty()

    @staticmethod
    def _zone_empty_in_use(obj, exclude_node):
        """检查空物体是否仍被其他拖拽交互节点引用。"""
        for tree in bpy.data.node_groups:
            for n in tree.nodes:
                if n.bl_idname != 'SSMTNode_PostProcess_DragInteraction' or n == exclude_node:
                    continue
                for it in n.zone_objects:
                    if it.zone_object == obj:
                        return True
        return False

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None or not (0 <= self.index < len(node.zone_objects)):
            return {'CANCELLED'}
        item = node.zone_objects[self.index]
        obj = item.zone_object
        node.zone_objects.remove(self.index)
        # 只有 SSMT 创建的区域空物体、且不再被任何拖拽节点引用时才随引用一并删除；
        # 用户手动指定的已有空物体（可能复用于其他用途）只移除引用
        if obj is not None and obj.name.startswith("SSMT_DragZone") and not self._zone_empty_in_use(obj, node):
            name = obj.name
            bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'INFO'}, f"已移除引用并删除空物体 {name}")
        else:
            self.report({'INFO'}, "已从列表移除（空物体保留在场景中）")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 主节点
# ---------------------------------------------------------------------------

class SSMTNode_PostProcess_DragInteraction(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_DragInteraction'
    bl_label = '拖拽交互'
    bl_description = (
        '把 LEWDHAND 的鼠标拖拽模型效果接入蓝图后处理链：Alt+左键抓取拖拽、弹簧回弹、戳，'
        '可与多文件/形态键叠加（多文件→形态键→拉扯三级接力）。'
        '着色器原样复制自 LEWDHAND，仅替换顶点结构体定义。'
    )

    # ---- 基本属性 ----
    hash_values: bpy.props.StringProperty(
        name="目标哈希值",
        description="目标 DrawIB hash 列表（= 组件列表），多个用逗号分隔。支持 IB hash 或完整名称",
        default="",
    )
    mod_namespace: bpy.props.StringProperty(
        name="命名空间后缀",
        description="变量/资源命名后缀；留空则从 ini 文件名推导",
        default="",
    )
    grab_key: bpy.props.EnumProperty(
        name="武装修饰键",
        description="抓取的武装修饰键",
        items=[('ALT', "Alt", "按住 Alt 才能抓取"), ('NONE', "无（常开）", "无需修饰键")],
        default='ALT',
    )
    grab_gesture: bpy.props.EnumProperty(
        name="抓取手势",
        description=(
            "触发抓取（isMouseButtonDown）的按键组合。"
            "原作设计：Alt+左右键同按或 Alt+X 才是抓取，单键按下释放是戳；"
            "选左键/右键时该键的戳手势不再可用（按下即抓取，无释放脉冲）"
        ),
        items=[
            ('LMB', "左键（或 X）", "Alt+左键按住即抓取（最直觉；左键戳失效，右键戳保留）"),
            ('RMB', "右键（或 X）", "Alt+右键按住即抓取（右键戳失效，左键戳保留）"),
            ('COMBO', "左右键同按 / X（原作）", "Alt+左右键同按或 Alt+X 抓取（原作手势，单键戳全部保留）"),
        ],
        default='LMB',
    )
    enable_poke: bpy.props.BoolProperty(name="启用戳", default=True)
    enable_hand_cursor: bpy.props.BoolProperty(
        name="启用手型光标",
        default=True,
        description="生成 S8 手型光标（按住 Alt 时显示，是 mode 已激活的视觉反馈）",
    )
    enable_viewport_probe: bpy.props.BoolProperty(
        name="启用视口探针",
        default=True,
        description="生成 viewport 探针系统（角色查看器等子区域渲染时校正光标映射，否则检测射线打偏、命中为零。原作必备）",
    )

    # ---- 全局物理档案（IniParams 70/71 字面量）----
    # 注：phys_release_kick / phys_target_follow 的键名是历史错位——
    # phys_release_kick 实际写 w71（原作“目标跟随”槽，默认 0.12），
    # phys_target_follow 实际写 y71（原作“释放冲击”槽，默认 1.10）。
    # 为不改旧工程数值与已生成 ini，仅按实际槽位语义改显示名。
    phys_grab_damping: bpy.props.FloatProperty(name="抓取阻尼", default=0.86)
    phys_grab_spring: bpy.props.FloatProperty(name="抓取弹簧", default=0.176)
    phys_release_damping: bpy.props.FloatProperty(name="释放阻尼", default=0.96)
    phys_release_spring: bpy.props.FloatProperty(name="释放弹簧", default=0.055)
    phys_release_kick: bpy.props.FloatProperty(name="目标跟随", default=0.12)
    phys_target_follow: bpy.props.FloatProperty(name="释放冲击", default=1.10)

    # ---- 全局倍率（IniParams 72）----
    mult_radius: bpy.props.FloatProperty(name="半径倍率", default=1.0)
    mult_strength: bpy.props.FloatProperty(name="强度倍率", default=0.333)
    mult_spring: bpy.props.FloatProperty(name="弹簧倍率", default=0.333)
    mult_damping: bpy.props.FloatProperty(name="阻尼倍率", default=1.0)

    # ---- 区域空物体引用列表 ----
    zone_objects: bpy.props.CollectionProperty(type=SSMT_DragZoneRef)

    # ---- 权重预览（视口热力图，仿高斯球预览）----
    preview_weights: bpy.props.BoolProperty(name="权重预览", default=False)
    preview_target: bpy.props.PointerProperty(
        name="预览网格", type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )

    # ---- 权重平台化（烘焙 + 预览共用）----
    mask_plateau: bpy.props.FloatProperty(
        name="权重平台化", default=0.0, min=0.0, max=0.99,
        description="0=纯高斯（画刷衰减 k 生效）；>0 时球内 d≤平台保持满权重、边缘平滑过渡（此模式下画刷衰减 k 不参与）",
    )

    # ---- 沿表面传播（烘焙 + 预览共用）----
    surface_propagate: bpy.props.BoolProperty(
        name="沿表面传播", default=True,
        description="权重从球与表面的接触点沿网格表面扩散（测地距离），不穿透到球体积覆盖的背面/对侧；关闭回退体积球",
    )

    # ---- 烘焙参考物体（可选手动覆盖）----
    bake_reference_object: bpy.props.PointerProperty(
        name="烘焙参考物体（可选）",
        description="空物体世界坐标换算到模组局部空间的参考；留空自动解析",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )

    # =======================================================================
    # UI
    # =======================================================================

    def draw_buttons(self, context, layout):
        layout.prop(self, "hash_values")
        layout.prop(self, "mod_namespace")
        row = layout.row(align=True)
        row.prop(self, "grab_key")
        row.prop(self, "grab_gesture")
        row = layout.row(align=True)
        row.prop(self, "enable_poke")
        row.prop(self, "enable_hand_cursor")

        box = layout.box()
        box.label(text="全局物理档案（弹簧常数）", icon='PHYSICS')
        col = box.column(align=True)
        col.prop(self, "phys_grab_damping")
        col.prop(self, "phys_grab_spring")
        col.prop(self, "phys_release_damping")
        col.prop(self, "phys_release_spring")
        col.prop(self, "phys_release_kick")
        col.prop(self, "phys_target_follow")

        box = layout.box()
        box.label(text="全局倍率", icon='MODIFIER')
        col = box.column(align=True)
        col.prop(self, "mult_radius")
        col.prop(self, "mult_strength")
        col.prop(self, "mult_spring")
        col.prop(self, "mult_damping")

        # 区域空物体列表
        box = layout.box()
        row = box.row()
        row.label(text=f"区域空物体（{len(self.zone_objects)}/{MAX_ZONES}）", icon='EMPTY_DATA')
        add = row.operator(SSMT_OT_DragZoneAdd.bl_idname, text="", icon='ADD')
        add.node_name = self.name
        add.node_tree = self.id_data.name if self.id_data else ""

        for i, item in enumerate(self.zone_objects):
            row = box.row(align=True)
            row.prop(item, "zone_object", text=f"区域 {i}")
            rm = row.operator(SSMT_OT_DragZoneRemove.bl_idname, text="", icon='X')
            rm.node_name = self.name
            rm.node_tree = self.id_data.name if self.id_data else ""
            rm.index = i
            obj = item.zone_object
            if obj is not None:
                sub = box.column(align=True)
                sub.use_property_split = True
                sub.prop(obj.ssmt_drag_zone, "enabled")
                sub.prop(obj.ssmt_drag_zone, "brush_strength")
                sub.prop(obj.ssmt_drag_zone, "brush_falloff_k")
                sub.prop(obj.ssmt_drag_zone, "radius")
                # 建议范围：衰减需在球内显著变化，否则整块刚体动（原版实测 ratio 0.3~2.2）
                ws = sum(obj.matrix_world.to_scale()) / 3.0
                if ws > 1e-6:
                    sub.label(text=f"球半径≈{ws:.3f}，建议影响半径 {ws:.3f}~{ws*2.5:.3f}", icon='INFO')
                sub.prop(obj.ssmt_drag_zone, "strength")
                sub.prop(obj.ssmt_drag_zone, "max_offset")
                sub.prop(obj.ssmt_drag_zone, "falloff")
                sub.prop(obj.ssmt_drag_zone, "damping")
                sub.prop(obj.ssmt_drag_zone, "grabbable")
                box.separator()

        layout.prop(self, "bake_reference_object")
        layout.prop(self, "mask_plateau")
        layout.prop(self, "surface_propagate")

        # 权重预览（视口热力图）
        box = layout.box()
        box.label(text="权重预览（视口热力图）", icon='RESTRICT_VIEW_OFF')
        col = box.column(align=True)
        col.prop(self, "preview_weights")
        col.prop(self, "preview_target")
        if self.preview_weights:
            if self.preview_target is None:
                col.label(text="请选择预览网格以显示热力图", icon='INFO')
            else:
                col.label(text="空物体缩放=影响半径；颜色=烘焙掩码（brush 参数）", icon='INFO')
        _ensure_preview_running()

        if not NUMPY_AVAILABLE:
            layout.label(text="警告: 未安装 numpy，烘焙不可用", icon='ERROR')
        if not GB_CORE_AVAILABLE:
            layout.label(text="警告: gb_core 不可用，高斯烘焙回退 zone0 全 1", icon='ERROR')

    # =======================================================================
    # 顶点结构 / 着色器源
    # =======================================================================

    def _get_vertex_attrs_node(self):
        if not self.inputs[0].is_linked:
            return None
        source_node = self.inputs[0].links[0].from_node
        if source_node.bl_idname == 'SSMTNode_PostProcess_VertexAttrs':
            return source_node
        if source_node.inputs and source_node.inputs[0].is_linked:
            prev_node = source_node.inputs[0].links[0].from_node
            if prev_node.bl_idname == 'SSMTNode_PostProcess_VertexAttrs':
                return prev_node
        return None

    def _get_vertex_struct_definition(self):
        node = self._get_vertex_attrs_node()
        if node:
            try:
                return node.get_vertex_struct_definition()
            except Exception:
                pass
        return DEFAULT_VERTEX_STRUCT

    def _get_position_layout(self):
        type_sizes = {
            'float': 4, 'float2': 8, 'float3': 12, 'float4': 16,
            'int': 4, 'int2': 8, 'int3': 12, 'int4': 16,
            'uint': 4, 'uint2': 8, 'uint3': 12, 'uint4': 16,
            'half': 2, 'half2': 4, 'half3': 6, 'half4': 8,
            'double': 8, 'double2': 16, 'double3': 24, 'double4': 32,
        }
        stride = 0
        position_offset = None
        position_type = None
        for raw_line in self._get_vertex_struct_definition().splitlines():
            line = raw_line.strip().rstrip(';').strip()
            parts = line.split()
            if len(parts) < 2 or parts[0] not in type_sizes:
                continue
            attr_type, attr_name = parts[0], parts[1]
            if attr_name.casefold() == 'position':
                position_offset = stride
                position_type = attr_type
            stride += type_sizes[attr_type]

        if stride <= 0 or position_offset is None or position_type is None:
            return 40, 0, 'float3'
        return stride, position_offset, position_type

    def _get_toolset_dir(self):
        addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(addon_dir, "Toolset", "drag_interaction")

    # =======================================================================
    # ini 读写 / hash 解析
    # =======================================================================

    @classmethod
    def _remove_existing_drag_tail_block(cls, tail_content):
        remaining = tail_content
        while True:
            start = remaining.find(DRAG_TAIL_MARKER)
            if start == -1:
                return remaining

            end = len(remaining)
            search_from = start + len(DRAG_TAIL_MARKER)
            for marker in cls.AUTO_APPENDED_SECTION_MARKERS:
                marker_position = remaining.find(marker, search_from)
                if marker_position != -1:
                    end = min(end, marker_position)

            before = remaining[:start].rstrip()
            after = remaining[end:].lstrip()
            remaining = "\n\n".join(part for part in (before, after) if part)

    @staticmethod
    def _remove_existing_draw_hooks(sections):
        for section_name, lines in sections.items():
            cleaned_lines = []
            index = 0
            while index < len(lines):
                if "DRAG HOOK BEGIN" not in lines[index]:
                    cleaned_lines.append(lines[index])
                    index += 1
                    continue

                hook_end = next(
                    (candidate for candidate in range(index + 1, len(lines))
                     if "DRAG HOOK END" in lines[candidate]),
                    None,
                )
                if hook_end is None:
                    cleaned_lines.append(lines[index])
                    index += 1
                    continue
                index = hook_end + 1
            sections[section_name] = cleaned_lines

    def _read_ini_to_ordered_dict(self, ini_file_path):
        sections = OrderedDict()
        current_section = None
        preserved_tail_content = ""
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content, preserved_tail_content = self.split_auto_appended_tail_content(content)
            preserved_tail_content = self._remove_existing_drag_tail_block(preserved_tail_content)
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped
                    sections[current_section] = []
                elif current_section is not None:
                    sections[current_section].append(line)
            self._remove_existing_draw_hooks(sections)
        except FileNotFoundError:
            return None, ""
        return sections, preserved_tail_content

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content=""):
        with open(ini_file_path, 'w', encoding='utf-8') as f:
            for section_name, lines in sections.items():
                f.write(f"{section_name}\n")
                for line in lines:
                    f.write(f"{line}\n")
                f.write("\n")
            if preserved_tail_content:
                f.write("\n")
                f.write(preserved_tail_content)

    def _parse_hash_values(self, hash_str):
        hash_list = [h.strip() for h in hash_str.split(',') if h.strip()]
        ib_hashes = OrderedDict()
        for hash_value in hash_list:
            prefix_info = ObjectPrefixHelper.extract_prefix_info(hash_value)
            if prefix_info:
                prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
                draw_ib = str(prefix_parts.get("draw_ib", "") or "").strip()
                if draw_ib:
                    ib_hashes[draw_ib] = True
                    continue
            normalized = str(hash_value or "").strip()
            if normalized:
                ib_hashes[normalized] = True
        return list(ib_hashes.keys())

    def _hash_to_resource_prefix(self, h):
        return h.replace('-', '_')

    def _resolve_namespace(self, ini_file_path):
        if self.mod_namespace.strip():
            return re.sub(r'\W+', '_', self.mod_namespace.strip()).lower()
        base = os.path.splitext(os.path.basename(ini_file_path))[0]
        return re.sub(r'\W+', '_', base).lower()

    def _find_existing_base_resource_name(self, sections, hash_filter, base_name):
        normalized_base = str(base_name or "").strip()
        normalized_hash = str(hash_filter or "").strip()
        preferred = f"Resource_{self._hash_to_resource_prefix(normalized_base)}_Position"
        legacy = f"Resource{self._hash_to_resource_prefix(normalized_hash)}Position"
        return find_base_position_resource_name(
            sections, normalized_hash, base_name=normalized_base,
            preferred_names=[preferred, legacy], fallback_name=preferred,
        )

    def _get_vertex_count(self, sections, hash_value):
        for section_name, lines in sections.items():
            if section_name.startswith(f'[TextureOverride_{hash_value}_') and '_VertexLimitRaise' in section_name:
                for line in lines:
                    if line.strip().startswith('override_vertex_count ='):
                        try:
                            return int(line.split('=', 1)[1].strip())
                        except ValueError:
                            continue
        return None

    # =======================================================================
    # execute_postprocess 主流程
    # =======================================================================

    def execute_postprocess(self, mod_export_path):
        print(f"[DragInteraction] 开始执行, 输出路径: {mod_export_path}")
        if not NUMPY_AVAILABLE:
            print("[DragInteraction][ERROR] 需要 numpy，已跳过")
            return

        hash_values = self._parse_hash_values(self.hash_values)
        if not hash_values:
            print("[DragInteraction] 未配置目标哈希值，已跳过")
            return

        ini_files = sorted(f for f in os.listdir(mod_export_path) if f.lower().endswith('.ini'))
        if not ini_files:
            print(f"[DragInteraction] 未找到 ini 文件: {mod_export_path}")
            return

        # 复制着色器（5 个核心 + 可选手部）
        res_dir = os.path.join(mod_export_path, "res", "drag_interaction")
        os.makedirs(res_dir, exist_ok=True)
        self._copy_shaders(res_dir)

        for ini_file in ini_files:
            ini_path = os.path.join(mod_export_path, ini_file)
            sections, preserved_tail = self._read_ini_to_ordered_dict(ini_path)
            if not sections:
                continue

            # 定位含目标 hash 且能到达 drawindexed 的组件
            components = self._locate_components(sections, hash_values)
            if not components:
                continue

            ns = self._resolve_namespace(ini_path)
            self._create_cumulative_backup(ini_path, mod_export_path)

            # 1) 资源烘焙（JiggleMasks/ObjectMap/PathVectors）
            for comp in components:
                self._bake_component_resources(mod_export_path, sections, comp, ns)

            # 2) 生成 CustomShader/CommandList/Resource 段
            self._emit_sections(sections, components, ns)

            # 3) 注入绘制钩子（ib= 之后、第一个 run=/drawindexed= 之前）
            for comp in components:
                self._inject_draw_hooks(sections, comp, ns)

            # 4) Present 块 + Constants globals
            self._emit_present_and_constants(sections, components, ns)

            # 5) 变形接力终态规整（幂等，含多文件/形态键条件锚定）
            deform_chain.finalize_deform_chain(sections)

            self._write_ordered_dict_to_ini(sections, ini_path, preserved_tail)
            print(f"[DragInteraction] 已注入 {len(components)} 个组件到 {ini_file}")

        print("[DragInteraction] 完成")

    # =======================================================================
    # 组件定位（含目标 hash 且能到达 drawindexed）
    # =======================================================================

    def _locate_components(self, sections, hash_values):
        components = []
        for hash_value in hash_values:
            parts = self._collect_draw_parts(sections, hash_value)
            if not parts:
                print(f"[DragInteraction][WARNING] hash {hash_value} 未找到绘制段，跳过")
                continue
            # 组件名以 Position 资源 stem 为准（去掉 part 后缀），与 ObjectMap/Masks 文件名一致；
            # 不要用 parts[0]["base_name"]（含 A/B part 后缀，会让 comp_name 带上 part 标签）。
            base_name = self._resolve_position_stem(sections, hash_value) or parts[0]["base_name"]
            vertex_count = self._get_vertex_count(sections, hash_value)
            base_resource = self._find_existing_base_resource_name(sections, hash_value, base_name)
            components.append({
                "hash": hash_value,
                "base_name": base_name,
                "comp_name": self._comp_name(base_name),
                "vertex_count": vertex_count or 0,
                "base_resource": base_resource,
                "parts": parts,
            })
        return components

    def _resolve_position_stem(self, sections, hash_value):
        """从 Position 资源段的 filename 推导组件 stem（如 abc123-43191）。"""
        res = self._find_existing_base_resource_name(sections, hash_value, hash_value)
        for line in sections.get(f"[{res}]", []):
            if line.strip().startswith("filename ="):
                fname = line.split("=", 1)[1].strip().replace('\\', '/').split('/')[-1]
                # 去掉 "-Position.buf" / ".buf" 后缀得 stem
                stem = re.sub(r"-Position.*$", "", fname)
                stem = re.sub(r"\.buf$", "", stem)
                if stem:
                    return stem
        return None

    def _comp_name(self, base_name):
        # 从组件 stem（如 abc123-43191）推导资源命名前缀（连字符转下划线）
        return self._hash_to_resource_prefix(base_name)

    @classmethod
    def _collect_referenced_draw_ranges(cls, sections, command_name, visited=None):
        normalized_name = str(command_name or "").strip().casefold()
        if not normalized_name:
            return []

        visited = set() if visited is None else visited
        if normalized_name in visited:
            return []
        visited.add(normalized_name)

        target_section = None
        expected_section = f"[{normalized_name}]"
        for section_name, section_lines in sections.items():
            if section_name.casefold() == expected_section:
                target_section = section_lines
                break
        if target_section is None:
            return []

        draw_ranges = []
        for line in target_section:
            stripped = line.strip()
            normalized = stripped.casefold()
            if normalized.startswith("drawindexed ="):
                nums = [value.strip() for value in stripped.split("=", 1)[1].split(',')]
                try:
                    draw_count = int(nums[0])
                    draw_offset = int(nums[1]) if len(nums) > 1 else 0
                except (ValueError, IndexError):
                    continue
                if draw_count > 0 and draw_offset >= 0:
                    draw_ranges.append((draw_offset, draw_count))
            elif normalized.startswith("run ="):
                nested_name = stripped.split("=", 1)[1].strip()
                draw_ranges.extend(cls._collect_referenced_draw_ranges(
                    sections, nested_name, visited,
                ))
        return draw_ranges

    def _collect_draw_parts(self, sections, hash_value):
        """按 mesh 前缀归属和活动 IB 收集绘制组；无前缀配置回退到段 hash。"""
        parts = []
        normalized_hash = hash_value.casefold()
        for section_name, lines in sections.items():
            if not (section_name.startswith("[TextureOverride_") and section_name.endswith("]")):
                continue

            section_hash = None
            match_first_index = None
            for line in lines:
                s = line.strip()
                normalized = s.casefold()
                if normalized.startswith("hash ="):
                    section_hash = s.split("=", 1)[1].strip()
                elif normalized.startswith("match_first_index ="):
                    try:
                        match_first_index = int(s.split("=", 1)[1].strip())
                    except ValueError:
                        pass

            active_ib_resource = None
            pending_mesh_owner = None
            pending_mesh_first_index = None
            pending_mesh_comment = None
            pending_mesh_comment_occurrence = 0
            comment_occurrences = {}
            draw_ordinal = 0
            draw_records = []

            for line in lines:
                s = line.strip()
                normalized = s.casefold()
                if normalized.startswith("ib ="):
                    val = s.split("=", 1)[1].strip()
                    active_ib_resource = None if val.casefold() == "null" else val
                    continue

                mesh_match = MESH_COMMENT_RE.match(s)
                if mesh_match:
                    pending_mesh_owner = None
                    pending_mesh_first_index = None
                    pending_mesh_comment = s
                    pending_mesh_comment_occurrence = comment_occurrences.get(s, 0)
                    comment_occurrences[s] = pending_mesh_comment_occurrence + 1
                    prefix_info = ObjectPrefixHelper.extract_prefix_info(mesh_match.group("object_name"))
                    if prefix_info:
                        prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
                        pending_mesh_owner = str(prefix_parts.get("draw_ib", "") or "").strip() or None
                        try:
                            pending_mesh_first_index = int(prefix_parts.get("first_index", ""))
                        except (TypeError, ValueError):
                            pending_mesh_first_index = None
                    continue

                if normalized.startswith("run =") and pending_mesh_comment:
                    command_name = s.split("=", 1)[1].strip()
                    referenced_ranges = self._collect_referenced_draw_ranges(sections, command_name)
                    if referenced_ranges:
                        draw_owner = pending_mesh_owner or section_hash
                        for draw_offset, draw_count in referenced_ranges:
                            if draw_owner and draw_owner.casefold() == normalized_hash and active_ib_resource:
                                draw_records.append({
                                    "ordinal": draw_ordinal,
                                    "ib_resource": active_ib_resource,
                                    "draw_offset": draw_offset,
                                    "draw_count": draw_count,
                                    "mesh_first_index": pending_mesh_first_index,
                                    "hook_anchor_comment": pending_mesh_comment,
                                    "hook_anchor_occurrence": pending_mesh_comment_occurrence,
                                })
                            draw_ordinal += 1
                        pending_mesh_owner = None
                        pending_mesh_first_index = None
                        pending_mesh_comment = None
                        pending_mesh_comment_occurrence = 0
                        continue

                if normalized.startswith("drawindexed ="):
                    nums = [n.strip() for n in s.split("=", 1)[1].split(',')]
                    try:
                        draw_count = int(nums[0])
                        draw_offset = int(nums[1]) if len(nums) > 1 else 0
                    except (ValueError, IndexError):
                        draw_ordinal += 1
                        continue

                    draw_owner = pending_mesh_owner or section_hash
                    if (
                        active_ib_resource
                        and draw_owner
                        and draw_owner.casefold() == normalized_hash
                        and draw_count > 0
                        and draw_offset >= 0
                    ):
                        draw_records.append({
                            "ordinal": draw_ordinal,
                            "ib_resource": active_ib_resource,
                            "draw_offset": draw_offset,
                            "draw_count": draw_count,
                            "mesh_first_index": pending_mesh_first_index,
                            "hook_anchor_comment": pending_mesh_comment,
                            "hook_anchor_occurrence": pending_mesh_comment_occurrence,
                        })
                    draw_ordinal += 1
                    pending_mesh_owner = None
                    pending_mesh_first_index = None
                    pending_mesh_comment = None
                    pending_mesh_comment_occurrence = 0

            draw_groups = []
            for record in draw_records:
                if (
                    draw_groups
                    and draw_groups[-1][-1]["ordinal"] + 1 == record["ordinal"]
                    and draw_groups[-1][-1]["ib_resource"] == record["ib_resource"]
                ):
                    draw_groups[-1].append(record)
                else:
                    draw_groups.append([record])

            for draw_group in draw_groups:
                ib_resource = draw_group[0]["ib_resource"]
                ib_first_index = min(record["draw_offset"] for record in draw_group)
                ib_index_end = max(record["draw_offset"] + record["draw_count"] for record in draw_group)
                index_count = ib_index_end - ib_first_index
                mesh_first_index = draw_group[0]["mesh_first_index"]
                first_index = (
                    mesh_first_index
                    if mesh_first_index is not None
                    else match_first_index if match_first_index is not None
                    else ib_first_index
                )
                base_name = section_name[len("[TextureOverride_"):-1]
                parts.append({
                    "section": section_name,
                    "ib_resource": ib_resource,
                    "index_count": index_count,
                    "ib_first_index": ib_first_index,
                    "first_index": first_index,
                    "base_name": base_name,
                    "hook_anchor_comment": draw_group[0]["hook_anchor_comment"],
                    "hook_anchor_occurrence": draw_group[0]["hook_anchor_occurrence"],
                })
        return parts

    # =======================================================================
    # 着色器复制 + struct 1 处机械替换
    # =======================================================================

    def _copy_shaders(self, res_dir):
        toolset = self._get_toolset_dir()
        files = list(SHADER_FILES)
        vertex_struct = self._get_vertex_struct_definition()
        for fname in files:
            src = os.path.join(toolset, fname)
            if not os.path.exists(src):
                print(f"[DragInteraction][WARNING] 着色器缺失: {src}")
                continue
            with open(src, 'r', encoding='utf-8') as f:
                content = f.read()
            # 仅 1 处机械替换：struct VertexAttributes {…} → 顶点属性定义节点的 struct
            content = re.sub(
                r"struct VertexAttributes\s*\{[^}]*\};",
                vertex_struct,
                content,
                flags=re.DOTALL,
            )
            dest = os.path.join(res_dir, fname)
            with open(dest, 'w', encoding='utf-8') as f:
                f.write(content)
        # PathVectors 模板占位
        pv_src = os.path.join(toolset, "PathVectors.buf")
        if os.path.exists(pv_src):
            shutil.copy2(pv_src, os.path.join(res_dir, "PathVectors.buf"))
        # 手部着色器 + 网格/法线资产：全部字节级原样复制。手部着色器读自己
        # 的 vb0（stride 28 固定布局），不含 struct VertexAttributes，无需也
        # 不许走文本替换路径（避免行尾转换）——与原作保持字节一致。
        if self.enable_hand_cursor:
            for fname in HAND_SHADER_FILES + HAND_ASSET_FILES:
                src = os.path.join(toolset, fname)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(res_dir, fname))
                else:
                    print(f"[DragInteraction][WARNING] 手部文件缺失: {src}")
        # 视口探针着色器：字节级原样复制（不读角色网格，无 struct VertexAttributes）
        if self.enable_viewport_probe:
            for fname in VIEWPORT_SHADER_FILES:
                src = os.path.join(toolset, fname)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(res_dir, fname))
                else:
                    print(f"[DragInteraction][WARNING] 视口探针着色器缺失: {src}")

    # =======================================================================
    # 资源烘焙（JiggleMasks / ObjectMap / PathVectors）
    # =======================================================================

    def _bake_component_resources(self, mod_export_path, sections, comp, ns):
        # ObjectMap：游戏索引空间 (1+N)×16B
        self._write_object_map(mod_export_path, sections, comp)
        # JiggleMasks：高斯场烘焙
        self._write_jiggle_masks(mod_export_path, sections, comp, ns)

    def _write_object_map(self, mod_export_path, sections, comp):
        parts = comp["parts"]
        n = len(parts)
        records = [struct.pack('<ffff', float(n), 0.0, 0.0, 0.0)]
        for part in parts:
            records.append(struct.pack(
                '<ffff',
                0.0,          # firstIndex 恒 0（分区局部）：detect 着色器 indexBase = firstIndex + tri*3，
                              # 各部件 IB 是独立分区文件，全局偏移会越界读垃圾（原作 ObjectMap 全为 0）
                float(part["index_count"]),
                7.0,          # mode
                0.0,          # objectID 恒 0（原作契约）：着色器 entry.w==0 时回退 objectID=firstIndex=0，
                              # 全部件命中统一匹配碰撞档案条目 0
            ))
        data = b''.join(records)
        out = os.path.join(mod_export_path, self._buffer_dir(sections, comp), f"{comp['base_name']}ObjectMap.buf")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'wb') as f:
            f.write(data)
        print(f"[DragInteraction] ObjectMap: {os.path.basename(out)} ({n} parts, {len(data)}B)")

    def _buffer_dir(self, sections, comp):
        """从 ini 的 Position 资源段解析缓冲文件夹名（动态，不硬编码）。"""
        res = comp["base_resource"]
        lines = sections.get(f"[{res}]", [])
        for line in lines:
            if line.strip().startswith("filename ="):
                path = line.split("=", 1)[1].strip().replace('\\', '/')
                if '/' in path:
                    return path.split('/')[0]
        return "Meshes"

    def _write_jiggle_masks(self, mod_export_path, sections, comp, ns):
        zones = self._collect_enabled_zones()
        vertex_count = comp["vertex_count"]
        mask_count = max(1, (max(len(zones), 1) + 3) // 4)  # ceil(zones/4)，至少 1

        # 无区域 → zone0 全 1
        if not zones or not GB_CORE_AVAILABLE:
            self._write_masks_fallback(mod_export_path, sections, comp, vertex_count, mask_count)
            return

        # radius 参数与球尺度失配检查（防“整块刚体动”失配，原版实测 ratio 0.3~2.2）
        self._check_zone_radius_scale(zones)

        # 沿表面传播拓扑：IB 读一次三角形 → 去重边，全部 zone 共用
        edge_verts = None
        if getattr(self, "surface_propagate", True):
            triangles = self._read_component_triangles(mod_export_path, sections, comp, vertex_count)
            if triangles is not None:
                edge_verts = gb_core.edges_from_triangles(triangles)
            else:
                print(f"[DragInteraction][WARNING] {comp['comp_name']} 无法读取 IB 拓扑，沿表面传播回退体积球")

        # 读取 Position.buf 顶点坐标
        positions = self._read_position_buf(mod_export_path, sections, comp, vertex_count)
        if positions is None:
            self._write_masks_fallback(mod_export_path, sections, comp, vertex_count, mask_count)
            return

        # 烘焙参考物体世界矩阵的逆（坐标系换算）
        ref_matrix_inv = self._get_reference_matrix_inv(comp)
        # 非镜像工作流补偿：场景网格被 X 镜像过（导入时 mesh.transform 翻转、物体矩阵不变）
        # 而空物体矩阵未跟随 → 需对球矩阵施加同样的 X 镜像后才与 Position.buf（还原后
        # 朝向）同空间，否则掩码左右颠倒（用户报告）。预览不受影响（所见即所得）。
        mirror = self._get_non_mirror_mirror()

        mask_arrays = [np.zeros((vertex_count, 4), dtype=np.float32) for _ in range(mask_count)]
        for z_idx, empty in enumerate(zones):
            buf_idx = z_idx // 4
            channel = z_idx % 4
            settings = empty.ssmt_drag_zone
            field = self._evaluate_zone_field(positions, empty, settings, ref_matrix_inv, mirror, edge_verts)
            if field is None:
                continue
            mask_arrays[buf_idx][:, channel] = field

        # 恒定写 3 个缓冲（未用通道清零填充），与 ini 里 3 个资源段一一对应
        for buf_idx in range(3):
            arr = mask_arrays[buf_idx] if buf_idx < len(mask_arrays) else np.zeros((vertex_count, 4), dtype=np.float32)
            out = os.path.join(
                mod_export_path, self._buffer_dir(sections, comp),
                f"{comp['base_name']}JiggleMasks{buf_idx}.buf",
            )
            os.makedirs(os.path.dirname(out), exist_ok=True)
            arr.tofile(out)
        print(f"[DragInteraction] JiggleMasks: {comp['comp_name']} ×3 缓冲 ({vertex_count} 顶点, {len(zones)} 区域)")

    def _write_masks_fallback(self, mod_export_path, sections, comp, vertex_count, mask_count):
        for buf_idx in range(3):
            arr = np.zeros((vertex_count, 4), dtype=np.float32)
            if buf_idx == 0:
                arr[:, 0] = 1.0  # zone0 全 1
            out = os.path.join(
                mod_export_path, self._buffer_dir(sections, comp),
                f"{comp['base_name']}JiggleMasks{buf_idx}.buf",
            )
            os.makedirs(os.path.dirname(out), exist_ok=True)
            arr.tofile(out)
        print(f"[DragInteraction] JiggleMasks（回退 zone0 全 1）: {comp['comp_name']} ({vertex_count} 顶点)")

    def _read_position_buf(self, mod_export_path, sections, comp, vertex_count):
        """从 ini 的 Position 资源段 filename 解析路径读取顶点坐标（float3×N）。"""
        res = comp["base_resource"]
        lines = sections.get(f"[{res}]", [])
        rel_path = None
        resource_stride = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("filename ="):
                rel_path = line.split("=", 1)[1].strip()
            elif stripped.startswith("stride ="):
                try:
                    resource_stride = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
        if not rel_path:
            print(f"[DragInteraction][WARNING] [{res}] 无 filename 行，无法读取 Position.buf")
            return None
        full_path = os.path.join(mod_export_path, rel_path.replace('\\', os.sep).replace('/', os.sep))
        if not os.path.exists(full_path):
            print(f"[DragInteraction][WARNING] Position.buf 不存在: {full_path}")
            return None
        try:
            data = np.fromfile(full_path, dtype=np.uint8)
            struct_stride, position_offset, position_type = self._get_position_layout()
            stride = resource_stride or struct_stride
            type_match = re.fullmatch(r'(float|half|double)([1-4]?)', position_type)
            if not type_match:
                print(f"[DragInteraction][WARNING] 不支持的 position 类型: {position_type}")
                return None
            component_count = int(type_match.group(2) or '1')
            if component_count < 3:
                print(f"[DragInteraction][WARNING] position 分量不足 3 个: {position_type}")
                return None
            scalar_type = {
                'float': np.float32,
                'half': np.float16,
                'double': np.float64,
            }[type_match.group(1)]
            attribute_size = np.dtype(scalar_type).itemsize * component_count
            if position_offset + attribute_size > stride:
                print(
                    f"[DragInteraction][WARNING] position 布局越界: "
                    f"offset={position_offset}, size={attribute_size}, stride={stride}"
                )
                return None
            n = len(data) // stride
            if vertex_count and n != vertex_count:
                print(f"[DragInteraction][WARNING] Position.buf 顶点数 {n} 与 VLR {vertex_count} 不一致，以 buf 为准")
            arr = data[:n * stride].reshape(n, stride)
            attribute_bytes = arr[:, position_offset:position_offset + attribute_size].copy()
            values = attribute_bytes.view(scalar_type).reshape(n, component_count)
            return values[:, :3].astype(np.float32, copy=False)
        except Exception as e:
            print(f"[DragInteraction][WARNING] 读取 Position.buf 失败: {e}")
            return None

    def _get_reference_matrix_inv(self, comp):
        """烘焙参考物体世界矩阵的逆；留空自动解析（OffsetToVertexSpace 恒等时用单位阵）。"""
        ref = self.bake_reference_object
        if ref is not None:
            return ref.matrix_world.inverted()
        return None  # None = 单位变换

    def _get_non_mirror_mirror(self):
        """非镜像工作流补偿矩阵：检测场景网格是否带 _ssmt_non_mirror_workflow_processed 标记
        （导入时被 X 镜像处理）。命中返回 X 镜像矩阵；否则返回 None。

        优先用烘焙参考物体判定（用户显式指定时最可靠）；参考物体非网格/无标记时
        退回扫描场景全部网格。
        """
        marker = "_ssmt_non_mirror_workflow_processed"
        mirror = np.diag([-1.0, 1.0, 1.0, 1.0])

        def _marked(obj):
            try:
                return bool(obj.get(marker, False))
            except Exception:
                try:
                    return bool(getattr(obj, marker, False))
                except Exception:
                    return False

        ref = self.bake_reference_object
        if ref is not None and ref.type == 'MESH' and _marked(ref):
            print("[DragInteraction] 非镜像工作流：区域空物体矩阵已施加 X 镜像补偿（参考物体标记）")
            return mirror
        try:
            objects = bpy.data.objects
        except Exception:
            objects = None
        if objects is not None:
            for obj in objects:
                if obj.type == 'MESH' and _marked(obj):
                    print("[DragInteraction] 非镜像工作流：区域空物体矩阵已施加 X 镜像补偿")
                    return mirror
        return None

    def _evaluate_zone_field(self, positions, empty, settings, ref_matrix_inv, mirror=None, edge_verts=None):
        """对单个区域空物体求权重场（0..1），含 bbox sanity check。

        mirror: 非镜像工作流 X 镜像矩阵（(4,4) numpy）或 None。矩阵运算统一走
        numpy（mathutils.Matrix 可被 np.asarray 转换），便于无 Blender 环境测试。
        edge_verts: 网格边拓扑 (E,2)；surface_propagate 开启且提供时走沿表面传播
        （测地距离，权重不穿透到球体积覆盖的背面/对侧），否则回退体积球欧氏距离。
        """
        try:
            # 空物体世界坐标 → 模组局部空间（必要时先施加非镜像工作流补偿）
            ball_world = np.asarray(empty.matrix_world, dtype=np.float64).reshape(4, 4)
            if mirror is not None:
                ball_world = np.asarray(mirror, dtype=np.float64).reshape(4, 4) @ ball_world
            if ref_matrix_inv is not None:
                ball_matrix = np.asarray(ref_matrix_inv, dtype=np.float64).reshape(4, 4) @ ball_world
            else:
                ball_matrix = ball_world

            d = self._zone_distances(positions, ball_matrix, edge_verts)
            if d is None:
                return None
            field = np.asarray(
                self._shape_field(d, settings.brush_strength, settings.brush_falloff_k, self.mask_plateau),
                dtype=np.float32,
            )

            # bbox sanity check：影响球与顶点包围盒需有交集
            if float(field.max(initial=0.0)) < 1e-4:
                center = ball_matrix[:3, 3]
                print(
                    f"[DragInteraction][WARNING] 区域空物体 {empty.name} 的影响球与顶点包围盒无交集 "
                    f"(中心 {tuple(round(c,3) for c in center)})，坐标系可能错位"
                )
                return None
            return field
        except Exception as e:
            print(f"[DragInteraction][WARNING] 区域 {empty.name} 求场失败: {e}")
            return None

    def _zone_distances(self, positions, ball_matrix, edge_verts):
        """球局部距离数组：surface_propagate 开启且有拓扑时用沿表面传播距离
        （种子 = 离球心最近的表面顶点/接触点），否则用欧氏距离。矩阵不可逆返回 None。"""
        local = gb_core._to_ball_local(np.asarray(positions, dtype=np.float64), ball_matrix)
        if local is None:
            return None
        d2 = np.einsum("ij,ij->i", local, local)
        if getattr(self, "surface_propagate", True) and edge_verts is not None and len(edge_verts) > 0:
            seeds = np.zeros(local.shape[0], dtype=bool)
            seeds[int(np.argmin(d2))] = True
            return gb_core.surface_distances(local, edge_verts, seeds)
        return np.sqrt(np.maximum(d2, 0.0))

    def _shape_field(self, d, strength, falloff_k, plateau):
        """统一衰减形状：plateau>0 平台化（d≤平台满强度、边缘平滑过渡），否则
        高斯 exp(-k·d²)；d≥1 或不可达（inf）硬截止为 0。欧氏/测地距离共用。"""
        d = np.asarray(d, dtype=np.float64)
        if plateau is not None and float(plateau) > 0.0:
            edge = float(plateau)
            t = np.clip((d - edge) / max(1.0 - edge, 1e-6), 0.0, 1.0)
            s = t * t * (3.0 - 2.0 * t)
            field = float(strength) * (1.0 - s)
        else:
            field = float(strength) * np.exp(-float(falloff_k) * d * d)
        field[d >= 1.0] = 0.0
        field[~np.isfinite(d)] = 0.0
        return field

    def _plateau_field(self, positions, ball_matrix, strength, falloff_k, plateau):
        """平台化权重场（欧氏距离版）：d ≤ plateau 保持满强度（平台），
        plateau < d < 1 平滑降到 0，d ≥ 1 为 0。plateau <= 0 时退化为原始高斯
        （向后兼容；沿表面传播路径与体积球路径共用 _shape_field 形状）。"""
        local = gb_core._to_ball_local(np.asarray(positions, dtype=np.float64), ball_matrix)
        if local is None:
            return np.zeros(len(positions), dtype=np.float64)
        d2 = np.einsum("ij,ij->i", local, local)
        d = np.sqrt(np.maximum(d2, 0.0))
        return self._shape_field(d, strength, falloff_k, plateau)

    def _read_component_triangles(self, mod_export_path, sections, comp, vertex_count):
        """从组件各 part 的 IB 资源段 filename 读 R32_UINT 索引 → (M,3) 三角形；
        读不到返回 None（调用方回退体积球）。"""
        tris = []
        for part in comp["parts"]:
            res = part.get("ib_resource")
            lines = sections.get(f"[{res}]", [])
            rel = None
            for line in lines:
                if line.strip().startswith("filename ="):
                    rel = line.split("=", 1)[1].strip()
                    break
            if not rel:
                continue
            full = os.path.join(mod_export_path, rel.replace("\\", os.sep).replace("/", os.sep))
            if not os.path.exists(full):
                continue
            try:
                idx = np.fromfile(full, dtype=np.uint32)
                tris.append(idx[: (len(idx) // 3) * 3].reshape(-1, 3))
            except Exception:
                continue
        if not tris:
            return None
        t = np.concatenate(tris, axis=0)
        if vertex_count:
            t = t[(t < int(vertex_count)).all(axis=1)]
        return t if len(t) else None

    def _check_zone_radius_scale(self, zones):
        """影响半径与球尺度失配检查（导出时警告）。

        RubberInfluence(dist, radius) 必须在区域内显著衰减，鼠标命中点才能成为
        变形峰（“点哪拖哪”）；radius 远大于球半径时 R 在球内几乎平坦，整块近似
        刚体平移，视觉上像“抓住权重中心整块拖走”。原版 LEWDHAND 实测各区域
        radius ≈ 区域空间宽度的 0.3~2.2 倍；这里用保守阈值 2.5× 打警告。

        返回警告条数（便于单测断言）。
        """
        warned = 0
        for empty in zones:
            s = empty.ssmt_drag_zone
            zone_radius = float(s.radius) if s.radius > 0 else 0.25  # 0 = 继承回退档案
            m = np.asarray(empty.matrix_world, dtype=np.float64).reshape(4, 4)
            # 球世界半径 = 空物体缩放（gaussian_field 球局部 d<1）
            scale = float(np.mean(np.linalg.norm(m[:3, :3], axis=0)))
            if scale > 1e-6 and zone_radius > scale * 2.5:
                warned += 1
                print(
                    f"[DragInteraction][WARNING] 区域 {empty.name} 影响半径 {zone_radius:.3f} "
                    f"是球半径 {scale:.3f} 的 {zone_radius/scale:.1f} 倍 → 衰减在球内几乎平坦，"
                    f"变形会整块刚体动（看起来像抓住权重中心拖）。"
                    f"建议把该区域影响半径调到 {scale*1.0:.3f}~{scale*2.5:.3f}"
                )
        return warned

    def _collect_enabled_zones(self):
        zones = []
        for item in self.zone_objects:
            obj = item.zone_object
            if obj is None:
                print("[DragInteraction][WARNING] 区域空物体已被删除，跳过")
                continue
            if obj.ssmt_drag_zone.enabled:
                zones.append(obj)
            if len(zones) >= MAX_ZONES:
                break
        return zones

    # =======================================================================
    # 段生成（CustomShader / CommandList / Resource）与钩子注入、Present/Constants
    # =======================================================================
    # 段生成器（对照 Remielle_MT.ini 逐行核对；shader 原样，删减全在 ini 侧）
    # =======================================================================

    def _emit_sections(self, sections, components, ns):
        """生成 Resource / CustomShader / CommandList / Key 段（幂等：已存在跳过）。"""
        res = f"res/drag_interaction"
        sections.setdefault(DRAG_TAIL_MARKER, [])

        # ---- 全局共享资源 ----
        global_resources = {
            f"[ResourceDragDetectID_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragPinnedDetectID_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
            f"[ResourceDragPinnedDetectInfo_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragJiggleScreenState_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragPathProgressState_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 12"],
            f"[ResourceDragPathVectors_{ns}]": ["type = Buffer", "format = R32G32B32A32_FLOAT", f"filename = {res}/PathVectors.buf"],
            f"[ResourceDragViewportFrameAPI_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 16"],
            f"[ResourceDragBakeRT_{ns}]": [
                # 与原作 ResourceLLBakeRT 完全一致：bind_flags 声明渲染目标+着色器资源，
                # Bake 的 o0 = set_viewport 需要 RT 能力，cs-t3 读需要 SRV 能力；
                # 非法/缺失 bind_flags 会导致 RT 创建失败 → 标定无效 → 检测不执行
                "type = Texture2D",
                "mode = mono",
                "width = 8",
                "height = 2",
                "mips = 1",
                "array = 1",
                "msaa = 1",
                "msaa_quality = 0",
                "format = DXGI_FORMAT_R32G32B32A32_FLOAT",
                "bind_flags = render_target shader_resource",
            ],
        }
        for sec, lines in global_resources.items():
            sections.setdefault(sec, lines)

        # ---- Key 段（hold 型，置位 + post 归零；Alt 修饰键同时臂动 mode）----
        key_defs = [
            (f"[KeyDragInputManagerLMB_{ns}]", "VK_LBUTTON", [f"$ssmtdrag_lmb_down_{ns} = 1"], [f"$ssmtdrag_lmb_down_{ns} = 0"]),
            (f"[KeyDragInputManagerRMB_{ns}]", "VK_RBUTTON", [f"$ssmtdrag_rmb_down_{ns} = 1"], [f"$ssmtdrag_rmb_down_{ns} = 0"]),
            (f"[KeyDragInputManagerX_{ns}]", "X", [f"$ssmtdrag_x_down_{ns} = 1"], [f"$ssmtdrag_x_down_{ns} = 0"]),
        ]
        if self.grab_key == 'ALT':
            key_defs.append((
                f"[KeyDragInputManagerModifier_{ns}]", "VK_MENU",
                [f"$ssmtdrag_modifier_down_{ns} = 1", f"$ssmtdrag_mode_{ns} = 1"],
                [f"$ssmtdrag_modifier_down_{ns} = 0", f"$ssmtdrag_mode_{ns} = 0"],
            ))
        for sec, key, set_lines, post_lines in key_defs:
            if sec not in sections:
                lines = [f"key = {key}", "type = hold"] + set_lines + [f"post {p}" for p in post_lines]
                sections[sec] = lines

        # ---- 每组件资源 + Detect / Bake×8 / Jiggle / PinComponent 段 ----
        for comp in components:
            self._emit_component_resources(sections, comp, ns)
            self._emit_detect_section(sections, comp, ns)
            self._emit_bake_sections(sections, comp, ns)
            self._emit_jiggle_section(sections, comp, ns)
            self._emit_pin_component_section(sections, comp, ns)

        # ---- 全局 Pin / UpdateScreenJiggle / CommandList 段 ----
        self._emit_pin_detected_section(sections, ns)
        self._emit_update_screen_jiggle_section(sections, ns)
        self._emit_command_lists(sections, components, ns)

        # ---- 手型光标（S8）：资源 + 绘制段 ----
        if self.enable_hand_cursor:
            self._emit_hand_resources(sections, ns)
            self._emit_hand_sections(sections, ns)

        # ---- 视口探针系统：资源 + 探针段 + ShaderOverride ----
        if self.enable_viewport_probe:
            self._emit_viewport_probe_sections(sections, ns)

    def _emit_component_resources(self, sections, comp, ns):
        cn = comp["comp_name"]
        stem = comp["base_name"]
        res_dir = self._buffer_dir(sections, comp)
        zones = self._collect_enabled_zones()
        mask_count = max(1, (max(len(zones), 1) + 3) // 4)

        comp_resources = {
            f"[ResourceDragDetect{cn}ObjectMap_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res_dir}/{stem}ObjectMap.buf",
            ],
            f"[ResourceDragDebugDetect_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 23"],
            f"[ResourceDragComponentDetect_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragPinnedComponentID_{cn}_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
            f"[ResourceDragPinnedComponentInfo_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragJiggleState_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 10"],
            # TempVB0：空声明段（type=RWBuffer，无 format/array），copy 往返后换绑
            f"[ResourceDragJiggleTempVB0_{cn}_{ns}]": ["type = RWBuffer"],
        }
        # 恒定 3 个掩码资源段（原作契约）：Detect/Jiggle 固定绑定 cs-t5/cs-t7/cs-t66/cs-t69，
        # 缺段会成为悬空引用，导致 CustomShader 段加载失败（导出模组无拉扯+抖动的主因之一）
        for i in range(3):
            comp_resources[f"[ResourceDragJiggleMasks{i}_{cn}_{ns}]"] = [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res_dir}/{stem}JiggleMasks{i}.buf",
            ]
        # JiggleParams：每部件 1 条碰撞体档案（原作契约，条目同值；array = 4×部件数）
        n_parts = max(1, len(comp["parts"]))
        jp = self._jiggle_params_data(n_parts)
        comp_resources[f"[ResourceDragJiggleParams_{cn}_{ns}]"] = [
            "type = Buffer", "format = R32G32B32A32_FLOAT", f"array = {4 * n_parts}",
            f"data = {jp}",
        ]
        for sec, lines in comp_resources.items():
            sections.setdefault(sec, lines)

    def _jiggle_params_data(self, collider_count=1):
        """每条 16 float：objectID radius strength falloff dragScale grabDamping grabSpring
        releaseDamping releaseSpring releaseKick maxOffset targetFollow mouseYDir mouseXDir 0 0。
        按部件数重复（原作契约：每部件一条同值档案，Body/Hair=2、Legs=1）。
        releaseKick=1.18 / targetFollow=0.12 为原作调好的标准碰撞档案——注意这与
        IniParams[71] 回退槽位（POLISH_PARAMS.y=releaseKick、.w=targetFollow，
        phys_target_follow/phys_release_kick 属性在那里恰好交叉喂到正确值）是两回事，
        缓冲档案必须用原作精确值，不能与属性混用。"""
        entry = [
            0, 0.25, 1.0, 1.5, 1.0,
            self.phys_grab_damping, self.phys_grab_spring,
            self.phys_release_damping, self.phys_release_spring,
            1.18, 0.5, 0.12,
            0, 0, 0, 0,
        ]
        vals = entry * max(1, collider_count)
        return " ".join(self._fmt(v) for v in vals)

    @staticmethod
    def _fmt(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        return str(int(f)) if f == int(f) else repr(f)

    # ---- zone 寄存器行（77-124，115 空档跳过；path=0 显式写）----

    def _zone_register_lines(self, ns, comp_tag, path_zero=True):
        """生成全部 zone 寄存器行（读必设纪律：每消费段写完整块）。"""
        zones = self._collect_enabled_zones()
        lines = []
        for param, regs in ZONE_REGISTER_MAP.items():
            is_path = (param == "path")
            is_grabbable = (param == "grabbable")
            for reg_group_idx, reg in enumerate(regs):
                comps = []
                for chan_idx in range(4):
                    zone_idx = reg_group_idx * 4 + chan_idx
                    comps.append(self._zone_value(param, zone_idx, zones, ns, comp_tag, is_path, is_grabbable))
                lines.extend([
                    f"x{reg} = {comps[0]}",
                    f"y{reg} = {comps[1]}",
                    f"z{reg} = {comps[2]}",
                    f"w{reg} = {comps[3]}",
                ])
        return lines

    def _zone_value(self, param, zone_idx, zones, ns, comp_tag, is_path, is_grabbable):
        if is_path:
            return "0"  # path 永不进入，显式写 0（读必设）
        if zone_idx >= len(zones):
            # 无任何区域时存在隐式回退 zone0（掩码全 1）：它的 grabbable 必须为 1，
            # 否则 ZoneGrabbable(0)=false → newCapture 永不开始（无拖拽效果的根因）。
            # 超出启用区域数的其余槽位：掩码为 0 不会命中，grabbable/参数全部写 0。
            if is_grabbable and zone_idx == 0 and not zones:
                return "1"
            return "0"
        empty = zones[zone_idx]
        s = empty.ssmt_drag_zone
        if is_grabbable:
            return "1" if s.grabbable else "0"
        val = getattr(s, param, 0.0)
        if param in ZONE_FALLBACK_PARAMS and float(val) > 0.0:
            return self._fmt(val)
        return "0"  # 0 = 继承回退

    # ---- Detect{Comp}（dispatch 1,1,1）----

    def _emit_detect_section(self, sections, comp, ns):
        cn = comp["comp_name"]
        sec = f"[CustomShaderDragDetect{cn}_{ns}]"
        if sec in sections:
            return
        lines = [
            f"cs = {RES_SHADER_DIR}/rzm_object_detect.hlsl",
            "x28 = 0",
            "cs-t0 = vb0",
            "cs-t1 = ib",
            f"cs-t2 = ResourceDragDetect{cn}ObjectMap_{ns}",
            f"cs-t3 = ResourceDragBakeRT_{ns}",
            f"cs-t4 = ResourceDragJiggleMasks0_{cn}_{ns}",
            f"cs-t5 = ResourceDragJiggleMasks1_{cn}_{ns}",
            f"cs-t7 = ResourceDragJiggleMasks2_{cn}_{ns}",
            f"cs-t6 = ResourceDragViewportFrameAPI_{ns}",
            f"cs-u0 = ResourceDragDetectID_{ns}",
            f"cs-u1 = ResourceDragComponentDetect_{cn}_{ns}",
            f"cs-u2 = ResourceDragDebugDetect_{cn}_{ns}",
            "x24 = $cursorX", "y24 = $cursorY", "z24 = $screenW", "w24 = $screenH",
            "x25 = $isMouseButtonDown",
            "x26 = 48", "w26 = 8.0",
            "x27 = $cursorX", "y27 = $cursorY", "z27 = res_width", "w27 = res_height",
            "x85 = 0", "y85 = 0", "z85 = 1", "w85 = 1",  # 视口恒等（offset=0,0 scale=1,1）
            # VIEWPORT_VALID 必须为 1：shader L720 的检测主循环被
            # ValidViewportCursor 门控（要求 IniParams[86].x > 0.5），x86=0 会让检测永远不执行
            "x86 = 1",
            "x74 = 0",  # debug dump 门控关闭
            "dispatch = 1, 1, 1",
            "cs-u0 = null", "cs-u1 = null", "cs-u2 = null",
        ]
        sections[sec] = lines

    # ---- Bake{Comp}{Part} + Sample×8 ----

    def _emit_bake_sections(self, sections, comp, ns):
        cn = comp["comp_name"]
        for p_idx, part in enumerate(comp["parts"]):
            part_tag = f"{cn}P{p_idx}"
            bake_sec = f"[CustomShaderDragBake{part_tag}_{ns}]"
            if bake_sec in sections:
                continue
            lines = [
                "run = BuiltInCommandListUnbindAllRenderTargets",
                f"clear = ResourceDragBakeRT_{ns} 0.0",
            ]
            ib_res = part["ib_resource"]
            step = part["index_count"] // 8
            ib_first_index = part["ib_first_index"]
            for i in range(8):
                sample_sec = f"[CustomShaderDragBakeSample{i}_{part_tag}_{ns}]"
                offset = ib_first_index + i * step
                lines.append(f"run = CustomShaderDragBakeSample{i}_{part_tag}_{ns}")
                sections[sample_sec] = [
                    f"gs = {RES_SHADER_DIR}/rzm_gs_probe.hlsl",
                    f"gs-t1 = {ib_res}",
                    f"ps = {RES_SHADER_DIR}/rzm_gs_probe.hlsl",
                    "topology = point_list",
                    f"o0 = set_viewport no_view_cache ResourceDragBakeRT_{ns}",
                    f"x26 = {i}",
                    f"y26 = {offset}",
                    f"drawindexed = 1, {offset}, 0",
                ]
            sections[bake_sec] = lines

    # ---- Jiggle{Comp}（TempVB0 序列 + 完整寄存器块）----

    def _emit_jiggle_section(self, sections, comp, ns):
        cn = comp["comp_name"]
        sec = f"[CustomShaderDragJiggle{cn}_{ns}]"
        if sec in sections:
            return
        vertex_count = comp["vertex_count"] or 100000
        dispatch_n = (vertex_count + 255) // 256
        tag = f"{self._hash_to_resource_prefix(comp['comp_name']).lower()}"

        lines = [
            "local $CursorXPast", "local $CursorYPast", "local $WasMouseButtonDown",
            "local $back_x69 = x69", "local $back_y69 = y69",
            "local $back_z69 = z69", "local $back_w69 = w69",
            "",
            "if $isMouseButtonDown == 1",
            "\tif $WasMouseButtonDown == 0",
            "\t\t$CursorXPast = $cursorX", "\t\t$CursorYPast = $cursorY",
            "\tendif",
            "\t$WasMouseButtonDown = 1", "\tw67 = 1",
            "else",
            "\t$WasMouseButtonDown = 0", "\t$CursorXPast = 0", "\t$CursorYPast = 0", "\tw67 = 0",
            "endif",
            "",
            f"cs = {RES_SHADER_DIR}/rzm_jiggle_interaction.hlsl",
            "x67 = $CursorXPast", "y67 = $CursorYPast",
            "x68 = 0.25", "y68 = 1.00", "z68 = 1.50", "w68 = 1.00",  # 回退档案
            "x69 = $cursorX", "y69 = $cursorY", "z69 = $screenW", "w69 = $screenH",
            "x70 = " + self._fmt(self.phys_grab_damping),
            "y70 = " + self._fmt(self.phys_grab_spring),
            "z70 = " + self._fmt(self.phys_release_damping),
            "w70 = " + self._fmt(self.phys_release_spring),
            "x71 = 0.50", "y71 = " + self._fmt(self.phys_target_follow),
            "z71 = 1.00", "w71 = " + self._fmt(self.phys_release_kick),
            f"x72 = {max(1, len(comp['parts']))}",  # 碰撞体条数 = 部件数（原作契约）
            "y72 = " + self._fmt(self.mult_radius),
            "z72 = " + self._fmt(self.mult_strength),
            "w72 = " + self._fmt(self.mult_spring),
            "x73 = " + self._fmt(self.mult_damping), "y73 = 1.00",
            "x74 = 0",  # debug dump 关闭
            "x75 = 0",  # vertex debug 关闭
            f"x76 = $ssmtdrag_delta_time_{ns}",
            f"y76 = $ssmtdrag_sim_speed_{ns}",
            f"z76 = $ssmtdrag_max_step_{ns}",
        ]
        lines.extend(self._zone_register_lines(ns, tag, path_zero=True))
        lines.extend([
            f"cs-t67 = ResourceDragPinnedComponentInfo_{cn}_{ns}",
            f"cs-t68 = ResourceDragJiggleParams_{cn}_{ns}",
            f"cs-t65 = ResourceDragJiggleMasks0_{cn}_{ns}",
            f"cs-t66 = ResourceDragJiggleMasks1_{cn}_{ns}",
            f"cs-t69 = ResourceDragJiggleMasks2_{cn}_{ns}",
            f"cs-t71 = ResourceDragJiggleScreenState_{ns}",
            f"cs-t73 = ResourceDragPathVectors_{ns}",
            f"cs-t74 = ResourceDragPathProgressState_{ns}",
            f"cs-u6 = ResourceDragJiggleState_{cn}_{ns}",
            "",
            f"ResourceDragJiggleTempVB0_{cn}_{ns} = vb0",
            "cs-t24 = vb0",
            f"cs-u5 = copy ResourceDragJiggleTempVB0_{cn}_{ns}",
            "",
            f"Dispatch = {dispatch_n}, 1, 1",
            "",
            "vb0 = null",
            f"ResourceDragJiggleTempVB0_{cn}_{ns} = copy cs-u5",
            "cs-u5 = null", "cs-u6 = null", "cs-t71 = null",
            "",
            "post x69 = $back_x69", "post y69 = $back_y69",
            "post z69 = $back_z69", "post w69 = $back_w69",
        ])
        sections[sec] = lines

    # ---- PinComponent{Comp} ----

    def _emit_pin_component_section(self, sections, comp, ns):
        cn = comp["comp_name"]
        sec = f"[CustomShaderDragPinComponent{cn}_{ns}]"
        if sec in sections:
            return
        sections[sec] = [
            f"cs = {RES_SHADER_DIR}/rzm_pin_detected.hlsl",
            f"cs-u0 = ResourceDragComponentDetect_{cn}_{ns}",
            f"cs-u1 = ResourceDragPinnedComponentID_{cn}_{ns}",
            f"cs-u2 = ResourceDragPinnedComponentInfo_{cn}_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-u2 = null",
        ]

    # ---- 全局 Pin（槽 10 注入光标 x24..w24）----

    def _emit_pin_detected_section(self, sections, ns):
        sec = f"[CustomShaderDragPinDetected_{ns}]"
        if sec in sections:
            return
        sections[sec] = [
            f"cs = {RES_SHADER_DIR}/rzm_pin_detected.hlsl",
            "x24 = $cursorX", "y24 = $cursorY", "z24 = $screenW", "w24 = $screenH",
            f"cs-u0 = ResourceDragDetectID_{ns}",
            f"cs-u1 = ResourceDragPinnedDetectID_{ns}",
            f"cs-u2 = ResourceDragPinnedDetectInfo_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-u2 = null",
        ]

    # ---- UpdateScreenJiggle（y72=1.0 非 mult_radius，照原作不对称）----

    def _emit_update_screen_jiggle_section(self, sections, ns):
        sec = f"[CustomShaderDragUpdateScreenJiggle_{ns}]"
        if sec in sections:
            return
        tag = ns
        lines = [
            "local $LLScreenCursorXPast", "local $LLScreenCursorYPast", "local $LLScreenWasMouseDown",
            "",
            "if $isMouseButtonDown == 1",
            "\tif $LLScreenWasMouseDown == 0",
            "\t\t$LLScreenCursorXPast = $cursorX", "\t\t$LLScreenCursorYPast = $cursorY",
            "\tendif",
            "\t$LLScreenWasMouseDown = 1", "\tw67 = 1",
            "else",
            "\t$LLScreenWasMouseDown = 0", "\t$LLScreenCursorXPast = 0", "\t$LLScreenCursorYPast = 0", "\tw67 = 0",
            "endif",
            "",
            f"cs = {RES_SHADER_DIR}/rzm_jiggle_screen_state.hlsl",
            "x67 = $LLScreenCursorXPast", "y67 = $LLScreenCursorYPast",
            "x68 = 0.25", "y68 = 1.0", "z68 = 1.5", "w68 = 1.0",
            "x69 = $cursorX", "y69 = $cursorY", "z69 = $screenW", "w69 = $screenH",
            "x70 = " + self._fmt(self.phys_grab_damping),
            "y70 = " + self._fmt(self.phys_grab_spring),
            "z70 = " + self._fmt(self.phys_release_damping),
            "w70 = " + self._fmt(self.phys_release_spring),
            "x71 = 0.50", "y71 = " + self._fmt(self.phys_target_follow),
            "z71 = 1.00", "w71 = " + self._fmt(self.phys_release_kick),
            "x72 = 0", "y72 = 1.0",  # 照原作不对称：此处 y72=1.0 非 mult_radius
            "z72 = " + self._fmt(self.mult_strength),
            "w72 = " + self._fmt(self.mult_spring),
            "x73 = " + self._fmt(self.mult_damping),
            "y73 = 1.00", "z73 = 1.00",  # depth_pull 默认 1.0
            f"x97 = $ssmtdrag_release_boost_{ns}",
            f"y97 = $ssmtdrag_release_decay_{ns}",
            f"x76 = $ssmtdrag_delta_time_{ns}",
            f"y76 = $ssmtdrag_sim_speed_{ns}",
            f"z76 = $ssmtdrag_max_step_{ns}",
            "x84 = " + (f"$ssmtdrag_poke_sign_{ns}" if self.enable_poke else "0"),
            "y84 = " + (f"$ssmtdrag_poke_hold_mult_{ns}" if self.enable_poke else "0"),
            "z84 = " + (f"$ssmtdrag_poke_hold_frames_{ns}" if self.enable_poke else "0"),
            "w84 = 0",  # 蓄力禁用
        ]
        lines.extend(self._zone_register_lines(ns, tag, path_zero=True))
        lines.extend([
            f"cs-t67 = ResourceDragPinnedDetectInfo_{ns}",
            f"cs-u0 = ResourceDragJiggleScreenState_{ns}",
            f"cs-u1 = ResourceDragPathProgressState_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-t67 = null",
        ])
        sections[sec] = lines

    # ---- CommandList：PinDetected（boot-clear + dt 钳制 + 门槛）/ Viewport / Cursor ----

    def _emit_command_lists(self, sections, components, ns):
        # PinDetected
        pin_sec = f"[CommandListDragPinDetected_{ns}]"
        if pin_sec not in sections:
            lines = [
                # boot-clear：RWBuffer 初始内容未定义，首帧垃圾会假命中/假位移
                f"if $ssmtdrag_booted_{ns} == 0",
                f"\tclear = ResourceDragDetectID_{ns} 0.0",
                f"\tclear = ResourceDragPinnedDetectID_{ns} 0.0",
                f"\tclear = ResourceDragPinnedDetectInfo_{ns} 0.0",
                f"\tclear = ResourceDragJiggleScreenState_{ns} 0.0",
                f"\tclear = ResourceDragPathProgressState_{ns} 0.0",
                f"\tclear = ResourceDragViewportFrameAPI_{ns} 0.0",
            ]
            if self.enable_hand_cursor:
                lines.append(f"\tclear = ResourceDragJiggleCursorPreview_{ns} 0.0")
            for comp in components:
                cn = comp["comp_name"]
                lines.extend([
                    f"\tclear = ResourceDragComponentDetect_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragPinnedComponentID_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragPinnedComponentInfo_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragJiggleState_{cn}_{ns} 0.0",
                ])
            lines.extend([
                f"\t$ssmtdrag_booted_{ns} = 1",
                "endif",
                "",
                "local $ssmtdrag_detect_next_time",
                "local $ssmtdrag_detect_interval = 0.25",
                # dt 钳制 [0.001, 0.100]（time 单位分钟 → 秒）
                f"if $ssmtdrag_prev_time_{ns} == 0",
                f"\t$ssmtdrag_delta_time_{ns} = 0.0166667",
                "else",
                f"\t$ssmtdrag_delta_time_{ns} = (time - $ssmtdrag_prev_time_{ns}) * 60.0",
                f"\tif $ssmtdrag_delta_time_{ns} > 0.100",
                f"\t\t$ssmtdrag_delta_time_{ns} = 0.100",
                f"\telif $ssmtdrag_delta_time_{ns} < 0.001",
                f"\t\t$ssmtdrag_delta_time_{ns} = 0.001",
                "\tendif",
                "endif",
                f"$ssmtdrag_prev_time_{ns} = time",
                "",
                "if $isMouseButtonDown == 1",
                "\t$ssmtdrag_detect_next_time = time",
                "endif",
                "",
                f"if $inputMode == 0 && $ssmtdrag_mode_{ns} == 1 && $ssmtdrag_drawn_{ns} == 1",
                f"\t$ObjectDetectAllowed_{ns} = 1",
                f"\trun = CustomShaderDragPinDetected_{ns}",
            ])
            for comp in components:
                lines.append(f"\trun = CustomShaderDragPinComponent{comp['comp_name']}_{ns}")
            lines.extend([
                f"\trun = CustomShaderDragUpdateScreenJiggle_{ns}",
                "else",
                f"\t$ObjectDetectAllowed_{ns} = 0",
                "endif",
            ])
            sections[pin_sec] = lines

        # ViewportUpdate
        vp_sec = f"[CommandListDragViewportUpdate_{ns}]"
        if vp_sec not in sections:
            sections[vp_sec] = [
                "$ssmtdrag_viewport_valid = 0",
                "$screenW = 1", "$screenH = 1",
                "if window_width > 0 && window_height > 0 && window_width <= 8192 && window_height <= 8192",
                "\t$screenW = window_width", "\t$screenH = window_height", "\t$ssmtdrag_viewport_valid = 1",
                "elif rt_width > 0 && rt_height > 0 && rt_width <= 8192 && rt_height <= 8192",
                "\t$screenW = rt_width", "\t$screenH = rt_height", "\t$ssmtdrag_viewport_valid = 1",
                "elif res_width > 0 && res_height > 0 && res_width <= 8192 && res_height <= 8192",
                "\t$screenW = res_width", "\t$screenH = res_height", "\t$ssmtdrag_viewport_valid = 1",
                "endif",
            ]

        # CursorUpdate
        cu_sec = f"[CommandListDragCursorUpdate_{ns}]"
        if cu_sec not in sections:
            sections[cu_sec] = [
                f"run = CommandListDragViewportUpdate_{ns}",
                "if $inputMode == 0 && $ssmtdrag_viewport_valid == 1",
                "\tif cursor_x > 0 && cursor_y > 0 && cursor_x < 1 && cursor_y < 1",
                "\t\t$cursorX = cursor_x", "\t\t$cursorY = 1.0 - cursor_y",
                "\telif cursor_window_x > 0 && cursor_window_y > 0 && cursor_window_x < 1 && cursor_window_y < 1",
                "\t\t$cursorX = cursor_window_x", "\t\t$cursorY = 1.0 - cursor_window_y",
                "\telif cursor_screen_x >= 0 && cursor_screen_y >= 0 && cursor_screen_x <= $screenW && cursor_screen_y <= $screenH",
                "\t\t$cursorX = cursor_screen_x / $screenW", "\t\t$cursorY = 1.0 - cursor_screen_y / $screenH",
                "\telse",
                "\t\t$cursorX = -1", "\t\t$cursorY = -1",
                "\tendif",
                "endif",
                "if $ssmtdrag_viewport_valid == 0",
                "\t$cursorX = -1", "\t$cursorY = -1",
                "endif",
                "if $inputMode == 0",
                "\t$cursorX = $cursorX * $screenW", "\t$cursorY = $cursorY * $screenH",
                "endif",
                "x24 = $cursorX", "y24 = $cursorY", "z24 = $screenW", "w24 = $screenH",
            ]

    # ===================================================================
    # 手型光标（S8）：资源声明 + 绘制段（对照原作 ResourceLLHand* /
    # CustomShaderLLUpdateJiggleCursorPreview / LLJiggleCursor / LLPresentHand×4）
    # ===================================================================

    def _emit_hand_resources(self, sections, ns):
        res = RES_SHADER_DIR
        hand_resources = {
            # 手部屏幕位置暂存（UpdateJiggleCursorPreview 每帧写入，手部 VS 读取）
            f"[ResourceDragJiggleCursorPreview_{ns}]": [
                "type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 4",
            ],
            f"[ResourceDragHandActionVB_{ns}]": [
                "type = Buffer", "stride = 28", f"filename = {res}/HandAction.buf",
            ],
            f"[ResourceDragHandActionIB_{ns}]": [
                "type = Buffer", "format = R32_UINT", f"filename = {res}/HandAction.ib",
            ],
            f"[ResourceDragHandNoActionVB_{ns}]": [
                "type = Buffer", "stride = 28", f"filename = {res}/HandNoAction.buf",
            ],
            f"[ResourceDragHandNoActionIB_{ns}]": [
                "type = Buffer", "format = R32_UINT", f"filename = {res}/HandNoAction.ib",
            ],
            # 烘焙的平滑逐顶点法线（描边外扩用；索引与位置缓冲对齐）
            f"[ResourceDragHandActionNormal_{ns}]": [
                "type = Buffer", "format = R32G32B32_FLOAT", f"filename = {res}/HandAction_Normal.buf",
            ],
            f"[ResourceDragHandNoActionNormal_{ns}]": [
                "type = Buffer", "format = R32G32B32_FLOAT", f"filename = {res}/HandNoAction_Normal.buf",
            ],
        }
        for sec, lines in hand_resources.items():
            sections.setdefault(sec, lines)

    def _hand_param_lines(self, ns, outline):
        """PresentHand 四段共享的 IniParams 寄存器行（83/89-95；描边加 98，y98 为描边标记）。"""
        lines = [
            f"x83 = $ssmtdrag_hand_surface_clip_{ns}",
            f"y83 = $ssmtdrag_hand_surface_lift_{ns}",
            f"z83 = $ssmtdrag_hand_surface_softness_{ns}",
            f"x89 = $ssmtdrag_hand_center_x_{ns}",
            f"y89 = $ssmtdrag_hand_center_y_{ns}",
            f"z89 = $ssmtdrag_hand_center_z_{ns}",
            f"x90 = $ssmtdrag_hand_scale_{ns}",
            f"y90 = $ssmtdrag_hand_opacity_{ns}",
            f"x91 = $ssmtdrag_lmb_hold_fraction_{ns} / $ssmtdrag_hand_windup_time_{ns}",
            "y91 = time",
            f"z91 = $ssmtdrag_rmb_hold_fraction_{ns} / $ssmtdrag_hand_rmb_windup_time_{ns}",
            f"x92 = $ssmtdrag_hand_tilt_min_{ns}",
            f"y92 = $ssmtdrag_hand_tilt_rmb_min_{ns}",
            f"z92 = $ssmtdrag_hand_tilt_max_deg_{ns}",
            f"w92 = $ssmtdrag_hand_tilt_rmb_max_deg_{ns}",
            f"x93 = $ssmtdrag_hand_vibrate_threshold_{ns}",
            f"y93 = $ssmtdrag_hand_vibrate_period_slow_{ns}",
            f"z93 = $ssmtdrag_hand_vibrate_period_fast_{ns}",
            f"w93 = $ssmtdrag_hand_vibrate_amplitude_{ns}",
            f"x94 = $ssmtdrag_hand_upright_max_deg_{ns}",
            f"z94 = $ssmtdrag_hand_tilt_grab_max_deg_{ns}",
            f"x95 = $ssmtdrag_hand_reference_height_{ns}",
        ]
        if outline:
            lines.extend([
                f"x98 = $ssmtdrag_hand_outline_width_{ns}",
                "y98 = 1.0",
                f"z98 = $ssmtdrag_hand_outline_opacity_{ns}",
            ])
        else:
            lines.append("y98 = 0.0")
        return lines

    def _emit_hand_sections(self, sections, ns):
        # UpdateJiggleCursorPreview：抓取点/光标 → 手部屏幕位置（每帧先于手部绘制运行）
        preview_sec = f"[CustomShaderDragUpdateJiggleCursorPreview_{ns}]"
        if preview_sec not in sections:
            sections[preview_sec] = [
                f"cs = {RES_SHADER_DIR}/rzm_jiggle_cursor_preview.hlsl",
                f"cs-t67 = ResourceDragJiggleScreenState_{ns}",
                f"cs-t68 = ResourceDragPinnedDetectInfo_{ns}",
                f"cs-u0 = ResourceDragJiggleCursorPreview_{ns}",
                "dispatch = 1, 1, 1",
                "post cs-t67 = null", "post cs-t68 = null", "post cs-u0 = null",
            ]

        # JiggleCursor：十字准星（hand_debug == 2 时额外绘制）
        cursor_sec = f"[CustomShaderDragJiggleCursor_{ns}]"
        if cursor_sec not in sections:
            sections[cursor_sec] = [
                f"vs = {RES_SHADER_DIR}/rzm_jiggle_cursor.hlsl",
                f"ps = {RES_SHADER_DIR}/rzm_jiggle_cursor.hlsl",
                "blend = ADD SRC_ALPHA INV_SRC_ALPHA",
                "cull = none",
                "topology = triangle_strip",
                "o0 = set_viewport bb",
                f"vs-t67 = ResourceDragJiggleCursorPreview_{ns}",
                "Draw = 4, 0",
                "post vs-t67 = null",
            ]

        # PresentHand ×4：NoAction/Action × 填充/描边（描边 y98=1 先画垫底，只露轮廓边）
        for action in (False, True):
            for outline in (False, True):
                tag = ("Action" if action else "") + ("Outline" if outline else "")
                sec = f"[CustomShaderDragPresentHand{tag}_{ns}]"
                if sec in sections:
                    continue
                mesh = "Action" if action else "NoAction"
                lines = [
                    f"vs = {RES_SHADER_DIR}/rzm_jiggle_hand.hlsl",
                    f"ps = {RES_SHADER_DIR}/rzm_jiggle_hand.hlsl",
                    "blend = ADD SRC_ALPHA INV_SRC_ALPHA",
                    "cull = none",
                    "topology = triangle_list",
                    "o0 = set_viewport bb",
                    f"vs-t67 = ResourceDragJiggleCursorPreview_{ns}",
                    f"vs-t68 = ResourceDragJiggleScreenState_{ns}",
                    f"vs-t69 = ResourceDragHand{mesh}Normal_{ns}",
                ]
                lines.extend(self._hand_param_lines(ns, outline))
                lines.extend([
                    f"vb0 = ResourceDragHand{mesh}VB_{ns}",
                    f"ib = ResourceDragHand{mesh}IB_{ns}",
                    # 254 quads → 508 tris → 1524 indices（与手部 IB 资产一致）
                    "DrawIndexed = 1524, 0, 0",
                    "post vs-t67 = null", "post vs-t68 = null", "post vs-t69 = null",
                    "post vb0 = null", "post ib = null",
                ])
                sections[sec] = lines

    # =======================================================================
    # 视口探针系统：抓角色渲染 RT → 分析视口矩形 → ViewportFrameAPI
    #（子区域渲染/角色查看器时校正光标映射；全屏等比时探针产出恒等，无害）
    # =======================================================================

    def _emit_viewport_probe_sections(self, sections, ns):
        # 资源：ViewportSource/LayoutT0 为动态 ref 占位（运行时由钩子快照/探针赋值）
        probe_resources = {
            f"[ResourceDragViewportSource_{ns}]": [],
            f"[ResourceDragViewportLayoutT0_{ns}]": [],
            f"[ResourceDragViewportLayoutData_{ns}]": [
                "type = Texture2D", "mode = mono", "width = 8", "height = 1",
                "mips = 1", "array = 1", "msaa = 1", "msaa_quality = 0",
                "format = DXGI_FORMAT_R32G32B32A32_FLOAT",
                "bind_flags = render_target shader_resource",
            ],
        }
        for sec, lines in probe_resources.items():
            sections.setdefault(sec, lines)

        # ShaderOverride：拦截游戏 UI 绘制帧，armed 时跑探针（hash 为 ZZZ UI 帧，照原作）
        so = f"[ShaderOverrideDragViewportLayoutProbe_{ns}]"
        if so not in sections:
            sections[so] = [
                "hash = cdc90aee00e7900d",
                "allow_duplicate_hash = true",
                f"if $ssmtdrag_viewport_probe_armed_{ns} == 1",
                f"\tpost run = CustomShaderDragViewportLayoutProbe_{ns}",
                "endif",
            ]

        # GS 探针：读 ViewportSource（角色 RT）→ 8×1 LayoutData
        probe_sec = f"[CustomShaderDragViewportLayoutProbe_{ns}]"
        if probe_sec not in sections:
            sections[probe_sec] = [
                f"ResourceDragViewportLayoutT0_{ns} = ref ps-t0",
                "run = BuiltInCommandListUnbindAllRenderTargets",
                "blend = ADD ONE ZERO",
                "alpha = ADD ONE ZERO",
                f"vs = {RES_SHADER_DIR}/rzm_viewport_layout_vs.hlsl",
                f"gs = {RES_SHADER_DIR}/rzm_viewport_layout_probe.hlsl",
                f"ps = {RES_SHADER_DIR}/rzm_viewport_layout_probe.hlsl",
                f"gs-t0 = ResourceDragViewportLayoutT0_{ns}",
                f"ps-t0 = ResourceDragViewportLayoutT0_{ns}",
                f"gs-t67 = ResourceDragViewportSource_{ns}",
                f"ps-t67 = ResourceDragViewportSource_{ns}",
                "topology = triangle_list",
                f"o0 = set_viewport no_view_cache ResourceDragViewportLayoutData_{ns}",
                f"x88 = $ssmtdrag_viewport_probe_generation_{ns}",
                "if draw_type == 2",
                "\tdrawindexed = INDEX_COUNT, FIRST_INDEX, FIRST_VERTEX",
                "elif draw_type == 4",
                "\tdrawindexedinstanced = INDEX_COUNT, INSTANCE_COUNT, FIRST_INDEX, FIRST_VERTEX, FIRST_INSTANCE",
                "else",
                "\tdrawindexed = auto",
                "endif",
            ]

        # Decode CS：LayoutData → ViewportFrameAPI（检测着色器 cs-t6 读取它映射光标）
        decode_sec = f"[CustomShaderDragViewportLayoutDecode_{ns}]"
        if decode_sec not in sections:
            sections[decode_sec] = [
                f"cs = {RES_SHADER_DIR}/rzm_viewport_layout_decode.hlsl",
                f"cs-t0 = ResourceDragViewportLayoutData_{ns}",
                f"cs-u0 = ResourceDragViewportFrameAPI_{ns}",
                f"x88 = $ssmtdrag_viewport_probe_generation_{ns}",
                "dispatch = 1, 1, 1",
                "post cs-t0 = null", "post cs-u0 = null",
            ]

    # =======================================================================
    # 钩子注入（ib= 之后、第一个 run=/drawindexed= 之前；跳过 ib=null）
    # =======================================================================

    def _inject_draw_hooks(self, sections, comp, ns):
        cn = comp["comp_name"]
        for p_idx, part in enumerate(comp["parts"]):
            section = part["section"]
            lines = sections.get(section)
            if not lines:
                continue
            part_tag = f"{cn}P{p_idx}"
            hook_marker = f"DRAG HOOK BEGIN {part_tag}_{ns}"
            if any(hook_marker in line for line in lines):
                continue
            hook = self._build_hook_block(comp, p_idx, ns)
            insert_at = self._find_part_hook_insert_index(lines, part)
            new_lines = lines[:insert_at] + hook + lines[insert_at:]
            sections[section] = new_lines

    @classmethod
    def _find_part_hook_insert_index(cls, lines, part):
        anchor_comment = part.get("hook_anchor_comment")
        if anchor_comment:
            occurrence = int(part.get("hook_anchor_occurrence", 0) or 0)
            matches = [
                index for index, line in enumerate(lines)
                if line.strip() == anchor_comment
            ]
            if occurrence < len(matches):
                return matches[occurrence]
        return cls._find_hook_insert_index(lines)

    @staticmethod
    def _find_hook_insert_index(lines):
        """ib= 绑定行之后、第一个 run=/drawindexed= 之前。"""
        ib_idx = -1
        for i, line in enumerate(lines):
            s = line.strip().lower()
            if s.startswith("ib ="):
                ib_idx = i
        # 在 ib 之后找第一个 run= 或 drawindexed=
        for i in range(ib_idx + 1, len(lines)):
            s = line.strip().lower()
            if s.startswith("run =") or s.startswith("drawindexed ="):
                return i
        # 没有 run/drawindexed（异常）：插到 ib 后一行
        return ib_idx + 1

    def _build_hook_block(self, comp, p_idx, ns):
        cn = comp["comp_name"]
        part_tag = f"{cn}P{p_idx}"
        temp_vb0 = f"ResourceDragJiggleTempVB0_{cn}_{ns}"
        last_dispatch = f"$ssmtdrag_last_dispatch_{cn}_{ns}"
        lines = [f"\t; --- DRAG HOOK BEGIN {part_tag}_{ns} ---"]
        if self.enable_viewport_probe:
            # 视口探针快照：armed 且尚无快照时，抓本帧角色渲染 RT 供探针分析视口矩形
            lines.extend([
                f"\tif $ssmtdrag_viewport_probe_armed_{ns} == 1 && ResourceDragViewportSource_{ns} === null",
                f"\t\tResourceDragViewportSource_{ns} = copy o0 unless_null",
                "\tendif",
            ])
        lines.extend([
            f"\t$ssmtdrag_drawn_{ns} = 1",
            f"\tif $ObjectDetectAllowed_{ns} == 1",
            f"\t\trun = CustomShaderDragBake{part_tag}_{ns}",
            f"\t\trun = CustomShaderDragDetect{cn}_{ns}",
            "\tendif",
            f"\tif $ssmtdrag_mode_{ns} == 1",
            f"\t\tif time != {last_dispatch}",
            f"\t\t\trun = CustomShaderDragJiggle{cn}_{ns}",
            f"\t\t\t{last_dispatch} = time",
            "\t\tendif",
            f"\t\tvb0 = {temp_vb0}",
            "\tendif",
            f"\t; --- DRAG HOOK END {part_tag}_{ns} ---",
        ])
        return lines

    # =======================================================================
    # Present 块 + Constants globals
    # =======================================================================

    def _emit_present_and_constants(self, sections, components, ns):
        # ---- Constants globals ----
        const_sec = "[Constants]"
        const_lines = sections.setdefault(const_sec, [])
        globals_to_add = [
            f"global $ssmtdrag_mode_{ns} = 0",
            f"global $ssmtdrag_drawn_{ns} = 0",
            f"global $ssmtdrag_booted_{ns} = 0",
            f"global $ssmtdrag_lmb_down_{ns} = 0",
            f"global $ssmtdrag_rmb_down_{ns} = 0",
            f"global $ssmtdrag_x_down_{ns} = 0",
            f"global $ssmtdrag_modifier_down_{ns} = 0",
            f"global $ssmtdrag_lmb_prev_{ns} = 0",
            f"global $ssmtdrag_rmb_prev_{ns} = 0",
            f"global $ssmtdrag_combo_active_{ns} = 0",
            f"global $ssmtdrag_poke_sign_{ns} = 0",
            f"global $ssmtdrag_poke_hold_mult_{ns} = 1.0",
            f"global $ssmtdrag_poke_hold_frames_{ns} = 8",
            f"global $ssmtdrag_poke_min_strength_{ns} = 0.25",
            f"global $ssmtdrag_poke_strength_{ns} = 1.0",
            f"global $ssmtdrag_release_boost_{ns} = 1.05",
            f"global $ssmtdrag_release_decay_{ns} = 0.92",
            f"global $ssmtdrag_modifier_ok_{ns} = 0",
            f"global $ssmtdrag_lmb_press_time_{ns} = 0",
            f"global $ssmtdrag_rmb_press_time_{ns} = 0",
            f"global $ssmtdrag_delta_time_{ns} = 0.0166667",
            f"global $ssmtdrag_prev_time_{ns} = 0",
            # 原作 LEWDHAND 取值：着色器步长 = clamp(dt*60*speed, 0.05, max_step)，
            # max_step < 0.05 时 clamp 直接返回 max_step —— 旧默认 1.0/0.0333 会把
            # 物理步长钳到 0.0333（比原作弱约 90 倍），表现为检测有反应但形变几乎不可见。
            f"global $ssmtdrag_sim_speed_{ns} = 3.0",
            f"global $ssmtdrag_max_step_{ns} = 3.0",
            f"global $ObjectDetectAllowed_{ns} = 0",
            f"global $isMouseButtonDown = 0",
            f"global $cursorX = -1", "global $cursorY = -1",
            f"global $screenW = 1", "global $screenH = 1",
            f"global $inputMode = 0",
            # CommandListDragViewportUpdate 赋值、CursorUpdate 读取。3DMigoto 中变量跨
            # run= 命令列表边界传递必须声明为 global，否则子列表里置 1 后回调用方仍读 0 →
            # 视口恒无效 → cursor 恒 (-1,-1) → 检测永不命中且手型光标锚定屏幕外（导出模组无效果的根因）
            f"global $ssmtdrag_viewport_valid = 0",
        ]
        if self.enable_viewport_probe:
            globals_to_add.extend([
                f"global $ssmtdrag_viewport_probe_enabled_{ns} = 1",
                f"global $ssmtdrag_viewport_probe_interval_{ns} = 0.50",
                f"global $ssmtdrag_viewport_probe_next_time_{ns} = 0",
                f"global $ssmtdrag_viewport_probe_armed_{ns} = 0",
                f"global $ssmtdrag_viewport_probe_generation_{ns} = 0",
            ])
        if self.enable_hand_cursor:
            # 手部参数：persist 项可在 d3dx_user.ini 调参并跨重载保留（对照原作 persist 集合）
            globals_to_add.extend([
                f"global $ssmtdrag_hand_debug_{ns} = 1",
                f"global $ssmtdrag_hand_surface_clip_{ns} = 1",
                f"global $ssmtdrag_hand_surface_lift_{ns} = 0.018",
                f"global $ssmtdrag_hand_surface_softness_{ns} = 0.020",
                f"global persist $ssmtdrag_hand_center_x_{ns} = -0.000614",
                f"global persist $ssmtdrag_hand_center_y_{ns} = 0.275962",
                f"global persist $ssmtdrag_hand_center_z_{ns} = 0.065117",
                f"global persist $ssmtdrag_hand_scale_{ns} = 0.5",
                f"global persist $ssmtdrag_hand_opacity_{ns} = 1.0",
                f"global persist $ssmtdrag_hand_tilt_min_{ns} = 10.0",
                f"global persist $ssmtdrag_hand_windup_time_{ns} = 1.0",
                f"global persist $ssmtdrag_hand_tilt_max_deg_{ns} = 45.0",
                f"global persist $ssmtdrag_hand_tilt_rmb_min_{ns} = 10.0",
                f"global persist $ssmtdrag_hand_rmb_windup_time_{ns} = 1.0",
                f"global persist $ssmtdrag_hand_tilt_rmb_max_deg_{ns} = 45.0",
                f"global persist $ssmtdrag_hand_tilt_grab_max_deg_{ns} = 45.0",
                f"global persist $ssmtdrag_hand_vibrate_threshold_{ns} = 0.5",
                f"global persist $ssmtdrag_hand_vibrate_period_slow_{ns} = 0.8",
                f"global persist $ssmtdrag_hand_vibrate_period_fast_{ns} = 0.1",
                f"global persist $ssmtdrag_hand_vibrate_amplitude_{ns} = 4.0",
                f"global persist $ssmtdrag_hand_upright_max_deg_{ns} = 80.0",
                f"global persist $ssmtdrag_hand_outline_width_{ns} = 2.0",
                f"global persist $ssmtdrag_hand_outline_opacity_{ns} = 1.0",
                f"global persist $ssmtdrag_hand_reference_height_{ns} = 2160.0",
                f"global $ssmtdrag_lmb_hold_fraction_{ns} = 0",
                f"global $ssmtdrag_rmb_hold_fraction_{ns} = 0",
                f"global $ssmtdrag_rmb_lone_hold_{ns} = 0",
            ])
        for comp in components:
            globals_to_add.append(f"global $ssmtdrag_last_dispatch_{comp['comp_name']}_{ns} = -1")
        for g in globals_to_add:
            var = g.split("=", 1)[0].replace("global ", "").replace("persist ", "").strip()
            if not any(var in line for line in const_lines):
                const_lines.append(g)

        # ---- Present 块（手势归约 + 手部蓄力进度 + S8 手部绘制）----
        present_sec = "[Present]"
        present_lines = sections.setdefault(present_sec, [])
        if any("DRAG PRESENT BEGIN" in line for line in present_lines):
            return
        # 抓取手势条件（原作默认 左右键同按/X；可选 左键/右键 单键抓取）
        if self.grab_gesture == 'RMB':
            grab_cond = f"$ssmtdrag_rmb_down_{ns} == 1 || $ssmtdrag_x_down_{ns} == 1"
        elif self.grab_gesture == 'COMBO':
            grab_cond = f"($ssmtdrag_lmb_down_{ns} == 1 && $ssmtdrag_rmb_down_{ns} == 1) || $ssmtdrag_x_down_{ns} == 1"
        else:  # LMB（默认，最直觉）
            grab_cond = f"$ssmtdrag_lmb_down_{ns} == 1 || $ssmtdrag_x_down_{ns} == 1"
        block = [
            "\t; --- DRAG PRESENT BEGIN ---",
            "$isMouseButtonDown = 0",
            f"$ssmtdrag_poke_sign_{ns} = 0",
            # NONE 模式无常驻修饰键 → 常开；ALT 模式 = modifier_down
            (f"$ssmtdrag_modifier_ok_{ns} = $ssmtdrag_modifier_down_{ns}" if self.grab_key == 'ALT'
             else f"$ssmtdrag_modifier_ok_{ns} = 1"),
            f"if $ssmtdrag_modifier_ok_{ns} == 1",
            f"\tif {grab_cond}",
            "\t\t$isMouseButtonDown = 1",
            "\tendif",
            "endif",
            f"if $isMouseButtonDown == 1",
            f"\t$ssmtdrag_combo_active_{ns} = 1",
            "endif",
            # 戳脉冲
            f"if $ssmtdrag_modifier_ok_{ns} == 1 && $ssmtdrag_combo_active_{ns} == 0",
            f"\tif $ssmtdrag_lmb_prev_{ns} == 1 && $ssmtdrag_lmb_down_{ns} == 0",
            f"\t\t$ssmtdrag_poke_sign_{ns} = -1",
            f"\t\t$ssmtdrag_poke_hold_mult_{ns} = time - $ssmtdrag_lmb_press_time_{ns}",
            f"\telif $ssmtdrag_rmb_prev_{ns} == 1 && $ssmtdrag_rmb_down_{ns} == 0",
            f"\t\t$ssmtdrag_poke_sign_{ns} = 1",
            f"\t\t$ssmtdrag_poke_hold_mult_{ns} = time - $ssmtdrag_rmb_press_time_{ns}",
            "\tendif",
            "endif",
            f"if $ssmtdrag_poke_sign_{ns} != 0",
            f"\tif $ssmtdrag_poke_hold_mult_{ns} < $ssmtdrag_poke_min_strength_{ns}",
            f"\t\t$ssmtdrag_poke_hold_mult_{ns} = $ssmtdrag_poke_min_strength_{ns}",
            f"\telif $ssmtdrag_poke_hold_mult_{ns} > 1.00",
            f"\t\t$ssmtdrag_poke_hold_mult_{ns} = 1.00",
            "\tendif",
            f"\t$ssmtdrag_poke_hold_mult_{ns} = $ssmtdrag_poke_hold_mult_{ns} * $ssmtdrag_poke_strength_{ns}",
            "else",
            f"\t$ssmtdrag_poke_hold_mult_{ns} = $ssmtdrag_poke_strength_{ns}",
            "endif",
            # press_time 记录
            f"if $ssmtdrag_lmb_down_{ns} == 1 && $ssmtdrag_lmb_prev_{ns} == 0",
            f"\t$ssmtdrag_lmb_press_time_{ns} = time",
            "endif",
            f"if $ssmtdrag_rmb_down_{ns} == 1 && $ssmtdrag_rmb_prev_{ns} == 0",
            f"\t$ssmtdrag_rmb_press_time_{ns} = time",
            "endif",
        ]
        if self.enable_hand_cursor:
            # 手部蓄力进度（对照原作 Present 的 hold-fraction 归约：独按 LMB/RMB
            # 期间持续累计并按 windup_time 封顶；组合键或非独按时归零）
            block.extend([
                f"if $ssmtdrag_lmb_down_{ns} == 1",
                f"\tif $ssmtdrag_rmb_down_{ns} == 0",
                f"\t\t$ssmtdrag_lmb_hold_fraction_{ns} = time - $ssmtdrag_lmb_press_time_{ns}",
                f"\t\tif $ssmtdrag_lmb_hold_fraction_{ns} > $ssmtdrag_hand_windup_time_{ns}",
                f"\t\t\t$ssmtdrag_lmb_hold_fraction_{ns} = $ssmtdrag_hand_windup_time_{ns}",
                "\t\tendif",
                "\telse",
                f"\t\t$ssmtdrag_lmb_hold_fraction_{ns} = 0",
                "\tendif",
                "else",
                f"\t$ssmtdrag_lmb_hold_fraction_{ns} = 0",
                "endif",
                f"$ssmtdrag_rmb_lone_hold_{ns} = 0",
                f"if $ssmtdrag_rmb_down_{ns} == 1",
                f"\tif $ssmtdrag_lmb_down_{ns} == 0",
                f"\t\t$ssmtdrag_rmb_lone_hold_{ns} = 1",
                f"\t\t$ssmtdrag_rmb_hold_fraction_{ns} = time - $ssmtdrag_rmb_press_time_{ns}",
                f"\t\tif $ssmtdrag_rmb_hold_fraction_{ns} > $ssmtdrag_hand_rmb_windup_time_{ns}",
                f"\t\t\t$ssmtdrag_rmb_hold_fraction_{ns} = $ssmtdrag_hand_rmb_windup_time_{ns}",
                "\t\tendif",
                "\telse",
                f"\t\t$ssmtdrag_rmb_hold_fraction_{ns} = 0",
                "\tendif",
                "else",
                f"\t$ssmtdrag_rmb_hold_fraction_{ns} = 0",
                "endif",
            ])
        block.extend([
            # prev 更新 + combo 复位
            f"$ssmtdrag_lmb_prev_{ns} = $ssmtdrag_lmb_down_{ns}",
            f"$ssmtdrag_rmb_prev_{ns} = $ssmtdrag_rmb_down_{ns}",
            f"if $ssmtdrag_lmb_down_{ns} == 0 && $ssmtdrag_rmb_down_{ns} == 0",
            f"\t$ssmtdrag_combo_active_{ns} = 0",
            "endif",
        ])
        if self.enable_viewport_probe:
            # 视口探针：有快照则解码出视口矩形（供 Detect 的 FrameAPI 光标映射）；
            # 到节流间隔则清旧快照+FrameAPI 并武装下一代（低频节流，非逐帧拷贝）
            block.extend([
                f"if $ssmtdrag_viewport_probe_armed_{ns} == 1 && ResourceDragViewportSource_{ns} !== null",
                f"\trun = CustomShaderDragViewportLayoutDecode_{ns}",
                "endif",
                f"if $ssmtdrag_viewport_probe_enabled_{ns} == 1 && time >= $ssmtdrag_viewport_probe_next_time_{ns}",
                f"\tResourceDragViewportSource_{ns} = null",
                f"\tclear = ResourceDragViewportFrameAPI_{ns} 0.0",
                f"\t$ssmtdrag_viewport_probe_armed_{ns} = 1",
                f"\t$ssmtdrag_viewport_probe_generation_{ns} = $ssmtdrag_viewport_probe_generation_{ns} + 1",
                f"\t$ssmtdrag_viewport_probe_next_time_{ns} = time + $ssmtdrag_viewport_probe_interval_{ns}",
                "else",
                f"\t$ssmtdrag_viewport_probe_armed_{ns} = 0",
                "endif",
            ])
        block.extend([
            # 执行序列
            f"if $ssmtdrag_mode_{ns} == 1",
            f"\tpre run = CommandListDragPinDetected_{ns}",
            "endif",
            f"run = CommandListDragCursorUpdate_{ns}",
        ])
        if self.enable_hand_cursor:
            # S8 手型光标：先更新手部屏幕位置，描边先画垫底（只露轮廓边）、填充后画；
            # 抓取中或 RMB 独按蓄力时用 Action 网格（握拳），否则 NoAction（张开）
            block.extend([
                f"if $ssmtdrag_drawn_{ns} == 1 && $ssmtdrag_mode_{ns} == 1",
                f"\trun = CustomShaderDragUpdateJiggleCursorPreview_{ns}",
                f"\tif $ssmtdrag_hand_debug_{ns} == 2",
                f"\t\trun = CustomShaderDragJiggleCursor_{ns}",
                "\tendif",
                f"\tif $ssmtdrag_hand_debug_{ns} >= 1",
                f"\t\tif $isMouseButtonDown == 1 || $ssmtdrag_rmb_lone_hold_{ns} == 1",
                f"\t\t\trun = CustomShaderDragPresentHandActionOutline_{ns}",
                f"\t\t\trun = CustomShaderDragPresentHandAction_{ns}",
                "\t\telse",
                f"\t\t\trun = CustomShaderDragPresentHandOutline_{ns}",
                f"\t\t\trun = CustomShaderDragPresentHand_{ns}",
                "\t\tendif",
                "\tendif",
                "endif",
            ])
        block.extend([
            f"post $ssmtdrag_drawn_{ns} = 0",
            "\t; --- DRAG PRESENT END ---",
        ])
        if present_lines:
            present_lines.append("")
        present_lines.extend(block)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 权重预览（视口热力图）：仿高斯球（toolkit/gb_operators）的轻量实现。
# 单例 draw handler + 轮询 timer；节点 preview_weights 启用后，对 preview_target
# 网格逐顶点合并各启用区域的高斯场（与导出烘焙 _write_jiggle_masks 同公式），
# 热力图直接叠加在模型表面（含 xray 幽灵层）。gpu/timers 均为函数内延迟导入，
# 测试环境（stub bpy，无 GPU）导入本模块零副作用。
# ---------------------------------------------------------------------------

_preview_handler = None
_preview_timer = None
_preview_batches = {}          # (tree,node) -> (target_name, batch, ghost_batch)
_preview_sig_cache = None
_PREVIEW_ALPHA = 0.85
_PREVIEW_GHOST_FACTOR = 0.3
_PREVIEW_TICK = 0.2


def _collect_preview_nodes():
    """全部节点树中启用了权重预览且已选网格的拖拽节点。"""
    nodes = []
    for tree in bpy.data.node_groups:
        for n in tree.nodes:
            if (n.bl_idname == 'SSMTNode_PostProcess_DragInteraction'
                    and getattr(n, "preview_weights", False)
                    and getattr(n, "preview_target", None) is not None):
                nodes.append(n)
    return nodes


def _preview_signature(n):
    """矩阵/参数签名：移动缩放空物体、改 brush 参数、换/动网格都会触发重算。"""
    target = n.preview_target
    parts = [n.id_data.name, n.name, target.name,
             tuple(round(v, 6) for v in np.array(target.matrix_world, dtype=np.float64).reshape(-1)),
             len(target.data.vertices)]
    for item in n.zone_objects:
        empty = item.zone_object
        if empty is None:
            parts.append(None)
            continue
        s = empty.ssmt_drag_zone
        parts.append((empty.name,
                      tuple(round(v, 6) for v in np.array(empty.matrix_world, dtype=np.float64).reshape(-1)),
                      s.enabled, round(s.brush_strength, 6), round(s.brush_falloff_k, 6)))
    return tuple(parts)


def _rebuild_preview_batches(nodes):
    """重算全部启用节点的热力图批次（与导出烘焙同公式：高斯场 → weights_to_colors）。"""
    global _preview_batches
    if not GB_CORE_AVAILABLE:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    _preview_batches = {}
    for n in nodes:
        target = n.preview_target
        mesh = target.data
        if len(mesh.vertices) == 0:
            continue
        verts = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", verts)
        verts = verts.reshape(-1, 3)
        mw = np.array(target.matrix_world, dtype=np.float64)
        verts_world = verts @ mw[:3, :3].T + mw[:3, 3]
        mesh.calc_loop_triangles()
        tri = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", tri)
        tri = tri.reshape(-1, 3)
        # 沿表面传播拓扑（预览与导出烘焙同路径；surface_propagate 关闭时为 None → 体积球）
        edge_verts = None
        if getattr(n, "surface_propagate", True):
            edge_verts = gb_core.edges_from_triangles(tri)
        field = np.zeros(len(verts), dtype=np.float64)
        plateau = getattr(n, "mask_plateau", 0.0)
        for item in n.zone_objects:
            empty = item.zone_object
            if empty is None or not empty.ssmt_drag_zone.enabled:
                continue
            s = empty.ssmt_drag_zone
            d = n._zone_distances(verts_world, empty.matrix_world, edge_verts)
            if d is None:
                continue
            f = n._shape_field(d, s.brush_strength, s.brush_falloff_k, plateau)
            np.maximum(field, f, out=field)
        colors = gb_core.weights_to_colors(field, _PREVIEW_ALPHA).astype(np.float32)
        ghost_colors = np.array(colors, copy=True)
        ghost_colors[:, 3] *= _PREVIEW_GHOST_FACTOR
        pos = verts_world.astype(np.float32)
        key = (n.id_data.name, n.name)
        _preview_batches[key] = (
            target.name,
            batch_for_shader(shader, 'TRIS', {"pos": pos, "color": colors}, indices=tri),
            batch_for_shader(shader, 'TRIS', {"pos": pos, "color": ghost_colors}, indices=tri),
        )


def _drag_preview_draw():
    """draw handler：先幽灵层（透视）后主层（深度测试），与高斯球预览一致。"""
    if not _preview_batches:
        return
    try:
        import gpu
        shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        for _name, _batch, ghost in _preview_batches.values():
            try:
                ghost.draw(shader)
            except Exception:
                pass
        gpu.state.depth_test_set('LESS_EQUAL')
        for _name, batch, _ghost in _preview_batches.values():
            try:
                batch.draw(shader)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            gpu.state.blend_set('NONE')
            gpu.state.depth_test_set('NONE')
        except Exception:
            pass


def _ensure_preview_handler():
    global _preview_handler
    if _preview_handler is None:
        try:
            _preview_handler = bpy.types.SpaceView3D.draw_handler_add(
                _drag_preview_draw, (), 'WINDOW', 'POST_VIEW')
        except Exception:
            _preview_handler = None


def _remove_preview_handler():
    global _preview_handler, _preview_batches
    if _preview_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_preview_handler, 'WINDOW')
        except Exception:
            pass
        _preview_handler = None
    _preview_batches = {}


def _preview_tick():
    """轮询：无启用节点 → 自清理并取消 timer；签名变化 → 重算批次。"""
    global _preview_timer, _preview_sig_cache
    nodes = _collect_preview_nodes()
    if not nodes:
        _remove_preview_handler()
        _preview_timer = None
        _preview_sig_cache = None
        return None  # 取消 timer
    _ensure_preview_handler()
    try:
        sig = tuple(_preview_signature(n) for n in nodes)
        if sig != _preview_sig_cache:
            _preview_sig_cache = sig
            _rebuild_preview_batches(nodes)
    except Exception:
        pass
    return _PREVIEW_TICK


def _ensure_preview_running():
    """幂等启动 timer（节点 draw 自愈调用；无启用节点时 tick 自行取消）。"""
    global _preview_timer
    if _preview_timer is None and _collect_preview_nodes():
        try:
            _preview_timer = bpy.app.timers.register(_preview_tick, first_interval=_PREVIEW_TICK)
        except Exception:
            _preview_timer = None


def _preview_cleanup():
    """插件注销时清理 handler 与 timer，防止重载残留。"""
    global _preview_timer
    if _preview_timer is not None:
        try:
            bpy.app.timers.unregister(_preview_timer)
        except Exception:
            pass
        _preview_timer = None
    _remove_preview_handler()


classes = (
    SSMT_DragZoneSettings,
    SSMT_DragZoneRef,
    SSMT_OT_DragZoneAdd,
    SSMT_OT_DragZoneRemove,
    SSMTNode_PostProcess_DragInteraction,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.ssmt_drag_zone = bpy.props.PointerProperty(type=SSMT_DragZoneSettings)


def unregister():
    _preview_cleanup()
    del bpy.types.Object.ssmt_drag_zone
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
