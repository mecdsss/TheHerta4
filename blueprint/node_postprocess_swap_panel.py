"""
物体切换面板 后处理节点
================================
读取蓝图中的「物体切换」节点(SSMTNode_ObjectSwap)，以及 mod 的 ini 中的物体切换配置
([KeySwap_*] 段落)，生成一个类似「滑块面板」的图形面板。

面板中为每个物体切换生成一个按钮（没有滑块），点击按钮的行为与按下该切换对应的
按键（KeySwap）完全一致：循环切换 $swapkeyX 的值。
"""

import math
import os
import glob
import re
import shutil
import uuid
from collections import OrderedDict

import bpy

from .node_postprocess_base import SSMTNode_PostProcess_Base

try:
    from .node_swap_ini import SwapKeyINIGenerator
    SWAP_GENERATOR_AVAILABLE = True
except ImportError:
    SWAP_GENERATOR_AVAILABLE = False

try:
    from .variable_registry import get_node_variable_name
    VARIABLE_REGISTRY_AVAILABLE = True
except ImportError:
    VARIABLE_REGISTRY_AVAILABLE = False

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw as PILDraw
    from PIL import ImageFont as PILImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class SSMT_SwapPanelEntry(bpy.types.PropertyGroup):
    """面板中一个物体切换按钮的条目信息（仅用于节点 UI 预览）。"""
    variable_name: bpy.props.StringProperty(name="变量名", default="")   # 如 $swapkey6
    comment: bpy.props.StringProperty(name="备注", default="")
    option_count: bpy.props.IntProperty(name="选项数", default=2, min=1, max=1024)
    hotkey: bpy.props.StringProperty(name="按键", default="")
    node_name: bpy.props.StringProperty(name="节点", default="")


class SSMT_OT_SwapPanel_ParseObject(bpy.types.Operator):
    bl_idname = "ssmt.swap_panel_parse_object"
    bl_label = "解析物体"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_SwapPanel':
                    node._update_from_object()
                    self.report({'INFO'}, f"已解析物体 '{node.target_object}' -> 哈希: {node.detect_hash}, IndexCount: {node.detect_index_count}")
                    return {'FINISHED'}
        self.report({'WARNING'}, "无法找到物体切换面板节点")
        return {'CANCELLED'}


class SSMT_OT_SwapPanel_Scan(bpy.types.Operator):
    bl_idname = "ssmt.swap_panel_scan"
    bl_label = "刷新物体切换列表"
    bl_description = "从蓝图收集物体切换节点，若设置了 INI 文件则同时读取其 [KeySwap_*] 配置"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_SwapPanel':
                    count = node._refresh_entries()
                    self.report({'INFO'}, f"已刷新物体切换列表，共 {count} 项")
                    return {'FINISHED'}
        self.report({'WARNING'}, "无法找到物体切换面板节点")
        return {'CANCELLED'}


class SSMT_OT_SwapPanel_Refresh(bpy.types.Operator):
    bl_idname = "ssmt.swap_panel_refresh"
    bl_label = "应用设置到Mod"
    bl_description = "按节点当前设置，原地更新 mod ini 中物体切换面板的配置与图片（无需重新导出整个 mod）"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_SwapPanel':
                    ini_path = (node.ini_file_path or "").strip() or node.last_mod_ini_path
                    if not ini_path or not os.path.isfile(ini_path):
                        self.report({'WARNING'}, "未找到目标 ini：请先导出一次 mod，或在节点「INI文件」中指定")
                        return {'CANCELLED'}
                    ok = node.execute_postprocess(os.path.dirname(ini_path), _in_place=True, _ini_path=ini_path)
                    if ok:
                        self.report({'INFO'}, f"已原地刷新物体切换面板: {os.path.basename(ini_path)}")
                        return {'FINISHED'}
                    self.report({'ERROR'}, "刷新失败，请查看控制台日志（旧格式可能需要先重新导出一次）")
                    return {'CANCELLED'}
        self.report({'WARNING'}, "无法找到物体切换面板节点")
        return {'CANCELLED'}


