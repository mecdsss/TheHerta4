import bpy
import os
import re
import struct
import shutil
import time
from collections import OrderedDict

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

from .node_postprocess_base import SSMTNode_PostProcess_Base
from . import deform_chain
from .variable_registry import normalize_variable_name
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.mod_path_compat import (
    find_base_position_resource_name,
    ensure_resource_alias_section,
    iter_position_buffer_candidates,
)
from ..utils.export_space import position_export_matrix

try:
    from ..toolkit import gb_core
    GB_CORE_AVAILABLE = True
except Exception:
    GB_CORE_AVAILABLE = False


# ---------------------------------------------------------------------------
# 稀疏区域 ABI
# ---------------------------------------------------------------------------

MAX_ZONES = 256
DEFAULT_MOD_NAMESPACE = "A"
SPARSE_ZONE_SLOTS = 4
INVALID_ZONE_ID = 0xFFFFFFFF
ZONES_PER_PAGE = 1


def _node_identity_key(node):
    """返回节点稳定身份键，避免 bpy 包装对象被反复创建导致 id()/is 失效。"""
    if node is None:
        return None
    as_pointer = getattr(node, "as_pointer", None)
    if callable(as_pointer):
        try:
            return ("pointer", as_pointer())
        except Exception:
            pass
    tree_name = getattr(getattr(node, "id_data", None), "name", "") or ""
    node_name = getattr(node, "name", "") or ""
    if tree_name or node_name:
        return ("name", tree_name, node_name)
    return ("id", id(node))


def is_postprocess_node_on_export_chain(tree, target_node):
    """判断后处理节点是否从结果输出节点可达；无结果节点时兼容旧数据并视为可达。"""
    result_nodes = [
        node for node in (getattr(tree, "nodes", None) or [])
        if getattr(node, "bl_idname", "") in {
            "SSMTNode_Result_Output",
            "SSMTNode_Result_Output_NTMIModImp",
        }
    ]
    if not result_nodes:
        return True

    target_key = _node_identity_key(target_node)
    if target_key is None:
        return False

    visited = set()
    pending = list(result_nodes)
    while pending:
        node = pending.pop()
        node_key = _node_identity_key(node)
        if node_key is None or node_key in visited:
            continue
        visited.add(node_key)
        if node_key == target_key:
            return True

        sockets = list(getattr(node, "outputs", None) or [])
        sockets.extend(getattr(node, "inputs", None) or [])
        for socket in sockets:
            for link in getattr(socket, "links", None) or []:
                for neighbor in (
                    getattr(link, "from_node", None),
                    getattr(link, "to_node", None),
                ):
                    neighbor_key = _node_identity_key(neighbor)
                    if neighbor_key is None or neighbor_key == node_key:
                        continue
                    neighbor_type = getattr(neighbor, "bl_idname", "")
                    if neighbor_type == "NodeReroute" or neighbor_type.startswith(
                        "SSMTNode_PostProcess_"
                    ):
                        pending.append(neighbor)
    return False

