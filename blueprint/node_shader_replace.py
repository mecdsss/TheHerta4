import bpy
import os
from bpy.types import Node, PropertyGroup
from bpy.props import StringProperty, CollectionProperty, BoolProperty, IntProperty

from .node_base import SSMTNodeBase, SSMTSocketObject


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def parse_shader_hash_from_filename(filepath):
    """从着色器文件名中解析哈希值。

    例如 ``55d2629283cbb3c0-ps_replace.txt`` → ``55d2629283cbb3c0``
    """
    if not filepath:
        return ""
    filename = os.path.basename(filepath)
    name_without_ext = os.path.splitext(filename)[0]
    if '-' in name_without_ext:
        return name_without_ext.split('-')[0]
    return name_without_ext


def _get_text_block_name(node, item):
    """获取着色器在 bpy.data.texts 中对应的文本块名称。"""
    return f"ShaderReplace_{node.name}_{item.variant_name}"


def _load_shader_into_text_block(node):
    """将当前活动着色器文件内容加载到 bpy.data.texts 文本块中。"""
    if node.active_shader_index < 0 or node.active_shader_index >= len(node.shader_list):
        return
    item = node.shader_list[node.active_shader_index]
    if not item.shader_file_path:
        return

    text_name = _get_text_block_name(node, item)
    content = ""
    try:
        with open(item.shader_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        content = ""

    if text_name in bpy.data.texts:
        text_block = bpy.data.texts[text_name]
        text_block.clear()
    else:
        text_block = bpy.data.texts.new(text_name)
    text_block.write(content)


def _clear_text_blocks(node):
    """清除节点对应的所有着色器文本块。"""
    for item in node.shader_list:
        text_name = _get_text_block_name(node, item)
        if text_name in bpy.data.texts:
            bpy.data.texts.remove(bpy.data.texts[text_name])


def _flush_text_block_to_file(node):
    """在关闭预览前强制写回当前文本块内容，避免未落盘编辑丢失。"""
    if node.active_shader_index < 0 or node.active_shader_index >= len(node.shader_list):
        return
    item = node.shader_list[node.active_shader_index]
    if not item.shader_file_path:
        return

    text_name = _get_text_block_name(node, item)
    text_block = bpy.data.texts.get(text_name)
    if text_block is None:
        return

    try:
        text_content = text_block.as_string()
        with open(item.shader_file_path, 'w', encoding='utf-8') as f:
            f.write(text_content)
        new_stat = os.stat(item.shader_file_path)
        _file_signature_cache[item.shader_file_path] = (
            int(new_stat.st_mtime_ns),
            new_stat.st_size,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 定时器：每 2 秒自动保存/刷新着色器文件
# ---------------------------------------------------------------------------

_shader_replace_timer_handle = None
_SHADER_REPLACE_TIMER_INTERVAL = 2.0

# {filepath: (mtime_ns, size)} 缓存，用于判断文件是否被外部修改
_file_signature_cache = {}


def _shader_replace_timer_callback():
    try:
        for tree in bpy.data.node_groups:
            if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
                continue
            for node in tree.nodes:
                if getattr(node, "bl_idname", "") != 'SSMTNode_ShaderReplace':
                    continue
                if not node.preview_enabled:
                    continue
                if node.active_shader_index < 0 or node.active_shader_index >= len(node.shader_list):
                    continue
                item = node.shader_list[node.active_shader_index]
                if not item.shader_file_path or not os.path.exists(item.shader_file_path):
                    continue

                text_name = _get_text_block_name(node, item)
                text_block = bpy.data.texts.get(text_name)
                if text_block is None:
                    _load_shader_into_text_block(node)
                    continue

                text_content = text_block.as_string()

                # 读取文件内容
                try:
                    with open(item.shader_file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                except Exception:
                    file_content = None

                if file_content is not None:
                    if file_content != text_content:
                        # 内容不一致，判断是文件被外部修改还是文本块被用户编辑
                        try:
                            current_stat = os.stat(item.shader_file_path)
                            current_sig = (int(current_stat.st_mtime_ns), current_stat.st_size)
                        except Exception:
                            current_sig = None

                        cached_sig = _file_signature_cache.get(item.shader_file_path)
                        if cached_sig != current_sig:
                            # 文件被外部修改 → 重新加载到文本块
                            text_block.clear()
                            text_block.write(file_content)
                            _file_signature_cache[item.shader_file_path] = current_sig
                        else:
                            # 文本块被用户编辑 → 保存到文件
                            try:
                                with open(item.shader_file_path, 'w', encoding='utf-8') as f:
                                    f.write(text_content)
                                new_stat = os.stat(item.shader_file_path)
                                _file_signature_cache[item.shader_file_path] = (
                                    int(new_stat.st_mtime_ns), new_stat.st_size
                                )
                            except Exception:
                                pass
                    else:
                        # 内容一致，仅更新缓存
                        try:
                            current_stat = os.stat(item.shader_file_path)
                            _file_signature_cache[item.shader_file_path] = (
                                int(current_stat.st_mtime_ns), current_stat.st_size
                            )
                        except Exception:
                            pass
    except Exception:
        pass

    return _SHADER_REPLACE_TIMER_INTERVAL


# ---------------------------------------------------------------------------
# PropertyGroup
# ---------------------------------------------------------------------------

class ShaderReplaceItem(PropertyGroup):
    variant_name: StringProperty(
        name="变体名称",
        description="着色器变体名称，如 World、NonWorld",
        default=""
    )
    shader_file_path: StringProperty(
        name="着色器文件",
        description="着色器 .txt 文件路径",
        default=""
    )
    shader_hash: StringProperty(
        name="着色器哈希",
        description="着色器哈希值，可从文件名自动解析",
        default=""
    )


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class SSMT_OT_ShaderReplace_AddItem(bpy.types.Operator):
    bl_idname = "ssmt.shader_replace_add_item"
    bl_label = "添加着色器"
    bl_description = "添加一个新的着色器变体"

    node_name: StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node:
            new_item = node.shader_list.add()
            variant_count = len(node.shader_list)
            if variant_count == 1:
                new_item.variant_name = "World"
            elif variant_count == 2:
                new_item.variant_name = "NonWorld"
            else:
                new_item.variant_name = f"Variant{variant_count}"
        return {'FINISHED'}


class SSMT_OT_ShaderReplace_RemoveItem(bpy.types.Operator):
    bl_idname = "ssmt.shader_replace_remove_item"
    bl_label = "删除着色器"
    bl_description = "删除选中的着色器变体"

    node_name: StringProperty()
    item_index: IntProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node and 0 <= self.item_index < len(node.shader_list):
            node.shader_list.remove(self.item_index)
            if node.active_shader_index >= len(node.shader_list):
                node.active_shader_index = max(0, len(node.shader_list) - 1)
        return {'FINISHED'}


class SSMT_OT_ShaderReplace_SelectFile(bpy.types.Operator):
    bl_idname = "ssmt.shader_replace_select_file"
    bl_label = "选择着色器文件"
    bl_description = "选择一个 .txt 着色器文件"

    node_name: StringProperty()
    item_index: IntProperty()
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.txt", options={'HIDDEN'})

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node or self.item_index >= len(node.shader_list):
            return {'CANCELLED'}
        item = node.shader_list[self.item_index]
        item.shader_file_path = bpy.path.abspath(self.filepath)
        parsed_hash = parse_shader_hash_from_filename(item.shader_file_path)
        if parsed_hash:
            item.shader_hash = parsed_hash
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class SSMT_OT_ShaderReplace_ParseHash(bpy.types.Operator):
    bl_idname = "ssmt.shader_replace_parse_hash"
    bl_label = "解析哈希"
    bl_description = "从着色器文件名自动解析哈希值"

    node_name: StringProperty()
    item_index: IntProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node or self.item_index >= len(node.shader_list):
            return {'CANCELLED'}
        item = node.shader_list[self.item_index]
        parsed_hash = parse_shader_hash_from_filename(item.shader_file_path)
        if parsed_hash:
            item.shader_hash = parsed_hash
            self.report({'INFO'}, f"已解析哈希: {parsed_hash}")
        else:
            self.report({'WARNING'}, "无法从文件名解析哈希")
        return {'FINISHED'}


class SSMT_OT_ShaderReplace_TogglePreview(bpy.types.Operator):
    bl_idname = "ssmt.shader_replace_toggle_preview"
    bl_label = "切换预览"
    bl_description = "开启/关闭着色器预览编辑模式"

    node_name: StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node:
            return {'CANCELLED'}
        node.preview_enabled = not node.preview_enabled
        if node.preview_enabled:
            _load_shader_into_text_block(node)
        else:
            _flush_text_block_to_file(node)
            _clear_text_blocks(node)
        return {'FINISHED'}


class SSMT_OT_ShaderReplace_OpenTextEditor(bpy.types.Operator):
    bl_idname = "ssmt.shader_replace_open_text_editor"
    bl_label = "在文本编辑器中打开"
    bl_description = "在 Blender 文本编辑器中打开着色器文件进行编辑"

    node_name: StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node or not node.preview_enabled:
            return {'CANCELLED'}
        if node.active_shader_index < 0 or node.active_shader_index >= len(node.shader_list):
            return {'CANCELLED'}
        item = node.shader_list[node.active_shader_index]
        text_name = _get_text_block_name(node, item)
        if text_name not in bpy.data.texts:
            _load_shader_into_text_block(node)

        text_block = bpy.data.texts.get(text_name)
        if not text_block:
            self.report({'ERROR'}, "无法创建文本块")
            return {'CANCELLED'}

        # 尝试在已有的文本编辑器中打开
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'TEXT_EDITOR':
                    for space in area.spaces:
                        if space.type == 'TEXT_EDITOR':
                            space.text = text_block
                            self.report({'INFO'}, f"已在文本编辑器中打开 '{text_name}'")
                            return {'FINISHED'}

        self.report({'INFO'}, f"文本块 '{text_name}' 已创建，请打开文本编辑器查看")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

class SSMTNode_ShaderReplace(SSMTNodeBase):
    bl_idname = 'SSMTNode_ShaderReplace'
    bl_label = 'Shader Replace'
    bl_icon = 'SHADERFX'
    bl_width_min = 350

    name_prefix: StringProperty(
        name="名称前缀",
        description="用于生成 INI 变量和段名的名称前缀，如 Rain",
        default="Rain"
    )
    shader_list: CollectionProperty(type=ShaderReplaceItem)
    toggle_key: StringProperty(
        name="快捷键",
        description="KeyToggle 的快捷键，如 VK_F5。留空则不分配快捷键",
        default=""
    )
    preview_enabled: BoolProperty(
        name="预览模式",
        description="开启后可在文本编辑器中编辑着色器文件",
        default=False
    )
    active_shader_index: IntProperty(
        name="活动着色器",
        description="当前预览的着色器索引",
        default=0
    )
    component_index: IntProperty(
        name="组件索引",
        description="CustomShader 段名中的组件索引",
        default=0
    )

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Input 1")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 350
        # 默认添加 World 和 NonWorld 两个条目
        world_item = self.shader_list.add()
        world_item.variant_name = "World"
        nonworld_item = self.shader_list.add()
        nonworld_item.variant_name = "NonWorld"

    def draw_buttons(self, context, layout):
        # 基本配置
        box = layout.box()
        box.label(text="着色器替换配置", icon='SHADERFX')
        box.prop(self, "name_prefix", text="前缀")
        box.prop(self, "toggle_key", text="快捷键")
        box.prop(self, "component_index", text="组件")

        # 着色器列表
        box = layout.box()
        box.label(text="着色器列表", icon='MATERIAL')

        for i, item in enumerate(self.shader_list):
            sub = box.box()
            row = sub.row(align=True)
            row.prop(item, "variant_name", text="变体")

            row2 = sub.row(align=True)
            row2.prop(item, "shader_file_path", text="文件")
            op = row2.operator("ssmt.shader_replace_select_file", text="", icon='FILEBROWSER')
            op.node_name = self.name
            op.item_index = i

            row3 = sub.row(align=True)
            row3.prop(item, "shader_hash", text="哈希")
            op = row3.operator("ssmt.shader_replace_parse_hash", text="", icon='FILE_REFRESH')
            op.node_name = self.name
            op.item_index = i

            op = sub.operator("ssmt.shader_replace_remove_item", text="删除", icon='X')
            op.node_name = self.name
            op.item_index = i

        op = box.operator("ssmt.shader_replace_add_item", text="添加着色器", icon='ADD')
        op.node_name = self.name

        # 预览/编辑
        box = layout.box()
        row = box.row()
        row.prop(self, "preview_enabled", text="预览/编辑模式")
        op = row.operator("ssmt.shader_replace_toggle_preview", text="切换", icon='RESTRICT_VIEW_OFF')
        op.node_name = self.name

        if self.preview_enabled:
            box.prop(self, "active_shader_index", text="活动着色器")
            if 0 <= self.active_shader_index < len(self.shader_list):
                item = self.shader_list[self.active_shader_index]
                if item.shader_file_path:
                    op = box.operator("ssmt.shader_replace_open_text_editor", text="在文本编辑器中打开", icon='TEXT')
                    op.node_name = self.name
                    box.label(text=f"文件: {os.path.basename(item.shader_file_path)}", icon='FILE_TEXT')
                    text_name = _get_text_block_name(self, item)
                    if text_name in bpy.data.texts:
                        box.label(text="文本块已就绪，每2秒自动保存/刷新", icon='CHECKMARK')
                    else:
                        box.label(text="文本块未创建", icon='ERROR')
                else:
                    box.label(text="请先选择着色器文件", icon='ERROR')

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new('SSMTSocketObject', f"Input {len(self.inputs) + 1}")
        if len(self.inputs) > 1 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
            self.inputs.remove(self.inputs[-1])

    def get_shader_replace_info(self):
        """返回着色器替换配置，供导出使用。"""
        shaders = []
        for i, item in enumerate(self.shader_list):
            shaders.append({
                'variant_name': item.variant_name,
                'shader_file_path': item.shader_file_path,
                'shader_hash': item.shader_hash,
                'env_value': i + 1,
            })
        return {
            'name_prefix': self.name_prefix,
            'toggle_key': self.toggle_key,
            'component_index': self.component_index,
            'shaders': shaders,
        }


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

classes = (
    ShaderReplaceItem,
    SSMT_OT_ShaderReplace_AddItem,
    SSMT_OT_ShaderReplace_RemoveItem,
    SSMT_OT_ShaderReplace_SelectFile,
    SSMT_OT_ShaderReplace_ParseHash,
    SSMT_OT_ShaderReplace_TogglePreview,
    SSMT_OT_ShaderReplace_OpenTextEditor,
    SSMTNode_ShaderReplace,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    global _shader_replace_timer_handle
    if _shader_replace_timer_handle is None:
        try:
            bpy.app.timers.register(
                _shader_replace_timer_callback,
                first_interval=_SHADER_REPLACE_TIMER_INTERVAL,
                persistent=True,
            )
            _shader_replace_timer_handle = True
        except Exception:
            _shader_replace_timer_handle = None


def unregister():
    global _shader_replace_timer_handle
    if _shader_replace_timer_handle is not None:
        try:
            bpy.app.timers.unregister(_shader_replace_timer_callback)
        except Exception:
            pass
        _shader_replace_timer_handle = None

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