class SSMTNode_PostProcess_SwapPanel(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_SwapPanel'
    bl_label = '物体切换面板'
    bl_description = '读取蓝图物体切换节点与 mod 的 KeySwap 配置，生成带按钮的图形切换面板（点击按钮=按下切换按键）'

    def init(self, context):
        super().init(context)
        self.width = 600  # 基类默认 300 的 2 倍（适配左右两列 UI）

    # ---- 配置行专有标识（生成时留在 ini 中，供「应用设置到Mod」精准定位与原地更新设置项）----
    PANEL_TAG = "SSMTSwapPanel"
    BLOCK_BEGIN = "; @@SSMTSwapPanel:BLOCK:BEGIN:{ns}@@"
    BLOCK_END = "; @@SSMTSwapPanel:BLOCK:END:{ns}@@"
    PRESENT_BEGIN = "; ========== SWAP PANEL LOGIC (appended) [{ns}] =========="
    PRESENT_END = "; ========== SWAP PANEL LOGIC END (appended) [{ns}] =========="
    DUP_GUARD = "SWAP PANEL LOGIC (appended)"

    create_cumulative_backup: bpy.props.BoolProperty(name="创建累积备份", default=True)

    help_key: bpy.props.StringProperty(name="显示/隐藏面板", default="home")
    reset_key: bpy.props.StringProperty(name="重置位置", default="ctrl home")
    zoom_in_key: bpy.props.StringProperty(name="放大", default="up")
    zoom_out_key: bpy.props.StringProperty(name="缩小", default="down")
    drag_key: bpy.props.StringProperty(name="拖拽键", default="VK_LBUTTON")

    button_height: bpy.props.FloatProperty(name="按钮高度", default=0.05, min=0.001, max=1.0, precision=4)
    panel_min_height: bpy.props.FloatProperty(name="面板最小高度", default=0.75, min=0.01, max=1.0, precision=4)

    panel_default_scale: bpy.props.FloatProperty(
        name="面板默认缩放",
        description="面板默认显示的缩放比例（1.0 = 原始大小；可在游戏内用放大/缩小键再调节）",
        default=1.0, min=0.1, max=5.0, precision=2
    )

    use_remark_as_icon: bpy.props.BoolProperty(name="用备注生成图标", default=True)
    remark_font_family: bpy.props.EnumProperty(
        name="字体",
        items=[('msyh.ttc', "微软雅黑", ""), ('simsun.ttc', "宋体", ""), ('simhei.ttf', "黑体", ""), ('arial.ttf', "Arial", "")],
        default='msyh.ttc'
    )
    remark_font_size: bpy.props.IntProperty(name="字号大小", default=36, min=10, max=300)
    remark_stroke_width: bpy.props.IntProperty(name="描边粗细", default=2, min=0, max=20)
    remark_text_color: bpy.props.FloatVectorProperty(name="文字颜色", subtype='COLOR', default=(1.0, 1.0, 1.0), min=0.0, max=1.0, size=3)
    remark_stroke_color: bpy.props.FloatVectorProperty(name="描边颜色", subtype='COLOR', default=(0.0, 0.0, 0.0), min=0.0, max=1.0, size=3)

    button_bg_color: bpy.props.FloatVectorProperty(name="按钮背景色", subtype='COLOR', default=(0.16, 0.22, 0.32), min=0.0, max=1.0, size=3)
    button_border_color: bpy.props.FloatVectorProperty(name="按钮边框色", subtype='COLOR', default=(0.59, 0.75, 0.94), min=0.0, max=1.0, size=3)
    button_border_width: bpy.props.IntProperty(name="边框宽度", default=2, min=0, max=20)
    button_opacity: bpy.props.FloatProperty(name="按钮透明度", default=0.9, min=0.0, max=1.0, precision=2)
    button_align: bpy.props.EnumProperty(
        name="按钮对齐",
        items=[('LEFT', "左对齐", ""), ('CENTER', "居中", ""), ('RIGHT', "右对齐", "")],
        default='CENTER'
    )

    def _update_target_object(self, context):
        self._update_from_object()

    target_object: bpy.props.StringProperty(name="目标物体", default="", update=_update_target_object)
    detect_hash: bpy.props.StringProperty(name="哈希值", default="")
    detect_index_count: bpy.props.StringProperty(name="IndexCount", default="")

    background_image: bpy.props.StringProperty(name="背景图片", subtype='FILE_PATH', default="")
    button_image: bpy.props.StringProperty(name="按钮图片", subtype='FILE_PATH', default="")

    # ---- 面板背景样式（圆角 + 边框，对背景图应用；自定义背景同样生效）----
    background_corner_radius: bpy.props.IntProperty(name="背景圆角", default=24, min=0, max=100)
    background_border_color: bpy.props.FloatVectorProperty(name="背景边框色", subtype='COLOR', default=(0.59, 0.75, 0.94), min=0.0, max=1.0, size=3)
    background_border_width: bpy.props.IntProperty(name="背景边框宽度", default=3, min=0, max=20)
    background_opacity: bpy.props.FloatProperty(name="背景透明度", default=0.85, min=0.0, max=1.0, precision=2)

    check_hash: bpy.props.StringProperty(name="检测Hash值", default="")
    match_index_count: bpy.props.IntProperty(name="Match Index Count", default=0, min=0)

    # 可选：用于预览/导出的物体切换配置 INI 文件
    # 留空时导出会自动读取目标 mod 的 ini（其中的 [KeySwap_*] 段落）
    ini_file_path: bpy.props.StringProperty(name="INI文件", subtype='FILE_PATH', default="")

    # 面板实例唯一命名空间：用于与滑块面板等其它面板隔离（自动生成，复制节点时重置）
    namespace: bpy.props.StringProperty(
        name="命名空间",
        description="面板实例唯一标识，用于隔离不同面板的变量/段落（自动生成）",
        default="",
        options={'HIDDEN'},
    )

    last_mod_ini_path: bpy.props.StringProperty(
        name="上次导出INI",
        description="最近一次导出时生成的 mod ini 路径，供「应用设置到Mod」原地更新",
        default="",
        options={'HIDDEN'},
    )

    swap_panel_entries: bpy.props.CollectionProperty(type=SSMT_SwapPanelEntry)

    def _ensure_namespace(self):
        """生成/返回面板实例唯一命名空间（稳定持久，保证位置/缩放跨导出保留）。"""
        if not self.namespace:
            self.namespace = "swp_" + uuid.uuid4().hex[:8]
        return self.namespace

    def copy(self, node):
        # 复制节点时重置命名空间，避免与源节点共用同一套面板变量/段落
        self.namespace = ""

    # ==========================================
    # 原地更新：按专有标识移除本面板旧配置
    # ==========================================
    def _is_owned_section(self, sec_name, ns):
        """判断该段落是否属于本物体切换面板实例（按命名空间段落名识别）。"""
        s = sec_name.strip()
        fixed = [
            f"[KeyHelp_{ns}]", f"[KeyResetPosition_{ns}]", f"[KeyZoomIn_{ns}]",
            f"[KeyZoomOut_{ns}]", f"[KeyMouseDrag_{ns}]",
            f"[CommandListZoomIn_{ns}]", f"[CommandListZoomOut_{ns}]",
            f"[ResourceImageToRender0_{ns}]", f"[CustomShaderDraw_{ns}]",
            f"[TextureOverrideCheckHash_{ns}]",
        ]
        if s in fixed:
            return True
        suffix = f"_{ns}]"
        if s.startswith("[CommandListSwap") and s.endswith(suffix):
            return True
        if s.startswith("[ResourceSwapButton") and s.endswith(suffix):
            return True
        return False

    def _remove_owned_config(self, sections, ns):
        """按专有标识移除本面板在 ini 中的旧配置（Constants 块、自有段落、Present 块）。"""
        begin_marker = self.BLOCK_BEGIN.format(ns=ns)
        end_marker = self.BLOCK_END.format(ns=ns)
        if '[Constants]' in sections:
            new_lines = []
            skipping = False
            for line in sections['[Constants]']:
                if begin_marker in line:
                    skipping = True
                    continue
                if end_marker in line:
                    skipping = False
                    continue
                if not skipping:
                    new_lines.append(line)
            sections['[Constants]'] = new_lines

        for sec_name in list(sections.keys()):
            if self._is_owned_section(sec_name, ns):
                del sections[sec_name]

        if '[Present]' in sections:
            new_lines = []
            skipping = False
            present_begin = self.PRESENT_BEGIN.format(ns=ns)
            present_end = self.PRESENT_END.format(ns=ns)
            for line in sections['[Present]']:
                if present_begin in line:
                    skipping = True
                    continue
                if present_end in line:
                    skipping = False
                    continue
                if not skipping:
                    new_lines.append(line)
            sections['[Present]'] = new_lines

    # ==========================================
    # UI
    # ==========================================
    def draw_buttons(self, context, layout):
        layout.prop(self, "create_cumulative_backup")

        # 顶部：原地刷新（应用设置到Mod，无需重新导出）
        box_top = layout.box()
        box_top.operator("ssmt.swap_panel_refresh", text="应用设置到Mod（原地更新）", icon='FILE_TICK').node_name = self.name
        box_top.prop(self, "ini_file_path", text="INI文件（可选，留空用上次导出）")
        box_top.label(text="修改设置后点上方按钮即可原地更新，无需重新导出", icon='INFO')
        layout.separator()

        # 左右两列：左=基础设置/检测/列表，右=文字图标/样式/资源（从「按钮文字图标」往下）
        split = layout.split(factor=0.5)
        col_left = split.column()
        col_right = split.column()

        # ---- 左列 ----
        box = col_left.box()
        box.label(text="快捷键设置", icon='KEYINGSET')
        col = box.column(align=True)
        col.prop(self, "help_key", text="显示/隐藏面板")
        col.prop(self, "reset_key", text="重置位置")
        col.prop(self, "zoom_in_key", text="放大")
        col.prop(self, "zoom_out_key", text="缩小")
        col.prop(self, "drag_key", text="拖拽键")

        box = col_left.box()
        box.label(text="UI尺寸设置", icon='PROPERTIES')
        col = box.column(align=True)
        col.prop(self, "panel_default_scale", text="面板默认缩放")
        col.separator()
        col.prop(self, "button_height", text="按钮高度")
        col.prop(self, "panel_min_height", text="面板最小高度")
        col.label(text="按钮/面板背景尺寸按图片比例自动计算", icon='INFO')

        box = col_left.box()
        box.label(text="角色检测设置", icon='VIEWZOOM')
        row = box.row(align=True)
        row.prop_search(self, "target_object", bpy.data, "objects", text="物体", icon='OBJECT_DATA')
        row.operator("ssmt.swap_panel_parse_object", text="", icon='FILE_REFRESH').node_name = self.name
        box.prop(self, "detect_hash", text="哈希值")
        box.prop(self, "detect_index_count", text="IndexCount")

        box = col_left.box()
        box.label(text="物体切换按钮列表", icon='SHADERFX')
        row = box.row(align=True)
        row.operator("ssmt.swap_panel_scan", text="刷新列表", icon='FILE_REFRESH').node_name = self.name
        box.label(text="修改设置后点顶部「应用设置到Mod」即可原地更新", icon='INFO')
        box.label(text="留空则导出时自动读取目标 mod 的 ini", icon='INFO')
        if self.swap_panel_entries:
            for entry in self.swap_panel_entries:
                note = entry.comment or "(无备注)"
                box.label(text=f"  {entry.variable_name}  |  {note}  |  {entry.option_count}项", icon='NONE')
        else:
            box.label(text="未检测到切换，点击「刷新列表」", icon='INFO')

        # ---- 右列（从「按钮文字图标」往下的部分）----
        box = col_right.box()
        box.label(text="按钮文字图标（用切换备注生成）", icon='FILE_FONT')
        col = box.column(align=True)
        col.prop(self, "use_remark_as_icon", text="用备注生成图标")
        col.prop(self, "remark_font_family", text="字体")
        col.prop(self, "remark_font_size", text="字号")
        row = col.row(align=True)
        row.prop(self, "remark_text_color", text="文字色")
        row.prop(self, "remark_stroke_color", text="描边色")
        col.prop(self, "remark_stroke_width", text="描边粗细")

        box = col_right.box()
        box.label(text="按钮样式", icon='COLOR')
        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(self, "button_bg_color", text="背景色")
        row.prop(self, "button_border_color", text="边框色")
        col.prop(self, "button_border_width", text="边框宽度")
        col.prop(self, "button_opacity", text="透明度")
        col.prop(self, "button_align", text="对齐")

        box = col_right.box()
        box.label(text="面板图片资源（自定义）", icon='TEXTURE')
        box.prop(self, "background_image", text="背景")
        box.prop(self, "button_image", text="按钮")
        box.label(text="留空则自动生成默认按钮图片", icon='INFO')

        box = col_right.box()
        box.label(text="面板背景样式（圆角/边框）", icon='MATERIAL')
        col = box.column(align=True)
        col.prop(self, "background_corner_radius", text="背景圆角")
        col.prop(self, "background_border_color", text="边框色")
        col.prop(self, "background_border_width", text="边框宽度")
        col.prop(self, "background_opacity", text="透明度")
        col.label(text="对背景图片应用圆角/边框（自定义背景同样生效）", icon='INFO')

    # ==========================================
    # 扫描 / 解析
    # ==========================================
    def _update_from_object(self):
        obj = bpy.data.objects.get(self.target_object)
        if not obj:
            return
        obj_name = obj.name
        match = re.search(r'([a-f0-9]{8})-([0-9]+)(?:-([0-9]+))?', obj_name)
        if match:
            self.detect_hash = match.group(1)
            self.detect_index_count = match.group(2)
        else:
            match2 = re.search(r'([a-f0-9]{8})', obj_name)
            if match2:
                self.detect_hash = match2.group(1)
                self.detect_index_count = ""
            else:
                print(f"[SwapPanel] 无法从物体名称 '{obj_name}' 解析哈希和IndexCount")

    def _collect_swap_nodes(self):
        """从蓝图收集物体切换节点（含嵌套蓝图）。"""
        tree = getattr(self, "id_data", None)
        if tree is None or not SWAP_GENERATOR_AVAILABLE:
            return []
        try:
            return SwapKeyINIGenerator.collect_all_swap_nodes_from_blueprint(tree)
        except Exception:
            return []

    def _refresh_entries(self):
        """刷新节点 UI 中的物体切换列表：蓝图节点 + 可选 INI 配置。"""
        self._ensure_namespace()
        self.swap_panel_entries.clear()

        # 1. 蓝图物体切换节点
        nodes = self._collect_swap_nodes()
        idx_map = {}
        for node in nodes:
            if not VARIABLE_REGISTRY_AVAILABLE:
                break
            var = get_node_variable_name(node)
            if not var:
                continue
            entry = self.swap_panel_entries.add()
            entry.variable_name = var
            entry.comment = str(getattr(node, "comment", "") or "")
            entry.option_count = max(1, int(getattr(node, "input_slot_count", 2) or 2))
            entry.hotkey = str(getattr(node, "hotkey", "") or "")
            entry.node_name = node.name
            idx_map[var] = len(self.swap_panel_entries) - 1

        # 2. 可选：从 INI 读取 [KeySwap_*] 配置并合并（覆盖备注/选项数/按键）
        ini_path = (self.ini_file_path or "").strip()
        if ini_path and os.path.isfile(ini_path):
            ini_swaps = self._parse_ini_key_swaps(ini_path)
            for s in ini_swaps:
                var = s["var_name"]
                if var in idx_map:
                    entry = self.swap_panel_entries[idx_map[var]]
                    if s["comment"]:
                        entry.comment = s["comment"]
                    entry.option_count = s["option_count"]
                    if s["key"]:
                        entry.hotkey = s["key"]
                else:
                    entry = self.swap_panel_entries.add()
                    entry.variable_name = var
                    entry.comment = s["comment"]
                    entry.option_count = s["option_count"]
                    entry.hotkey = s["key"]
                    entry.node_name = ""
        return len(self.swap_panel_entries)

    # ==========================================
    # INI 解析
    # ==========================================
    @classmethod
    def split_anim_driver_block_content(cls, content):
        """Extract a complete animation-driver block from the top of an INI file.

        Mirrors SSMTNode_PostProcess_Base.split_anim_driver_block_content on
        main so this branch works standalone (the method was added to the base
        class after this PR forked).
        """
        text = str(content or "")
        lines = text.splitlines(keepends=True)
        start_index = next(
            (index for index, line in enumerate(lines)
             if getattr(cls, "ANIM_DRIVER_SECTION_MARKER_START", "; --- ANIMATION DRIVER SECTION ---") in line),
            None,
        )
        if start_index is None:
            return "", text
        end_marker = getattr(cls, "ANIM_DRIVER_SECTION_MARKER_END", "; --- END ANIMATION DRIVER SECTION ---")
        end_index = next(
            (index for index in range(start_index + 1, len(lines)) if end_marker in lines[index]),
            None,
        )
        if end_index is None:
            return "", text
        driver_content = "".join(lines[start_index:end_index + 1])
        remaining_content = "".join(lines[end_index + 1:]).lstrip("\r\n")
        return driver_content, remaining_content

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
            for line in content.splitlines():
                stripped_line = line.strip()
                if stripped_line.startswith('[') and stripped_line.endswith(']') and len(stripped_line) > 2:
                    current_section = stripped_line
                    sections[current_section] = []
                elif current_section is not None:
                    sections[current_section].append(line.rstrip())
        except FileNotFoundError:
            return None
        return sections, preserved_tail_content, preserved_driver_content

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content="", preserved_driver_content=""):
        try:
            with open(ini_file_path, 'w', encoding='utf-8') as f:
                if preserved_driver_content:
                    f.write(preserved_driver_content)
                    if not preserved_driver_content.endswith('\n'):
                        f.write('\n')
                    f.write('\n')
                for section_name, lines in sections.items():
                    if section_name.startswith(';;'):
                        f.write(section_name + '\n')
                    else:
                        f.write(section_name + '\n')
                    for line in lines:
                        f.write(line + '\n')
                    f.write('\n')
                if preserved_tail_content:
                    f.write('\n' + preserved_tail_content)
        except Exception as e:
            print(f"写入INI文件失败: {e}")

    def _parse_ini_key_swaps(self, ini_path):
        """解析 ini 中所有 [KeySwap_N] 段落，返回 [{index,var_name,comment,key,option_count}]。"""
        result = self._read_ini_to_ordered_dict(ini_path)
        if result is None or not result[0]:
            return []
        sections, _, _ = result

        swaps = []
        for section_name, lines in sections.items():
            m = re.match(r'^\[KeySwap_(\d+)\]$', section_name.strip())
            if not m:
                continue
            index = int(m.group(1))
            entry = {"index": index, "var_name": "", "comment": "", "key": "", "option_count": 2}
            for line in lines:
                s = line.strip()
                if not s:
                    continue
                if s.startswith(';'):
                    if not entry["comment"]:
                        entry["comment"] = s.lstrip(';').strip()
                    continue
                m2 = re.match(r'key\s*=\s*(.+)', s, re.IGNORECASE)
                if m2:
                    entry["key"] = m2.group(1).strip()
                    continue
                m3 = re.match(r'^(\$[A-Za-z0-9_]+)\s*=\s*(.*)$', s)
                if m3:
                    entry["var_name"] = m3.group(1)
                    vals = [v for v in m3.group(2).split(',') if v.strip() != '']
                    if vals:
                        entry["option_count"] = len(vals)
            if entry["var_name"]:
                swaps.append(entry)

        swaps.sort(key=lambda e: e["index"])
        return swaps

    # ==========================================
    # 导出
    # ==========================================
    def _build_button_list(self, target_ini_file):
        """构建按钮数据列表。

        优先顺序：
        1. 用户显式指定的 INI 文件（ini_file_path）
        2. 目标 mod 导出 ini 中的 [KeySwap_*] 段落
        3. 蓝图物体切换节点（回退）
        """
        # 1. 显式指定 INI
        explicit = (self.ini_file_path or "").strip()
        if explicit and os.path.isfile(explicit):
            ini_swaps = self._parse_ini_key_swaps(explicit)
            if ini_swaps:
                return [{"var_name": s["var_name"], "comment": s["comment"],
                         "option_count": s["option_count"], "hotkey": s["key"]} for s in ini_swaps]

        # 2. 目标 ini
        ini_swaps = self._parse_ini_key_swaps(target_ini_file)
        if ini_swaps:
            return [{"var_name": s["var_name"], "comment": s["comment"],
                     "option_count": s["option_count"], "hotkey": s["key"]} for s in ini_swaps]

        # 3. 蓝图节点
        buttons = []
        nodes = self._collect_swap_nodes()
        for node in nodes:
            if not VARIABLE_REGISTRY_AVAILABLE:
                break
            var = get_node_variable_name(node)
            if not var:
                continue
            buttons.append({
                "var_name": var,
                "comment": str(getattr(node, "comment", "") or ""),
                "option_count": max(1, int(getattr(node, "input_slot_count", 2) or 2)),
                "hotkey": str(getattr(node, "hotkey", "") or ""),
            })
        return buttons

    @staticmethod
    def _cycle_command_lines(var, option_count):
        """生成循环切换 $var 的 CommandList 行（等价于按下 KeySwap 的 cycle 按键）。"""
        n = max(2, int(option_count or 2))
        lines = []
        for v in range(n - 1):
            prefix = "if" if v == 0 else "else if"
            lines.append(f"{prefix} {var} == {v}")
            lines.append(f"    {var} = {v + 1}")
        lines.append("else")
        lines.append(f"    {var} = 0")
        lines.append("endif")
        return lines

    def _copy_default_image(self, std_name, dest_res_dir, source_asset_dir):
        default_path = os.path.join(source_asset_dir, std_name)
        dest_path = os.path.join(dest_res_dir, std_name)
        if os.path.exists(default_path):
            if not os.path.exists(dest_path):
                shutil.copy2(default_path, dest_path)
                print(f"已复制默认图片: {std_name}")
        else:
            print(f"警告: 默认图片不存在 {default_path}")

    def _generate_text_icon(self, text, dest_path, font_size=36, font_family="msyh.ttc",
                            text_color=(1.0, 1.0, 1.0), stroke_width=2, stroke_color=(0.0, 0.0, 0.0),
                            bg_color=(0.16, 0.22, 0.32), border_color=(0.59, 0.75, 0.94),
                            border_width=2, opacity=0.9):
        """根据备注文本生成按钮图标（圆角按钮背景 + 居中文字）。"""
        try:
            if not PIL_AVAILABLE:
                return None
            text = text.replace('/', '\n').strip()
            if not text:
                return None

            def float_to_int_rgb(vals):
                return tuple(int(val * 255) for val in vals)

            text_rgb = float_to_int_rgb(text_color)
            stroke_rgb = float_to_int_rgb(stroke_color)

            font = None
            try:
                font = PILImageFont.truetype(font_family, font_size)
            except Exception:
                for f in ["msyh.ttc", "simsun.ttc", "simhei.ttf", "arial.ttf"]:
                    try:
                        font = PILImageFont.truetype(f, font_size)
                        break
                    except Exception:
                        continue
            if font is None:
                font = PILImageFont.load_default()

            temp_img = PILImage.new('RGBA', (1, 1), (0, 0, 0, 0))
            temp_draw = PILDraw.Draw(temp_img)
            bbox = temp_draw.multiline_textbbox((0, 0), text, font=font, align='center', spacing=4)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            pad_x = 18 + (stroke_width * 2)
            pad_y = 10 + (stroke_width * 2)
            img_w = math.ceil(text_w + pad_x * 2)
            img_h = math.ceil(text_h + pad_y * 2)

            bg_rgb = float_to_int_rgb(bg_color)
            bd_rgb = float_to_int_rgb(border_color)
            btn_alpha = int(255 * max(0.0, min(1.0, opacity)))

            img = PILImage.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
            draw = PILDraw.Draw(img)
            radius = min(14, img_h // 3)
            try:
                draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=radius,
                                       fill=bg_rgb + (btn_alpha,),
                                       outline=bd_rgb + (btn_alpha,), width=border_width)
            except Exception:
                draw.rectangle([0, 0, img_w - 1, img_h - 1], fill=bg_rgb + (btn_alpha,),
                               outline=bd_rgb + (btn_alpha,))

            x = (img_w - text_w) / 2 - bbox[0]
            y = (img_h - text_h) / 2 - bbox[1]
            draw.multiline_text((x, y), text, font=font, fill=text_rgb + (255,),
                                align='center', spacing=4,
                                stroke_width=stroke_width, stroke_fill=stroke_rgb + (255,))
            img.save(dest_path)
            return dest_path
        except Exception as e:
            print(f"[物体切换面板] 生成文字图标失败: {e}")
            return None

    def _ensure_button_image(self, dest_res_dir, ns, i, source_asset_dir, button):
        """生成第 i 个按钮的图标图片，返回路径。
        优先：备注文字图标 → 用户按钮图 → 默认圆角按钮图。"""
        dest_name = f"swpbtn_{ns}_{i}.png"
        dest_path = os.path.join(dest_res_dir, dest_name)

        comment = (button.get("comment") or "").strip()
        if self.use_remark_as_icon and comment:
            generated = self._generate_text_icon(
                comment, dest_path,
                font_size=self.remark_font_size,
                font_family=self.remark_font_family,
                text_color=self.remark_text_color,
                stroke_width=self.remark_stroke_width,
                stroke_color=self.remark_stroke_color,
                bg_color=self.button_bg_color,
                border_color=self.button_border_color,
                border_width=self.button_border_width,
                opacity=self.button_opacity,
            )
            if generated:
                return generated

        custom = (self.button_image or "").strip()
        if custom and os.path.isfile(custom):
            shutil.copy2(custom, dest_path)
            return dest_path

        if PIL_AVAILABLE:
            try:
                def _c(vals):
                    return tuple(int(val * 255) for val in vals)
                bg_rgb = _c(self.button_bg_color)
                bd_rgb = _c(self.button_border_color)
                btn_alpha = int(255 * max(0.0, min(1.0, self.button_opacity)))
                img = PILImage.new('RGBA', (384, 64), (0, 0, 0, 0))
                draw = PILDraw.Draw(img)
                try:
                    draw.rounded_rectangle([0, 0, 383, 63], radius=12,
                                           fill=bg_rgb + (btn_alpha,),
                                           outline=bd_rgb + (btn_alpha,), width=self.button_border_width)
                except Exception:
                    draw.rectangle([0, 0, 383, 63], fill=bg_rgb + (btn_alpha,),
                                   outline=bd_rgb + (btn_alpha,))
                img.save(dest_path)
                return dest_path
            except Exception as e:
                print(f"生成默认按钮图片失败: {e}")

        fallback = os.path.join(source_asset_dir, "0.png")
        if os.path.exists(fallback):
            shutil.copy2(fallback, dest_path)
            return dest_path
        return None

    def _generate_background_image(self, dest_path, panel_w, panel_h, use_existing=False):
        """生成/处理面板背景图（圆角 + 边框）。panel_w/panel_h 为面板在屏幕单位的宽高。

        use_existing=False：生成纯色圆角背景（默认 512 高）。
        use_existing=True：基于 dest_path 现有图（自定义背景）应用圆角遮罩 + 边框。
        """
        try:
            if not PIL_AVAILABLE:
                return None

            def float_to_int_rgb(vals):
                return tuple(int(val * 255) for val in vals)

            if use_existing and os.path.exists(dest_path):
                # 基于自定义背景图应用圆角/边框
                with PILImage.open(dest_path) as src:
                    img = src.convert('RGBA')
            else:
                # 生成纯色底图（按面板在 16:9 屏幕上的显示比例，保证圆角不变形）
                img_h = 512
                pixel_ratio = (panel_w / panel_h) * (1920.0 / 1080.0) if panel_h > 0 else 1.0
                img_w = max(64, int(round(img_h * pixel_ratio)))
                fill_rgb = float_to_int_rgb((0.05, 0.08, 0.12))
                img = PILImage.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
                d = PILDraw.Draw(img)
                d.rectangle([0, 0, img_w - 1, img_h - 1], fill=fill_rgb + (255,))

            img_w, img_h = img.size
            bd_rgb = float_to_int_rgb(self.background_border_color)
            alpha = int(255 * max(0.0, min(1.0, self.background_opacity)))

            # 圆角半径按图片实际尺寸比例（自定义大图圆角视觉一致）
            radius_px = int(min(img_w, img_h) * max(0, min(100, self.background_corner_radius)) / 100.0)
            # 边框宽度按 512 参考高缩放（自定义大图也可见）
            border_scale = max(1.0, img_h / 512.0)
            border_px = max(0, int(round(self.background_border_width * border_scale)))

            # 应用整体透明度
            if alpha < 255:
                r, g, b, a = img.split()
                a = a.point(lambda v: int(v * alpha / 255))
                img = PILImage.merge('RGBA', (r, g, b, a))

            # 圆角 alpha 遮罩
            mask = PILImage.new('L', (img_w, img_h), 0)
            md = PILDraw.Draw(mask)
            if radius_px > 0:
                md.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=radius_px, fill=255)
            else:
                md.rectangle([0, 0, img_w - 1, img_h - 1], fill=255)
            r, g, b, a = img.split()
            a = PILImage.composite(a, PILImage.new('L', (img_w, img_h), 0), mask)
            img = PILImage.merge('RGBA', (r, g, b, a))

            # 边框（在圆角区域内）
            if border_px > 0:
                draw = PILDraw.Draw(img)
                inset = max(0, border_px // 2)
                try:
                    draw.rounded_rectangle([inset, inset, img_w - 1 - inset, img_h - 1 - inset],
                                           radius=max(0, radius_px - inset),
                                           outline=bd_rgb + (alpha,), width=border_px)
                except Exception:
                    draw.rectangle([inset, inset, img_w - 1 - inset, img_h - 1 - inset],
                                   outline=bd_rgb + (alpha,), width=border_px)
            img.save(dest_path)
            return dest_path
        except Exception as e:
            print(f"[物体切换面板] 生成背景图失败: {e}")
            return None

    def execute_postprocess(self, mod_export_path, _in_place=False, _ini_path=None):
        """生成 / 原地刷新物体切换面板配置。

        - 导出时（_in_place=False）：在 mod 目录 ini 中追加面板配置（含专有标识注释）。
        - 刷新时（_in_place=True）：按专有标识移除本面板旧配置后原地重新生成，无需重新导出整个 mod。
        """
        if _ini_path:
            target_ini_file = _ini_path
        else:
            ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
            if not ini_files:
                return False
            target_ini_file = ini_files[0]

        print(f"物体切换面板{'原地刷新' if _in_place else '开始执行'}: {target_ini_file}")

        # 防重复：仅导出时检查（刷新时按标识替换旧配置）
        if not _in_place:
            try:
                with open(target_ini_file, 'r', encoding='utf-8') as f:
                    if self.DUP_GUARD in f.read():
                        print("物体切换面板配置已存在于文件中。请手动删除后再生成，或用节点上的「应用设置到Mod」原地更新。")
                        return False
            except Exception:
                pass

        try:
            if self.create_cumulative_backup:
                self._create_cumulative_backup(target_ini_file, mod_export_path)
        except Exception as e:
            print(f"创建备份时出错: {e}")
            return False

        ns = self._ensure_namespace()
        self.last_mod_ini_path = target_ini_file

        # 1. 收集按钮数据
        buttons = self._build_button_list(target_ini_file)
        if not buttons:
            print("未检测到任何物体切换节点 / [KeySwap_*] 配置，跳过面板生成")
            return False
        num_buttons = len(buttons)

        # 2. 复制资源（shader + 背景图 + 按钮图）
        try:
            addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            source_asset_dir = os.path.join(addon_dir, "Toolset")
            dest_res_dir = os.path.join(mod_export_path, "res")
            os.makedirs(dest_res_dir, exist_ok=True)

            shader_src = os.path.join(source_asset_dir, "draw_2d.hlsl")
            shader_dst = os.path.join(dest_res_dir, "draw_2d.hlsl")
            if os.path.exists(shader_src) and not os.path.exists(shader_dst):
                shutil.copy2(shader_src, shader_dst)

            btn_h = self.button_height
            SCREEN_RATIO_CORRECTION = 0.5625  # 固定 16:9 宽屏修正系数

            bg_custom = (self.background_image or "").strip()
            bg_dest_path = os.path.join(dest_res_dir, f"swpbg_{ns}.png")
            if bg_custom and os.path.isfile(bg_custom):
                shutil.copy2(bg_custom, bg_dest_path)
            else:
                default_bg_src = os.path.join(source_asset_dir, "0.png")
                if os.path.exists(default_bg_src) and not os.path.exists(bg_dest_path):
                    shutil.copy2(default_bg_src, bg_dest_path)

            # 读取实际使用的背景图宽高（用户选择的背景图片优先；未选择时为内置默认图）
            # → 面板背景尺寸按「面板高度 × 所选背景图宽高比」自动计算
            bg_img_w = None
            bg_img_h = None
            if os.path.exists(bg_dest_path) and PIL_AVAILABLE:
                try:
                    with PILImage.open(bg_dest_path) as img:
                        _w, _h = img.size
                        if _w > 0 and _h > 0:
                            bg_img_w, bg_img_h = _w, _h
                except Exception:
                    pass
            bg_aspect = (bg_img_w / bg_img_h) if (bg_img_w and bg_img_h) else None

            # 生成每个按钮图标，并按「按钮高度×各图片宽高比」计算渲染宽度（保持图片比例不变形）
            btn_w_list = []
            max_btn_w = 0.0
            for i, b in enumerate(buttons, start=1):
                img_path = self._ensure_button_image(dest_res_dir, ns, i, source_asset_dir, b)
                btn_w_i = btn_h * 3.0 * SCREEN_RATIO_CORRECTION  # 缺省按 3:1 比例
                if img_path and PIL_AVAILABLE:
                    try:
                        with PILImage.open(img_path) as img:
                            w, h = img.size
                            if h > 0:
                                btn_w_i = btn_h * (w / h) * SCREEN_RATIO_CORRECTION
                    except Exception:
                        pass
                btn_w_list.append(btn_w_i)
                if btn_w_i > max_btn_w:
                    max_btn_w = btn_w_i
        except Exception as e:
            print(f"准备和复制资源文件时出错: {e}")
            return False

        # 3. 读取目标 ini
        result = self._read_ini_to_ordered_dict(target_ini_file)
        if result is None or not result[0]:
            return False
        sections, preserved_tail_content, preserved_driver_content = result

        # 刷新模式：先按专有标识移除本面板旧配置，再原地重新生成
        if _in_place:
            self._remove_owned_config(sections, ns)
            if '[Present]' in sections and any(self.DUP_GUARD in l for l in sections['[Present]']):
                print("检测到旧格式的物体切换面板配置（无标识标记）。请先重新导出一次 mod 以生成带标识的新配置。")
                return False

        # 4. 生成面板配置（面板背景宽度按「面板高度×背景图片宽高比」推导，保持背景比例）
        btn_h = self.button_height

        top_bottom_padding = 0.03
        spacing = 0.02
        total_button_height = num_buttons * btn_h
        total_spacing_height = max(0, (num_buttons - 1) * spacing)
        parent_height = total_button_height + total_spacing_height + (top_bottom_padding * 2)
        # 面板最小高度：自动计算高度过小时使用设置值
        parent_height = max(parent_height, self.panel_min_height)

        # 面板背景尺寸：高度 = 按钮堆叠高度；宽度 = 面板高度 × 实际背景图宽高比（严格按所选图片自动计算）
        # 若无法读取背景图比例，则退化为仅包住最宽按钮（不采用任何固定比例）
        if bg_aspect:
            derived_bg_width = parent_height * bg_aspect * SCREEN_RATIO_CORRECTION
            adjusted_panel_bg_width = max(derived_bg_width, max_btn_w + 0.04)
        else:
            adjusted_panel_bg_width = max_btn_w + 0.04

        # 生成/处理面板背景（圆角+边框）：无论是否自定义背景图都生效
        bg_custom = (self.background_image or "").strip()
        self._generate_background_image(os.path.join(dest_res_dir, f"swpbg_{ns}.png"),
                                        adjusted_panel_bg_width, parent_height,
                                        use_existing=bool(bg_custom))

        help_key = self.help_key.strip() or "home"
        reset_key = self.reset_key.strip() or "ctrl home"
        zoom_in_key = self.zoom_in_key.strip() or "up"
        zoom_out_key = self.zoom_out_key.strip() or "down"
        drag_key = self.drag_key.strip() or "VK_LBUTTON"
        detect_hash = self.detect_hash.strip() or self.check_hash.strip()
        detect_index_count_val = self.detect_index_count.strip()
        if not detect_index_count_val and self.match_index_count > 0:
            detect_index_count_val = str(self.match_index_count)

        # 每个按钮固定行位置（相对父级高度）
        fixed_rel_y = []
        for i in range(1, num_buttons + 1):
            offset_y = top_bottom_padding + (i - 1) * (btn_h + spacing) + (btn_h / 2)
            fixed_rel_y.append(offset_y / parent_height)

        constants_additions = []
        present_additions = []
        other_sections = OrderedDict()

        constants_additions.extend([
            self.BLOCK_BEGIN.format(ns=ns),
            "; --- UI 几何与位置配置 (由物体切换面板生成) ---",
            f"; @@{self.PANEL_TAG}:panel_width@@",
            f"global ${ns}_base_width0 = {adjusted_panel_bg_width:.4f}",
            f"; @@{self.PANEL_TAG}:panel_height@@",
            f"global ${ns}_base_height0 = {parent_height:.4f}",
            f"global ${ns}_set_x0 = 0.5", f"global ${ns}_set_y0 = 0.5",
        ])
        for i in range(1, num_buttons + 1):
            constants_additions.extend([
                f"; @@{self.PANEL_TAG}:btn_width_{i}@@",
                f"global ${ns}_btn_width{i} = {btn_w_list[i-1]:.4f}",
                f"; @@{self.PANEL_TAG}:btn_height_{i}@@",
                f"global ${ns}_btn_height{i} = {btn_h:.4f}",
                f"; @@{self.PANEL_TAG}:fixed_rel_y_{i}@@",
                f"global ${ns}_fixed_rel_y{i} = {fixed_rel_y[i-1]:.4f}",
                f"global ${ns}_rel_y{i}",
                f"global ${ns}_btn_x{i}", f"global ${ns}_btn_y{i}", f"global ${ns}_btn_pressed{i} = 0",
            ])

        constants_additions.extend([
            f"global ${ns}_ui_active", f"global ${ns}_help",
            f"global ${ns}_max_zoom = 5.0", f"global ${ns}_min_zoom = 0.1",
            f"global ${ns}_mouse_clicked = 0",
            f"global ${ns}_click_outside = 0", f"global ${ns}_is_dragging = 0",
            f"global ${ns}_drag_x = 0", f"global ${ns}_drag_y = 0",
            f"global persist ${ns}_img0_x = 0", f"global persist ${ns}_img0_y = 0",
            f"; @@{self.PANEL_TAG}:zoom0@@",
            f"global persist ${ns}_zoom0 = {self.panel_default_scale:.2f}",
            f"global ${ns}_norm_width0", f"global ${ns}_norm_height0",
            f"global ${ns}_btn_click_processed = 0",
        ])
        # 确保每个切换变量已声明（若 ini 中已存在会自动去重）
        for b in buttons:
            var_line = f"global persist {b['var_name']} = 0"
            if var_line not in constants_additions:
                constants_additions.append(var_line)
        constants_additions.append(self.BLOCK_END.format(ns=ns))

        detect_lines = []
        has_any_check = False
        if detect_hash:
            detect_lines.append(f"hash = {detect_hash}")
            has_any_check = True
        if detect_index_count_val:
            detect_lines.append(f"match_index_count = {detect_index_count_val}")
            has_any_check = True
        detect_lines.append(f"${ns}_ui_active = 1")
        if has_any_check:
            other_sections[f"[TextureOverrideCheckHash_{ns}]"] = detect_lines

        other_sections[f"[ResourceImageToRender0_{ns}]"] = [f"filename = ./res/swpbg_{ns}.png"]
        for i in range(1, num_buttons + 1):
            other_sections[f"[ResourceSwapButton{i}_{ns}]"] = [f"filename = ./res/swpbtn_{ns}_{i}.png"]

        reset_lines = [f"${ns}_img0_x = 0", f"${ns}_img0_y = 0",
                       f"; @@{self.PANEL_TAG}:reset_zoom0@@",
                       f"${ns}_zoom0 = {self.panel_default_scale:.2f}"]
        zoom_in_lines = [f"${ns}_zoom0 = ${ns}_zoom0 + 0.05"]
        zoom_out_lines = [f"${ns}_zoom0 = ${ns}_zoom0 - 0.05"]

        other_sections[f"[KeyHelp_{ns}]"] = [
            f"condition = ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:help_key@@", f"key = {help_key}",
            "type = cycle", f"${ns}_help = 0,1"
        ]
        other_sections[f"[KeyResetPosition_{ns}]"] = [
            f"condition = ${ns}_help == 1 && ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:reset_key@@", f"key = {reset_key}",
            "type = cycle",
        ] + reset_lines
        other_sections[f"[KeyZoomIn_{ns}]"] = [
            f"condition = ${ns}_help == 1 && ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:zoom_in_key@@", f"key = {zoom_in_key}",
            "type = press", f"run = CommandListZoomIn_{ns}"
        ]
        other_sections[f"[KeyZoomOut_{ns}]"] = [
            f"condition = ${ns}_help == 1 && ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:zoom_out_key@@", f"key = {zoom_out_key}",
            "type = press", f"run = CommandListZoomOut_{ns}"
        ]
        other_sections[f"[KeyMouseDrag_{ns}]"] = [
            f"condition = ${ns}_help == 1 && ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:drag_key@@", f"key = {drag_key}",
            "type = hold", f"${ns}_mouse_clicked = 1"
        ]

        other_sections[f"[CommandListZoomIn_{ns}]"] = zoom_in_lines
        other_sections[f"[CommandListZoomOut_{ns}]"] = zoom_out_lines

        # 每个按钮的循环切换 CommandList（等价于按下对应 KeySwap 按键）
        for i, b in enumerate(buttons, start=1):
            other_sections[f"[CommandListSwap{i}_{ns}]"] = self._cycle_command_lines(b["var_name"], b["option_count"])

        # ---- Present 逻辑（全部使用命名空间变量，与其它面板隔离）----
        present_additions.append(f"post ${ns}_ui_active = 0")
        present_additions.append(f"if ${ns}_help == 1 && ${ns}_ui_active == 1")
        present_additions.append("    ; --- 1. 尺寸计算 ---")
        present_additions.append(f"    ${ns}_norm_width0 = ${ns}_base_width0 * ${ns}_zoom0")
        present_additions.append(f"    ${ns}_norm_height0 = ${ns}_base_height0 * ${ns}_zoom0")

        present_additions.append("\n    ; --- 2. 位置初始化 ---")
        present_additions.append(f"    if ${ns}_img0_x == 0 && ${ns}_img0_y == 0")
        present_additions.append(f"        ${ns}_img0_x = ${ns}_set_x0 * (1 - ${ns}_norm_width0)")
        present_additions.append(f"        ${ns}_img0_y = ${ns}_set_y0 * (1 - ${ns}_norm_height0)")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 3. 计算按钮位置 ---")
        for i in range(1, num_buttons + 1):
            present_additions.append(f"    ${ns}_rel_y{i} = (${ns}_fixed_rel_y{i} * ${ns}_norm_height0) - (${ns}_btn_height{i} / 2)")
            present_additions.append(f"    ; @@{self.PANEL_TAG}:btn_align_{i}@@")
            if self.button_align == 'LEFT':
                present_additions.append(f"    ${ns}_btn_x{i} = ${ns}_img0_x + 0.02")
            elif self.button_align == 'RIGHT':
                present_additions.append(f"    ${ns}_btn_x{i} = ${ns}_img0_x + ${ns}_norm_width0 - ${ns}_btn_width{i} - 0.02")
            else:
                present_additions.append(f"    ${ns}_btn_x{i} = ${ns}_img0_x + (${ns}_norm_width0 - ${ns}_btn_width{i}) * 0.5")
            present_additions.append(f"    ${ns}_btn_y{i} = ${ns}_img0_y + ${ns}_rel_y{i}")

        present_additions.append("\n    ; --- 4. 按钮按下/弹起检测 ---")
        present_additions.append(f"    ${ns}_btn_click_processed = 0")
        present_additions.append(f"    if ${ns}_mouse_clicked && ${ns}_is_dragging == 0")
        for i in range(1, num_buttons + 1):
            present_additions.append(f"        if cursor_x > ${ns}_btn_x{i} && cursor_x < ${ns}_btn_x{i} + ${ns}_btn_width{i} && cursor_y > ${ns}_btn_y{i} && cursor_y < ${ns}_btn_y{i} + ${ns}_btn_height{i}")
            present_additions.append(f"            ${ns}_btn_pressed{i} = 1")
            present_additions.append(f"            ${ns}_btn_click_processed = 1")
            present_additions.append(f"        endif")
        present_additions.append("    else")
        present_additions.append(f"        if ${ns}_is_dragging == 0")
        for i in range(1, num_buttons + 1):
            present_additions.append(f"            if ${ns}_btn_pressed{i} == 1")
            present_additions.append(f"                if cursor_x > ${ns}_btn_x{i} && cursor_x < ${ns}_btn_x{i} + ${ns}_btn_width{i} && cursor_y > ${ns}_btn_y{i} && cursor_y < ${ns}_btn_y{i} + ${ns}_btn_height{i}")
            present_additions.append(f"                    run = CommandListSwap{i}_{ns}")
            present_additions.append(f"                endif")
            present_additions.append(f"                ${ns}_btn_pressed{i} = 0")
            present_additions.append(f"            endif")
        present_additions.append("        endif")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 5. 面板拖拽逻辑 ---")
        present_additions.append(f"    if ${ns}_mouse_clicked")
        present_additions.append(f"        if ${ns}_is_dragging == 0")
        present_additions.append(f"            if cursor_x > ${ns}_img0_x && cursor_x < ${ns}_img0_x + ${ns}_norm_width0 && cursor_y > ${ns}_img0_y && cursor_y < ${ns}_img0_y + ${ns}_norm_height0")
        present_additions.append(f"                ${ns}_is_dragging = 1")
        present_additions.append(f"                ${ns}_drag_x = cursor_x - ${ns}_img0_x")
        present_additions.append(f"                ${ns}_drag_y = cursor_y - ${ns}_img0_y")
        present_additions.append("            else")
        present_additions.append(f"                if ${ns}_btn_click_processed == 0")
        present_additions.append(f"                    ${ns}_click_outside = 1")
        present_additions.append("                endif")
        present_additions.append("            endif")
        present_additions.append("        endif")
        present_additions.append("    else")
        present_additions.append(f"        ${ns}_is_dragging = 0")
        present_additions.append("    endif")
        present_additions.append(f"    if ${ns}_click_outside == 1 && ${ns}_mouse_clicked == 0")
        present_additions.append(f"        ${ns}_help = 0")
        present_additions.append(f"        ${ns}_click_outside = 0")
        present_additions.append("    endif")
        present_additions.append(f"    if ${ns}_is_dragging == 1")
        present_additions.append(f"        ${ns}_img0_x = cursor_x - ${ns}_drag_x")
        present_additions.append(f"        ${ns}_img0_y = cursor_y - ${ns}_drag_y")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 6. 渲染 ---")
        present_additions.append("    ; 渲染面板背景 (最底层)")
        present_additions.append(f"    ps-t100 = ResourceImageToRender0_{ns}")
        present_additions.append(f"    x87 = ${ns}_norm_width0")
        present_additions.append(f"    y87 = ${ns}_norm_height0")
        present_additions.append(f"    z87 = ${ns}_img0_x")
        present_additions.append(f"    w87 = ${ns}_img0_y")
        present_additions.append(f"    run = CustomShaderDraw_{ns}")
        for i, b in enumerate(buttons, start=1):
            present_additions.append(f"\n    ; 渲染切换按钮{i} ({b['var_name']})")
            present_additions.append(f"    ps-t100 = ResourceSwapButton{i}_{ns}")
            present_additions.append(f"    x87 = ${ns}_btn_width{i}")
            present_additions.append(f"    y87 = ${ns}_btn_height{i}")
            present_additions.append(f"    z87 = ${ns}_btn_x{i}")
            present_additions.append(f"    if ${ns}_btn_pressed{i} == 1")
            present_additions.append(f"        w87 = ${ns}_btn_y{i} + 0.002")
            present_additions.append(f"    else if {b['var_name']} != 0")
            present_additions.append(f"        w87 = ${ns}_btn_y{i} + 0.001")
            present_additions.append(f"    else")
            present_additions.append(f"        w87 = ${ns}_btn_y{i}")
            present_additions.append(f"    endif")
            present_additions.append(f"    run = CustomShaderDraw_{ns}")
        present_additions.append("endif")

        shader_def = [
            "hs = null", "ds = null", "gs = null", "cs = null",
            "vs = ./res/draw_2d.hlsl", "ps = ./res/draw_2d.hlsl",
            "blend = ADD SRC_ALPHA INV_SRC_ALPHA", "cull = none",
            "topology = triangle_strip", "o0 = set_viewport bb", "Draw = 4,0", "clear = ps-t100"
        ]

        # 5. 合并写入 ini
        if '[Constants]' not in sections:
            sections['[Constants]'] = []
        for line in constants_additions:
            if line not in sections['[Constants]']:
                sections['[Constants]'].append(line)

        for sec_name, lines in other_sections.items():
            if sec_name not in sections:
                sections[sec_name] = []
            for line in lines:
                if line not in sections[sec_name]:
                    sections[sec_name].append(line)

        custom_shader_section = f"[CustomShaderDraw_{ns}]"
        if custom_shader_section not in sections:
            sections[custom_shader_section] = []
        for line in shader_def:
            if line not in sections[custom_shader_section]:
                sections[custom_shader_section].append(line)

        if '[Present]' not in sections:
            sections['[Present]'] = []
        has_existing_logic = any(self.DUP_GUARD in l for l in sections['[Present]'])
        if not has_existing_logic or _in_place:
            sections['[Present]'].append("")
            sections['[Present]'].append(self.PRESENT_BEGIN.format(ns=ns))
            sections['[Present]'].extend(present_additions)
            sections['[Present]'].append(self.PRESENT_END.format(ns=ns))

        try:
            with open(target_ini_file, 'w', encoding='utf-8') as f:
                f.write("")
            self._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)
            print(f"物体切换面板配置已{'原地更新' if _in_place else '合并到'}: {os.path.basename(target_ini_file)}")
            print(f"共生成 {num_buttons} 个切换按钮")
            return True
        except Exception as e:
            print(f"写入INI文件失败: {e}")
            import traceback
            traceback.print_exc()
            return False


classes = (
    SSMT_SwapPanelEntry,
    SSMT_OT_SwapPanel_ParseObject,
    SSMT_OT_SwapPanel_Scan,
    SSMT_OT_SwapPanel_Refresh,
    SSMTNode_PostProcess_SwapPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