SHADER_FILES = (
    "rzm_gs_probe.hlsl",
    "rzm_object_detect.hlsl",
    "rzm_pin_detected.hlsl",
    "rzm_jiggle_screen_state.hlsl",
    "rzm_jiggle_interaction.hlsl",
    "rzm_shapekey_drive.hlsl",
    "rzm_shapekey_var_sync.hlsl",
    "rzm_vis_publish.hlsl",
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
# TTL 二次绘制的区间参数：$\TTL\_1 = 索引数，$\TTL\_2 = 起始索引；
# run 目标形如 CommandListSSMTTTLDraw_<token>（TTL 库 drawindexedinstanced 读取 $_1/$_2）。
TTL_ARG1_RE = re.compile(r'^\$\\TTL\\_1\s*=\s*(\d+)', re.IGNORECASE)
TTL_ARG2_RE = re.compile(r'^\$\\TTL\\_2\s*=\s*(\d+)', re.IGNORECASE)
TTL_DRAW_RUN_RE = re.compile(r'^run\s*=\s*CommandListSSMTTTLDraw_', re.IGNORECASE)
# 物体显隐 flag 行（注入绘制分支内；material 的 TTL 块重建必须保留）
DRAG_OBJVIS_LINE_RE = re.compile(r'^\s*\$ssmtdrag_objvis_[\w]*\s*=\s*1\s*$')

# 导出的着色器在 ini 中的引用路径（mod 根 → res/drag_interaction/）
RES_SHADER_DIR = "res/drag_interaction"


# ---------------------------------------------------------------------------
# 空物体区域参数 PropertyGroup
# ---------------------------------------------------------------------------

class SSMT_DragZoneIncludeRef(bpy.types.PropertyGroup):
    """SSMT_DragZoneSettings.include_objects 里的一项：指向一个 MESH 物体。"""
    object: bpy.props.PointerProperty(
        name="物体",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )


class SSMT_DragZoneSettings(bpy.types.PropertyGroup):
    """挂在一个 Empty 上的区域参数（画刷 + 拖拽物理）。"""

    # 画刷参数
    brush_strength: bpy.props.FloatProperty(name="画刷强度", default=1.0, min=0.0, max=4.0)
    brush_falloff_k: bpy.props.FloatProperty(name="画刷衰减 k", default=4.6, min=0.1, max=50.0)
    enabled: bpy.props.BoolProperty(name="启用", default=True)
    propagate: bpy.props.BoolProperty(
        name="沿表面扩散",
        description="此权重球开启时沿网格表面测地扩散；关闭回退体积球（仅作用于本球）",
        default=True,
    )
    include_objects: bpy.props.CollectionProperty(type=SSMT_DragZoneIncludeRef)

    # 拖拽参数（0 = 继承回退到全局）
    radius: bpy.props.FloatProperty(name="影响半径", default=0.0, min=0.0)
    strength: bpy.props.FloatProperty(name="拖拽强度", default=0.0, min=0.0)
    max_offset: bpy.props.FloatProperty(name="最大位移", default=0.0, min=0.0)
    falloff: bpy.props.FloatProperty(name="衰减", default=0.0, min=0.0)
    damping: bpy.props.FloatProperty(name="阻尼", default=0.0, min=0.0)
    grabbable: bpy.props.BoolProperty(name="可抓取", default=True)


# ---------------------------------------------------------------------------
# 区域 helper：球级沿表面扩散 + 包含物体列表过滤
# ---------------------------------------------------------------------------


def _zone_propagate(settings, node):
    """球级沿表面扩散开关；旧工程（无 propagate 属性）默认开启（节点级总开关已移除）。"""
    value = getattr(settings, "propagate", None)
    if value is not None:
        return bool(value)
    return True


def _zone_allowed_names(settings):
    """包含列表内物体的候选名（含去重后缀形式，如 Body.001 → Body）。"""
    include = getattr(settings, "include_objects", None) or ()
    names = set()
    for item in include:
        obj = getattr(item, "object", None)
        if obj is None:
            continue
        candidates = (getattr(obj, "name", None), getattr(obj, "name_full", None))
        for candidate in candidates:
            if not candidate:
                continue
            names.add(str(candidate))
            names.add(str(candidate).rsplit(".", 1)[0])
            key = _blender_object_key(candidate)
            if key:
                names.add(key)
                names.add(key.rsplit(".", 1)[0])
    return names


def _blender_object_key(name):
    """把导出 mesh 注释名归一化为场景物体名：仅剥掉导出追加的运行时后缀。

    保留完整 LOD/IB 结构化前缀。不同 IB 可能包含同名物体，前缀用于区分 IB，
    不能被剥掉。
    """
    if not name:
        return ""
    clean = str(name).strip()
    try:
        clean, _suffix = ObjectPrefixHelper._strip_runtime_suffix(clean)
    except Exception:
        pass
    return clean.strip()


def _zone_has_included_objects(settings):
    """返回该权重球是否配置了非空的包含物体列表（导出侧据此读取 IB 部件名）。"""
    include = getattr(settings, "include_objects", None)
    if include is None:
        return False
    try:
        return len(include) > 0
    except (TypeError, ValueError):
        return False


def _zone_allowed_by_target(settings, target_name):
    """预览单物体：包含列表为空 → 全部允许；否则仅列表内物体名命中。
    列表项全部无效（物体指针已清空）时同样按全部允许，避免预览被误过滤。"""
    include = getattr(settings, "include_objects", None) or ()
    if not include or len(include) == 0:
        return True
    if not target_name:
        return True
    allowed_names = _zone_allowed_names(settings)
    if not allowed_names:
        return True
    return str(target_name) in allowed_names


def _zone_allowed_vertex_mask(settings, vertex_count, triangles, tri_part_names):
    """烘焙侧：包含列表 → (N,) bool 只允许列表内部件覆盖的顶点；空列表/无拓扑 → None（全允许）。

    tri_part_names 与 triangles 逐行对齐（object 数组，无注释部件名为 None）。无注释的
    三角形按“无法判定归属”处理为允许，避免旧工程无 mesh 注释时权重被清零。
    """
    include = getattr(settings, "include_objects", None) or ()
    if not include or len(include) == 0:
        return None
    allowed_names = _zone_allowed_names(settings)
    if not allowed_names or triangles is None or tri_part_names is None:
        return None
    mask = np.zeros(int(vertex_count), dtype=bool)
    tri_names = np.asarray(tri_part_names)
    tri_keys = np.asarray([_blender_object_key(name) for name in tri_part_names], dtype=object)
    for name in allowed_names:
        name_key = _blender_object_key(name)
        tri_sel = (tri_names == name) | (tri_names == name_key) | (tri_keys == name) | (tri_keys == name_key)
        idx = triangles[tri_sel].reshape(-1)
        idx = idx[idx < int(vertex_count)]
        mask[idx] = True
    # 无注释部件按允许处理（无法判定归属，不因列表过滤而清零）
    none_sel = tri_names == None  # noqa: E711
    if np.any(none_sel):
        idx = triangles[none_sel].reshape(-1)
        idx = idx[idx < int(vertex_count)]
        mask[idx] = True
    return mask


class SSMT_DragZoneRef(bpy.types.PropertyGroup):
    """节点 zone_objects 列表里的一项：指向一个 Empty。"""
    zone_id: bpy.props.IntProperty(
        name="区域 ID", default=-1, min=-1, max=MAX_ZONES - 1,
        description="稳定区域编号；用于运行时命中和 UI 绑定，不随前方区域禁用而改变",
    )
    expanded: bpy.props.BoolProperty(name="展开区域参数", default=False)
    zone_object: bpy.props.PointerProperty(
        name="区域空物体", type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'EMPTY',
    )


def _zone_page_count(node):
    return max(1, (len(node.zone_objects) + ZONES_PER_PAGE - 1) // ZONES_PER_PAGE)


def _zone_page_state(node, requested_page=None):
    page_count = _zone_page_count(node)
    raw_page = getattr(node, "zone_page", 0) if requested_page is None else requested_page
    page = min(max(int(raw_page), 0), page_count - 1)
    return page, page_count


def _clamp_zone_page(node, requested_page=None):
    page, page_count = _zone_page_state(node, requested_page)
    node.zone_page = page
    return page, page_count


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
            self.report({'WARNING'}, f"节点不存在或区域已达上限 {MAX_ZONES}")
            return {'CANCELLED'}

        used_ids = {
            int(getattr(entry, "zone_id", -1))
            for entry in node.zone_objects
            if 0 <= int(getattr(entry, "zone_id", -1)) < MAX_ZONES
        }
        zone_id = next((candidate for candidate in range(MAX_ZONES) if candidate not in used_ids), -1)
        if zone_id < 0:
            self.report({'WARNING'}, f"没有可用区域 ID（0-{MAX_ZONES - 1}）")
            return {'CANCELLED'}

        empty = bpy.data.objects.new(f"SSMT_DragZone_{zone_id}", None)
        empty.empty_display_type = 'SPHERE'
        empty.empty_display_size = 0.25
        empty.ssmt_drag_zone.radius = 0.5
        context.scene.collection.objects.link(empty)

        item = node.zone_objects.add()
        item.zone_id = zone_id
        item.zone_object = empty
        _clamp_zone_page(node, (len(node.zone_objects) - 1) // ZONES_PER_PAGE)
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
        _clamp_zone_page(node)
        # 只有 SSMT 创建的区域空物体、且不再被任何拖拽节点引用时才随引用一并删除；
        # 用户手动指定的已有空物体（可能复用于其他用途）只移除引用
        if obj is not None and obj.name.startswith("SSMT_DragZone") and not self._zone_empty_in_use(obj, node):
            name = obj.name
            bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'INFO'}, f"已移除引用并删除空物体 {name}")
        else:
            self.report({'INFO'}, "已从列表移除（空物体保留在场景中）")
        return {'FINISHED'}


class SSMT_OT_DragZonePage(bpy.types.Operator):
    bl_idname = "ssmt.drag_zone_page"
    bl_label = "切换拖拽区域页"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()
    node_tree: bpy.props.StringProperty()
    direction: bpy.props.IntProperty(default=0)

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None:
            return {'CANCELLED'}
        current_page, _page_count = _zone_page_state(node)
        _clamp_zone_page(node, current_page + self.direction)
        return {'FINISHED'}


class SSMT_OT_DragZoneIncludeAdd(bpy.types.Operator):
    """把当前活动 MESH 物体加入权重球的包含列表。"""
    bl_idname = "ssmt.drag_zone_include_add"
    bl_label = "添加包含物体"
    bl_options = {'INTERNAL'}

    empty_name: bpy.props.StringProperty()

    def execute(self, context):
        obj = getattr(context, "active_object", None)
        if obj is None or getattr(obj, "type", None) != 'MESH':
            self.report({'WARNING'}, "请先选中一个网格物体")
            return {'CANCELLED'}
        empty = bpy.data.objects.get(self.empty_name) if self.empty_name else None
        if empty is None or not hasattr(empty, "ssmt_drag_zone"):
            self.report({'WARNING'}, "权重球不存在")
            return {'CANCELLED'}
        settings = empty.ssmt_drag_zone
        for item in settings.include_objects:
            if item.object == obj:
                self.report({'INFO'}, f"{obj.name} 已在包含列表")
                return {'FINISHED'}
        item = settings.include_objects.add()
        item.object = obj
        self.report({'INFO'}, f"已加入包含物体 {obj.name}")
        return {'FINISHED'}


class SSMT_OT_DragZoneIncludeRemove(bpy.types.Operator):
    """从权重球包含列表移除一项。"""
    bl_idname = "ssmt.drag_zone_include_remove"
    bl_label = "移除包含物体"
    bl_options = {'INTERNAL'}

    empty_name: bpy.props.StringProperty()
    index: bpy.props.IntProperty(min=0)

    def execute(self, context):
        empty = bpy.data.objects.get(self.empty_name) if self.empty_name else None
        if empty is None or not hasattr(empty, "ssmt_drag_zone"):
            self.report({'WARNING'}, "权重球不存在")
            return {'CANCELLED'}
        settings = empty.ssmt_drag_zone
        if 0 <= self.index < len(settings.include_objects):
            settings.include_objects.remove(self.index)
            self.report({'INFO'}, "已移除包含物体")
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
        description="变量/资源命名后缀；默认使用稳定命名空间 A",
        default=DEFAULT_MOD_NAMESPACE,
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
    poke_gesture: bpy.props.EnumProperty(
        name="戳按键",
        description="选择释放时触发向内戳击的鼠标按键；与抓取手势使用同一按键时，抓取优先",
        items=[
            ('RMB', "右键", "右键点击释放后沿表面法线向内戳"),
            ('LMB', "左键", "左键点击释放后沿表面法线向内戳"),
            ('BOTH', "左右键", "左键或右键点击释放后均向内戳"),
        ],
        default='RMB',
    )
    drag_enabled_default: bpy.props.BoolProperty(
        name="旧版默认拖拽开关",
        default=True,
        options={'HIDDEN'},
    )
    drag_system_mode_default: bpy.props.IntProperty(
        name="默认运行模式",
        description="0=完全关闭，1=仅命中检测，2=命中检测并允许模型变形",
        default=2,
        min=0,
        max=2,
    )
    drag_mode_initialized: bpy.props.BoolProperty(
        name="运行模式已迁移",
        default=False,
        options={'HIDDEN'},
    )
    drag_mode_variable_name: bpy.props.StringProperty(
        name="运行模式变量",
        description="预分配运行模式变量；保留默认名时自动附加命名空间后缀，也可输入自定义全局变量名",
        default="ssmtdrag_drag_enabled",
    )
    mode_toggle_key: bpy.props.StringProperty(
        name="模式切换快捷键",
        description="按下循环切换运行模式：0=关闭 → 1=仅命中 → 2=命中+拖拽（type=cycle），始终生成切换键",
        default="f8",
    )
    ui_detected_variable_name: bpy.props.StringProperty(
        name="命中绘制 ID 变量",
        description="预分配鼠标当前命中绘制 ID 的只读联动变量；未命中时为 -1",
        default="ssmtdrag_ui_detected",
    )
    ui_zone_variable_name: bpy.props.StringProperty(
        name="命中区域 ID 变量",
        description="预分配鼠标当前命中稳定区域 ID 的只读联动变量；编号与节点区域 ID 完全一致",
        default="ssmtdrag_ui_zone",
    )
    # ---- 形态键驱动输出（ShapeKeyDrive 缓冲，纯 GPU 无回读）----
    enable_shapekey_drive: bpy.props.BoolProperty(
        name="形态键驱动输出",
        description="在仅命中模式下，命中区域并按住左键或 X：按鼠标位移驱动方向强度，或按点击档位直接 0/1 开关，写入 ShapeKeyDrive 缓冲区供形态键节点读取",
        default=False,
    )
    shapekey_drive_move_sensitivity: bpy.props.FloatProperty(
        name="位移灵敏度",
        description="鼠标位移控制：每像素位移对应的强度增量（向上增、向下减），默认 0.02；无方向形态键不随位移，按点击档位 0/1 开关",
        default=0.02,
        min=0.0001,
        max=1.0,
    )
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
    zone_page: bpy.props.IntProperty(
        name="区域页", default=0, min=0, max=MAX_ZONES - 1,
    )

    # ---- 权重预览（视口热力图，仿高斯球预览）----
    preview_weights: bpy.props.BoolProperty(name="权重预览", default=False)
    preview_target: bpy.props.PointerProperty(
        name="预览网格", type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )
    preview_collection: bpy.props.PointerProperty(
        name="预览集合", type=bpy.types.Collection,
        description="递归预览集合及其子集合中的全部网格；设置后优先于单个预览网格",
    )

    # ---- 权重平台化（烘焙 + 预览共用）----
    mask_plateau: bpy.props.FloatProperty(
        name="权重平台化", default=0.0, min=0.0, max=0.99,
        description="0=纯高斯（画刷衰减 k 生效）；>0 时球内 d≤平台保持满权重、边缘平滑过渡（此模式下画刷衰减 k 不参与）",
    )

    # ---- 烘焙参考物体（可选手动覆盖）----
    bake_reference_object: bpy.props.PointerProperty(
        name="烘焙参考物体（可选）",
        description="空物体世界坐标换算到模组局部空间的参考；留空自动解析",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )

    _RUNTIME_VARIABLE_DEFAULTS = {
        "drag_mode_variable_name": "ssmtdrag_drag_enabled",
        "ui_detected_variable_name": "ssmtdrag_ui_detected",
        "ui_zone_variable_name": "ssmtdrag_ui_zone",
    }

    def init(self, context):
        super().init(context)
        self.drag_system_mode_default = 2 if self.drag_enabled_default else 1
        self.drag_mode_initialized = True

    def _default_drag_system_mode(self):
        if not getattr(self, "drag_mode_initialized", False):
            mode = 2 if getattr(self, "drag_enabled_default", True) else 1
            try:
                self.drag_system_mode_default = mode
                self.drag_mode_initialized = True
            except Exception:
                pass
            return mode
        return min(max(int(getattr(self, "drag_system_mode_default", 2)), 0), 2)

    def _runtime_variable_name(self, property_name, ns):
        default_base = self._RUNTIME_VARIABLE_DEFAULTS[property_name]
        raw_name = normalize_variable_name(getattr(self, property_name, ""))
        resolved = f"{default_base}_{ns}" if not raw_name or raw_name == default_base else raw_name
        return f"${resolved}"

    def _runtime_variable_names(self, ns):
        return (
            self._runtime_variable_name("drag_mode_variable_name", ns),
            self._runtime_variable_name("ui_detected_variable_name", ns),
            self._runtime_variable_name("ui_zone_variable_name", ns),
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
        poke_row = row.row(align=True)
        poke_row.enabled = self.enable_poke
        poke_row.prop(self, "poke_gesture")
        layout.prop(self, "enable_hand_cursor")

        # ---- 形态键驱动输出 ----
        drive_box = layout.box()
        drive_box.label(text="形态键驱动输出", icon='SHAPEKEY_DATA')
        drive_box.prop(self, "enable_shapekey_drive")
        if self.enable_shapekey_drive:
            drive_box.prop(self, "shapekey_drive_move_sensitivity", text="位移灵敏度")
            drive_box.label(text="点击档位数：由各形态键节点配置的最大点击档位自动推导", icon='INFO')
            drive_box.label(text="仅命中模式：命中区域按住左键/X，方向形态键随鼠标位移驱动，无方向形态键按点击档位直接 0/1 开关", icon='INFO')

        runtime_box = layout.box()
        runtime_box.label(text="运行时联动变量", icon='DRIVER')
        row = runtime_box.row(align=True)
        row.prop(self, "drag_system_mode_default", text="默认模式")
        row.prop(self, "drag_mode_variable_name", text="")
        row = runtime_box.row(align=True)
        row.label(text="模式切换键")
        row.prop(self, "mode_toggle_key", text="")
        row = runtime_box.row(align=True)
        row.label(text="命中绘制 ID")
        row.prop(self, "ui_detected_variable_name", text="")
        row = runtime_box.row(align=True)
        row.label(text="命中区域 ID")
        row.prop(self, "ui_zone_variable_name", text="")

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

        # draw 回调只读页码，避免在绘制中改 RNA 属性导致后续面板短暂消失。
        page, page_count = _zone_page_state(self)
        page_row = box.row(align=True)
        previous_row = page_row.row(align=True)
        previous_row.enabled = page > 0
        previous = previous_row.operator(SSMT_OT_DragZonePage.bl_idname, text="", icon='TRIA_LEFT')
        previous.node_name = self.name
        previous.node_tree = self.id_data.name if self.id_data else ""
        previous.direction = -1
        page_row.label(text=f"第 {page + 1} / {page_count} 页")
        next_row = page_row.row(align=True)
        next_row.enabled = page + 1 < page_count
        next_page = next_row.operator(SSMT_OT_DragZonePage.bl_idname, text="", icon='TRIA_RIGHT')
        next_page.node_name = self.name
        next_page.node_tree = self.id_data.name if self.id_data else ""
        next_page.direction = 1
        start = page * ZONES_PER_PAGE
        end = min(start + ZONES_PER_PAGE, len(self.zone_objects))
        for i in range(start, end):
            item = self.zone_objects[i]
            row = box.row(align=True)
            zone_id = int(getattr(item, "zone_id", i))
            row.prop(
                item, "expanded", text="", emboss=False,
                icon='DISCLOSURE_TRI_DOWN' if item.expanded else 'DISCLOSURE_TRI_RIGHT',
            )
            row.prop(item, "zone_object", text=f"区域 {zone_id}")
            rm = row.operator(SSMT_OT_DragZoneRemove.bl_idname, text="", icon='X')
            rm.node_name = self.name
            rm.node_tree = self.id_data.name if self.id_data else ""
            rm.index = i
            obj = item.zone_object
            if obj is not None and item.expanded:
                sub = box.column(align=True)
                sub.use_property_split = True
                sub.prop(obj.ssmt_drag_zone, "enabled")
                sub.prop(obj.ssmt_drag_zone, "propagate", text="沿表面扩散")
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
                # 包含物体列表（留空 = 全部物体）
                inc_box = sub.box()
                inc_box.label(text="包含物体（留空=全部）", icon='OBJECT_DATA')
                inc_row = inc_box.row(align=True)
                add_inc = inc_row.operator(SSMT_OT_DragZoneIncludeAdd.bl_idname, text="添加活动物体", icon='ADD')
                add_inc.empty_name = obj.name
                for inc_index, inc_item in enumerate(obj.ssmt_drag_zone.include_objects):
                    inc_item_row = inc_box.row(align=True)
                    inc_item_row.prop(inc_item, "object", text="")
                    rm_inc = inc_item_row.operator(SSMT_OT_DragZoneIncludeRemove.bl_idname, text="", icon='X')
                    rm_inc.empty_name = obj.name
                    rm_inc.index = inc_index
                box.separator()

        layout.prop(self, "bake_reference_object")
        layout.prop(self, "mask_plateau")

        # 权重预览（视口热力图）
        box = layout.box()
        box.label(text="权重预览（视口热力图）", icon='RESTRICT_VIEW_OFF')
        col = box.column(align=True)
        col.prop(self, "preview_weights")
        col.prop(self, "preview_target")
        col.prop(self, "preview_collection")
        if self.preview_weights:
            preview_count = len(_preview_targets(self))
            if preview_count == 0:
                col.label(text="请选择预览网格或包含网格的集合", icon='INFO')
            else:
                col.label(text=f"正在预览 {preview_count} 个网格", icon='INFO')
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
        preserved_driver_content = ""
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            preserved_driver_content, content = self.split_anim_driver_block_content(content)
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
            return None, "", ""
        return sections, preserved_tail_content, preserved_driver_content

    @staticmethod
    def _strip_legacy_help_mode_block(text):
        """Remove every old help-gated drag-mode override from UI tails."""
        lines = str(text or "").splitlines(keepends=True)
        changed = True
        while changed:
            changed = False
            for index, line in enumerate(lines):
                stripped = line.strip()
                if stripped != "if $help == 1":
                    continue
                body_parts = "".join(lines[index:index + 4]).lower()
                if "$ssmtdrag_mode_" not in body_parts:
                    continue
                depth = 1
                end = None
                for j in range(index + 1, len(lines)):
                    current = lines[j].strip()
                    if current.startswith("if "):
                        depth += 1
                    elif current == "endif":
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                if end is None:
                    continue
                body = "".join(lines[index:end + 1])
                if "$ssmtdrag_mode_" not in body:
                    continue
                del lines[index:end + 1]
                if index < len(lines) and lines[index].strip() == "":
                    del lines[index]
                elif index > 0 and lines[index - 1].strip() == "":
                    del lines[index - 1]
                changed = True
                break
        return "".join(lines)

    def _normalize_ui_drag_references(self, tail_content, ns):
        """Align UI-generated model-drag variables with this node's actual namespace.

        The UI panel is kept as an auto-appended tail block, so it can be exported
        before the drag node suffix is known. Rewriting only the small block around
        the model-drag binding marker avoids touching other drag modules in the same
        tail while making the UI read the same globals that this node declares.
        """
        text = str(tail_content or "")
        marker = "; --- MODEL DRAG BINDING BEGIN ---"
        marker_index = text.find(marker)
        if marker_index < 0:
            return text

        text = self._strip_legacy_help_mode_block(text[:marker_index]) + text[marker_index:]
        marker_index = text.find(marker)
        end_marker = "; --- MODEL DRAG BINDING END ---"
        end_index = text.find(end_marker, marker_index)
        if end_index < 0:
            end_index = len(text)
        else:
            end_index += len(end_marker)

        start = text.rfind("\n", 0, marker_index) + 1
        block = text[start:end_index]
        _drag_mode_var, ui_detected_var, ui_zone_var = self._runtime_variable_names(ns)
        replacements = [
            (r"\$ssmtdrag_ui_detected_([A-Za-z0-9_]+)", ui_detected_var),
            (r"\$ssmtdrag_ui_zone_([A-Za-z0-9_]+)", ui_zone_var),
        ]
        for pattern, replacement in replacements:
            block = re.sub(pattern, lambda _m: replacement, block)
        return text[:start] + block + text[end_index:]

    @staticmethod
    def _extract_drag_present_block(present_lines):
        start = next(
            (i for i, line in enumerate(present_lines) if "; --- DRAG PRESENT BEGIN ---" in line),
            None,
        )
        if start is None:
            return None
        end = next(
            (i for i in range(start + 1, len(present_lines))
             if "; --- DRAG PRESENT END ---" in present_lines[i]),
            None,
        )
        if end is None:
            return None
        block = present_lines[start:end + 1]
        del present_lines[start:end + 1]
        return block

    def _relocate_drag_present_into_ui_tail(self, sections, tail_content):
        """Put the drag Present block in the same [Present] as the UI binding.

        The UI panel is preserved as an auto-appended tail with its own
        [Present] section. Moving the generated drag block in front of the UI
        model-drag marker removes any dependence on duplicate [Present] order
        and makes the bridge variables available on the exact binding frame.
        """
        text = str(tail_content or "")
        if "; --- MODEL DRAG BINDING BEGIN ---" not in text:
            return text

        lines = text.splitlines(keepends=True)
        tail_block = None
        begin = next(
            (i for i, line in enumerate(lines) if "; --- DRAG PRESENT BEGIN ---" in line),
            None,
        )
        if begin is not None:
            end = next(
                (i for i in range(begin + 1, len(lines)) if "; --- DRAG PRESENT END ---" in lines[i]),
                None,
            )
            if end is not None:
                tail_block = lines[begin:end + 1]
                del lines[begin:end + 1]

        block = self._extract_drag_present_block(sections.get("[Present]", []))
        if block is None:
            block = tail_block
        if block is None:
            return "".join(lines)
        marker_idx = next(
            (i for i, line in enumerate(lines) if "; --- MODEL DRAG BINDING BEGIN ---" in line),
            None,
        )
        if marker_idx is None:
            return "".join(lines)
        block_text = "\n".join(block) + "\n"
        lines[marker_idx:marker_idx] = block_text.splitlines(keepends=True)
        return "".join(lines)

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content="", preserved_driver_content="", ns=None):
        if ns:
            preserved_tail_content = self._relocate_drag_present_into_ui_tail(sections, preserved_tail_content)
            preserved_tail_content = self._normalize_ui_drag_references(preserved_tail_content, ns)
        with open(ini_file_path, 'w', encoding='utf-8') as f:
            if preserved_driver_content:
                f.write(preserved_driver_content)
                if not preserved_driver_content.endswith(chr(10)):
                    f.write(chr(10))
                f.write(chr(10))
            for section_name, lines in sections.items():
                f.write(f"{section_name}\n")
                normalized_lines = list(lines)
                while normalized_lines and not str(normalized_lines[-1]).strip():
                    normalized_lines.pop()
                for line in normalized_lines:
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
            normalized = re.sub(r'\W+', '_', self.mod_namespace.strip())
            return normalized or DEFAULT_MOD_NAMESPACE
        # 点击计数导出、形态键后处理和拖拽后处理必须共享同一命名空间。
        # 旧配置可能保存了空值，因此空值也回退到稳定默认值，而不是按 INI
        # 文件名分别推导出不同后缀。
        return DEFAULT_MOD_NAMESPACE

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
            sections, preserved_tail, preserved_driver = self._read_ini_to_ordered_dict(ini_path)
            if not sections:
                continue

            # 定位含目标 hash 且能到达 drawindexed 的组件
            components = self._locate_components(sections, hash_values)
            if not components:
                continue

            ns = self._resolve_namespace(ini_path)
            self._create_cumulative_backup(ini_path, mod_export_path)

            # 1) 资源烘焙（稀疏区域权重/ObjectMap/ZoneParams/PathVectors）
            # 无有效区域的组件（包含列表未命中/影响球无交集）整体跳过，不注入钩子。
            self._write_zone_resources(mod_export_path, ns)
            active_components = []
            skipped_components = []
            for comp in components:
                if self._bake_component_resources(mod_export_path, sections, comp, ns):
                    active_components.append(comp)
                else:
                    skipped_components.append(comp)
            if skipped_components:
                print(
                    "[DragInteraction] 已跳过无有效区域的组件: "
                    + ", ".join(comp["comp_name"] for comp in skipped_components)
                )
            if not active_components:
                print(
                    f"[DragInteraction][WARNING] {ini_file} 的所有目标组件均无有效拖拽区域，"
                    "未做任何注入（着色器已复制，INI 保持不变）"
                )
                continue
            components = active_components

            # 2) 生成 CustomShader/CommandList/Resource 段
            self._emit_sections(sections, components, ns)

            # 3) 注入绘制钩子（ib= 之后、第一个 run=/drawindexed= 之前）
            for comp in components:
                self._inject_draw_hooks(sections, comp, ns)

            # 4) Present 块 + Constants globals
            self._emit_present_and_constants(sections, components, ns)

            # 5) 变形接力终态规整（幂等，含多文件/形态键条件锚定）
            deform_chain.finalize_deform_chain(sections)

            self._write_ordered_dict_to_ini(
                sections,
                ini_path,
                preserved_tail,
                preserved_driver,
                ns,
            )
            print(f"[DragInteraction] 已注入 {len(components)} 个组件到 {ini_file}")

        print("[DragInteraction] 完成")

    # =======================================================================
    # 组件定位（含目标 hash 且能到达 drawindexed）
    # =======================================================================

    def _locate_components(self, sections, hash_values):
        components = []
        object_offset = 0
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
            # 物体显隐：按记录稳定分配全局物体编号（mesh 注释名 → id；跨组件连续），
            # 供 flag 变量注入与 TriangleObjectIDs 烘焙共用同一编号空间。
            name_to_id = {}
            for part in parts:
                for record in part.get("draw_records") or []:
                    key = self._record_object_key(record)
                    if key not in name_to_id:
                        name_to_id[key] = object_offset
                        object_offset += 1
            components.append({
                "hash": hash_value,
                "base_name": base_name,
                "comp_name": self._comp_name(base_name),
                "vertex_count": vertex_count or 0,
                "base_resource": base_resource,
                "parts": parts,
                "object_id_map": name_to_id,
                "object_count": len(name_to_id),
            })
        return components

    @staticmethod
    def _record_object_key(record):
        """记录对应的物体稳定键：优先 mesh 注释名，无注释回退 section+偏移。"""
        comment = record.get("hook_anchor_comment") or ""
        match = MESH_COMMENT_RE.match(str(comment))
        if match:
            return match.group("object_name")
        return "{}#{}".format(record.get("section"), record.get("draw_offset"))

    @staticmethod
    def _global_object_oids(components):
        """跨组件全局物体编号的有序并集（object_id_map 取值）。

        oid 由 _locate_components 的 object_offset 跨组件累计分配，所有按 oid
        索引的发射点（flag 声明 / post 清零）都必须使用该并集，禁止按组件内
        range 重数——否则第二个组件起的编号整体错位。
        """
        oids = set()
        for comp in components or []:
            oids.update((comp.get("object_id_map") or {}).values())
        return sorted(oids)

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
        """按目标 hash 和活动 IB 收集绘制组，并把同一 IB 的 copy 段合并为一个 part。

        材质 copy 段会重复声明同一个 IB；按 IB 归并既保留完整 index_count，又避免每个
        copy 段各注入一个重复 hook。
        """
        parts = []
        normalized_hash = hash_value.casefold()
        all_records = []
        for section_order, (section_name, lines) in enumerate(sections.items()):
            if not (section_name.startswith("[TextureOverride") and section_name.endswith("]")):
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
            pending_ttl_1 = None
            pending_ttl_2 = None

            for line_idx, line in enumerate(lines):
                s = line.strip()
                normalized = s.casefold()
                ttl_arg1 = TTL_ARG1_RE.match(s)
                if ttl_arg1:
                    pending_ttl_1 = int(ttl_arg1.group(1))
                    continue
                ttl_arg2 = TTL_ARG2_RE.match(s)
                if ttl_arg2:
                    pending_ttl_2 = int(ttl_arg2.group(1))
                    continue
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

                if TTL_DRAW_RUN_RE.match(s) and pending_mesh_comment:
                    # TTL 二次绘制站点：范围来自 $\TTL\_1(index count)/$\TTL\_2(first index)，
                    # 而非 drawindexed。跨命名空间的 CommandList\TTL\Draw 无法被
                    # _collect_referenced_draw_ranges 解析，这里显式补记录。
                    draw_owner = pending_mesh_owner or section_hash
                    if (
                        draw_owner
                        and draw_owner.casefold() == normalized_hash
                        and active_ib_resource
                        and pending_ttl_1 is not None
                        and pending_ttl_2 is not None
                        and pending_ttl_1 > 0
                        and pending_ttl_2 >= 0
                    ):
                        draw_records.append({
                            "ordinal": draw_ordinal,
                            "ib_resource": active_ib_resource,
                            "draw_offset": pending_ttl_2,
                            "draw_count": pending_ttl_1,
                            "mesh_first_index": pending_mesh_first_index,
                            "hook_anchor_comment": pending_mesh_comment,
                            "hook_anchor_occurrence": pending_mesh_comment_occurrence,
                            "line_index": line_idx,
                        })
                        draw_ordinal += 1
                    pending_ttl_1 = None
                    pending_ttl_2 = None
                    pending_mesh_owner = None
                    pending_mesh_first_index = None
                    pending_mesh_comment = None
                    pending_mesh_comment_occurrence = 0
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
                                    "line_index": line_idx,
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
                            "line_index": line_idx,
                        })
                    draw_ordinal += 1
                    pending_mesh_owner = None
                    pending_mesh_first_index = None
                    pending_mesh_comment = None
                    pending_mesh_comment_occurrence = 0

            base_name = (
                section_name[len("[TextureOverride_"):-1]
                if section_name.startswith("[TextureOverride_")
                else section_name[1:-1]
            )
            for record in draw_records:
                record["section"] = section_name
                record["section_order"] = section_order
                record["match_first_index"] = match_first_index
                record["base_name"] = base_name
                all_records.append(record)

        def _record_sort_key(record):
            is_base = 0 if record["section"].startswith("[TextureOverride_") else 1
            return (record["draw_offset"], is_base, record["section_order"])

        records_by_ib = {}
        for record in all_records:
            records_by_ib.setdefault(record["ib_resource"], []).append(record)

        for ib_resource, records in records_by_ib.items():
            ib_first_index = min(record["draw_offset"] for record in records)
            ib_index_end = max(record["draw_offset"] + record["draw_count"] for record in records)
            index_count = ib_index_end - ib_first_index

            # 保留同一 IB 内每个 draw 的索引区间与 mesh 注释名，供烘焙侧按物体过滤。
            # 同一区间因材质/换装条件重复出现时名字相同；若偶发重叠，base 段优先。
            name_ranges = []
            for record in sorted(records, key=_record_sort_key):
                comment = record.get("hook_anchor_comment") or ""
                mesh_match = MESH_COMMENT_RE.match(comment)
                if not mesh_match:
                    continue
                object_name = mesh_match.group("object_name")
                if object_name:
                    name_ranges.append((record["draw_offset"], record["draw_count"], object_name))

            representative = min(records, key=_record_sort_key)
            mesh_first_index = representative["mesh_first_index"]
            if mesh_first_index is None:
                for record in sorted(records, key=_record_sort_key):
                    if record["mesh_first_index"] is not None:
                        mesh_first_index = record["mesh_first_index"]
                        break

            match_first_index = representative["match_first_index"]
            first_index = (
                mesh_first_index
                if mesh_first_index is not None
                else match_first_index if match_first_index is not None
                else ib_first_index
            )

            parts.append({
                "section": representative["section"],
                "ib_resource": ib_resource,
                "index_count": index_count,
                "ib_first_index": ib_first_index,
                "first_index": first_index,
                "base_name": representative["base_name"],
                "hook_anchor_comment": representative["hook_anchor_comment"],
                "hook_anchor_occurrence": representative["hook_anchor_occurrence"],
                "name_ranges": name_ranges,
                "draw_records": records,
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
        # PathVectors 按稳定区域 ID 容量在导出阶段生成，不再复制旧版 12 项模板。
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
    # 资源烘焙（稀疏区域权重 / ObjectMap / ZoneParams / PathVectors）
    # =======================================================================

    def _bake_component_resources(self, mod_export_path, sections, comp, ns):
        # 每顶点 Top-K 区域权重：高斯场烘焙。所有配置区域对本组件均无有效权重时
        # （包含物体列表未命中 / 影响球与顶点无交集）跳过该组件——不生成
        # ObjectMap/逐三角编号/掩码，调用方也不再为它注入任何拖拽钩子。
        if not self._write_jiggle_masks(mod_export_path, sections, comp, ns):
            return False
        # ObjectMap：游戏索引空间 (1+N)×16B
        self._write_object_map(mod_export_path, sections, comp)
        # 逐三角形物体编号（显隐过滤：几何映射，与权重烘焙同类）
        self._write_triangle_object_ids(mod_export_path, sections, comp)
        return True

    def _write_triangle_object_ids(self, mod_export_path, sections, comp):
        """逐 part 读 IB 文件，按 name_ranges 给每个三角形写入物体编号
        （0xFFFFFFFF = 未映射，不受显隐门控）。文件名 {stem}TriangleObjectIDsP{p}.buf，
        与检测着色器的 indexBase/3 三角形序一致。"""
        name_to_id = comp.get("object_id_map") or {}
        for p_idx, part in enumerate(comp["parts"]):
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
                n_tri = len(idx) // 3
                ids = np.full(n_tri, 0xFFFFFFFF, dtype=np.uint32)
                for offset, count, name in part.get("name_ranges", []):
                    oid = name_to_id.get(name)
                    if oid is None:
                        continue
                    start = max(0, min(int(offset) // 3, n_tri))
                    end = max(0, min((int(offset) + int(count)) // 3, n_tri))
                    if start >= end:
                        continue
                    fill = np.zeros(n_tri, dtype=bool)
                    fill[start:end] = True
                    fill &= ids == 0xFFFFFFFF
                    ids[fill] = oid
                out = os.path.join(
                    mod_export_path, self._buffer_dir(sections, comp),
                    f"{comp['base_name']}TriangleObjectIDsP{p_idx}.buf",
                )
                os.makedirs(os.path.dirname(out), exist_ok=True)
                ids.tofile(out)
            except Exception:
                continue

    def _write_zone_resources(self, mod_export_path, ns):
        """写每区域物理参数和路径向量；按稳定 zone_id 直接索引。"""
        entries = self._collect_enabled_zone_entries()
        capacity = self._zone_capacity(entries)
        params = np.zeros((capacity * 2, 4), dtype=np.float32)
        paths = np.zeros((capacity, 4), dtype=np.float32)

        if not entries:
            # 与旧版“无区域时 zone0 全模型”回退一致。
            params[1] = (0.0, 1.0, 0.0, 1.0)
        for zone_id, empty in entries:
            settings = empty.ssmt_drag_zone
            params[zone_id * 2 + 0] = (
                float(settings.radius),
                float(settings.strength),
                float(settings.max_offset),
                float(settings.falloff),
            )
            params[zone_id * 2 + 1] = (
                float(settings.damping),
                1.0 if settings.grabbable else 0.0,
                0.0,  # path mode 尚未进入节点 UI，保留 ABI 槽位
                1.0,
            )

        res_dir = os.path.join(mod_export_path, "res", "drag_interaction")
        os.makedirs(res_dir, exist_ok=True)
        params.tofile(os.path.join(res_dir, f"ZoneParams_{ns}.buf"))
        paths.tofile(os.path.join(res_dir, f"PathVectors_{ns}.buf"))
        if getattr(self, "enable_shapekey_drive", False):
            _total_slots, _zone_bases, _zone_stage_counts = self._drag_drive_buffer_layout()
            stage_counts = np.array(_zone_stage_counts, dtype=np.uint32)
            stage_counts.tofile(os.path.join(res_dir, f"ZoneStageCounts_{ns}.buf"))
            sync_bindings = self._drag_drive_var_sync_bindings()
            if sync_bindings:
                # 变量同步映射表：每绑定 4×uint32 = (驱动槽位, 区域 ID, 无方向档位, 保留)；
                # 方向形态键无档位概念，nd_stage 以 0xFFFFFFFF 哨兵表示
                sync_map = np.array(
                    [
                        (slot, zone, 0xFFFFFFFF if nd_stage < 0 else nd_stage, 0)
                        for _var, slot, zone, nd_stage in sync_bindings
                    ],
                    dtype=np.uint32,
                )
                sync_map.tofile(os.path.join(res_dir, f"ShapeKeyVarSyncMap_{ns}.buf"))
        legacy_path_vectors = os.path.join(res_dir, "PathVectors.buf")
        if os.path.isfile(legacy_path_vectors):
            os.remove(legacy_path_vectors)

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
        """烘焙每顶点 Top-K 区域权重。返回 True = 掩码已写出（或回退写出）；
        False = 所有配置区域对本组件均无有效权重，调用方应整体跳过该组件。"""
        entries = self._collect_enabled_zone_entries()
        zones = [empty for _, empty in entries]
        vertex_count = comp["vertex_count"]

        # 无区域 → zone0 全 1
        if not zones or not GB_CORE_AVAILABLE:
            fallback_zone_id = entries[0][0] if entries else 0
            self._write_masks_fallback(mod_export_path, sections, comp, vertex_count, fallback_zone_id)
            return True

        # radius 参数与球尺度失配检查（防“整块刚体动”失配，原版实测 ratio 0.3~2.2）
        self._check_zone_radius_scale(zones)

        # 拓扑读取：任一启用区域开了球级沿表面扩散，或任一权重球配置了包含物体列表时读一次
        # IB 三角形 → 去重边（沿表面扩散用）；同时得到逐三角形部件名供包含列表过滤。
        # 只关闭沿表面扩散但仍需要包含列表过滤时也必须读 IB，否则 allowed_mask 会退化成全允许。
        edge_verts = None
        triangles = None
        tri_part_names = None
        needs_component_topology = any(
            _zone_propagate(empty.ssmt_drag_zone, self)
            or _zone_has_included_objects(empty.ssmt_drag_zone)
            for _, empty in entries
        )
        if needs_component_topology:
            result = self._read_component_triangles(mod_export_path, sections, comp, vertex_count)
            if result is not None:
                triangles, tri_part_names = result
                if any(_zone_propagate(empty.ssmt_drag_zone, self) for _, empty in entries):
                    edge_verts = gb_core.edges_from_triangles(triangles)
            else:
                print(f"[DragInteraction][WARNING] {comp['comp_name']} 无法读取 IB 拓扑，沿表面传播回退体积球")

        # 读取 Position.buf 顶点坐标
        positions = self._read_position_buf(mod_export_path, sections, comp, vertex_count)
        if positions is None:
            self._write_masks_fallback(mod_export_path, sections, comp, vertex_count, entries[0][0])
            return True

        # 烘焙参考物体世界矩阵的逆（坐标系换算）
        ref_matrix_inv = self._get_reference_matrix_inv(comp)
        export_matrix = self._get_export_space_matrix()
        # 非镜像工作流补偿：场景网格被 X 镜像过（导入时 mesh.transform 翻转、物体矩阵不变）
        # 而空物体矩阵未跟随 → 需对球矩阵施加同样的 X 镜像后才与 Position.buf（还原后
        # 朝向）同空间，否则掩码左右颠倒（用户报告）。预览不受影响（所见即所得）。
        mirror = self._get_non_mirror_mirror()

        zone_ids = np.full((vertex_count, SPARSE_ZONE_SLOTS), INVALID_ZONE_ID, dtype=np.uint32)
        zone_weights = np.zeros((vertex_count, SPARSE_ZONE_SLOTS), dtype=np.float32)
        overlap_counts = np.zeros(vertex_count, dtype=np.uint16)
        row_indices = np.arange(vertex_count)
        for zone_id, empty in entries:
            settings = empty.ssmt_drag_zone
            allowed_mask = _zone_allowed_vertex_mask(
                settings, len(positions), triangles, tri_part_names
            )
            if allowed_mask is not None and not np.any(allowed_mask):
                print(
                    f"[DragInteraction][WARNING] {comp['comp_name']} 区域 {empty.name} "
                    "包含物体列表未命中任何 IB 部件，该区域对本组件被跳过；"
                    "请检查包含物体是否属于当前 IB 前缀"
                )
                continue
            field = self._evaluate_zone_field(
                positions,
                empty,
                settings,
                ref_matrix_inv,
                mirror,
                edge_verts,
                export_matrix=export_matrix,
                propagate=_zone_propagate(settings, self),
                allowed_mask=allowed_mask,
            )
            if field is None:
                continue
            field = np.asarray(field, dtype=np.float32)
            positive = field > 1e-4
            overlap_counts[positive] += 1
            weakest_slot = np.argmin(zone_weights, axis=1)
            weakest_weight = zone_weights[row_indices, weakest_slot]
            replace = field > weakest_weight
            replace_rows = row_indices[replace]
            replace_slots = weakest_slot[replace]
            zone_weights[replace_rows, replace_slots] = field[replace]
            zone_ids[replace_rows, replace_slots] = np.uint32(zone_id)

        if not np.any(zone_weights > 1e-4):
            # 所有配置区域对本组件均无有效权重（包含物体列表未命中 IB 部件，
            # 或影响球与顶点包围盒无交集）：该组件按配置即不受拖拽，跳过注入
            # 而不是中止整个导出；逐区域原因已在上方逐条警告。
            print(
                f"[DragInteraction][WARNING] {comp['comp_name']}: "
                "所有配置区域对本组件均无有效权重，已跳过该组件的拖拽注入；"
                "若该组件预期应有拖拽，请检查区域的包含物体列表与空物体摆放"
            )
            self._remove_mask_files(mod_export_path, sections, comp)
            return False

        out_dir = os.path.join(mod_export_path, self._buffer_dir(sections, comp))
        os.makedirs(out_dir, exist_ok=True)
        zone_ids.tofile(os.path.join(out_dir, f"{comp['base_name']}JiggleZoneIDs.buf"))
        zone_weights.tofile(os.path.join(out_dir, f"{comp['base_name']}JiggleZoneWeights.buf"))
        self._remove_legacy_mask_files(out_dir, comp["base_name"])
        overflow_vertices = int(np.count_nonzero(overlap_counts > SPARSE_ZONE_SLOTS))
        if overflow_vertices:
            max_overlap = int(overlap_counts.max())
            print(
                f"[DragInteraction][WARNING] {comp['comp_name']} 有 {overflow_vertices} 个顶点同时覆盖超过 "
                f"{SPARSE_ZONE_SLOTS} 个区域（最大 {max_overlap}）；仅保留权重最高的 {SPARSE_ZONE_SLOTS} 个"
            )
        print(
            f"[DragInteraction] SparseZoneMasks: {comp['comp_name']} "
            f"({vertex_count} 顶点, {len(zones)} 区域, Top-{SPARSE_ZONE_SLOTS})"
        )
        return True

    def _remove_mask_files(self, mod_export_path, sections, comp):
        """组件被跳过时清掉可能残留的掩码文件（含旧命名），避免陈旧缓冲被打包。"""
        out_dir = os.path.join(mod_export_path, self._buffer_dir(sections, comp))
        for suffix in ("JiggleZoneIDs.buf", "JiggleZoneWeights.buf"):
            path = os.path.join(out_dir, f"{comp['base_name']}{suffix}")
            if os.path.isfile(path):
                os.remove(path)
        self._remove_legacy_mask_files(out_dir, comp["base_name"])

    def _write_masks_fallback(self, mod_export_path, sections, comp, vertex_count, fallback_zone_id=0):
        zone_ids = np.full((vertex_count, SPARSE_ZONE_SLOTS), INVALID_ZONE_ID, dtype=np.uint32)
        zone_weights = np.zeros((vertex_count, SPARSE_ZONE_SLOTS), dtype=np.float32)
        zone_ids[:, 0] = np.uint32(fallback_zone_id)
        zone_weights[:, 0] = 1.0
        out_dir = os.path.join(mod_export_path, self._buffer_dir(sections, comp))
        os.makedirs(out_dir, exist_ok=True)
        zone_ids.tofile(os.path.join(out_dir, f"{comp['base_name']}JiggleZoneIDs.buf"))
        zone_weights.tofile(os.path.join(out_dir, f"{comp['base_name']}JiggleZoneWeights.buf"))
        self._remove_legacy_mask_files(out_dir, comp["base_name"])
        print(
            f"[DragInteraction] SparseZoneMasks（回退 zone{fallback_zone_id} 全 1）: "
            f"{comp['comp_name']} ({vertex_count} 顶点)"
        )

    @staticmethod
    def _remove_legacy_mask_files(out_dir, base_name):
        for index in range(3):
            legacy_path = os.path.join(out_dir, f"{base_name}JiggleMasks{index}.buf")
            if os.path.isfile(legacy_path):
                os.remove(legacy_path)

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

    @staticmethod
    def _get_export_space_matrix(logic_name=None):
        if logic_name is None:
            try:
                from ..common.global_config import GlobalConfig
                logic_name = GlobalConfig.logic_name
            except Exception:
                logic_name = ""
        return position_export_matrix(logic_name)

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

    def _evaluate_zone_field(
        self,
        positions,
        empty,
        settings,
        ref_matrix_inv,
        mirror=None,
        edge_verts=None,
        export_matrix=None,
        propagate=None,
        allowed_mask=None,
    ):
        """对单个区域空物体求权重场（0..1），含 bbox sanity check。

        mirror: 非镜像工作流 X 镜像矩阵（(4,4) numpy）或 None。矩阵运算统一走
        numpy（mathutils.Matrix 可被 np.asarray 转换），便于无 Blender 环境测试。
        edge_verts: 网格边拓扑 (E,2)；本球沿表面扩散开启且提供时走沿表面传播
        （测地距离，权重不穿透到球体积覆盖的背面/对侧），否则回退体积球欧氏距离。
        propagate: 本球是否沿表面扩散；None = 默认开启（球级开关移除后无总开关）。
        allowed_mask: (N,) bool 可选，仅对这些顶点施加权重（包含物体列表过滤）。
        """
        try:
            if propagate is None:
                # 直接调用（测试/内部路径）未显式指定时，跟随球级沿表面扩散开关
                propagate = _zone_propagate(settings, self)
            # 空物体世界坐标 → 模组局部空间（必要时先施加非镜像工作流补偿）
            ball_world = np.asarray(empty.matrix_world, dtype=np.float64).reshape(4, 4)
            if mirror is not None:
                ball_world = np.asarray(mirror, dtype=np.float64).reshape(4, 4) @ ball_world
            if ref_matrix_inv is not None:
                ball_matrix = np.asarray(ref_matrix_inv, dtype=np.float64).reshape(4, 4) @ ball_world
            else:
                ball_matrix = ball_world
            if export_matrix is not None:
                ball_matrix = np.asarray(export_matrix, dtype=np.float64).reshape(4, 4) @ ball_matrix

            d = self._zone_distances(
                positions,
                ball_matrix,
                edge_verts,
                propagate=propagate,
                allowed_mask=allowed_mask,
            )
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

    def _zone_distances(self, positions, ball_matrix, edge_verts, propagate=None, allowed_mask=None):
        """球局部距离数组：本球沿表面扩散开启且有拓扑时用沿表面传播距离
        （种子 = 离球心最近的表面顶点/接触点），否则用欧氏距离。矩阵不可逆返回 None。"""
        local = gb_core._to_ball_local(np.asarray(positions, dtype=np.float64), ball_matrix)
        if local is None:
            return None
        d2 = np.einsum("ij,ij->i", local, local)
        use_propagate = True if propagate is None else bool(propagate)
        if use_propagate and edge_verts is not None and len(edge_verts) > 0:
            seeds = np.zeros(local.shape[0], dtype=bool)
            if allowed_mask is None:
                seeds[int(np.argmin(d2))] = True
            else:
                allowed = np.asarray(allowed_mask, dtype=bool).reshape(-1)
                valid_d2 = np.where(allowed, d2, np.inf)
                if not np.any(np.isfinite(valid_d2)):
                    return np.full(local.shape[0], np.inf)
                seeds[int(np.argmin(valid_d2))] = True
            return gb_core.surface_distances(
                local, edge_verts, seeds, allowed_mask=allowed_mask
            )
        if allowed_mask is not None:
            d_euclid = np.sqrt(np.maximum(d2, 0.0))
            d_euclid[~np.asarray(allowed_mask, dtype=bool).reshape(-1)] = np.inf
            return d_euclid
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
        """从组件各 part 的 IB 资源段 filename 读 R32_UINT 索引 → (M,3) 三角形，
        并返回与三角形逐行对齐的部件 mesh 名（object 数组，无注释为 None）；
        读不到返回 None（调用方回退体积球）。"""
        tris = []
        name_arrays = []
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
                t_part = idx[: (len(idx) // 3) * 3].reshape(-1, 3)
                tris.append(t_part)
                # 按 draw 区间标注每个三角形所属物体；未标注的三角形按 None
                # 处理（无法判定归属，不因包含物体列表而清零）。区间重叠时先到者优先。
                names = np.full(len(t_part), None, dtype=object)
                for offset, count, name in part.get("name_ranges", []):
                    start = int(offset) // 3
                    end = (int(offset) + int(count)) // 3
                    start = max(0, min(start, len(names)))
                    end = max(0, min(end, len(names)))
                    if start >= end:
                        continue
                    fill = np.zeros(len(names), dtype=bool)
                    fill[start:end] = True
                    fill &= names == None  # noqa: E711
                    names[fill] = name
                name_arrays.append(names)
            except Exception:
                continue
        if not tris:
            return None
        t = np.concatenate(tris, axis=0)
        tri_part_names = np.concatenate(name_arrays, axis=0)
        if vertex_count:
            keep = (t < int(vertex_count)).all(axis=1)
            t = t[keep]
            tri_part_names = tri_part_names[keep]
        return (t, tri_part_names) if len(t) else None

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

    def _collect_enabled_zone_entries(self):
        """返回 (稳定 zone_id, Empty)；为旧工程和重复 ID 自动分配空闲编号。"""
        entries = []
        used_ids = set()
        next_candidate = 0
        for item in self.zone_objects:
            obj = item.zone_object
            if obj is None:
                print("[DragInteraction][WARNING] 区域空物体已被删除，跳过")
                continue

            try:
                zone_id = int(getattr(item, "zone_id", -1))
            except (TypeError, ValueError):
                zone_id = -1
            if not (0 <= zone_id < MAX_ZONES) or zone_id in used_ids:
                while next_candidate in used_ids and next_candidate < MAX_ZONES:
                    next_candidate += 1
                if next_candidate >= MAX_ZONES:
                    print(f"[DragInteraction][WARNING] 区域超过稳定 ID 上限 {MAX_ZONES}，其余区域已跳过")
                    break
                zone_id = next_candidate
                try:
                    item.zone_id = zone_id
                except Exception:
                    pass
            used_ids.add(zone_id)
            while next_candidate in used_ids and next_candidate < MAX_ZONES:
                next_candidate += 1
            if obj.ssmt_drag_zone.enabled:
                entries.append((zone_id, obj))
        return sorted(entries, key=lambda entry: entry[0])

    def _collect_enabled_zones(self):
        """兼容预览/参数检查调用方；顺序按稳定区域 ID。"""
        return [obj for _, obj in self._collect_enabled_zone_entries()]

    @staticmethod
    def _zone_capacity(entries):
        return max(1, max((zone_id for zone_id, _ in entries), default=0) + 1)

    def _collect_click_export_drivers(self):
        """扫描同主树「动画驱动蓝图」关联树中的点击计数导出节点，
        返回 [(zone_id, 循环档数, 首个受控变量名或"")]：
        供缓冲布局把导出区域与循环档数纳入容量/档位计算，并供冷启动播种读取。"""
        entries = []
        tree = getattr(self, "id_data", None)
        if tree is None:
            return entries
        for node in getattr(tree, "nodes", None) or []:
            if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_AnimDriver":
                continue
            if getattr(node, "mute", False):
                continue
            if not is_postprocess_node_on_export_chain(tree, node):
                continue
            anim_tree = bpy.data.node_groups.get(str(getattr(node, "blueprint_name", "") or ""))
            if anim_tree is None:
                continue
            for anim_node in getattr(anim_tree, "nodes", None) or []:
                if getattr(anim_node, "bl_idname", "") != "SSMTNode_AnimDriver_ClickExport":
                    continue
                if getattr(anim_node, "mute", False):
                    continue
                try:
                    zone = int(getattr(anim_node, "click_zone_id", -1))
                except Exception:
                    zone = -1
                try:
                    cycle = int(getattr(anim_node, "cycle_length", 0) or 0)
                except Exception:
                    cycle = 0
                if not (0 <= zone < MAX_ZONES):
                    print(
                        f"[DragInteraction][WARNING] 点击导出区域 ID {zone} 超出稳定范围 "
                        f"0-{MAX_ZONES - 1}，已跳过"
                    )
                    continue
                first_var = ""
                for target in getattr(anim_node, "click_target_list", None) or []:
                    name = normalize_variable_name(getattr(target, "variable_name", "") or "")
                    if name:
                        first_var = f"${name}"
                        break
                entries.append((zone, min(64, max(0, cycle)), first_var))
        return entries

    def _click_export_seed_entries(self):
        """冷启动播种项：[(zone_id, $首个受控变量)]，同区域去重，最多 8 条。
        仅含配置了受控变量的导出节点；超限时明确报错，避免静默错误播种。"""
        seed_entries = []
        seen_zones = set()
        for zone, _cycle, first_var in self._collect_click_export_drivers():
            if not first_var or zone in seen_zones:
                continue
            seen_zones.add(zone)
            seed_entries.append((zone, first_var))
        if len(seed_entries) > 8:
            raise ValueError(
                "拖拽交互的点击导出冷启动最多支持 8 个不同区域，"
                f"当前配置了 {len(seed_entries)} 个；请合并区域或减少点击导出节点。"
            )
        return seed_entries

    def validate_export_configuration(self):
        """在写入任何导出文件前验证拖拽交互的跨节点约束。"""
        if getattr(self, "enable_shapekey_drive", False):
            self._click_export_seed_entries()

    def _drag_drive_zone_stage_counts(self):
        """按区域统计点击档位数：扫描同树所有开启拖拽驱动的形态键节点，
        每个区域取该区域无方向形态键 drag_click_stage 最大值（方向形态键忽略档位），最少 1。
        返回 {zone_id: stage_count}，只含被形态键引用的区域。"""
        tree = getattr(self, "id_data", None)
        counts = {}
        if tree is None:
            return counts
        for node in getattr(tree, "nodes", None) or []:
            if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_ShapeKey":
                continue
            if not getattr(node, "drag_drive_enabled", False):
                continue
            for item in getattr(node, "shapekey_variable_items", None) or []:
                if not getattr(item, "export_enabled", True):
                    # 未勾选导出的形态键不生成变量与缓冲，不参与档位统计
                    continue
                try:
                    zone = int(getattr(item, "drag_zone_id", -1))
                except Exception:
                    zone = -1
                if zone < 0:
                    continue
                try:
                    dir_val = int(getattr(item, "drag_dir_id", "-1") or "-1")
                except Exception:
                    dir_val = -1
                if dir_val >= 0:
                    # 方向形态键忽略档位，不参与该区域档位统计
                    continue
                try:
                    stage = int(getattr(item, "drag_click_stage", 1) or 1)
                except Exception:
                    stage = 1
                counts[zone] = max(counts.get(zone, 1), max(1, stage))
        return counts

    def _drag_drive_buffer_layout(self):
        """按区域独立档位计算驱动缓冲布局。
        返回 (total_slots, zone_bases, zone_stage_counts)：
        每个区域段 = 4 方向槽 + 该区域档位数 N 个无方向槽；
        zone_bases[z] = 区域 z 段在缓冲中的起始槽位；total_slots = 所有区域段长之和。"""
        capacity = self._zone_capacity(self._collect_enabled_zone_entries())
        export_entries = self._collect_click_export_drivers()
        if export_entries:
            # 点击计数导出节点的区域必须落在缓冲容量内
            capacity = max(capacity, max(zone for zone, _cycle, _var in export_entries) + 1)
        zone_counts = self._drag_drive_zone_stage_counts()
        zone_stage_counts = [max(1, int(zone_counts.get(z, 1))) for z in range(capacity)]
        for zone, cycle, _first_var in export_entries:
            if cycle >= 2:
                # 点击计数导出节点的循环档数扩展该区域点击循环（0..档数-1），与形态键档位取大
                zone_stage_counts[zone] = max(zone_stage_counts[zone], cycle - 1)
        zone_bases = []
        running = 0
        for stage_count in zone_stage_counts:
            zone_bases.append(running)
            running += 4 + stage_count
        return running, zone_bases, zone_stage_counts

    def _drag_drive_var_sync_bindings(self):
        """扫描同树开启拖拽驱动的形态键节点，收集「导出变量 → 驱动缓冲槽位」同步绑定。
        返回 [(var_name, slot_id, zone_id, nd_stage), ...]，按节点/项顺序稳定排列：
        var_name 带 $ 前缀（与形态键节点写入 IniParams 的变量一致，同一分配入口保证同名）；
        nd_stage 为无方向档位数（>=1），方向形态键为 -1（烘焙时映射为 0xFFFFFFFF）。
        槽位布局与形态键着色器 SHAPEKEY_SLOT_IDS 的 CPU 前缀和完全一致。"""
        tree = getattr(self, "id_data", None)
        if tree is None:
            return []
        _total_slots, zone_bases, _zone_stage_counts = self._drag_drive_buffer_layout()
        bindings = []
        for node in getattr(tree, "nodes", None) or []:
            if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_ShapeKey":
                continue
            if not getattr(node, "drag_drive_enabled", False):
                continue
            get_var_name = getattr(node, "get_shape_key_export_variable_name", None)
            if get_var_name is None:
                continue
            for item in getattr(node, "shapekey_variable_items", None) or []:
                if not getattr(item, "export_enabled", True):
                    # 未勾选导出的形态键不会生成 INI 变量，不产生同步绑定
                    continue
                try:
                    zone = int(getattr(item, "drag_zone_id", -1))
                except Exception:
                    zone = -1
                if zone < 0 or zone >= len(zone_bases):
                    continue
                try:
                    dir_val = int(getattr(item, "drag_dir_id", "-1") or "-1")
                except Exception:
                    dir_val = -1
                if 0 <= dir_val < 4:
                    slot_id = zone_bases[zone] + dir_val
                    nd_stage = -1
                else:
                    try:
                        stage = int(getattr(item, "drag_click_stage", 1) or 1)
                    except Exception:
                        stage = 1
                    nd_stage = max(1, stage)
                    slot_id = zone_bases[zone] + 4 + (nd_stage - 1)
                var_name = get_var_name(getattr(item, "shape_key_name", "") or "")
                if not var_name:
                    continue
                bindings.append((var_name, slot_id, zone, nd_stage))
        return bindings

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
            f"[ResourceDragPinnedDetectID_{ns}]": ["type = StructuredBuffer", "stride = 4", "array = 2"],
            f"[ResourceDragPinnedDetectInfo_{ns}]": ["type = StructuredBuffer", "stride = 16", "array = 15"],
            f"[ResourceDragZoneOut_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
            f"[ResourceDragJiggleScreenState_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragPathProgressState_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {self._zone_capacity(self._collect_enabled_zone_entries())}",
            ],
            f"[ResourceDragPathVectors_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res}/PathVectors_{ns}.buf",
            ],
            f"[ResourceDragZoneParams_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res}/ZoneParams_{ns}.buf",
            ],
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
        if getattr(self, "enable_shapekey_drive", False):
            _total_slots, _zone_bases, _zone_stage_counts = self._drag_drive_buffer_layout()
            _drive_capacity = len(_zone_stage_counts)
            global_resources[f"[ResourceDragShapeKeyDrive_{ns}]"] = [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {_total_slots}",
            ]
            # 方向缓冲：与驱动缓冲同构，末位 1 个上一帧按键状态槽
            global_resources[f"[ResourceDragShapeKeyDir_{ns}]"] = [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {_total_slots + 1}",
            ]
            # 点击计数缓冲：每区域 1 个点击档位（1..stage_count，0=未点击），供多段切换
            global_resources[f"[ResourceDragShapeKeyClickCount_{ns}]"] = [
                "type = RWBuffer", "format = R32_UINT",
                f"array = {_drive_capacity}",
            ]
            # 点击计数浮点镜像：CPU 经 store 回读仅对浮点格式有验证过的先例
            #（R32_UINT 直接 store 无格式保证），供点击计数导出使用
            global_resources[f"[ResourceDragShapeKeyClickCountF_{ns}]"] = [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {_drive_capacity}",
            ]
            # 主导方向缓冲：每区域 1 个当前主导方向（0=上 1=右 2=下 3=左），供“任意方向”绑定读取
            global_resources[f"[ResourceDragShapeKeyActiveDir_{ns}]"] = [
                "type = RWBuffer", "format = R32_UINT",
                f"array = {_drive_capacity}",
            ]
            # 每区域档位数缓冲：供驱动着色器按区域独立循环档位
            global_resources[f"[ResourceDragShapeKeyZoneStageCounts_{ns}]"] = [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/ZoneStageCounts_{ns}.buf",
            ]
            # 变量→驱动缓冲同步（绑定项非空时生成）：prev 值缓冲用于变更检测，
            # 映射表为导出期烘焙的静态 Buffer（每绑定 4×uint32）
            sync_bindings = self._drag_drive_var_sync_bindings()
            if sync_bindings:
                global_resources[f"[ResourceDragShapeKeyVarPrev_{ns}]"] = [
                    "type = RWBuffer", "format = R32_FLOAT",
                    f"array = {len(sync_bindings)}",
                ]
                global_resources[f"[ResourceDragShapeKeyVarSyncMap_{ns}]"] = [
                    "type = Buffer", "format = R32G32B32A32_UINT",
                    f"filename = {res}/ShapeKeyVarSyncMap_{ns}.buf",
                ]
                # 拖拽激活标志（每区域）：同步 CS 每帧按命中判定重算（仅命中模式+绘制+
                # 按住+命中区域），门控 CPU 回读与变量→缓冲写入，实现两写入方分时互斥。
                # CPU 经 store 直接读取（store = $var, <资源名>, <float 索引>，
                # 不能带 ref 关键字——分词器会把 ref 交给 GetTarget 报 Unknown target，
                # 整条 store 行在加载期被静默丢弃）；
                # 需要含 StoreCommand 的 ZZMI 构建——旧构建（如 8/4 前）不含该实现，
                # 此时 store 行在加载期被丢弃，本链路静默失效但其余功能不受影响）
                global_resources[f"[ResourceDragShapeKeyZoneActive_{ns}]"] = [
                    "type = RWBuffer", "format = R32_FLOAT",
                    f"array = {_drive_capacity}",
                ]
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

        # ---- 运行模式切换热键（type=cycle，每次按键循环 0→1→2→0）----
        mode_toggle_sec = f"[KeyDragInputManagerModeToggle_{ns}]"
        mode_toggle_key = str(getattr(self, "mode_toggle_key", "") or "").strip()
        drag_mode_var = self._runtime_variable_names(ns)[0]
        toggle_lines = [
            f"key = {mode_toggle_key}",
            "type = cycle",
            f"{drag_mode_var} = 0,1,2",
        ]
        # 快捷键可修改后重新导出：段已存在时也按当前值覆盖（保持幂等）
        if sections.get(mode_toggle_sec) != toggle_lines:
            sections[mode_toggle_sec] = toggle_lines

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
        if getattr(self, "enable_shapekey_drive", False):
            self._emit_shapekey_drive_section(sections, ns)
            self._emit_shapekey_var_sync_section(sections, ns)
            self._emit_shapekey_var_readback_command_list(sections, ns)
        # ---- 物体显隐：发布 CS + 命令列表 + 缓冲资源 ----
        self._emit_vis_publish_sections(sections, components, ns)
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
        comp_resources = {
            f"[ResourceDragDetect{cn}ObjectMap_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res_dir}/{stem}ObjectMap.buf",
            ],
            f"[ResourceDragDebugDetect_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 23"],
            f"[ResourceDragComponentDetect_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragPinnedComponentID_{cn}_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 2"],
            f"[ResourceDragPinnedComponentInfo_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragComponentZoneOut_{cn}_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
            f"[ResourceDragJiggleState_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 10"],
            # TempVB0：空声明段（type=RWBuffer，无 format/array），copy 往返后换绑
            f"[ResourceDragJiggleTempVB0_{cn}_{ns}]": ["type = RWBuffer"],
        }
        comp_resources[f"[ResourceDragJiggleZoneIDs_{cn}_{ns}]"] = [
            "type = Buffer", "format = R32G32B32A32_UINT",
            f"filename = {res_dir}/{stem}JiggleZoneIDs.buf",
        ]
        comp_resources[f"[ResourceDragJiggleZoneWeights_{cn}_{ns}]"] = [
            "type = Buffer", "format = R32G32B32A32_FLOAT",
            f"filename = {res_dir}/{stem}JiggleZoneWeights.buf",
        ]
        # 逐三角形物体编号（显隐过滤：检测着色器按命中三角形查 ObjectVis）
        for p_idx, _part in enumerate(comp["parts"]):
            comp_resources[f"[ResourceDragTriangleObjectIDs_{cn}P{p_idx}_{ns}]"] = [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res_dir}/{stem}TriangleObjectIDsP{p_idx}.buf",
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

    def _emit_vis_publish_sections(self, sections, components, ns):
        """物体显隐发布：CPU flag 变量 → ini params → 发布 CS → GPU 缓冲。
        复用与 rzm_shapekey_var_sync 相同的“变量→缓冲”链路；flag 槽位从
        IniParams[130] 起按 4/float4 打包（与 81/100/119 的既有用法错开）。"""
        total = sum(int(comp.get("object_count") or 0) for comp in components)
        if not total:
            return
        sections.setdefault(
            f"[ResourceDragObjectVis_{ns}]",
            ["type = RWBuffer", "format = R32_FLOAT", f"array = {total}"],
        )
        sections.setdefault(f"[CustomShaderDragVisPublish_{ns}]", [
            f"cs = {RES_SHADER_DIR}/rzm_vis_publish.hlsl",
            f"cs-u0 = ResourceDragObjectVis_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null",
        ])
        pub_lines = []
        for oid in range(total):
            slot = 130 + oid // 4
            channel = "xyzw"[oid % 4]
            pub_lines.append(f"{channel}{slot} = $ssmtdrag_objvis_{ns}_{oid}")
        pub_lines.append(f"run = CustomShaderDragVisPublish_{ns}")
        sections.setdefault(f"[CommandListDragVisPublish_{ns}]", pub_lines)

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

    # ---- Detect{Comp}（dispatch 1,1,1）----

    def _detect_section_lines(self, comp, ns, part_index):
        cn = comp["comp_name"]
        lines = [
            f"cs = {RES_SHADER_DIR}/rzm_object_detect.hlsl",
            "x28 = 0",
        ]
        if part_index is not None:
            # 每部件变体段只 raycast 本部件对应的 ObjectMap 条目，避免每个部件钩子
            # 都遍历整个 mesh 的所有三角形（N 倍放大）。y28/z28 为 shader 子区间。
            # t7/t8 = 逐三角形物体编号 + 物体显隐缓冲（隐式跳过隐藏物体）。
            lines.extend([
                f"y28 = {part_index}",
                "z28 = 1",
                f"cs-t7 = ResourceDragTriangleObjectIDs_{cn}P{part_index}_{ns}",
                f"cs-t8 = ResourceDragObjectVis_{ns}",
            ])
        lines.extend([
            "cs-t0 = vb0",
            "cs-t1 = ib",
            f"cs-t2 = ResourceDragDetect{cn}ObjectMap_{ns}",
            f"cs-t3 = ResourceDragBakeRT_{ns}",
            f"cs-t4 = ResourceDragJiggleZoneIDs_{cn}_{ns}",
            f"cs-t5 = ResourceDragJiggleZoneWeights_{cn}_{ns}",
            f"cs-t6 = ResourceDragViewportFrameAPI_{ns}",
            f"cs-u0 = ResourceDragDetectID_{ns}",
            f"cs-u1 = ResourceDragComponentDetect_{cn}_{ns}",
            f"cs-u2 = ResourceDragDebugDetect_{cn}_{ns}",
            "x24 = $cursorX", "y24 = $cursorY", "z24 = $screenW", "w24 = $screenH",
            "x25 = $isMouseButtonDown",
            "x26 = 48", "w26 = 8.0",
            "x27 = $cursorX", "y27 = $cursorY", "z27 = res_width", "w27 = res_height",
            "x85 = 0", "y85 = 0", "z85 = 1", "w85 = 1",  # 视口恒等（offset=0,0 scale=1,1）
            # VIEWPORT_VALID 必须为 1：shader 检测主循环被 ValidViewportCursor 门控。
            "x86 = 1",
            "x74 = 0",  # debug dump 门控关闭
            "dispatch = 1, 1, 1",
            "cs-u0 = null", "cs-u1 = null", "cs-u2 = null",
        ])
        return lines

    def _emit_detect_section(self, sections, comp, ns):
        cn = comp["comp_name"]
        sec = f"[CustomShaderDragDetect{cn}_{ns}]"
        if sec not in sections:
            sections[sec] = self._detect_section_lines(comp, ns, None)
        # 每个部件额外生成一个限定范围的变体段；基座段保留旧默认（遍历全部件）。
        for p_idx in range(len(comp["parts"])):
            part_sec = f"[CustomShaderDragDetect{cn}P{p_idx}_{ns}]"
            if part_sec not in sections:
                sections[part_sec] = self._detect_section_lines(comp, ns, p_idx)

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
        lines.extend([
            "x119 = 0",  # 是否存在 Path Slide；当前节点尚未开放路径模式
            f"y119 = {self._zone_capacity(self._collect_enabled_zone_entries())}",
        ])
        lines.extend([
            f"cs-t67 = ResourceDragPinnedComponentInfo_{cn}_{ns}",
            f"cs-t68 = ResourceDragJiggleParams_{cn}_{ns}",
            f"cs-t65 = ResourceDragJiggleZoneIDs_{cn}_{ns}",
            f"cs-t66 = ResourceDragJiggleZoneWeights_{cn}_{ns}",
            f"cs-t71 = ResourceDragJiggleScreenState_{ns}",
            f"cs-t73 = ResourceDragPathVectors_{ns}",
            f"cs-t74 = ResourceDragPathProgressState_{ns}",
            f"cs-t75 = ResourceDragZoneParams_{ns}",
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
            f"cs-u3 = ResourceDragComponentZoneOut_{cn}_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-u2 = null", "post cs-u3 = null",
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
            f"cs-u3 = ResourceDragZoneOut_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-u2 = null", "post cs-u3 = null",
        ]

    # ---- UpdateScreenJiggle（y72=1.0 非 mult_radius，照原作不对称）----

    def _emit_update_screen_jiggle_section(self, sections, ns):
        sec = f"[CustomShaderDragUpdateScreenJiggle_{ns}]"
        if sec in sections:
            return
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
            "x119 = 0",
            f"y119 = {self._zone_capacity(self._collect_enabled_zone_entries())}",
        ]
        lines.extend([
            f"cs-t67 = ResourceDragPinnedDetectInfo_{ns}",
            f"cs-t75 = ResourceDragZoneParams_{ns}",
            f"cs-u0 = ResourceDragJiggleScreenState_{ns}",
            f"cs-u1 = ResourceDragPathProgressState_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-t67 = null",
        ])
        sections[sec] = lines

    # ---- ShapeKeyDrive：仅命中模式下按鼠标位移 / 点击档位写入驱动缓冲 ----

    def _emit_shapekey_drive_section(self, sections, ns):
        sec = f"[CustomShaderDragShapeKeyDrive_{ns}]"
        if sec in sections:
            return
        drag_mode_var = self._runtime_variable_names(ns)[0]
        lines = [
            f"cs = {RES_SHADER_DIR}/rzm_shapekey_drive.hlsl",
            f"x76 = $ssmtdrag_delta_time_{ns}",
            f"y76 = $ssmtdrag_sim_speed_{ns}",
            f"z76 = $ssmtdrag_max_step_{ns}",
            f"z77 = {drag_mode_var}",
            f"w77 = $ssmtdrag_lmb_down_{ns}",
            f"x78 = $ssmtdrag_x_down_{ns}",
            "x79 = $ssmtdrag_shapekey_dy_" + ns,
            "y79 = $ssmtdrag_shapekey_dx_" + ns,
            "x80 = " + self._fmt(self.shapekey_drive_move_sensitivity),
            f"y80 = $ssmtdrag_seed_pending_{ns}",
        ]
        seed_entries = self._click_export_seed_entries()
        if seed_entries:
            # 冷启动播种参数：x81=条目数，从 82 起每条 (x=区域, y=导出变量当前值)
            lines.append(f"x81 = {len(seed_entries)}")
            for seed_idx, (seed_zone, seed_var) in enumerate(seed_entries):
                lines.append(f"x{82 + seed_idx} = {seed_zone}")
                lines.append(f"y{82 + seed_idx} = {seed_var}")
        lines.extend([
            f"cs-t67 = ResourceDragPinnedDetectInfo_{ns}",
            f"cs-t68 = ResourceDragShapeKeyZoneStageCounts_{ns}",
            f"cs-u0 = ResourceDragShapeKeyDrive_{ns}",
            f"cs-u1 = ResourceDragShapeKeyDir_{ns}",
            f"cs-u2 = ResourceDragShapeKeyClickCount_{ns}",
            f"cs-u3 = ResourceDragShapeKeyActiveDir_{ns}",
            f"cs-u4 = ResourceDragShapeKeyClickCountF_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-t67 = null",
            "post cs-t68 = null",
            "post cs-u0 = null",
            "post cs-u1 = null",
            "post cs-u2 = null",
            "post cs-u3 = null",
            "post cs-u4 = null",
        ])
        sections[sec] = lines

    # ---- ShapeKeyVarSync：变量变化时把当前强度实时写入 ShapeKeyDrive 缓冲 ----

    def _emit_shapekey_var_sync_section(self, sections, ns):
        bindings = self._drag_drive_var_sync_bindings()
        if not bindings:
            return
        sec = f"[CustomShaderDragShapeKeyVarSync_{ns}]"
        if sec in sections:
            return
        drag_mode_var = self._runtime_variable_names(ns)[0]
        # IniParams 打包：驱动 CS 用 76-80、形态键动画 CS 用 100+，同步段从 81 起，
        # 每 float4 装 4 个变量，着色器按 [81 + i/4][i%4] 读取；
        # 门控输入固定 IniParams[75]（低于驱动 CS 段，不与其他段冲突）
        lines = [
            f"cs = {RES_SHADER_DIR}/rzm_shapekey_var_sync.hlsl",
            f"x75 = {drag_mode_var}",
            f"y75 = $ssmtdrag_skheld_{ns}",
            f"z75 = $ssmtdrag_drawn_{ns}",
            "w75 = $inputMode",
        ]
        for i, (var_name, _slot, _zone, _nd_stage) in enumerate(bindings):
            lines.append(f"{'xyzw'[i % 4]}{81 + i // 4} = {var_name}")
        # 回读回声标志（每绑定 1 位，从 83 起打包）：回读把缓冲值拉进变量时
        # 置 1，同步 CS 据此跳过本帧的变量→缓冲回推（防 store 延迟的旧值
        # 与拖拽 CS 打架）；变量变化帧置 0，保证变量优先回写。
        for i, (_var_name, _slot, _zone, _nd_stage) in enumerate(bindings):
            lines.append(f"{'xyzw'[i % 4]}{83 + i // 4} = $ssmtdrag_skpull_{ns}_{i}")
        lines.extend([
            f"cs-t67 = ResourceDragPinnedDetectInfo_{ns}",
            f"cs-t69 = ResourceDragShapeKeyVarSyncMap_{ns}",
            f"cs-u0 = ResourceDragShapeKeyDrive_{ns}",
            f"cs-u1 = ResourceDragShapeKeyClickCount_{ns}",
            f"cs-u2 = ResourceDragShapeKeyVarPrev_{ns}",
            f"cs-u3 = ResourceDragShapeKeyClickCountF_{ns}",
            f"cs-u4 = ResourceDragShapeKeyZoneActive_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-t67 = null",
            "post cs-t69 = null",
            "post cs-u0 = null",
            "post cs-u1 = null",
            "post cs-u2 = null",
            "post cs-u3 = null",
            "post cs-u4 = null",
        ])
        sections[sec] = lines

    # ---- ShapeKeyVarReadback：store 回读命令列表（[Present] pre run、boot 门控）----

    def _emit_shapekey_var_readback_command_list(self, sections, ns):
        bindings = self._drag_drive_var_sync_bindings()
        if not bindings:
            return
        sec = f"[CommandListDragShapeKeyVarReadback_{ns}]"
        if sec in sections:
            return
        # store 放在命名命令列表内（[Present] 以 pre run 调用、boot 门控）。
        # 写法：store = $var, <资源名>, <float 索引>——不能带 ref 关键字！
        # 分词器把 ref 当独立 token 交给 GetTarget，ParseTarget 无此分支 →
        # Unknown target: ref，整条 store 行在加载期被静默丢弃（GIMI 实证有效
        # 写法为 store = $health, ps-cb0, 33，裸目标）。
        # 值仲裁、变量为主（修复「快捷键切换下一瞬间被回读顶掉」）：
        #   - 变量变化（热键/驱动器步进）→ 不回读，prev 跟随，进入沉淀期；
        #     沉淀期只等缓冲追平，期间绝不回读——store 有数帧延迟，旧缓冲值
        #     会把刚切换的变量立刻顶回去（历史 bug 根因）。
        #   - 变量未变且拖拽激活 → 缓冲为主（回读）。
        #   - 变量未变且缓冲变化（点击联动/释放收敛）→ 缓冲为主（回读）。
        #   - 回读帧置 pull 标志：同步 CS 据此跳过「把拉取值推回缓冲」的
        #     回声，避免拖拽中把带延迟的旧值写回缓冲与拖拽 CS 打架。
        lines = []
        for i, (var_name, slot, zone, _nd_stage) in enumerate(bindings):
            act = f"$ssmtdrag_skact_{ns}_{i}"
            rb = f"$ssmtdrag_skrb_{ns}_{i}"
            prev = f"$ssmtdrag_skprev_{ns}_{i}"
            cd = f"$ssmtdrag_skcd_{ns}_{i}"
            pull = f"$ssmtdrag_skpull_{ns}_{i}"
            lines.extend([
                f"store = {act}, ResourceDragShapeKeyZoneActive_{ns}, {zone}",
                f"store = {rb}, ResourceDragShapeKeyDrive_{ns}, {slot}",
                f"if {var_name} != {prev}",
                f"\t{prev} = {var_name}",
                f"\t{cd} = 6",
                f"\t{pull} = 0",
                f"elif {cd} > 0",
                f"\t{cd} = {cd} - 1",
                f"\tif {rb} == {var_name}",
                f"\t\t{cd} = 0",
                "\tendif",
                f"elif {act} >= 1",
                f"\t{var_name} = {rb}",
                f"\t{prev} = {rb}",
                f"\t{pull} = 1",
                f"elif {rb} != {var_name}",
                f"\t{var_name} = {rb}",
                f"\t{prev} = {rb}",
                f"\t{pull} = 1",
                "else",
                f"\t{pull} = 0",
                "endif",
            ])
        sections[sec] = lines

    # ---- CommandList：PinDetected（boot-clear + dt 钳制 + 门槛）/ Viewport / Cursor ----

    def _emit_command_lists(self, sections, components, ns):
        drag_mode_var, _ui_detected_var, _ui_zone_var = self._runtime_variable_names(ns)
        # PinDetected
        pin_sec = f"[CommandListDragPinDetected_{ns}]"
        if pin_sec not in sections:
            lines = [
                # boot-clear：RWBuffer 初始内容未定义，首帧垃圾会假命中/假位移
                f"if $ssmtdrag_booted_{ns} == 0",
                f"\tclear = ResourceDragDetectID_{ns} 0.0",
                f"\tclear = ResourceDragPinnedDetectID_{ns}",
                f"\tclear = ResourceDragPinnedDetectInfo_{ns}",
                f"\tclear = ResourceDragZoneOut_{ns} 0.0",
                f"\tclear = ResourceDragJiggleScreenState_{ns} 0.0",
                f"\tclear = ResourceDragPathProgressState_{ns} 0.0",
                f"\tclear = ResourceDragViewportFrameAPI_{ns} 0.0",
            ]
            if any(int(comp.get("object_count") or 0) for comp in components):
                lines.append(f"\tclear = ResourceDragObjectVis_{ns} 0.0")
            if getattr(self, "enable_shapekey_drive", False):
                lines.append(f"\tclear = ResourceDragShapeKeyDrive_{ns} 0.0")
                lines.append(f"\tclear = ResourceDragShapeKeyDir_{ns} 0.0")
                lines.append(f"\tclear = ResourceDragShapeKeyClickCount_{ns}")
                lines.append(f"\tclear = ResourceDragShapeKeyClickCountF_{ns} 0.0")
                lines.append(f"\tclear = ResourceDragShapeKeyActiveDir_{ns}")
                if self._click_export_seed_entries():
                    # 冷启动播种：缓冲随游戏关闭销毁、persist 变量仍有上次值——
                    # boot 清零后由驱动 CS 把导出变量值写回点击计数（含镜像与档位槽）
                    lines.append(f"\t$ssmtdrag_seed_pending_{ns} = 1")
                if self._drag_drive_var_sync_bindings():
                    # prev 值缓冲/激活标志随 boot 一并清零：persist 变量非 0 时首帧即触发同步写入
                    lines.append(f"\tclear = ResourceDragShapeKeyVarPrev_{ns} 0.0")
                    lines.append(f"\tclear = ResourceDragShapeKeyZoneActive_{ns} 0.0")
            if self.enable_hand_cursor:
                lines.append(f"\tclear = ResourceDragJiggleCursorPreview_{ns} 0.0")
            for comp in components:
                cn = comp["comp_name"]
                lines.extend([
                    f"\tclear = ResourceDragComponentDetect_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragPinnedComponentID_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragPinnedComponentInfo_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragComponentZoneOut_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragJiggleState_{cn}_{ns} 0.0",
                ])
            lines.extend([
                f"\t$ssmtdrag_booted_{ns} = 1",
                "endif",
                "",
            ])
            if getattr(self, "enable_shapekey_drive", False) and self._click_export_seed_entries():
                lines.extend([
                    # 冷启动恢复不能依赖 Alt/命中门控；boot 清零后立即从 persist 变量播种。
                    f"if $ssmtdrag_seed_pending_{ns} == 1",
                    f"\trun = CustomShaderDragShapeKeyDrive_{ns}",
                    f"\t$ssmtdrag_seed_pending_{ns} = 0",
                    "endif",
                    "",
                ])
            lines.extend([
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
                f"if {drag_mode_var} >= 1 && $inputMode == 0 && $ssmtdrag_mode_{ns} == 1 && $ssmtdrag_drawn_{ns} == 1",
                f"\t$ObjectDetectAllowed_{ns} = 1",
                f"\trun = CustomShaderDragPinDetected_{ns}",
            ])
            for comp in components:
                lines.append(f"\trun = CustomShaderDragPinComponent{comp['comp_name']}_{ns}")
            lines.extend([
                f"\trun = CustomShaderDragUpdateScreenJiggle_{ns}",
            ])
            if getattr(self, "enable_shapekey_drive", False):
                lines.append(f"\trun = CustomShaderDragShapeKeyDrive_{ns}")
            lines.extend([
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

        # 1) 物体显隐 flag：注入到每条绘制行（drawindexed=/run=）之前、其 if 块内部。
        #    分支执行才置位——条件求值留给 ini 引擎（活条件，手改即生效，零复制）。
        #    同段内按行号倒序插入避免索引位移；幂等防护同站点方案。
        name_to_id = comp.get("object_id_map") or {}
        sites = {}
        for part in comp["parts"]:
            for record in part.get("draw_records") or []:
                oid = name_to_id.get(self._record_object_key(record))
                if oid is None:
                    continue
                sites.setdefault(record["section"], []).append((record["line_index"], oid))
        for section, items in sites.items():
            lines = sections.get(section)
            if not lines:
                continue
            for line_idx, oid in sorted(items, key=lambda item: -item[0]):
                target = ""
                if 0 <= line_idx < len(lines):
                    target = (lines[line_idx] or "").strip().casefold()
                if not (target.startswith("drawindexed =") or target.startswith("run =")):
                    continue
                window = lines[max(0, line_idx - 4):line_idx]
                if any("$ssmtdrag_objvis_{}_{}".format(ns, oid) in (line or "") for line in window):
                    continue
                lines[line_idx:line_idx] = ["\t$ssmtdrag_objvis_{}_{} = 1".format(ns, oid)]
            sections[section] = lines

        # 2) 提升式 part 钩子（探针 + Bake + Detect + jiggle + vb0）
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
                index = matches[occurrence]
                # The anchor mesh comment may sit inside an if/else visibility block.
                # A hook placed right before it would be gated by that condition, so
                # when the base draw is hidden but a TTL/material copy of the same IB
                # is drawn, the only hook would not run and detection would be lost.
                # Move the hook above the enclosing conditional headers.
                while index > 0:
                    previous = lines[index - 1].strip()
                    if not previous:
                        index -= 1
                        continue
                    lowered = previous.casefold()
                    if lowered.startswith("if ") or lowered == "else" or lowered.startswith("else if"):
                        index -= 1
                        continue
                    break
                return index
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
        drag_mode_var, _ui_detected_var, _ui_zone_var = self._runtime_variable_names(ns)
        cn = comp["comp_name"]
        part_tag = f"{cn}P{p_idx}"
        temp_vb0 = f"ResourceDragJiggleTempVB0_{cn}_{ns}"
        last_dispatch = f"$ssmtdrag_last_dispatch_{cn}_{ns}"
        lines = [f"\t; --- DRAG HOOK BEGIN {part_tag}_{ns} ---"]
        if self.enable_viewport_probe:
            # 视口探针快照：armed 且尚无快照时，抓本帧角色渲染 RT 供探针分析视口矩形
            lines.extend([
                f"\tif {drag_mode_var} >= 1 && $ssmtdrag_viewport_probe_armed_{ns} == 1 && ResourceDragViewportSource_{ns} === null",
                f"\t\tResourceDragViewportSource_{ns} = copy o0 unless_null",
                "\tendif",
            ])
        lines.extend([
            f"\t$ssmtdrag_drawn_{ns} = 1",
            # 悬停检测不要求 Alt（与实机验证过的可用配置一致）：
            # 模式开关（drag_mode_var >= 1）打开即检测，Alt 只门控实际形变（jiggle 门）。
            f"\tif {drag_mode_var} >= 1 && $ObjectDetectAllowed_{ns} == 1",
            f"\t\trun = CustomShaderDragBake{part_tag}_{ns}",
            f"\t\trun = CustomShaderDragDetect{part_tag}_{ns}",
            "\tendif",
            f"\tif {drag_mode_var} >= 2 && $ssmtdrag_mode_{ns} == 1",
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

    @staticmethod
    def _place_drag_present_block(present_lines, block):
        """Position the generated Present block before the UI binding marker."""
        start = None
        end = None
        for i, line in enumerate(present_lines):
            if start is None and "; --- DRAG PRESENT BEGIN ---" in line:
                start = i
            if start is not None and "; --- DRAG PRESENT END ---" in line:
                end = i
                break
        if start is not None:
            if end is None:
                del present_lines[start:]
            else:
                del present_lines[start:end + 1]

        marker_idx = next(
            (i for i, line in enumerate(present_lines) if "; --- MODEL DRAG BINDING BEGIN ---" in line),
            None,
        )
        if marker_idx is not None:
            present_lines[marker_idx:marker_idx] = block
            return
        binding_idx = next(
            (i for i, line in enumerate(present_lines)
             if "if $mouse_clicked == 1" in line
             and "$is_dragging == 0" in line),
            None,
        )
        if binding_idx is not None:
            present_lines[binding_idx:binding_idx] = block
            return
        if present_lines and present_lines[-1] != "":
            present_lines.append("")
        present_lines.extend(block)

    def _emit_present_and_constants(self, sections, components, ns):
        drag_mode_var, ui_detected_var, ui_zone_var = self._runtime_variable_names(ns)
        default_drag_mode = self._default_drag_system_mode()
        # ---- Constants globals ----
        const_sec = "[Constants]"
        const_lines = sections.setdefault(const_sec, [])
        globals_to_add = [
            f"global $ssmtdrag_mode_{ns} = 0",
            "global $help = 0",
            f"global persist {drag_mode_var} = {default_drag_mode}",
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
            f"global $ssmtdrag_shapekey_dy_{ns} = 0",
            f"global $ssmtdrag_shapekey_dx_{ns} = 0",
            f"global $ssmtdrag_shapekey_prev_y_{ns} = 0",
            f"global $ssmtdrag_shapekey_prev_x_{ns} = 0",
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
            # UI 构造器桥接：把 GPU 检测结果回读成可跨 INI 命名空间引用的只读全局量。
            # detected < 0 表示未命中；zone 仅在 detected >= 0 时有效。
            f"global {ui_detected_var} = -1",
            f"global {ui_zone_var} = -1",
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
        # 变量↔缓冲双向同步辅助变量：激活标志/回读值/变量上一帧/沉淀计数（每绑定一组）
        if getattr(self, "enable_shapekey_drive", False):
            _sync_n = len(self._drag_drive_var_sync_bindings())
            if _sync_n:
                globals_to_add.append(f"global $ssmtdrag_skheld_{ns} = 0")
            # 冷启动播种标志：开启驱动即声明，保证引用处变量恒存在（默认 0）
            globals_to_add.append(f"global $ssmtdrag_seed_pending_{ns} = 0")
            for i in range(_sync_n):
                globals_to_add.extend([
                    f"global $ssmtdrag_skact_{ns}_{i} = 0",
                    f"global $ssmtdrag_skrb_{ns}_{i} = 0",
                    f"global $ssmtdrag_skprev_{ns}_{i} = 0",
                    f"global $ssmtdrag_skcd_{ns}_{i} = 0",
                    f"global $ssmtdrag_skpull_{ns}_{i} = 0",
                ])
        for comp in components:
            globals_to_add.append(f"global $ssmtdrag_last_dispatch_{comp['comp_name']}_{ns} = -1")
        # 物体显隐 flag：绘制分支内置位、Present 发布进 GPU 缓冲、post 清零。
        # oid 是跨组件连续编号（_locate_components 的 object_offset 累计），
        # 声明必须取 object_id_map 的全局 id 并集；按组件内 range 重数会导致
        # 第二个组件起全部缺失/错位（曾致 b1870eee 透明布料 flag 无 global、
        # 发布恒读 0、显隐门控误杀全部命中）。
        for oid in self._global_object_oids(components):
            globals_to_add.append(f"global $ssmtdrag_objvis_{ns}_{oid} = 0")
        for g in globals_to_add:
            var = g.split("=", 1)[0].replace("global ", "").replace("persist ", "").strip()
            # 精确匹配变量名（防 $ssmtdrag_objvis_A_3 误匹配 A_37 这类前缀串）
            if not any(f"{var} " in line or f"{var}=" in line for line in const_lines):
                const_lines.append(g)

        # ---- Present 块（手势归约 + 手部蓄力进度 + S8 手部绘制）----
        present_sec = "[Present]"
        present_lines = sections.setdefault(present_sec, [])
        ui_bridge_lines = [
            "\t; --- DRAG UI BRIDGE BEGIN ---",
            # zone 直出回读（store 无条件执行，结果有效性由 pre 阶段设置的
            # ObjectDetectAllowed 仲裁，避免 post 阶段门控变量时序不可靠）：
            #   - 命中 ID：ResourceDragPinnedDetectID 槽 0（RWStructuredBuffer<float> stride 4）
            #   - 区域 ID：ResourceDragZoneOut 槽 0（RWBuffer R32_FLOAT 标量）
            # 两者同走 R32 标量路径，与点击计数回读同型。历史版本曾从
            # stride-16 StructuredBuffer 按 float 标量索引 31 读 zone，实测不可靠
            # （zone 恒 -1），故改由 pin 着色器直出到 R32 标量槽。
            # store 读取上一份已完成的 GPU 数据，允许 UI 侧以一帧延迟稳定消费；
            # 本帧检测未运行（ObjectDetectAllowed=0）或未命中时主动失效，避免残留命中。
            f"\tstore = {ui_detected_var}, ResourceDragPinnedDetectID_{ns}, 0",
            f"\tstore = {ui_zone_var}, ResourceDragZoneOut_{ns}, 0",
            f"\tif {ui_detected_var} < 0 || $ObjectDetectAllowed_{ns} != 1",
            f"\t\t{ui_detected_var} = -1",
            f"\t\t{ui_zone_var} = -1",
            "\tendif",
            "\t; --- DRAG UI BRIDGE END ---",
        ]
        interaction_gate_lines = [
            "\t; --- DRAG INTERACTION GATE BEGIN ---",
            f"if {drag_mode_var} < 2",
            "\t$isMouseButtonDown = 0",
            f"\t$ssmtdrag_poke_sign_{ns} = 0",
            f"\t$ssmtdrag_combo_active_{ns} = 0",
            f"\tclear = ResourceDragJiggleScreenState_{ns} 0.0",
            f"\tclear = ResourceDragPathProgressState_{ns} 0.0",
        ]
        for comp in components:
            interaction_gate_lines.append(
                f"\tclear = ResourceDragJiggleState_{comp['comp_name']}_{ns} 0.0")
        if self.enable_hand_cursor:
            interaction_gate_lines.extend([
                f"\t$ssmtdrag_lmb_hold_fraction_{ns} = 0",
                f"\t$ssmtdrag_rmb_hold_fraction_{ns} = 0",
                f"\t$ssmtdrag_rmb_lone_hold_{ns} = 0",
            ])
        interaction_gate_lines.extend([
            "endif",
        ])
        # 形态键驱动只在“仅命中”模式（1）下生效；其余模式不驱动但保持当前数值
        # （驱动 CS 内已有 mode != 1 时保持的处理，此处不再清零）
        interaction_gate_lines.extend([
            f"if {drag_mode_var} < 1",
            f"\t$ObjectDetectAllowed_{ns} = 0",
            f"\tclear = ResourceDragDetectID_{ns} 0.0",
            f"\tclear = ResourceDragPinnedDetectID_{ns}",
            f"\tclear = ResourceDragPinnedDetectInfo_{ns}",
            f"\tclear = ResourceDragZoneOut_{ns} 0.0",
        ])
        for comp in components:
            cn = comp['comp_name']
            interaction_gate_lines.extend([
                f"\tclear = ResourceDragComponentDetect_{cn}_{ns} 0.0",
                f"\tclear = ResourceDragPinnedComponentID_{cn}_{ns} 0.0",
                f"\tclear = ResourceDragPinnedComponentInfo_{cn}_{ns} 0.0",
                f"\tclear = ResourceDragComponentZoneOut_{cn}_{ns} 0.0",
            ])
        interaction_gate_lines.extend([
            "endif",
            "\t; --- DRAG INTERACTION GATE END ---",
        ])
        # 抓取手势条件（原作默认 左右键同按/X；可选 左键/右键 单键抓取）
        if self.grab_gesture == 'RMB':
            grab_cond = f"$ssmtdrag_rmb_down_{ns} == 1 || $ssmtdrag_x_down_{ns} == 1"
        elif self.grab_gesture == 'COMBO':
            grab_cond = f"($ssmtdrag_lmb_down_{ns} == 1 && $ssmtdrag_rmb_down_{ns} == 1) || $ssmtdrag_x_down_{ns} == 1"
        else:  # LMB（默认，最直觉）
            grab_cond = f"$ssmtdrag_lmb_down_{ns} == 1 || $ssmtdrag_x_down_{ns} == 1"
        poke_gesture = getattr(self, "poke_gesture", 'RMB')
        poke_release_lines = []
        if poke_gesture in {'LMB', 'BOTH'}:
            poke_release_lines.extend([
                f"\tif $ssmtdrag_lmb_prev_{ns} == 1 && $ssmtdrag_lmb_down_{ns} == 0",
                f"\t\t$ssmtdrag_poke_sign_{ns} = -1",
                f"\t\t$ssmtdrag_poke_hold_mult_{ns} = time - $ssmtdrag_lmb_press_time_{ns}",
            ])
        if poke_gesture in {'RMB', 'BOTH'}:
            branch = "elif" if poke_release_lines else "if"
            poke_release_lines.extend([
                f"\t{branch} $ssmtdrag_rmb_prev_{ns} == 1 && $ssmtdrag_rmb_down_{ns} == 0",
                f"\t\t$ssmtdrag_poke_sign_{ns} = -1",
                f"\t\t$ssmtdrag_poke_hold_mult_{ns} = time - $ssmtdrag_rmb_press_time_{ns}",
            ])
        if poke_release_lines:
            poke_release_lines.append("\tendif")
        block = [
            "\t; --- DRAG PRESENT BEGIN ---",
            "$isMouseButtonDown = 0",
            f"$ssmtdrag_poke_sign_{ns} = 0",
            (f"$ssmtdrag_mode_{ns} = $ssmtdrag_modifier_down_{ns}"
             if self.grab_key == 'ALT' else f"$ssmtdrag_mode_{ns} = 1"),
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
            *poke_release_lines,
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
        if getattr(self, "enable_shapekey_drive", False):
            # 鼠标位移控制：帧内计算 Y/X 位移（$cursorY 自下而上，向上移动为增），
            # 由 CustomShaderDragShapeKeyDrive 段的 x79/y79 写入 IniParams[79] 供驱动 CS 读取
            block.extend([
                f"	$ssmtdrag_shapekey_dy_{ns} = $cursorY - $ssmtdrag_shapekey_prev_y_{ns}",
                f"	$ssmtdrag_shapekey_dx_{ns} = $cursorX - $ssmtdrag_shapekey_prev_x_{ns}",
                f"	$ssmtdrag_shapekey_prev_y_{ns} = $cursorY",
                f"	$ssmtdrag_shapekey_prev_x_{ns} = $cursorX",
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
                f"if {drag_mode_var} >= 1 && $ssmtdrag_viewport_probe_armed_{ns} == 1 && ResourceDragViewportSource_{ns} !== null",
                f"\trun = CustomShaderDragViewportLayoutDecode_{ns}",
                "endif",
                f"if {drag_mode_var} >= 1 && $ssmtdrag_viewport_probe_enabled_{ns} == 1 && time >= $ssmtdrag_viewport_probe_next_time_{ns}",
                f"\tResourceDragViewportSource_{ns} = null",
                f"\tclear = ResourceDragViewportFrameAPI_{ns} 0.0",
                f"\t$ssmtdrag_viewport_probe_armed_{ns} = 1",
                f"\t$ssmtdrag_viewport_probe_generation_{ns} = $ssmtdrag_viewport_probe_generation_{ns} + 1",
                f"\t$ssmtdrag_viewport_probe_next_time_{ns} = time + $ssmtdrag_viewport_probe_interval_{ns}",
                "else",
                f"\t$ssmtdrag_viewport_probe_armed_{ns} = 0",
                "endif",
            ])
        block.extend(interaction_gate_lines)
        block.extend([
            # boot / 冷启动播种不依赖 Alt；正常检测与拖拽仍要求交互门控。
            f"if $ssmtdrag_booted_{ns} == 0",
            f"\tpre run = CommandListDragPinDetected_{ns}",
        ])
        if getattr(self, "enable_shapekey_drive", False) and self._click_export_seed_entries():
            block.extend([
                f"elif $ssmtdrag_seed_pending_{ns} == 1",
                f"\tpre run = CommandListDragPinDetected_{ns}",
            ])
        block.extend([
            # 与实机可用配置一致：Pin 检测/光标更新只由模式开关门控，不要求 Alt。
            f"elif {drag_mode_var} >= 1",
            f"\tpre run = CommandListDragPinDetected_{ns}",
            f"\trun = CommandListDragCursorUpdate_{ns}",
            "endif",
        ])
        sync_bindings = (
            self._drag_drive_var_sync_bindings()
            if getattr(self, "enable_shapekey_drive", False) else []
        )
        if sync_bindings:
            # 分时互斥：ZoneActive 标志由同步 CS 每帧按命中判定重算；
            # 回读只在「对应区域拖拽激活」时进行，其余时间变量完全归驱动器/用户所有
            block.extend([
                f"$ssmtdrag_skheld_{ns} = 0",
                f"if $ssmtdrag_mode_{ns} == 1 && ($ssmtdrag_lmb_down_{ns} == 1 || $ssmtdrag_x_down_{ns} == 1)",
                f"\t$ssmtdrag_skheld_{ns} = 1",
                "endif",
                f"if $ssmtdrag_booted_{ns} == 1",
                f"\tpre run = CommandListDragShapeKeyVarReadback_{ns}",
                "endif",
                # 变量→驱动缓冲同步：每帧运行、不受模式门控；排在回读之后让采用结果当帧生效
                f"run = CustomShaderDragShapeKeyVarSync_{ns}",
            ])
        ui_readback_sec = f"[CommandListDragUIReadback_{ns}]"
        if ui_readback_sec not in sections:
            sections[ui_readback_sec] = list(ui_bridge_lines)

        if self.enable_hand_cursor:
            # S8 手型光标：先更新手部屏幕位置，描边先画垫底（只露轮廓边）、填充后画；
            # 抓取中或 RMB 独按蓄力时用 Action 网格（握拳），否则 NoAction（张开）
            block.extend([
                # 门控必须含 $ssmtdrag_mode（Alt 臂动）：检测只在臂动时刷新命中数据，
                # 松开 Alt 后 PinnedDetectInfo 是陈旧值——若只按 drawn 门控，手型光标
                # 会停留在松开前的命中点上不消失（用户报告）。NONE 常开模式下
                # $ssmtdrag_mode 恒 1，行为与之前一致。
                f"if {drag_mode_var} >= 1 && $ssmtdrag_mode_{ns} == 1 && $ssmtdrag_drawn_{ns} == 1",
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
        # 物体显隐发布：每帧把分支置位的 flag 经 ini params 喂给发布 CS →
        # GPU 缓冲（下一帧检测消费）；随后 post 清零供下一帧分支重算。
        total_objs = sum(int(comp.get("object_count") or 0) for comp in components)
        if total_objs:
            block.append(f"\tpre run = CommandListDragVisPublish_{ns}")
            # 同上：post 清零按全局 oid 并集发射，按组件 range 会产生重复且漏掉
            # 第二组件起的 oid（曾出现 0-14 重复、37-51 缺失）。
            for oid in self._global_object_oids(components):
                block.append(f"post $ssmtdrag_objvis_{ns}_{oid} = 0")
        block.extend([
            f"post run = CommandListDragUIReadback_{ns}",
            f"post $ssmtdrag_drawn_{ns} = 0",
        ])
        block.append("\t; --- DRAG PRESENT END ---")
        self._place_drag_present_block(present_lines, block)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 权重预览（视口热力图）：仿高斯球（toolkit/gb_operators）的轻量实现。
# 单例 draw handler + 轮询 timer；节点 preview_weights 启用后，对单个目标或集合内
# 全部网格逐顶点合并各启用区域的高斯场。集合或大量区域使用快速体积距离，
# 避免交互预览反复运行逐区域 Dijkstra；导出烘焙仍使用精确的沿表面传播。
# 热力图直接叠加在模型表面（含 xray 幽灵层）。gpu/timers 均为函数内延迟导入，
# 测试环境（stub bpy，无 GPU）导入本模块零副作用。
# ---------------------------------------------------------------------------

_preview_handler = None
_preview_timer = None
_preview_batches = {}          # (tree,node) -> (target_name, batch, ghost_batch)
_preview_sig_cache = None
_preview_pending_signature = None
_preview_pending_since = None
_PREVIEW_ALPHA = 0.85
_PREVIEW_GHOST_FACTOR = 0.3
_PREVIEW_TICK = 0.2
_PREVIEW_DEBOUNCE = 0.6
def _preview_now():
    return time.monotonic()


def _collect_preview_nodes():
    """全部节点树中启用了权重预览且存在有效目标的拖拽节点。"""
    nodes = []
    for tree in bpy.data.node_groups:
        for n in tree.nodes:
            if (n.bl_idname == 'SSMTNode_PostProcess_DragInteraction'
                    and getattr(n, "preview_weights", False)
                    and _preview_targets(n)):
                nodes.append(n)
    return nodes


def _preview_targets(node):
    """集合优先；递归返回其中全部网格。未设集合时回退单物体。"""
    collection = getattr(node, "preview_collection", None)
    if collection is not None:
        objects = getattr(collection, "all_objects", None)
        if objects is None:
            objects = getattr(collection, "objects", ())
        unique = {}
        for obj in objects:
            if getattr(obj, "type", None) == 'MESH':
                unique[getattr(obj, "name_full", obj.name)] = obj
        return [unique[name] for name in sorted(unique, key=str.casefold)]
    target = getattr(node, "preview_target", None)
    return [target] if target is not None else []


def _preview_signature(n):
    """矩阵/参数签名：集合成员、网格变换和区域参数变化都会触发重算。"""
    collection = getattr(n, "preview_collection", None)
    parts = [
        n.id_data.name,
        n.name,
        getattr(collection, "name", None),
        round(float(getattr(n, "mask_plateau", 0.0) or 0.0), 6),
    ]
    for target in _preview_targets(n):
        parts.append((
            target.name,
            tuple(round(v, 6) for v in np.array(target.matrix_world, dtype=np.float64).reshape(-1)),
            len(target.data.vertices),
        ))
    for item in n.zone_objects:
        empty = item.zone_object
        if empty is None:
            parts.append(None)
            continue
        s = empty.ssmt_drag_zone
        include_names = tuple(
            getattr(getattr(item, "object", None), "name", None)
            for item in getattr(s, "include_objects", ())
        )
        parts.append((empty.name,
                      tuple(round(v, 6) for v in np.array(empty.matrix_world, dtype=np.float64).reshape(-1)),
                      s.enabled, round(s.brush_strength, 6), round(s.brush_falloff_k, 6),
                      bool(getattr(s, "propagate", True)), include_names))
    return tuple(parts)


def _preview_target_field(node, verts_world, tri, zones, plateau, target_name=None,
                          topo_key=None, mesh_key=None):
    """按 Blender 当前场景坐标计算单个目标网格的逐顶点合并权重（所见即所得）。
    预览直接使用网格 world 顶点与空物体 world 矩阵，不做任何导出期变换
    （非镜像 X 镜像补偿、参考物体逆、导出空间矩阵）——那些只属于导出烘焙；
    否则球会被翻到另一侧，沿表面扩散到非当前物体。
    包含物体列表过滤：球未包含当前目标网格时跳过；每球独立决定是否沿表面扩散。
    topo_key/mesh_key 由调用方传入以命中拓扑/逐球场缓存；缺省时现场计算。"""
    verts_world = np.asarray(verts_world, dtype=np.float64).reshape(-1, 3)
    zones = [z for z in zones if _zone_allowed_by_target(z[1], target_name)]
    if not zones:
        return np.zeros(len(verts_world), dtype=np.float64)
    if any(_zone_propagate(s, node) for _empty, s in zones):
        if topo_key is None:
            topo_key = _preview_topology_key(verts_world, tri, None)
        topo = _preview_cached_topology(verts_world, tri, weld_tol=None, key=topo_key)
        field = _preview_field_from_topology(
            node, topo, zones, plateau, topo_key=topo_key
        )
    else:
        # 全部体积球：无需拓扑，只保留顶点集（逐球场缓存按 mesh_key 区分）
        topo = {"world_pts": verts_world, "edge_verts": None, "adjacency": None}
        topo_key = ("vol", mesh_key, len(verts_world))
        field = _preview_field_from_topology(
            node, topo, zones, plateau, topo_key=topo_key
        )
    return field


_PREVIEW_TOPOLOGY_CACHE = {}
_PREVIEW_TOPOLOGY_ORDER = []
_PREVIEW_TOPOLOGY_CACHE_MAX = 16

# 网格数据缓存（按目标矩阵 + 顶点/面数复用 verts_world/tri/topo_key，
# 避免每次重建都 foreach_get + calc_loop_triangles + 全量哈希）
_PREVIEW_MESH_CACHE = {}
_PREVIEW_MESH_CACHE_MAX = 32
# 集合模式合并网格拓扑缓存（按各目标矩阵/顶点/面数签名复用焊接+邻接表）
_PREVIEW_MERGED_CACHE = {}
_PREVIEW_MERGED_CACHE_MAX = 8
# 逐球权重场缓存：拓扑与球参数未变时直接复用，区域多时只重算变化的球
_PREVIEW_ZONE_FIELD_CACHE = {}
_PREVIEW_ZONE_FIELD_CACHE_MAX = 96


def _preview_topology_key(verts_world, tri, weld_tol=None):
    """网格数据签名：世界顶点 + 三角形 + 焊接容差，内容变化即失效。"""
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    h.update(np.ascontiguousarray(np.asarray(verts_world, dtype=np.float64)).tobytes())
    h.update(np.ascontiguousarray(np.asarray(tri, dtype=np.int64)).tobytes())
    h.update(str(weld_tol).encode("ascii"))
    return h.hexdigest()


def _preview_cache_get(cache, key):
    """LRU 命中：取值并把 key 移到末尾（dict 保序）。"""
    value = cache.get(key)
    if value is not None:
        cache[key] = cache.pop(key)
    return value


def _preview_cache_put(cache, key, value, max_items):
    """LRU 写入：新项放末尾，超限逐出最旧。"""
    if key in cache:
        cache.pop(key)
    cache[key] = value
    while len(cache) > max_items:
        cache.pop(next(iter(cache)))


def _preview_cached_topology(verts_world, tri, weld_tol=None, key=None):
    """焊接 + 边拓扑 + 世界坐标邻接表，按网格数据签名缓存。
    网格未变时复用（例如拖动空物体触发预览重建时不再重算
    np.unique 三维焊接与邻接表）；球只移动/缩放时各区域共享同一邻接表。
    Returns:
        dict(world_pts, edge_verts, adjacency, cluster_ids)。
    """
    verts_world = np.asarray(verts_world, dtype=np.float64).reshape(-1, 3)
    tri = np.asarray(tri, dtype=np.int64).reshape(-1, 3)
    if key is None:
        key = _preview_topology_key(verts_world, tri, weld_tol)
    cached = _PREVIEW_TOPOLOGY_CACHE.get(key)
    if cached is not None:
        return cached
    if weld_tol is not None:
        quantized = np.round(verts_world / float(weld_tol))
        _, cluster_ids = np.unique(quantized, axis=0, return_inverse=True)
        cluster_ids = cluster_ids.astype(np.int64, copy=False)
        cluster_count = int(cluster_ids.max()) + 1
        cluster_centers = np.zeros((cluster_count, 3), dtype=np.float64)
        np.add.at(cluster_centers, cluster_ids, verts_world)
        counts = np.bincount(cluster_ids, minlength=cluster_count).astype(np.float64)
        cluster_centers /= counts[:, None]
        cluster_tris = cluster_ids[tri.reshape(-1)].reshape(-1, 3)
        cluster_tris = cluster_tris[
            (cluster_tris[:, 0] != cluster_tris[:, 1])
            & (cluster_tris[:, 1] != cluster_tris[:, 2])
            & (cluster_tris[:, 0] != cluster_tris[:, 2])
        ]
        world_pts = cluster_centers
        ids_out = cluster_ids
    else:
        world_pts = verts_world
        cluster_tris = tri
        ids_out = None
    edge_verts = (
        gb_core.edges_from_triangles(cluster_tris)
        if len(cluster_tris) else np.zeros((0, 2), dtype=np.int64)
    )
    adjacency = (
        gb_core.build_surface_adjacency(world_pts, edge_verts)
        if len(edge_verts) else None
    )
    value = {
        "world_pts": world_pts,
        "edge_verts": edge_verts,
        "adjacency": adjacency,
        "cluster_ids": ids_out,
    }
    if key not in _PREVIEW_TOPOLOGY_ORDER:
        _PREVIEW_TOPOLOGY_ORDER.append(key)
    if len(_PREVIEW_TOPOLOGY_ORDER) > _PREVIEW_TOPOLOGY_CACHE_MAX:
        old = _PREVIEW_TOPOLOGY_ORDER.pop(0)
        _PREVIEW_TOPOLOGY_CACHE.pop(old, None)
    _PREVIEW_TOPOLOGY_CACHE[key] = value
    return value


def _preview_zone_distances(node, verts_world, ball_matrix, adjacency, edge_verts,
                            propagate=None, allowed_mask=None):
    """预览沿表面传播距离：均匀缩放球（旋转/平移/等比缩放）复用世界坐标
    邻接表快速路径；非均匀缩放回退逐球构建。propagate=None 默认开启（球级
    沿表面扩散开关移除后无总开关）；allowed_mask 可选，仅允许这些顶点（其余 inf）。"""
    m = np.asarray(ball_matrix, dtype=np.float64).reshape(4, 4)
    use_propagate = True if propagate is None else bool(propagate)
    allowed = None if allowed_mask is None else np.asarray(allowed_mask, dtype=bool).reshape(-1)
    linear = m[:3, :3]
    col_norms = np.linalg.norm(linear, axis=0)
    scale = float(np.mean(col_norms))
    uniform = (
        scale > 1e-9
        and np.allclose(col_norms, scale)
        and np.allclose(linear @ linear.T, scale * scale * np.eye(3), atol=1e-4)
    )
    if use_propagate and adjacency is not None and len(edge_verts) > 0:
        if uniform:
            return gb_core.surface_distances_uniform_scale(
                adjacency, verts_world, m[:3, 3], scale, allowed_mask=allowed
            )
        local = gb_core._to_ball_local(verts_world, ball_matrix)
        if local is None:
            return None
        d2 = np.einsum("ij,ij->i", local, local)
        seeds = np.zeros(local.shape[0], dtype=bool)
        if allowed is None:
            seeds[int(np.argmin(d2))] = True
        else:
            valid_d2 = np.where(allowed, d2, np.inf)
            if not np.any(np.isfinite(valid_d2)):
                return np.full(local.shape[0], np.inf)
            seeds[int(np.argmin(valid_d2))] = True
        return gb_core.surface_distances(local, edge_verts, seeds, allowed_mask=allowed)
    local = gb_core._to_ball_local(verts_world, ball_matrix)
    if local is None:
        return None
    d2 = np.einsum("ij,ij->i", local, local)
    d = np.sqrt(np.maximum(d2, 0.0))
    if allowed is not None:
        d = np.where(allowed, d, np.inf)
    return d


def _preview_zone_field(node, topo, topo_key, empty, s, plateau,
                        allowed=None, allowed_key=None):
    """单球权重场（含逐球缓存）：拓扑与球矩阵/参数未变时直接复用，
    区域很多时只重算变化的球，消除整体重建卡顿。"""
    m = np.asarray(empty.matrix_world, dtype=np.float64).reshape(4, 4)
    zone_key = (
        empty.name,
        tuple(round(v, 6) for v in m.reshape(-1)),
        bool(getattr(s, "propagate", True)),
        round(float(s.brush_strength), 6),
        round(float(s.brush_falloff_k), 6),
        round(float(plateau or 0.0), 6),
        allowed_key,
    )
    cache_key = (topo_key, zone_key)
    cached = _preview_cache_get(_PREVIEW_ZONE_FIELD_CACHE, cache_key)
    if cached is not None:
        return cached
    d = _preview_zone_distances(
        node, topo["world_pts"], m, topo["adjacency"], topo["edge_verts"],
        propagate=_zone_propagate(s, node), allowed_mask=allowed,
    )
    if d is None:
        field = np.zeros(len(topo["world_pts"]), dtype=np.float64)
    else:
        field = np.asarray(
            node._shape_field(d, s.brush_strength, s.brush_falloff_k, plateau),
            dtype=np.float64,
        )
    _preview_cache_put(_PREVIEW_ZONE_FIELD_CACHE, cache_key, field,
                       _PREVIEW_ZONE_FIELD_CACHE_MAX)
    return field


def _preview_mesh_data(node, target):
    """读取并缓存单个目标网格的世界顶点/三角形/拓扑 key。
    目标矩阵、顶点数、面数均未变时复用（跳过 foreach_get + calc_loop_triangles +
    拓扑哈希——这些是大量区域下预览重建的主要开销）。网格顶点原地编辑但数量
    不变时预览可能滞后，关闭再开启预览或移动目标即可刷新。"""
    key = (node.id_data.name, node.name, target.name)
    mesh = target.data
    vcount = len(mesh.vertices)
    if vcount == 0:
        return None
    mw = np.array(target.matrix_world, dtype=np.float64)
    matrix_sig = tuple(round(v, 6) for v in mw.reshape(-1))
    pcount = len(mesh.polygons)
    cached = _preview_cache_get(_PREVIEW_MESH_CACHE, key)
    if cached is not None:
        (c_matrix_sig, c_vcount, c_pcount, c_verts_world, c_tri, c_topo_key) = cached
        if (c_matrix_sig == matrix_sig and c_vcount == vcount and c_pcount == pcount):
            return (c_verts_world, c_tri, c_topo_key)
    verts = np.empty(vcount * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)
    verts_world = verts @ mw[:3, :3].T + mw[:3, 3]
    mesh.calc_loop_triangles()
    tri = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", tri)
    tri = tri.reshape(-1, 3)
    topo_key = _preview_topology_key(verts_world, tri, None)
    item = (matrix_sig, vcount, pcount, verts_world, tri, topo_key)
    _preview_cache_put(_PREVIEW_MESH_CACHE, key, item, _PREVIEW_MESH_CACHE_MAX)
    return (verts_world, tri, topo_key)


def _preview_field_from_topology(node, topo, zones, plateau, allowed_by_zone=None,
                                 topo_key=None, allowed_keys=None):
    """对缓存拓扑的顶点集逐区域合并权重：所有区域共享同一邻接表。
    allowed_by_zone: 可选，与 zones 对齐的每球 allowed 掩码（拓扑节点级），
    用于合并预览里包含物体列表过滤。逐球走 _preview_zone_field 缓存。"""
    world_pts = topo["world_pts"]
    if topo_key is None:
        topo_key = ("topology", len(world_pts))
    field = np.zeros(len(world_pts), dtype=np.float64)
    for idx, (empty, s) in enumerate(zones):
        allowed = None if allowed_by_zone is None else allowed_by_zone[idx]
        if allowed is not None and not np.any(allowed):
            continue
        allowed_key = None if allowed_keys is None else allowed_keys[idx]
        f = _preview_zone_field(
            node, topo, topo_key, empty, s, plateau,
            allowed=allowed, allowed_key=allowed_key,
        )
        np.maximum(field, f, out=field)
    return field


def _preview_merged_mesh(node, meshes, zones, plateau, mesh_names=None, weld_tol=1e-5,
                         merged_key=None):
    """集合预览：把集合内全部网格合并为同一个连续表面再计算沿表面传播。
    共享接缝的顶点（世界坐标差 < weld_tol）会被焊接为同一拓扑节点，
    因此球命中任一部件后能沿表面连续传播到相邻部件；各自独立的部分
    仍保持不连通。包含物体列表按 mesh_names 生成每球 allowed 掩码：
    簇内所有顶点都属于列表内网格才允许（阻止沿焊接接缝进入未包含物体）。
    merged_key 由调用方按各目标签名传入，命中时复用焊接拓扑；逐球走
    _preview_zone_field 缓存。
    返回与 meshes 对齐的逐网格权重数组列表。"""
    parts = []
    vertex_offset = 0
    all_verts = []
    all_tris = []
    for verts_world, tri in meshes:
        n_verts = len(verts_world)
        parts.append((vertex_offset, n_verts))
        all_verts.append(np.asarray(verts_world, dtype=np.float64).reshape(-1, 3))
        all_tris.append(np.asarray(tri, dtype=np.int64).reshape(-1, 3) + vertex_offset)
        vertex_offset += n_verts
    if not all_verts:
        return []
    all_verts = np.concatenate(all_verts, axis=0)
    all_tris = np.concatenate(all_tris, axis=0)
    if merged_key is None:
        # 未由调用方传签名（直接调用/测试）时必须用几何哈希，避免不同网格
        # 因相同顶点/面数命中同一合并拓扑缓存
        merged_key = _preview_topology_key(all_verts, all_tris, weld_tol)
    cached_topo = _preview_cache_get(_PREVIEW_MERGED_CACHE, merged_key)
    if cached_topo is None:
        topo_key = _preview_topology_key(all_verts, all_tris, weld_tol)
        topo = _preview_cached_topology(all_verts, all_tris, weld_tol=weld_tol,
                                        key=topo_key)
        _preview_cache_put(_PREVIEW_MERGED_CACHE, merged_key, (topo, topo_key),
                           _PREVIEW_MERGED_CACHE_MAX)
    else:
        topo, topo_key = cached_topo
    allowed_by_zone, allowed_keys = _merged_zone_allowed_masks(
        zones, mesh_names, parts, topo
    )
    cluster_field = _preview_field_from_topology(
        node, topo, zones, plateau, allowed_by_zone=allowed_by_zone,
        topo_key=topo_key, allowed_keys=allowed_keys,
    )
    cluster_ids = topo.get("cluster_ids")
    if cluster_ids is None:
        cluster_ids = np.arange(len(all_verts))
    field_all = cluster_field[cluster_ids]
    return [field_all[offset:offset + n] for offset, n in parts]


def _merged_zone_allowed_masks(zones, mesh_names, parts, topo):
    """集合预览：每球生成拓扑节点级 allowed 掩码（长度 = len(topo['world_pts'])）。
    包含列表为空 → None（全部允许）；否则簇内所有顶点都属于列表内网格才允许，
    防止沿焊接接缝把权重传播进未包含物体。返回 (allowed_list, allowed_keys_list)，
    allowed_key 由包含名单与网格名决定，供逐球场缓存使用。"""
    cluster_ids = topo.get("cluster_ids")
    if cluster_ids is None:
        cluster_ids = np.arange(sum(n for _offset, n in parts))
    allowed_by_zone = []
    allowed_keys = []
    mesh_names_tuple = tuple(str(m) for m in (mesh_names or ()))
    for _empty, s in zones:
        include = getattr(s, "include_objects", None) or ()
        if not include or not mesh_names:
            allowed_by_zone.append(None)
            allowed_keys.append(("inc", (), mesh_names_tuple))
            continue
        allowed_names = _zone_allowed_names(s)
        if not allowed_names:
            allowed_by_zone.append(None)
            allowed_keys.append(("inc", (), mesh_names_tuple))
            continue
        vertex_allowed = np.zeros(cluster_ids.shape[0], dtype=bool)
        for (offset, n), mname in zip(parts, mesh_names):
            if mname and str(mname) in allowed_names:
                vertex_allowed[offset:offset + n] = True
        if not np.any(vertex_allowed):
            cluster_allowed = np.zeros(int(cluster_ids.max()) + 1, dtype=bool)
            allowed_by_zone.append(cluster_allowed)
            allowed_keys.append(("inc", tuple(sorted(allowed_names)), mesh_names_tuple))
            continue
        counts = np.bincount(cluster_ids, minlength=int(cluster_ids.max()) + 1)
        allowed_counts = np.bincount(
            cluster_ids, weights=vertex_allowed.astype(np.int64), minlength=int(cluster_ids.max()) + 1
        )
        allowed_by_zone.append(allowed_counts == counts)
        allowed_keys.append(("inc", tuple(sorted(allowed_names)), mesh_names_tuple))
    return allowed_by_zone, allowed_keys


def _rebuild_preview_batches(nodes):
    """重算全部启用节点的热力图批次。
    新批次构建成功后才整体替换（中途失败保留旧预览不黑屏）；网格数据、合并拓扑、
    逐球权重场均走缓存，拖动单个区域时只重算该球，避免大量区域下反复全量 Dijkstra。"""
    global _preview_batches
    if not GB_CORE_AVAILABLE:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    new_batches = {}
    for n in nodes:
        zones = []
        for item in n.zone_objects:
            empty = item.zone_object
            if empty is None or not empty.ssmt_drag_zone.enabled:
                continue
            zones.append((empty, empty.ssmt_drag_zone))
        targets = _preview_targets(n)
        plateau = getattr(n, "mask_plateau", 0.0)
        # 收集每个目标网格的 world 顶点与三角形（命中网格缓存时跳过
        # foreach_get / calc_loop_triangles / 拓扑哈希）
        mesh_data = []
        for target in targets:
            result = _preview_mesh_data(n, target)
            if result is None:
                continue
            verts_world, tri, topo_key = result
            mesh_data.append((target, verts_world, tri, topo_key))
        if not mesh_data:
            continue

        # 集合预览：所有网格合并为同一连续表面（焊接共享接缝），
        # 沿表面传播可跨部件连续扩散；单物体直接按自身拓扑计算。
        collection_mode = getattr(n, "preview_collection", None) is not None
        if collection_mode:
            merged_key = tuple(
                (target.name, topo_key, len(verts_world))
                for target, verts_world, _tri, topo_key in mesh_data
            )
            fields = _preview_merged_mesh(
                n,
                [(verts_world, tri) for _target, verts_world, tri, _tk in mesh_data],
                zones,
                plateau,
                mesh_names=[target.name for target, _verts_world, _tri, _tk in mesh_data],
                merged_key=merged_key,
            )
        else:
            fields = [
                _preview_target_field(
                    n, verts_world, tri, zones, plateau, target_name=target.name,
                    topo_key=topo_key, mesh_key=(n.id_data.name, n.name, target.name),
                )
                for target, verts_world, tri, topo_key in mesh_data
            ]

        for (target, verts_world, tri, _topo_key), field in zip(mesh_data, fields):
            field = np.asarray(field, dtype=np.float64)
            colors = gb_core.weights_to_colors(field, _PREVIEW_ALPHA).astype(np.float32)
            ghost_colors = np.array(colors, copy=True)
            ghost_colors[:, 3] *= _PREVIEW_GHOST_FACTOR
            pos = verts_world.astype(np.float32)
            indices = np.asarray(tri, dtype=np.int32)
            key = (n.id_data.name, n.name, target.name)
            new_batches[key] = (
                target.name,
                batch_for_shader(shader, 'TRIS', {"pos": pos, "color": colors}, indices=indices),
                batch_for_shader(shader, 'TRIS', {"pos": pos, "color": ghost_colors}, indices=indices),
            )
    _preview_batches = new_batches


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
    _PREVIEW_TOPOLOGY_CACHE.clear()
    _PREVIEW_TOPOLOGY_ORDER.clear()
    _PREVIEW_MESH_CACHE.clear()
    _PREVIEW_MERGED_CACHE.clear()
    _PREVIEW_ZONE_FIELD_CACHE.clear()


def _preview_tick():
    """轮询预览，并将连续编辑产生的多次变化合并为一次批次重建。"""
    global _preview_timer, _preview_sig_cache
    global _preview_pending_signature, _preview_pending_since
    nodes = _collect_preview_nodes()
    if not nodes:
        _remove_preview_handler()
        _preview_timer = None
        _preview_sig_cache = None
        _preview_pending_signature = None
        _preview_pending_since = None
        return None  # 取消 timer
    _ensure_preview_handler()
    try:
        sig = tuple(_preview_signature(n) for n in nodes)
        now = _preview_now()
        if _preview_sig_cache is None:
            _rebuild_preview_batches(nodes)
            _preview_sig_cache = sig
            _preview_pending_signature = None
            _preview_pending_since = None
        elif sig == _preview_sig_cache:
            _preview_pending_signature = None
            _preview_pending_since = None
        elif sig != _preview_pending_signature:
            _preview_pending_signature = sig
            _preview_pending_since = now
        elif now - _preview_pending_since >= _PREVIEW_DEBOUNCE:
            _rebuild_preview_batches(nodes)
            _preview_sig_cache = sig
            _preview_pending_signature = None
            _preview_pending_since = None
    except Exception:
        # 重建失败时保留可诊断输出（控制台可见 traceback），避免静默黑屏
        import traceback
        traceback.print_exc()
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
    global _preview_timer, _preview_sig_cache
    global _preview_pending_signature, _preview_pending_since
    if _preview_timer is not None:
        try:
            bpy.app.timers.unregister(_preview_timer)
        except Exception:
            pass
        _preview_timer = None
    _preview_sig_cache = None
    _preview_pending_signature = None
    _preview_pending_since = None
    _remove_preview_handler()


classes = (
    SSMT_DragZoneIncludeRef,
    SSMT_DragZoneSettings,
    SSMT_DragZoneRef,
    SSMT_OT_DragZoneAdd,
    SSMT_OT_DragZoneRemove,
    SSMT_OT_DragZonePage,
    SSMT_OT_DragZoneIncludeAdd,
    SSMT_OT_DragZoneIncludeRemove,
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
