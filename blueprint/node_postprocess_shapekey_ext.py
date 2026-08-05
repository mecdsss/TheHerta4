import bpy
import os
import glob
import re
import math
import shutil
import uuid
from collections import OrderedDict

from .node_postprocess_base import SSMTNode_PostProcess_Base

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class ShapeKeyPlayGroupItem(bpy.types.PropertyGroup):
    shape_key_name: bpy.props.StringProperty(name="形态键名称")
    object_name: bpy.props.StringProperty(name="物体名称")
    group_index: bpy.props.IntProperty(name="分组编号", default=0, min=0)


class ShapeKeySpeedIntervalItem(bpy.types.PropertyGroup):
    start: bpy.props.FloatProperty(default=0.0, min=0.0, max=100.0, subtype='PERCENTAGE')
    end: bpy.props.FloatProperty(default=50.0, min=0.0, max=100.0, subtype='PERCENTAGE')
    base_step: bpy.props.IntProperty(default=10, min=1, max=10000)


class ShapeKeyPlayGroupSettings(bpy.types.PropertyGroup):
    group_index: bpy.props.IntProperty(name="分组编号", default=1, min=1)
    group_mode: bpy.props.EnumProperty(
        name="分组模式",
        items=[
            ('SYNC', "同步", "组内所有形态键同时受滑块控制（默认）"),
            ('SEQUENCE', "序列", "组内形态键依次受滑块控制，形成连续动画路径"),
        ], default='SYNC'
    )
    enable_auto_playback: bpy.props.BoolProperty(name="启用自动播放", default=False)
    auto_playback_frame_count: bpy.props.IntProperty(name="细分份数", default=30, min=2, max=5000)
    auto_playback_step_frames: bpy.props.IntProperty(name="每帧初始步进次数", default=5, min=1, max=5000)
    auto_playback_cycle_mode: bpy.props.EnumProperty(
        name="循环模式",
        items=[('FORWARD', "正向", "从0到1循环"), ('REVERSE', "反向", "从1到0循环"), ('PINGPONG', "往返", "0→1→0→1往返循环")],
        default='FORWARD'
    )
    speed_percent_min: bpy.props.IntProperty(name="速度百分比最小值", default=10, min=1, max=10000)
    speed_percent_max: bpy.props.IntProperty(name="速度百分比最大值", default=1000, min=1, max=10000)
    speed_percent: bpy.props.IntProperty(name="当前速度百分比", default=100, min=1, max=10000)
    speed_intervals: bpy.props.CollectionProperty(type=ShapeKeySpeedIntervalItem)
    active_interval_index: bpy.props.IntProperty(default=0)
    max_step_unroll: bpy.props.IntProperty(name="最大步进展开次数", default=100, min=1, max=500)
    remark: bpy.props.StringProperty(name="备注")
    button_icon_image: bpy.props.StringProperty(name="按钮图标", subtype='FILE_PATH', default="")
    use_remark_as_icon: bpy.props.BoolProperty(name="用备注生成图标", default=True)

    expanded: bpy.props.BoolProperty(name="展开", default=True)

    remark_font_family: bpy.props.EnumProperty(
        name="字体",
        items=[
            ('msyh.ttc', "微软雅黑", "Windows 标准中文字体"),
            ('simsun.ttc', "宋体", "Windows 经典衬线字体"),
            ('simhei.ttf', "黑体", "Windows 经典无衬线字体"),
            ('arial.ttf', "Arial", "标准英文字体"),
        ],
        default='msyh.ttc'
    )
    remark_font_size: bpy.props.IntProperty(name="字号大小", default=36, min=10, max=300)
    remark_text_color: bpy.props.FloatVectorProperty(
        name="文字颜色",
        subtype='COLOR',
        size=3,
        default=(1.0, 1.0, 1.0),
        min=0.0, max=1.0
    )
    remark_stroke_width: bpy.props.IntProperty(name="描边宽度", default=4, min=0, max=20)
    remark_stroke_color: bpy.props.FloatVectorProperty(
        name="描边颜色",
        subtype='COLOR',
        size=3,
        default=(0.0, 0.0, 0.0),
        min=0.0, max=1.0
    )


class SSMT_OT_SpeedIntervalAdd(bpy.types.Operator):
    bl_idname = "ssmt.speed_interval_add"
    bl_label = "添加区间"
    bl_options = {'REGISTER', 'INTERNAL'}
    group_index: bpy.props.IntProperty(default=1)
    node_tree_name: bpy.props.StringProperty()
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree_name)
        if not tree: return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node: return {'CANCELLED'}
        settings = node._find_group_setting(self.group_index)
        if settings:
            interval = settings.speed_intervals.add()
            last = settings.speed_intervals[-2] if len(settings.speed_intervals) > 1 else None
            if last:
                interval.start = last.end
                interval.end = min(100.0, last.end + 50.0)
            else:
                interval.start = 0.0
                interval.end = 50.0
            interval.base_step = 10
        return {'FINISHED'}


class SSMT_OT_SpeedIntervalRemove(bpy.types.Operator):
    bl_idname = "ssmt.speed_interval_remove"
    bl_label = "删除区间"
    bl_options = {'REGISTER', 'INTERNAL'}
    group_index: bpy.props.IntProperty(default=1)
    interval_index: bpy.props.IntProperty(default=0)
    node_tree_name: bpy.props.StringProperty()
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree_name)
        if not tree: return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node: return {'CANCELLED'}
        settings = node._find_group_setting(self.group_index)
        if settings and self.interval_index < len(settings.speed_intervals):
            settings.speed_intervals.remove(self.interval_index)
        return {'FINISHED'}


class SSMT_OT_ShapeKeyExtSetGroup(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_ext_set_group"
    bl_label = "设置播放分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    shape_key_name: bpy.props.StringProperty()
    group_index: bpy.props.IntProperty(name="分组编号", min=0, default=1)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "group_index", text="播放分组")

    def execute(self, context):
        node = self._get_node(context)
        if not node: return {'CANCELLED'}
        found = False
        for entry in node.play_group_entries:
            if entry.shape_key_name == self.shape_key_name:
                entry.group_index = self.group_index
                found = True
                break
        if not found:
            entry = node.play_group_entries.add()
            entry.shape_key_name = self.shape_key_name
            entry.group_index = self.group_index
        self.report({'INFO'}, f"已设置 {self.shape_key_name} -> 播放分组 {self.group_index}")
        return {'FINISHED'}

    def _get_node(self, context):
        space = getattr(context, "space_data", None)
        if space and space.type == 'NODE_EDITOR':
            tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
            if tree:
                return tree.nodes.get(self.node_name)
        return None


class SSMT_OT_ScanShapeKeyExt(bpy.types.Operator):
    bl_idname = "ssmt.scan_shapekey_ext"
    bl_label = "扫描形态键"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        names = node._scan_shapekey_names_from_variable_items()
        if not names:
            names = node._scan_shapekey_names_from_classification()
        if not names:
            self.report({'WARNING'}, "未扫描到任何形态键")
            return {'CANCELLED'}
        current_names = set(names)
        stale_indices = [i for i, entry in enumerate(node.play_group_entries) if entry.shape_key_name not in current_names]
        for i in reversed(stale_indices): node.play_group_entries.remove(i)
        count = 0
        for name in names:
            if not any(entry.shape_key_name == name for entry in node.play_group_entries):
                entry = node.play_group_entries.add()
                entry.shape_key_name = name
                entry.group_index = 0
                count += 1
        self.report({'INFO'}, f"扫描到 {len(names)} 个形态键，清除 {len(stale_indices)} 个失效条目，新增 {count} 个")
        return {'FINISHED'}


class SSMT_OT_SelectPlayGroup(bpy.types.Operator):
    bl_idname = "ssmt.select_play_group"
    bl_label = "切换到此分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    target_group_index: bpy.props.IntProperty(default=1)

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        node.active_group_index = self.target_group_index
        node._ensure_group_setting(self.target_group_index)
        return {'FINISHED'}


class SSMT_OT_ShapeKeyGroupAdd(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_group_add"
    bl_label = "创建新分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        max_idx = 0
        for setting in node.play_group_settings:
            if setting.group_index > max_idx:
                max_idx = setting.group_index
        new_group = node.play_group_settings.add()
        new_group.group_index = max_idx + 1
        node.active_group_index = new_group.group_index
        self.report({'INFO'}, f"已创建并切换至分组 {new_group.group_index}")
        return {'FINISHED'}


class SSMT_OT_ShapeKeyGroupRemove(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_group_remove"
    bl_label = "删除分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    target_group_index: bpy.props.IntProperty(default=1)

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        if len(node.play_group_settings) <= 1:
            self.report({'WARNING'}, "至少保留一个分组，不能删除最后一个分组")
            return {'CANCELLED'}

        for entry in node.play_group_entries:
            if entry.group_index == self.target_group_index:
                entry.group_index = 0

        idx_to_remove = -1
        for i, setting in enumerate(node.play_group_settings):
            if setting.group_index == self.target_group_index:
                idx_to_remove = i
                break
        if idx_to_remove != -1:
            node.play_group_settings.remove(idx_to_remove)

        if node.active_group_index == self.target_group_index:
            if len(node.play_group_settings) > 0:
                node.active_group_index = node.play_group_settings[0].group_index

        self.report({'INFO'}, f"已删除分组 {self.target_group_index}，相关形态键已移至未分组")
        return {'FINISHED'}


class SSMT_OT_ShapeKeyGroupMoveUpDown(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_group_move"
    bl_label = "移动分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    direction: bpy.props.EnumProperty(items=[('UP', "上移", ""), ('DOWN', "下移", "")])
    target_group_index: bpy.props.IntProperty(default=1)

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        settings = node.play_group_settings
        idx = -1
        for i, setting in enumerate(settings):
            if setting.group_index == self.target_group_index:
                idx = i
                break
        if idx == -1: return {'CANCELLED'}
        new_idx = idx + (1 if self.direction == 'DOWN' else -1)
        if new_idx < 0 or new_idx >= len(settings): return {'CANCELLED'}

        # 交换列表顺序（settings.move 只重排列表位置，不会改变各分组头的 group_index 值）
        settings.move(idx, new_idx)

        # 重新编号：直接以每个分组头自身移动前的旧编号作为身份标识建立映射。
        # 注意：不能使用 as_pointer() —— CollectionProperty.move() 之后元素指针
        # 不保证稳定，会导致映射错误、形态键不跟随分组移动。
        index_map = {}
        for i, setting in enumerate(settings):
            old_index = setting.group_index
            setting.group_index = i + 1
            index_map[old_index] = i + 1

        # 同步移动所有形态键条目，使其跟随各自的分组头一起移动
        for entry in node.play_group_entries:
            if entry.group_index in index_map:
                entry.group_index = index_map[entry.group_index]

        # 当前编辑分组若随移动而改变编号，同步更新
        if node.active_group_index in index_map:
            node.active_group_index = index_map[node.active_group_index]

        return {'FINISHED'}


class SSMT_OT_OpenGroupSettings(bpy.types.Operator):
    bl_idname = "ssmt.open_group_settings"
    bl_label = "分组详细设置"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    node_name: bpy.props.StringProperty()
    node_tree_name: bpy.props.StringProperty()
    target_group_index: bpy.props.IntProperty(default=1)
    
    prop_group_mode: bpy.props.EnumProperty(name="组内控制模式", items=[('SYNC', "同步", ""), ('SEQUENCE', "序列", "")], default='SYNC')
    prop_enable_auto_playback: bpy.props.BoolProperty(name="启用自动播放", default=False)
    prop_auto_playback_frame_count: bpy.props.IntProperty(name="细分份数", default=30, min=2, max=5000)
    prop_auto_playback_cycle_mode: bpy.props.EnumProperty(name="循环模式", items=[('FORWARD', "正向", ""), ('REVERSE', "反向", ""), ('PINGPONG', "往返", "")], default='FORWARD')
    prop_speed_percent_min: bpy.props.IntProperty(name="速度百分比最小值", default=10, min=1, max=10000)
    prop_speed_percent_max: bpy.props.IntProperty(name="速度百分比最大值", default=1000, min=1, max=10000)
    prop_speed_percent: bpy.props.IntProperty(name="当前速度百分比", default=100, min=1, max=10000)
    prop_max_step_unroll: bpy.props.IntProperty(name="最大步进展开次数", default=100, min=1, max=500)
    prop_remark: bpy.props.StringProperty(name="分组备注")
    prop_use_remark_as_icon: bpy.props.BoolProperty(name="用备注生成图标", default=True)
    prop_remark_font_family: bpy.props.EnumProperty(name="字体", items=[('msyh.ttc', "微软雅黑", ""), ('simsun.ttc', "宋体", ""), ('simhei.ttf', "黑体", ""), ('arial.ttf', "Arial", "")], default='msyh.ttc')
    prop_remark_font_size: bpy.props.IntProperty(name="字号大小", default=36, min=10, max=300)
    prop_remark_text_color: bpy.props.FloatVectorProperty(name="文字颜色", subtype='COLOR', size=3, default=(1.0, 1.0, 1.0), min=0.0, max=1.0, soft_min=0.0, soft_max=1.0)
    prop_remark_stroke_width: bpy.props.IntProperty(name="描边宽度", default=4, min=0, max=20)
    prop_remark_stroke_color: bpy.props.FloatVectorProperty(name="描边颜色", subtype='COLOR', size=3, default=(0.0, 0.0, 0.0), min=0.0, max=1.0, soft_min=0.0, soft_max=1.0)
    prop_button_icon_image: bpy.props.StringProperty(name="按钮图标", subtype='FILE_PATH', default="")

    def invoke(self, context, event):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
            
        self.node_tree_name = tree.name
        self.node_name = node.name
            
        setting = node._find_group_setting(self.target_group_index)
        if not setting:
            return {'CANCELLED'}
            
        self.prop_group_mode = setting.group_mode
        self.prop_enable_auto_playback = setting.enable_auto_playback
        self.prop_auto_playback_frame_count = setting.auto_playback_frame_count
        self.prop_auto_playback_cycle_mode = setting.auto_playback_cycle_mode
        self.prop_speed_percent_min = setting.speed_percent_min
        self.prop_speed_percent_max = setting.speed_percent_max
        self.prop_speed_percent = setting.speed_percent
        self.prop_max_step_unroll = setting.max_step_unroll
        self.prop_remark = setting.remark
        self.prop_use_remark_as_icon = setting.use_remark_as_icon
        self.prop_remark_font_family = setting.remark_font_family
        self.prop_remark_font_size = setting.remark_font_size
        self.prop_remark_text_color = setting.remark_text_color
        self.prop_remark_stroke_width = setting.remark_stroke_width
        self.prop_remark_stroke_color = setting.remark_stroke_color
        self.prop_button_icon_image = setting.button_icon_image

        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text=f"分组 {self.target_group_index} - 详细设置", icon='GROUP')
        
        box.prop(self, "prop_group_mode")
        box.prop(self, "prop_enable_auto_playback")
        
        if self.prop_enable_auto_playback:
            box.prop(self, "prop_auto_playback_frame_count")
            box.prop(self, "prop_auto_playback_cycle_mode")
            col = box.column(align=True)
            col.prop(self, "prop_speed_percent_min")
            col.prop(self, "prop_speed_percent_max")
            col.prop(self, "prop_speed_percent")
            
            box.label(text="变速区间 (0-100%)", icon='IPO_EASE_IN_OUT')
            interval_box = box.box()
            
            tree = bpy.data.node_groups.get(self.node_tree_name)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_ShapeKeyExt':
                    setting = node._find_group_setting(self.target_group_index)
                    if setting:
                        for idx, interval in enumerate(setting.speed_intervals):
                            row_int = interval_box.row(align=True)
                            row_int.prop(interval, "start", text="起点")
                            row_int.prop(interval, "end", text="终点")
                            row_int.prop(interval, "base_step", text="步进")
                            
                            op = row_int.operator("ssmt.speed_interval_remove", text="", icon='X')
                            op.group_index = self.target_group_index
                            op.interval_index = idx
                            op.node_name = self.node_name
                            op.node_tree_name = self.node_tree_name

                        row_add = interval_box.row(align=True)
                        op = row_add.operator("ssmt.speed_interval_add", text="添加区间", icon='ADD')
                        op.group_index = self.target_group_index
                        op.node_name = self.node_name
                        op.node_tree_name = self.node_tree_name

            box.prop(self, "prop_max_step_unroll")
        
        box.prop(self, "prop_remark")
        box.label(text="提示：输入 / 符号可强制换行", icon='INFO')

        if PIL_AVAILABLE:
            box.prop(self, "prop_use_remark_as_icon")
            if self.prop_use_remark_as_icon:
                box_style = box.box()
                box_style.label(text="文字图标样式", icon='COLOR')
                box_style.prop(self, "prop_remark_font_family")
                box_style.prop(self, "prop_remark_font_size")
                row_c = box_style.row(align=True)
                row_c.prop(self, "prop_remark_text_color")
                row_c.prop(self, "prop_remark_stroke_color")
                box_style.prop(self, "prop_remark_stroke_width")
                box.label(text="注：导出时将自动生成文字图标替代图片选择", icon='INFO')
        else:
            box.prop(self, "prop_button_icon_image")

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
            
        setting = node._find_group_setting(self.target_group_index)
        if not setting:
            return {'CANCELLED'}
            
        setting.group_mode = self.prop_group_mode
        setting.enable_auto_playback = self.prop_enable_auto_playback
        setting.auto_playback_frame_count = self.prop_auto_playback_frame_count
        setting.auto_playback_cycle_mode = self.prop_auto_playback_cycle_mode
        setting.speed_percent_min = self.prop_speed_percent_min
        setting.speed_percent_max = self.prop_speed_percent_max
        setting.speed_percent = self.prop_speed_percent
        setting.max_step_unroll = self.prop_max_step_unroll
        setting.remark = self.prop_remark
        setting.use_remark_as_icon = self.prop_use_remark_as_icon
        setting.remark_font_family = self.prop_remark_font_family
        setting.remark_font_size = self.prop_remark_font_size
        setting.remark_text_color = self.prop_remark_text_color
        setting.remark_stroke_width = self.prop_remark_stroke_width
        setting.remark_stroke_color = self.prop_remark_stroke_color
        setting.button_icon_image = self.prop_button_icon_image
            
        return {'FINISHED'}


class SSMT_OT_ShapeKeyMoveUpDown(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_move"
    bl_label = "移动形态键"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    direction: bpy.props.EnumProperty(items=[('UP', "上移", ""), ('DOWN', "下移", "")])
    shape_key_name: bpy.props.StringProperty()

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        entries = node.play_group_entries
        idx = -1
        for i, e in enumerate(entries):
            if e.shape_key_name == self.shape_key_name:
                idx = i
                break
        if idx == -1: return {'CANCELLED'}
        if self.direction == 'UP' and idx > 0:
            entries.move(idx, idx - 1)
        elif self.direction == 'DOWN' and idx < len(entries) - 1:
            entries.move(idx, idx + 1)
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAssignToCurrentGroup(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_assign_current_group"
    bl_label = "移入当前分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    shape_key_name: bpy.props.StringProperty()

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        group = node.active_group_index
        for entry in node.play_group_entries:
            if entry.shape_key_name == self.shape_key_name:
                entry.group_index = group
                break
        return {'FINISHED'}


class SSMT_OT_ShapeKeyExt_ParseSliderObject(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_ext_parse_slider_object"
    bl_label = "解析物体"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_ShapeKeyExt':
                    node._update_from_object()
                    self.report({'INFO'}, f"已解析物体 '{node.target_object}' -> 哈希: {node.detect_hash}, IndexCount: {node.detect_index_count}")
                    return {'FINISHED'}
        self.report({'WARNING'}, "无法找到形态键扩展配置节点")
        return {'CANCELLED'}


class SSMT_OT_ShapeKeyExt_Refresh(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_ext_refresh"
    bl_label = "应用设置到Mod"
    bl_description = "按节点当前设置，原地更新 mod ini 中形态键扩展（含滑块面板）的配置，无需重新导出整个 mod"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_ShapeKeyExt':
                    ini_path = (node.ini_file_path or "").strip() or node.last_mod_ini_path
                    if not ini_path or not os.path.isfile(ini_path):
                        self.report({'WARNING'}, "未找到目标 ini：请先导出一次 mod，或在节点「INI文件」中指定")
                        return {'CANCELLED'}
                    ok = node.execute_postprocess(os.path.dirname(ini_path), _in_place=True, _ini_path=ini_path)
                    if ok:
                        self.report({'INFO'}, f"已原地刷新形态键扩展配置: {os.path.basename(ini_path)}")
                        return {'FINISHED'}
                    self.report({'ERROR'}, "刷新失败，请查看控制台日志（旧格式可能需要先重新导出一次）")
                    return {'CANCELLED'}
        self.report({'WARNING'}, "无法找到形态键扩展配置节点")
        return {'CANCELLED'}


class SSMTNode_PostProcess_ShapeKeyExt(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_ShapeKeyExt'
    bl_label = '形态键扩展配置'
    bl_description = '为形态键配置节点添加自动播放和分组功能（前置需连接形态键配置节点）'

    # ---- 配置行专有标识（生成时留在 ini 中，供「应用设置到Mod」精准定位与原地更新）----
    PANEL_TAG = "ShapeKeyExt"
    BLOCK_BEGIN = "; @@ShapeKeyExt:BLOCK:BEGIN:{ns}@@"
    BLOCK_END = "; @@ShapeKeyExt:BLOCK:END:{ns}@@"
    SLIDERCONST_BEGIN = "; @@ShapeKeyExt:SLIDERCONST:BEGIN@@"
    SLIDERCONST_END = "; @@ShapeKeyExt:SLIDERCONST:END@@"
    SLIDER_PRESENT_BEGIN = "; ========== SLIDER PANEL CUSTOM LOGIC (appended) =========="
    SLIDER_PRESENT_END = "; ========== SLIDER PANEL CUSTOM LOGIC END (appended) =========="
    DUP_GUARD = "; @@ShapeKeyExt:BLOCK:BEGIN:"

    play_group_settings: bpy.props.CollectionProperty(type=ShapeKeyPlayGroupSettings)
    active_group_index: bpy.props.IntProperty(
        name="编辑分组", default=1, min=1,
        update=lambda self, ctx: self._on_active_group_changed(self.active_group_index)
    )
    play_group_entries: bpy.props.CollectionProperty(type=ShapeKeyPlayGroupItem)
    auto_play_toggle_key: bpy.props.StringProperty(name="自动播放快捷键", default="space")
    auto_play_key_global: bpy.props.BoolProperty(name="全局生效", default=False)

    show_unassigned: bpy.props.BoolProperty(name="显示未分组", default=True)

    # ==========================================
    # 【已合并】滑块面板-自定义 设置
    # ==========================================
    use_slider_panel: bpy.props.BoolProperty(name="启用滑块面板", default=True)
    create_cumulative_backup: bpy.props.BoolProperty(name="创建累积备份", default=True)
    help_key: bpy.props.StringProperty(name="显示/隐藏面板", default="home")
    reset_key: bpy.props.StringProperty(name="重置位置", default="ctrl home")
    zoom_in_key: bpy.props.StringProperty(name="放大", default="up")
    zoom_out_key: bpy.props.StringProperty(name="缩小", default="down")
    drag_key: bpy.props.StringProperty(name="拖拽键", default="VK_LBUTTON")

    slider_height: bpy.props.FloatProperty(name="滑块高度", default=0.042, min=0.001, max=1.0, precision=4)
    button_height: bpy.props.FloatProperty(name="按钮高度", default=0.045, min=0.001, max=1.0, precision=4)
    panel_min_height: bpy.props.FloatProperty(name="面板最小高度", default=0.75, min=0.01, max=1.0, precision=4)

    def _update_target_object(self, context):
        self._update_from_object()

    target_object: bpy.props.StringProperty(name="目标物体", default="", update=_update_target_object)
    detect_hash: bpy.props.StringProperty(name="哈希值", default="")
    detect_index_count: bpy.props.StringProperty(name="IndexCount", default="")

    background_image: bpy.props.StringProperty(name="背景图片", subtype='FILE_PATH', default="")
    slider_handle_image: bpy.props.StringProperty(name="滑块图片", subtype='FILE_PATH', default="")
    left_bar_image: bpy.props.StringProperty(name="左进度条图片", subtype='FILE_PATH', default="")
    right_bar_image: bpy.props.StringProperty(name="右进度条图片", subtype='FILE_PATH', default="")
    button_image: bpy.props.StringProperty(name="按钮图片", subtype='FILE_PATH', default="")

    # ---- 文字按钮样式（播放/暂停按钮，用分组备注生成；背景/边框紧贴文字，内边距极小不发糊）----
    button_bg_color: bpy.props.FloatVectorProperty(name="按钮背景色", subtype='COLOR', default=(0.16, 0.22, 0.32), min=0.0, max=1.0, size=3)
    button_border_color: bpy.props.FloatVectorProperty(name="按钮边框色", subtype='COLOR', default=(0.59, 0.75, 0.94), min=0.0, max=1.0, size=3)
    button_border_width: bpy.props.IntProperty(name="按钮边框宽度", default=2, min=0, max=20)
    button_opacity: bpy.props.FloatProperty(name="按钮透明度", default=0.9, min=0.0, max=1.0, precision=2)
    # ---- 面板背景样式（圆角 + 边框，未自定义背景图时生效）----
    background_corner_radius: bpy.props.IntProperty(name="背景圆角", default=24, min=0, max=100)
    background_border_color: bpy.props.FloatVectorProperty(name="背景边框色", subtype='COLOR', default=(0.59, 0.75, 0.94), min=0.0, max=1.0, size=3)
    background_border_width: bpy.props.IntProperty(name="背景边框宽度", default=3, min=0, max=20)
    background_opacity: bpy.props.FloatProperty(name="背景透明度", default=0.85, min=0.0, max=1.0, precision=2)

    panel_default_scale: bpy.props.FloatProperty(
        name="面板默认缩放",
        description="面板默认显示的缩放比例（1.0 = 原始大小；可在游戏内用放大/缩小键再调节）",
        default=1.0, min=0.1, max=5.0, precision=2
    )
    check_hash: bpy.props.StringProperty(name="检测Hash值", default="")
    match_index_count: bpy.props.IntProperty(name="Match Index Count", default=0, min=0)

    # ---- 原地刷新 - 实例标识与上次导出路径 ----
    namespace: bpy.props.StringProperty(name="命名空间", default="", options={'HIDDEN'})
    last_mod_ini_path: bpy.props.StringProperty(name="上次导出INI", default="", options={'HIDDEN'})
    ini_file_path: bpy.props.StringProperty(name="INI文件", subtype='FILE_PATH', default="")

    def _ensure_namespace(self):
        """生成/返回节点实例唯一命名空间（仅用于标识注释，不改动 mod 业务变量）。"""
        if not self.namespace:
            self.namespace = "skx_" + uuid.uuid4().hex[:8]
        return self.namespace

    def copy(self, node):
        # 复制节点时重置命名空间，避免共用同一套标识注释
        self.namespace = ""

    def init(self, context):
        super().init(context)
        self.width = 700
        if len(self.play_group_settings) == 0:
            s = self.play_group_settings.add()
            s.group_index = 1

    def _find_group_setting(self, group_index):
        for s in self.play_group_settings:
            if s.group_index == group_index: return s
        return None

    def _on_active_group_changed(self, group_index):
        """active_group_index 变化时的回调。Blender 要求 property update 回调返回 None。"""
        self._ensure_group_setting(group_index)

    def _ensure_group_setting(self, group_index):
        s = self._find_group_setting(group_index)
        if s is None:
            s = self.play_group_settings.add()
            s.group_index = group_index
            if len(s.speed_intervals) == 0:
                interval = s.speed_intervals.add()
                interval.start = 0.0
                interval.end = 100.0
                interval.base_step = 10
        return s

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
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped
                    if current_section not in sections: sections[current_section] = []
                    continue
                if not stripped: continue
                if current_section: sections[current_section].append(line)
        except Exception:
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
                    if section_name.startswith(';;'): f.write(section_name + '\n')
                    else: f.write(section_name + '\n')
                    for line in lines: f.write(line + '\n')
                    f.write('\n')
                if preserved_tail_content:
                    f.write('\n' + preserved_tail_content)
        except Exception as e:
            print(f"写入INI文件失败: {e}")

    def _scan_freq_vars_from_ini(self, sections):
        freq_vars = OrderedDict()
        if '[Constants]' not in sections: return freq_vars
        pattern = re.compile(r'^\s*global\s+persist\s+(\$Freq_[^\s=]+)\s*=\s*([\d.]+)')
        prev_line = ""
        for line in sections['[Constants]']:
            m = pattern.match(line)
            if m:
                var_name = m.group(1)
                default_val = m.group(2)
                name_match = re.search(r";\s*控制形态键\s+'([^']+)'\s*的强度", prev_line)
                label = name_match.group(1).strip() if name_match else var_name
                freq_vars[var_name] = {"default": default_val, "label": label}
            prev_line = line
        return freq_vars

    def _scan_shapekey_names_from_classification(self):
        text_obj = next((t for t in bpy.data.texts if "Shape_Key_Classification" in t.name), None)
        if not text_obj: return []
        names = set()
        for line in text_obj.as_string().splitlines():
            m = re.search(r'名称:\s*(.+)', line)
            if m: names.add(m.group(1).strip())
        return sorted(names)

    def _scan_shapekey_names_from_variable_items(self):
        if not self.inputs[0].is_linked: return []
        upstream = self.inputs[0].links[0].from_node
        if upstream.bl_idname != 'SSMTNode_PostProcess_ShapeKey': return []
        items = getattr(upstream, "shapekey_variable_items", None)
        if not items: return []
        return [item.shape_key_name for item in items if item.shape_key_name.strip()]

    def _build_var_to_group_map(self, freq_vars, shapekey_names):
        name_to_group = {}
        for name in shapekey_names:
            group = 1
            for entry in self.play_group_entries:
                if entry.shape_key_name == name:
                    group = entry.group_index
                    break
            name_to_group[name] = group
        var_to_group = {}
        for var_name, info in freq_vars.items():
            label = info["label"]
            group = name_to_group.get(label, 1)
            var_to_group[var_name] = group
        return var_to_group, list(sorted(set(name_to_group.values())))

    def _add_auto_playback_logic_for_group(self, sections, group_id, intensity_var, frame_count, cycle_mode, speed_percent_min, speed_percent_max, speed_intervals, max_unroll):
        if '[Constants]' not in sections: sections['[Constants]'] = []
        const_lines = sections['[Constants]']
        const_content = "\n".join(const_lines)
        prefix = f"group{group_id}"
        auto_enabled_var = f"$auto_play_enabled_{prefix}"
        step_accum_var = f"$step_accum_{prefix}"
        frame_var = f"$shapekey_frame_{prefix}"
        frame_end_var = f"$frameEnd_{prefix}"
        speed_percent_var = f"$speed_percent_{prefix}"
        speed_percent_min_var = f"$speed_percent_min_{prefix}"
        speed_percent_max_var = f"$speed_percent_max_{prefix}"
        step_frames_var = f"$step_frames_{prefix}"
        base_step_var = f"$base_step_{prefix}"
        norm_frame_var = f"$norm_frame_{prefix}"
        pingpong_dir_var = f"$pingpong_dir_{prefix}" if cycle_mode == 'PINGPONG' else None
        auto_vars = [
            f"global persist {auto_enabled_var} = 1", f"global {step_accum_var} = 0",
            f"global {frame_var} = 0", f"global persist {frame_end_var} = {frame_count}",
            f"global persist {speed_percent_var} = 100", f"global persist {speed_percent_min_var} = {speed_percent_min}",
            f"global persist {speed_percent_max_var} = {speed_percent_max}", f"global {step_frames_var} = 0",
            f"global {base_step_var} = 0", f"global {norm_frame_var} = 0"
        ]
        if cycle_mode == 'PINGPONG': auto_vars.append(f"global {pingpong_dir_var} = 1")
        for var_line in auto_vars:
            var_name_only = var_line.split('=')[0].strip()
            if var_name_only not in const_content: const_lines.append(var_line)
        present_code = []
        present_code.append(f"; @@ShapeKeyExt:PLAY:{self._ensure_namespace()}@@")
        present_code.append(f"; ========== AUTO PLAYBACK GROUP {group_id} (Variable Speed) ==========")
        present_code.append(f"if ({auto_enabled_var} == 1)")
        present_code.append(f"    {norm_frame_var} = {frame_var} / {frame_end_var}")
        intervals = sorted(speed_intervals, key=lambda x: x.start)
        if intervals:
            for i, interval in enumerate(intervals):
                start = interval.start / 100.0
                end = interval.end / 100.0
                step = interval.base_step
                if i == 0: cond = f"{norm_frame_var} < {end}"
                else: cond = f"({norm_frame_var} >= {start} && {norm_frame_var} < {end})"
                present_code.append(f"    if ({cond})")
                present_code.append(f"        {base_step_var} = {step}")
                if i < len(intervals) - 1: present_code.append(f"    else")
                else:
                    present_code.append(f"    else")
                    present_code.append(f"        {base_step_var} = 1")
        else:
            present_code.append(f"    {base_step_var} = 10")
        for _ in range(len(intervals)): present_code.append(f"    endif")
        present_code.append(f"    {step_frames_var} = {base_step_var} * {speed_percent_var} / 100.0")
        present_code.append(f"    {step_accum_var} = {step_accum_var} + {step_frames_var}")
        for _ in range(max_unroll):
            present_code.append(f"    if ({step_accum_var} > 0)")
            if cycle_mode == 'PINGPONG':
                present_code.append(f"        {frame_var} = {frame_var} + {pingpong_dir_var}")
                present_code.append(f"        if ({frame_var} >= {frame_end_var})")
                present_code.append(f"            {frame_var} = {frame_end_var}")
                present_code.append(f"            {pingpong_dir_var} = -1")
                present_code.append(f"        endif")
                present_code.append(f"        if ({frame_var} <= 0)")
                present_code.append(f"            {frame_var} = 0")
                present_code.append(f"            {pingpong_dir_var} = 1")
                present_code.append(f"        endif")
            else:
                present_code.append(f"        {frame_var} = {frame_var} + 1")
                present_code.append(f"        if ({frame_var} > {frame_end_var})")
                present_code.append(f"            {frame_var} = 0")
                present_code.append(f"        endif")
            present_code.append(f"        {step_accum_var} = {step_accum_var} - 1")
            present_code.append(f"    endif")
        if cycle_mode == 'REVERSE':
            present_code.append(f"    {intensity_var} = ({frame_end_var} - {frame_var}) / {frame_end_var}")
        else:
            present_code.append(f"    {intensity_var} = {frame_var} / {frame_end_var}")
        present_code.append(f"endif")
        present_code.append(f"; ========== END AUTO PLAYBACK GROUP {group_id} ==========")
        if '[Present]' not in sections: sections['[Present]'] = []
        present_lines = sections['[Present]']
        insert_pos = 0
        for i, line in enumerate(present_lines):
            stripped = line.strip()
            if stripped and not stripped.startswith(';'):
                insert_pos = i
                break
            else:
                insert_pos = i + 1
        for code_line in reversed(present_code): present_lines.insert(insert_pos, code_line)

    def _generate_remark_icon(self, text, dest_path, font_size=36,
                              font_family="msyh.ttc",
                              text_color=(1.0, 1.0, 1.0),
                              stroke_width=2,
                              stroke_color=(0.0, 0.0, 0.0),
                              with_style=True):
        """根据分组备注生成文字按钮图标。

        with_style=True：绘制紧贴文字的圆角背景 + 边框（内边距小，图片接近 1:1 不发糊）。
        with_style=False：透明背景仅文字（无背景/边框），用于未启用自动播放的分组。
        """
        try:
            if not PIL_AVAILABLE: return None

            text = text.replace('/', '\n').strip()
            if not text: return None

            def float_to_int_rgb(vals):
                return tuple(int(val * 255) for val in vals)

            text_rgb = float_to_int_rgb(text_color)
            stroke_rgb = float_to_int_rgb(stroke_color)

            font = None
            try:
                font = ImageFont.truetype(font_family, font_size)
            except:
                for f in ["msyh.ttc", "simsun.ttc", "simhei.ttf", "arial.ttf"]:
                    try:
                        font = ImageFont.truetype(f, font_size)
                        break
                    except:
                        continue
            if font is None:
                font = ImageFont.load_default()

            temp_img = Image.new('RGBA', (1, 1), (0, 0, 0, 0))
            temp_draw = ImageDraw.Draw(temp_img)
            bbox = temp_draw.multiline_textbbox((0, 0), text, font=font, align='center', spacing=6)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            if with_style:
                # 紧凑内边距：文字到背景边缘 6px + 边框宽，边框紧挨文字，图片不过大
                border_w = max(0, int(self.button_border_width))
                gap = 6
                padding = gap + border_w
            else:
                # 无背景/边框：透明背景仅文字，紧凑内边距（旧版机制，最锐利）
                border_w = 0
                padding = 8 + (stroke_width * 2)

            img_w = math.ceil(text_w + padding * 2)
            img_h = math.ceil(text_h + padding * 2)
            img = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            if with_style:
                # 按钮背景：圆角矩形紧贴文字 + 边框
                bg_rgb = float_to_int_rgb(self.button_bg_color)
                bd_rgb = float_to_int_rgb(self.button_border_color)
                btn_alpha = int(255 * max(0.0, min(1.0, self.button_opacity)))
                radius = min(6, img_h // 4)
                try:
                    draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=radius,
                                           fill=bg_rgb + (btn_alpha,),
                                           outline=bd_rgb + (btn_alpha,), width=border_w)
                except Exception:
                    draw.rectangle([0, 0, img_w - 1, img_h - 1], fill=bg_rgb + (btn_alpha,),
                                   outline=bd_rgb + (btn_alpha,))
            x = (img_w - text_w) / 2 - bbox[0]
            y = (img_h - text_h) / 2 - bbox[1]
            draw.multiline_text((x, y), text, font=font, fill=text_rgb + (255,),
                                align='center', spacing=6,
                                stroke_width=stroke_width, stroke_fill=stroke_rgb + (255,))
            img.save(dest_path)
            return dest_path
        except Exception as e:
            print(f"[形态键扩展] 生成文字图标失败: {e}")
            return None

    def _measure_image_aspect(self, dest_res_dir, std_name, fallback=1.0):
        """读取已复制到 res 的图片宽高比（用于按高度×比例推导宽度）。"""
        path = os.path.join(dest_res_dir, std_name)
        if os.path.exists(path) and PIL_AVAILABLE:
            try:
                with Image.open(path) as img:
                    w, h = img.size
                    if h > 0:
                        return w / h
            except Exception:
                pass
        return fallback

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
                with Image.open(dest_path) as src:
                    img = src.convert('RGBA')
            else:
                # 生成纯色底图（按面板在 16:9 屏幕上的显示比例，保证圆角不变形）
                img_h = 512
                pixel_ratio = (panel_w / panel_h) * (1920.0 / 1080.0) if panel_h > 0 else 1.0
                img_w = max(64, int(round(img_h * pixel_ratio)))
                fill_rgb = float_to_int_rgb((0.05, 0.08, 0.12))
                img = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
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
                img = Image.merge('RGBA', (r, g, b, a))

            # 圆角 alpha 遮罩
            mask = Image.new('L', (img_w, img_h), 0)
            md = ImageDraw.Draw(mask)
            if radius_px > 0:
                md.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=radius_px, fill=255)
            else:
                md.rectangle([0, 0, img_w - 1, img_h - 1], fill=255)
            r, g, b, a = img.split()
            a = Image.composite(a, Image.new('L', (img_w, img_h), 0), mask)
            img = Image.merge('RGBA', (r, g, b, a))

            # 边框（在圆角区域内）
            if border_px > 0:
                draw = ImageDraw.Draw(img)
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
            print(f"[滑块面板] 生成背景图失败: {e}")
            return None
            
    # ==========================================
    # 【修复】添加自然排序的辅助函数
    # ==========================================
    def _natural_sort_key(self, s):
        """
        自然排序算法，将字符串中的数字转为整数进行智能比较。
        例如：Motion_Key_2 会排在 Motion_Key_19 前面。
        """
        return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', s)]

    # ==========================================
    # 原地更新：按专有标识移除本节点旧配置
    # ==========================================
    def _is_owned_section(self, sec_name, ns):
        """判断段落是否由本节点生成（形态键扩展 + 合并的滑块面板）。"""
        s = sec_name.strip()
        fixed = [
            f"[KeyToggleAutoPlay_{ns}]", f"[CommandListToggleAutoPlay_{ns}]",
            "[KeyHelp]", "[KeyResetPosition]", "[KeyZoomIn]", "[KeyZoomOut]", "[KeyMouseDrag]",
            "[CommandListZoomIn]", "[CommandListZoomOut]",
            "[ResourceImageToRender0]", "[ResourceSliderHandle]", "[ResourceLeftBar]", "[ResourceRightBar]",
            "[CustomShaderDraw]", "[TextureOverrideCheckHash]",
        ]
        if s in fixed:
            return True
        if s.startswith("[CommandListToggleAutoPlayGroup") and s.endswith(f"_{ns}]"):
            return True
        if s.startswith("[ResourcePlayPauseButton") and s.endswith("]"):
            return True
        return False

    def _remove_owned_config(self, sections, ns):
        """按专有标识移除本节点在 ini 中的旧配置（Constants 块、Present 块、自有段落）。"""
        # Constants：形态键块 + 滑块常量块
        const_pairs = [
            (self.BLOCK_BEGIN.format(ns=ns), self.BLOCK_END.format(ns=ns)),
            (self.SLIDERCONST_BEGIN, self.SLIDERCONST_END),
        ]
        if '[Constants]' in sections:
            new_lines = []
            skip_until = None
            for line in sections['[Constants]']:
                if skip_until is None:
                    for begin, end in const_pairs:
                        if begin in line:
                            skip_until = end
                            break
                    if skip_until is not None:
                        continue
                    new_lines.append(line)
                else:
                    if skip_until in line:
                        skip_until = None
            sections['[Constants]'] = new_lines

        # Present：移除插入的形态键同步块、自动播放块与滑块面板块（按块标识注释定位）
        if '[Present]' in sections:
            new_lines = []
            skip_state = None  # None / 'SYNC' / 'PLAY' / 'SLIDER'
            sync_marker = f"; @@ShapeKeyExt:SYNC:{ns}@@"
            play_marker = f"; @@ShapeKeyExt:PLAY:{ns}@@"
            for line in sections['[Present]']:
                if skip_state is None:
                    if sync_marker in line:
                        skip_state = 'SYNC'
                        continue
                    if play_marker in line:
                        skip_state = 'PLAY'
                        continue
                    if self.SLIDER_PRESENT_BEGIN in line:
                        skip_state = 'SLIDER'
                        continue
                    new_lines.append(line)
                else:
                    if skip_state == 'SYNC' and "结束组内变量同步" in line:
                        skip_state = None
                    elif skip_state == 'PLAY' and "END AUTO PLAYBACK GROUP" in line:
                        skip_state = None
                    elif skip_state == 'SLIDER' and self.SLIDER_PRESENT_END in line:
                        skip_state = None
            sections['[Present]'] = new_lines

        # 自有段落（整体删除）
        for sec_name in list(sections.keys()):
            if self._is_owned_section(sec_name, ns):
                del sections[sec_name]

    # ==========================================
    # 【已合并】滑块面板-自定义逻辑
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
                print(f"[滑块面板] 无法从物体名称 '{obj_name}' 解析哈希和IndexCount")

    def _copy_default_image(self, std_name, dest_res_dir, source_asset_dir):
        default_path = os.path.join(source_asset_dir, std_name)
        dest_path = os.path.join(dest_res_dir, std_name)
        if os.path.exists(default_path):
            if not os.path.exists(dest_path):
                shutil.copy2(default_path, dest_path)
        else:
            print(f"警告: 默认图片不存在 {default_path}")

    def _scan_slider_group_info(self, sections):
        group_ids = set()
        if '[Constants]' in sections:
            const_text = "\n".join(sections['[Constants]'])
            for m in re.finditer(r'^\s*global(?:\s+persist)?\s+\$auto_play_enabled_group(\d+)', const_text, re.MULTILINE):
                group_ids.add(int(m.group(1)))
        if group_ids:
            return sorted(group_ids), True
        return [], False

    def _scan_slot_icons(self, sections):
        slot_icon_map = {}
        icon_pattern = re.compile(r'^\s*;\s*SLOT_ICON_(\d+)\s*=\s*(.+)$')
        if '[Constants]' in sections:
            for line in sections['[Constants]']:
                m = icon_pattern.match(line)
                if m:
                    slot = int(m.group(1))
                    path = m.group(2).strip()
                    if os.path.isfile(path):
                        slot_icon_map[slot] = path
        return slot_icon_map

    def _build_slider_to_group(self, sorted_freq_params, multi_group_mode, group_ids):
        slider_to_group = {}
        if multi_group_mode and group_ids:
            has_exact = False
            for idx, var_name in enumerate(sorted_freq_params):
                m = re.match(r'\$Freq_group(\d+)', var_name)
                if m:
                    slider_to_group[idx] = int(m.group(1))
                    has_exact = True
            if not has_exact:
                for idx in range(len(sorted_freq_params)):
                    gid = group_ids[idx % len(group_ids)]
                    slider_to_group[idx] = gid
        return slider_to_group

    def _apply_slider_panel(self, mod_export_path, target_ini_file, sections):
        """【已合并】生成滑块面板-自定义配置并合并到 sections（不写盘）。"""
        ns = self._ensure_namespace()
        if '[Present]' in sections and any("SLIDER PANEL CUSTOM LOGIC (appended)" in l for l in sections['[Present]']):
            print("滑块面板配置已存在于文件中。请手动删除后再生成。")
            return False

        freq_params_temp = set()
        param_pattern = re.compile(r'^\s*global(?:\s+persist)?\s+(\$Freq_[^\s=]+)')
        if '[Constants]' in sections:
            for line in sections['[Constants]']:
                m = param_pattern.match(line)
                if m:
                    freq_params_temp.add(m.group(1))

        group_pattern = re.compile(r'^\$Freq_Group(\d+)$')
        group_var_matches = {}
        for v in freq_params_temp:
            m = group_pattern.match(v)
            if m:
                group_var_matches[int(m.group(1))] = v

        if group_var_matches:
            multi_group_mode = True
            group_ids = sorted(group_var_matches.keys())
            sorted_freq_params = [group_var_matches[g] for g in group_ids]
            slider_to_group = {i: gid for i, gid in enumerate(group_ids)}
            num_sliders = len(sorted_freq_params)
        else:
            sorted_freq_params = sorted(list(freq_params_temp))
            num_sliders = len(sorted_freq_params)
            group_ids, multi_group_mode = self._scan_slider_group_info(sections)
            slider_to_group = self._build_slider_to_group(sorted_freq_params, multi_group_mode, group_ids)

        if num_sliders == 0:
            return False

        slot_icon_map = self._scan_slot_icons(sections)

        try:
            addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            source_asset_dir = os.path.join(addon_dir, "Toolset")
            dest_res_dir = os.path.join(mod_export_path, "res")
            os.makedirs(dest_res_dir, exist_ok=True)

            shader_src = os.path.join(source_asset_dir, "draw_2d.hlsl")
            shader_dst = os.path.join(dest_res_dir, "draw_2d.hlsl")
            if os.path.exists(shader_src) and not os.path.exists(shader_dst):
                shutil.copy2(shader_src, shader_dst)

            image_mappings = [("0.png", self.background_image), ("1.png", self.slider_handle_image),
                              ("2.png", self.left_bar_image), ("3.png", self.right_bar_image)]
            for std_name, custom_path in image_mappings:
                dest_path = os.path.join(dest_res_dir, std_name)
                if custom_path and os.path.isfile(custom_path):
                    shutil.copy2(custom_path, dest_path)
                else:
                    self._copy_default_image(std_name, dest_res_dir, source_asset_dir)

            btn_w_list = []
            max_btn_w = 0.0
            btn_h_fixed = self.button_height
            SCREEN_RATIO_CORRECTION = 0.5625

            for i in range(1, num_sliders + 1):
                idx = i - 1
                group_id = slider_to_group.get(idx) if multi_group_mode else None
                custom_icon = None
                if group_id is not None and group_id in slot_icon_map:
                    custom_icon = slot_icon_map[group_id]
                if not custom_icon and self.button_image and os.path.isfile(self.button_image):
                    custom_icon = self.button_image
                if custom_icon and os.path.isfile(custom_icon):
                    src_btn = custom_icon
                else:
                    src_btn = os.path.join(source_asset_dir, "4.png")

                if os.path.exists(src_btn):
                    dest_name = f"4_{i}.png"
                    dest_path = os.path.join(dest_res_dir, dest_name)
                    if os.path.normpath(src_btn) != os.path.normpath(dest_path):
                        shutil.copy2(src_btn, dest_path)

                    btn_w_i = btn_h_fixed * 3.0 * SCREEN_RATIO_CORRECTION  # 缺省按 3:1
                    if PIL_AVAILABLE:
                        try:
                            with Image.open(dest_path) as img:
                                w, h = img.size
                                if h > 0:
                                    btn_w_i = btn_h_fixed * (w / h) * SCREEN_RATIO_CORRECTION
                        except Exception:
                            pass
                    btn_w_list.append(btn_w_i)
                    if btn_w_i > max_btn_w:
                        max_btn_w = btn_w_i
                else:
                    btn_w_list.append(btn_h_fixed * 3.0 * SCREEN_RATIO_CORRECTION)
        except Exception as e:
            print(f"准备和复制资源文件时出错: {e}")
            return False

        slider_h = self.slider_height
        # 滑块宽度 = 滑块高度 × 滑块柄图片宽高比（与物体切换面板按钮一致，保持图片比例）
        slider_w = self._measure_image_aspect(dest_res_dir, "1.png") * slider_h * SCREEN_RATIO_CORRECTION

        child_height = slider_h
        top_bottom_padding = 0.03
        spacing = 0.02
        total_slider_height = num_sliders * child_height
        total_spacing_height = max(0, (num_sliders - 1) * spacing)
        parent_height = total_slider_height + total_spacing_height + (top_bottom_padding * 2)
        # 面板最小高度：自动计算高度过小时使用设置值
        parent_height = max(parent_height, self.panel_min_height)

        # 背景尺寸：宽 = 自动计算高度 × 背景图片宽高比 × 屏幕校正（保持图片比例）
        bg_aspect = self._measure_image_aspect(dest_res_dir, "0.png")
        panel_bg_width_auto = parent_height * bg_aspect * SCREEN_RATIO_CORRECTION

        # 生成/处理面板背景（圆角+边框）：无论是否自定义背景图都生效
        bg_custom = (self.background_image or "").strip()
        self._generate_background_image(os.path.join(dest_res_dir, "0.png"),
                                        panel_bg_width_auto, parent_height,
                                        use_existing=bool(bg_custom))

        help_key = self.help_key.strip() or "home"
        reset_key = self.reset_key.strip() or "ctrl home"
        zoom_in_key = self.zoom_in_key.strip() or "up"
        zoom_out_key = self.zoom_out_key.strip() or "down"
        drag_key = self.drag_key.strip() or "VK_LBUTTON"
        auto_play_key = self.auto_play_toggle_key.strip() or "space"
        detect_hash = self.detect_hash.strip() or self.check_hash.strip()
        detect_index_count_val = self.detect_index_count.strip()
        if not detect_index_count_val and self.match_index_count > 0:
            detect_index_count_val = str(self.match_index_count)

        constants_additions = []
        present_additions = []
        other_sections = OrderedDict()

        constants_additions.extend([
            "; --- UI 几何与位置配置 (由滑块面板-自定义生成) ---",
            f"global $base_width0 = {panel_bg_width_auto:.4f}",
            f"global $base_height0 = {parent_height:.4f}",
            "global $set_x0 = 0.5", "global $set_y0 = 0.5",
        ])
        for i in range(1, num_sliders + 1):
            current_y_offset = top_bottom_padding + (i - 1) * (child_height + spacing) + (child_height / 2)
            relative_y = current_y_offset / parent_height
            btn_w_i = btn_w_list[i-1]
            constants_additions.extend([
                f"global $base_width{i} = {slider_w:.4f}",
                f"global $base_height{i} = {slider_h:.4f}",
                f"global $set_rel_x{i} = 0.5",
                f"global $fixed_rel_y{i} = {relative_y:.4f}",
                f"global $btn_width{i} = {btn_w_i:.4f}",
                f"global $btn_height{i} = {btn_h_fixed:.4f}",
                f"global $btn_x{i}", f"global $btn_y{i}", f"global $btn_pressed{i} = 0",
            ])

        constants_additions.extend([
            "global $ui_active", "global $help",
            "global $max_zoom = 5.0", "global $min_zoom = 0.1",
            "global $dragged_slider = 0", "global $mouse_clicked = 0",
            "global $click_outside = 0", "global $is_dragging = 0",
            "global $drag_x = 0", "global $drag_y = 0",
            "global persist $img0_x = 0", "global persist $img0_y = 0",
            f"global persist $zoom0 = {self.panel_default_scale:.2f}",
            "global $norm_width0", "global $norm_height0",
            "global $btn_click_processed = 0",
        ])
        if not multi_group_mode:
            constants_additions.extend([
                "global persist $auto_play_enabled = 0", "global persist $frameEnd = 30",
                "global persist $step_accum = 0", "global persist $step_frames = 5",
                "global persist $step_frames_min = 1", "global persist $step_frames_max = 30",
            ])
        for i in range(1, num_sliders + 1):
            constants_additions.extend([
                f"global persist $rel_x{i} = 0", f"global persist $zoom{i} = 1.0",
                f"global $norm_width{i}", f"global $norm_height{i}",
                f"global $img{i}_x", f"global $img{i}_y", f"global $rel_y{i}",
                f"global $param{i}",
                f"global $left_bar{i}_x", f"global $left_bar{i}_y",
                f"global $left_bar{i}_width", f"global $left_bar{i}_height",
                f"global $right_bar{i}_x", f"global $right_bar{i}_y",
                f"global $right_bar{i}_width", f"global $right_bar{i}_height",
                f"global $min_rel_x{i}", f"global $max_rel_x{i}",
                f"global $range_x{i}", f"global $slider{i}_center_x",
            ])

        detect_lines = []
        has_any_check = False
        if detect_hash:
            detect_lines.append(f"hash = {detect_hash}")
            has_any_check = True
        if detect_index_count_val:
            detect_lines.append(f"match_index_count = {detect_index_count_val}")
            has_any_check = True
        detect_lines.append("$ui_active = 1")
        if has_any_check:
            other_sections["[TextureOverrideCheckHash]"] = detect_lines

        other_sections["[ResourceImageToRender0]"] = ["filename = ./res/0.png"]
        other_sections["[ResourceSliderHandle]"] = ["filename = ./res/1.png"]
        other_sections["[ResourceLeftBar]"] = ["filename = ./res/2.png"]
        other_sections["[ResourceRightBar]"] = ["filename = ./res/3.png"]
        for i in range(1, num_sliders + 1):
            other_sections[f"[ResourcePlayPauseButton{i}]"] = [f"filename = ./res/4_{i}.png"]

        reset_lines = ["$img0_x = 0", "$img0_y = 0", f"$zoom0 = {self.panel_default_scale:.2f}"]
        for i in range(1, num_sliders + 1):
            reset_lines.extend([f"$rel_x{i} = 0", f"$zoom{i} = 1.0"])
        zoom_in_lines = ["$zoom0 = $zoom0 + 0.05"] + [f"$zoom{i} = $zoom{i} + 0.05" for i in range(1, num_sliders + 1)]
        zoom_out_lines = ["$zoom0 = $zoom0 - 0.05"] + [f"$zoom{i} = $zoom{i} - 0.05" for i in range(1, num_sliders + 1)]

        other_sections["[KeyHelp]"] = [
            f"condition = $ui_active == 1", f"key = {help_key}", "type = cycle", "$help = 0,1"
        ]
        other_sections["[KeyResetPosition]"] = [
            f"condition = $help == 1 && $ui_active == 1", f"key = {reset_key}", "type = cycle",
        ] + reset_lines
        other_sections["[KeyZoomIn]"] = [
            f"condition = $help == 1 && $ui_active == 1", f"key = {zoom_in_key}",
            "type = press", "run = CommandListZoomIn"
        ]
        other_sections["[KeyZoomOut]"] = [
            f"condition = $help == 1 && $ui_active == 1", f"key = {zoom_out_key}",
            "type = press", "run = CommandListZoomOut"
        ]
        other_sections["[KeyMouseDrag]"] = [
            f"condition = $help == 1 && $ui_active == 1", f"key = {drag_key}",
            "type = hold", "$mouse_clicked = 1"
        ]

        auto_play_key_lines = []
        if not self.auto_play_key_global:
            auto_play_key_lines.append("condition = $help == 1 && $ui_active == 1")
        auto_play_key_lines.extend([f"key = {auto_play_key}", "type = press", f"run = CommandListToggleAutoPlay_{ns}"])
        other_sections[f"[KeyToggleAutoPlay_{ns}]"] = auto_play_key_lines

        other_sections["[CommandListZoomIn]"] = zoom_in_lines
        other_sections["[CommandListZoomOut]"] = zoom_out_lines

        if multi_group_mode:
            toggle_lines = [f"$auto_play_enabled_group{gid} = 1 - $auto_play_enabled_group{gid}" for gid in group_ids]
            other_sections[f"[CommandListToggleAutoPlay_{ns}]"] = toggle_lines
            for gid in group_ids:
                other_sections[f"[CommandListToggleAutoPlayGroup{gid}_{ns}]"] = [f"$auto_play_enabled_group{gid} = 1 - $auto_play_enabled_group{gid}"]
        else:
            other_sections[f"[CommandListToggleAutoPlay_{ns}]"] = ["$auto_play_enabled = 1 - $auto_play_enabled"]

        present_additions.append("post $ui_active = 0")
        present_additions.append("if $help == 1 && $ui_active == 1")
        present_additions.append("    ; --- 1. 尺寸计算 ---")
        present_additions.append("    $norm_width0 = $base_width0 * $zoom0")
        present_additions.append("    $norm_height0 = $base_height0 * $zoom0")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    $norm_width{i} = $base_width{i} * $zoom{i}")
            present_additions.append(f"    $norm_height{i} = $base_height{i} * $zoom{i}")

        present_additions.append("\n    ; --- 2. 计算子级的拖拽边界 ---")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    $min_rel_x{i} = $btn_width{i} + 0.02")
            present_additions.append(f"    $max_rel_x{i} = ($norm_width0 * 0.95) - $norm_width{i}")
            present_additions.append(f"    $range_x{i} = $max_rel_x{i} - $min_rel_x{i}")

        present_additions.append("\n    ; --- 3. 位置初始化 ---")
        present_additions.append("    if $img0_x == 0 && $img0_y == 0")
        present_additions.append("        $img0_x = $set_x0 * (1 - $norm_width0)")
        present_additions.append("        $img0_y = $set_y0 * (1 - $norm_height0)")
        present_additions.append("    endif")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    if $rel_x{i} == 0")
            if multi_group_mode:
                gid = slider_to_group.get(i - 1)
                if gid is not None and gid in group_ids:
                    present_additions.append(f"        if ($auto_play_enabled_group{gid} == 1)")
                    present_additions.append(f"            $param{i} = ($speed_percent_group{gid} - $speed_percent_min_group{gid}) / ($speed_percent_max_group{gid} - $speed_percent_min_group{gid})")
                    present_additions.append(f"        else")
                    present_additions.append(f"            $param{i} = {sorted_freq_params[i-1]}")
                    present_additions.append(f"        endif")
                else:
                    present_additions.append(f"        $param{i} = {sorted_freq_params[i-1]}")
            else:
                present_additions.append(f"        if ($auto_play_enabled == 1)")
                present_additions.append(f"            $param{i} = ($step_frames - $step_frames_min) / ($step_frames_max - $step_frames_min)")
                present_additions.append(f"        else")
                present_additions.append(f"            $param{i} = {sorted_freq_params[i-1]}")
                present_additions.append(f"        endif")
            present_additions.append(f"        $rel_x{i} = $min_rel_x{i} + $param{i} * $range_x{i}")
            present_additions.append("    endif")

        present_additions.append("\n    ; --- 4. 计算滑块和按钮位置 ---")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    $rel_y{i} = ($fixed_rel_y{i} * $norm_height0) - ($norm_height{i} / 2)")
            present_additions.append(f"    $img{i}_x = $img0_x + $rel_x{i}")
            present_additions.append(f"    $img{i}_y = $img0_y + $rel_y{i}")
            present_additions.append(f"    $btn_x{i} = $img0_x + $min_rel_x{i} - $btn_width{i} - 0.01")
            present_additions.append(f"    $btn_y{i} = $img{i}_y + ($norm_height{i} - $btn_height{i}) * 0.5")

        present_additions.append("\n    ; --- 5. 按钮按下/弹起检测（仅自动播放分组可点击）---")
        present_additions.append("    $btn_click_processed = 0")
        present_additions.append("    if $mouse_clicked && $is_dragging == 0")
        for i in range(1, num_sliders + 1):
            gid = slider_to_group.get(i - 1) if multi_group_mode else None
            # 仅启用自动播放的分组按钮可点击（切换播放/暂停）
            setting = self._find_group_setting(gid) if (multi_group_mode and gid is not None) else None
            if setting is not None:
                auto_playable = bool(setting.enable_auto_playback)
            else:
                auto_playable = any(s.enable_auto_playback for s in self.play_group_settings)
            if not auto_playable:
                continue
            present_additions.append(f"        if cursor_x > $btn_x{i} && cursor_x < $btn_x{i} + $btn_width{i} && cursor_y > $btn_y{i} && cursor_y < $btn_y{i} + $btn_height{i}")
            present_additions.append(f"            $btn_pressed{i} = 1")
            present_additions.append(f"            $btn_click_processed = 1")
            present_additions.append(f"        endif")
        present_additions.append("    else")
        present_additions.append("        if $is_dragging == 0")
        for i in range(1, num_sliders + 1):
            gid = slider_to_group.get(i - 1) if multi_group_mode else None
            setting = self._find_group_setting(gid) if (multi_group_mode and gid is not None) else None
            if setting is not None:
                auto_playable = bool(setting.enable_auto_playback)
            else:
                auto_playable = any(s.enable_auto_playback for s in self.play_group_settings)
            if not auto_playable:
                continue
            present_additions.append(f"            if $btn_pressed{i} == 1")
            present_additions.append(f"                if cursor_x > $btn_x{i} && cursor_x < $btn_x{i} + $btn_width{i} && cursor_y > $btn_y{i} && cursor_y < $btn_y{i} + $btn_height{i}")
            if multi_group_mode and gid is not None and gid in group_ids:
                present_additions.append(f"                    run = CommandListToggleAutoPlayGroup{gid}_{ns}")
            else:
                present_additions.append(f"                    run = CommandListToggleAutoPlay_{ns}")
            present_additions.append(f"                endif")
            present_additions.append(f"                $btn_pressed{i} = 0")
            present_additions.append(f"            endif")
        present_additions.append("        endif")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 6. 拖拽逻辑与位置更新 ---")
        present_additions.append("    if $mouse_clicked")
        present_additions.append("        if $is_dragging == 0")
        for i in range(num_sliders, 0, -1):
            prefix = "if" if i == num_sliders else "            else if"
            present_additions.append(f"            {prefix} cursor_x > $img{i}_x && cursor_x < $img{i}_x + $norm_width{i} && cursor_y > $img{i}_y && cursor_y < $img{i}_y + $norm_height{i}")
            present_additions.append(f"                $is_dragging = {i + 1}")
            present_additions.append(f"                $drag_x = cursor_x - $img{i}_x")
        present_additions.append("            else if cursor_x > $img0_x && cursor_x < $img0_x + $norm_width0 && cursor_y > $img0_y && cursor_y < $img0_y + $norm_height0")
        present_additions.append("                $is_dragging = 1")
        present_additions.append("                $drag_x = cursor_x - $img0_x")
        present_additions.append("                $drag_y = cursor_y - $img0_y")
        present_additions.append("            else")
        present_additions.append("                if $btn_click_processed == 0")
        present_additions.append("                    $click_outside = 1")
        present_additions.append("                endif")
        present_additions.append("            endif")
        present_additions.append("        endif")
        present_additions.append("    else")
        present_additions.append("        $is_dragging = 0")
        present_additions.append("    endif")
        present_additions.append("    if $click_outside == 1 && $mouse_clicked == 0")
        present_additions.append("        $help = 0")
        present_additions.append("        $click_outside = 0")
        present_additions.append("    endif")
        present_additions.append("    if $is_dragging == 1")
        present_additions.append("        $img0_x = cursor_x - $drag_x")
        present_additions.append("        $img0_y = cursor_y - $drag_y")
        for i in range(2, num_sliders + 2):
            present_additions.append(f"    else if $is_dragging == {i}")
            present_additions.append(f"        $rel_x{i-1} = (cursor_x - $drag_x) - $img0_x")
            present_additions.append(f"        if $rel_x{i-1} < $min_rel_x{i-1}")
            present_additions.append(f"            $rel_x{i-1} = $min_rel_x{i-1}")
            present_additions.append(f"        endif")
            present_additions.append(f"        if $rel_x{i-1} > $max_rel_x{i-1}")
            present_additions.append(f"            $rel_x{i-1} = $max_rel_x{i-1}")
            present_additions.append(f"        endif")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 7. 计算进度条的几何信息 ---")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    $slider{i}_center_x = $img{i}_x + ($norm_width{i} * 0.5)")
            present_additions.append(f"    $left_bar{i}_height = $norm_height{i} * 0.5")
            present_additions.append(f"    $left_bar{i}_y = $img{i}_y + ($norm_height{i} * 0.25)")
            present_additions.append(f"    $left_bar{i}_x = $img0_x + $min_rel_x{i}")
            present_additions.append(f"    $left_bar{i}_width = $slider{i}_center_x - $left_bar{i}_x")
            present_additions.append(f"    $right_bar{i}_height = $left_bar{i}_height")
            present_additions.append(f"    $right_bar{i}_y = $left_bar{i}_y")
            present_additions.append(f"    $right_bar{i}_x = $slider{i}_center_x")
            present_additions.append(f"    $right_bar{i}_width = ($img0_x + $norm_width0 * 0.95) - $right_bar{i}_x")

        present_additions.append("\n    ; --- 8. 计算映射参数并链接到形态键强度或播放速度百分比 ---")
        present_additions.append("    $dragged_slider = 0")
        present_additions.append("    if $is_dragging >= 2")
        present_additions.append("        $dragged_slider = $is_dragging - 1")
        present_additions.append("    endif")

        if multi_group_mode:
            for i in range(1, num_sliders + 1):
                gid = slider_to_group.get(i - 1)
                present_additions.append(f"    if ($dragged_slider == {i})")
                present_additions.append(f"        $param{i} = ($rel_x{i} - $min_rel_x{i}) / $range_x{i}")
                if gid is not None and gid in group_ids:
                    present_additions.append(f"        if ($auto_play_enabled_group{gid} == 1)")
                    present_additions.append(f"            $speed_percent_group{gid} = $speed_percent_min_group{gid} + $param{i} * ($speed_percent_max_group{gid} - $speed_percent_min_group{gid})")
                    present_additions.append(f"        else")
                    present_additions.append(f"            {sorted_freq_params[i-1]} = $param{i}")
                    present_additions.append(f"        endif")
                else:
                    present_additions.append(f"        {sorted_freq_params[i-1]} = $param{i}")
                present_additions.append(f"    else")
                if gid is not None and gid in group_ids:
                    present_additions.append(f"        if ($auto_play_enabled_group{gid} == 1)")
                    present_additions.append(f"            $param{i} = ($speed_percent_group{gid} - $speed_percent_min_group{gid}) / ($speed_percent_max_group{gid} - $speed_percent_min_group{gid})")
                    present_additions.append(f"        else")
                    present_additions.append(f"            $param{i} = {sorted_freq_params[i-1]}")
                    present_additions.append(f"        endif")
                else:
                    present_additions.append(f"        $param{i} = {sorted_freq_params[i-1]}")
                present_additions.append(f"        $rel_x{i} = $min_rel_x{i} + $param{i} * $range_x{i}")
                present_additions.append(f"    endif")
        else:
            for i in range(1, num_sliders + 1):
                present_additions.append(f"    if ($dragged_slider == {i})")
                present_additions.append(f"        $param{i} = ($rel_x{i} - $min_rel_x{i}) / $range_x{i}")
                present_additions.append(f"        if ($auto_play_enabled == 1)")
                present_additions.append(f"            $step_frames = $step_frames_min + $param{i} * ($step_frames_max - $step_frames_min)")
                present_additions.append(f"        else")
                present_additions.append(f"            {sorted_freq_params[i-1]} = $param{i}")
                present_additions.append(f"        endif")
                present_additions.append(f"    else")
                present_additions.append(f"        if ($auto_play_enabled == 1)")
                present_additions.append(f"            $param{i} = ($step_frames - $step_frames_min) / ($step_frames_max - $step_frames_min)")
                present_additions.append(f"        else")
                present_additions.append(f"            $param{i} = {sorted_freq_params[i-1]}")
                present_additions.append(f"        endif")
                present_additions.append(f"        $rel_x{i} = $min_rel_x{i} + $param{i} * $range_x{i}")
                present_additions.append(f"    endif")

        present_additions.append("\n    ; --- 9. 执行渲染 (按层级) ---")
        present_additions.append("    ; 渲染父级 (最底层)")
        present_additions.append("    ps-t100 = ResourceImageToRender0")
        present_additions.append("    x87 = $norm_width0")
        present_additions.append("    y87 = $norm_height0")
        present_additions.append("    z87 = $img0_x")
        present_additions.append("    w87 = $img0_y")
        present_additions.append("    run = CustomShaderDraw")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"\n    ; 渲染进度条{i}")
            present_additions.append(f"    ps-t100 = ResourceLeftBar")
            present_additions.append(f"    x87 = $left_bar{i}_width")
            present_additions.append(f"    y87 = $left_bar{i}_height")
            present_additions.append(f"    z87 = $left_bar{i}_x")
            present_additions.append(f"    w87 = $left_bar{i}_y")
            present_additions.append(f"    run = CustomShaderDraw")
            present_additions.append(f"    ps-t100 = ResourceRightBar")
            present_additions.append(f"    x87 = $right_bar{i}_width")
            present_additions.append(f"    y87 = $right_bar{i}_height")
            present_additions.append(f"    z87 = $right_bar{i}_x")
            present_additions.append(f"    w87 = $right_bar{i}_y")
            present_additions.append(f"    run = CustomShaderDraw")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"\n    ; 渲染播放/暂停按钮{i}")
            present_additions.append(f"    ps-t100 = ResourcePlayPauseButton{i}")
            present_additions.append(f"    x87 = $btn_width{i}")
            present_additions.append(f"    y87 = $btn_height{i}")
            present_additions.append(f"    z87 = $btn_x{i}")
            present_additions.append(f"    if $btn_pressed{i} == 1")
            present_additions.append(f"        w87 = $btn_y{i} + 0.002")
            present_additions.append(f"    else")
            present_additions.append(f"        w87 = $btn_y{i}")
            present_additions.append(f"    endif")
            present_additions.append(f"    run = CustomShaderDraw")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"\n    ; 渲染滑块{i} (最顶层)")
            present_additions.append(f"    ps-t100 = ResourceSliderHandle")
            present_additions.append(f"    x87 = $norm_width{i}")
            present_additions.append(f"    y87 = $norm_height{i}")
            present_additions.append(f"    z87 = $img{i}_x")
            present_additions.append(f"    w87 = $img{i}_y")
            present_additions.append(f"    run = CustomShaderDraw")
        present_additions.append("endif")

        shader_def = [
            "hs = null", "ds = null", "gs = null", "cs = null",
            "vs = ./res/draw_2d.hlsl", "ps = ./res/draw_2d.hlsl",
            "blend = ADD SRC_ALPHA INV_SRC_ALPHA", "cull = none",
            "topology = triangle_strip", "o0 = set_viewport bb", "Draw = 4,0", "clear = ps-t100"
        ]

        if '[Constants]' not in sections:
            sections['[Constants]'] = []
        constants_additions.insert(0, self.SLIDERCONST_BEGIN)
        constants_additions.append(self.SLIDERCONST_END)
        for line in constants_additions:
            if line not in sections['[Constants]']:
                sections['[Constants]'].append(line)

        for sec_name, lines in other_sections.items():
            if sec_name not in sections:
                sections[sec_name] = []
            for line in lines:
                if line not in sections[sec_name]:
                    sections[sec_name].append(line)

        if '[CustomShaderDraw]' not in sections:
            sections['[CustomShaderDraw]'] = []
        for line in shader_def:
            if line not in sections['[CustomShaderDraw]']:
                sections['[CustomShaderDraw]'].append(line)

        if '[Present]' not in sections:
            sections['[Present]'] = []
        has_existing_logic = any(self.SLIDER_PRESENT_BEGIN in l for l in sections['[Present]'])
        if not has_existing_logic:
            sections['[Present]'].append("")
            sections['[Present]'].append(self.SLIDER_PRESENT_BEGIN)
            sections['[Present]'].extend(present_additions)
            sections['[Present]'].append(self.SLIDER_PRESENT_END)

        print(f"滑块面板-自定义配置已合并（共 {num_sliders} 个滑块）")
        return True

    def draw_buttons(self, context, layout):
        # 顶部：原地刷新（应用设置到Mod，无需重新导出）
        box_top = layout.box()
        box_top.operator("ssmt.shapekey_ext_refresh", text="应用设置到Mod（原地更新）", icon='FILE_TICK').node_name = self.name
        box_top.prop(self, "ini_file_path", text="INI文件（可选，留空用上次导出）")
        box_top.label(text="修改设置后点上方按钮即可原地更新，无需重新导出", icon='INFO')
        layout.separator()
        # 左右两列布局：左=形态键扩展配置，右=滑块面板-自定义
        split = layout.split(factor=0.5)
        col_left = split.column()
        col_right = split.column()
        self._draw_shapekey_ui(col_left)
        self._draw_slider_ui(col_right)

    def _draw_shapekey_ui(self, layout):
        # --- 顶部：主容器 ---
        box = layout.box()
        box.label(text="形态键与分组管理", icon='SHAPEKEY_DATA')
        row = box.row(align=True)
        row.operator("ssmt.scan_shapekey_ext", text="刷新形态键列表", icon='FILE_REFRESH').node_name = self.name

        row = box.row(align=True)
        row.operator("ssmt.shapekey_group_add", text="创建新分组", icon='ADD').node_name = self.name

        for setting in self.play_group_settings:
            group_container = box.box()

            # 1. 分组头行
            row = group_container.row(align=True)
            # 折叠切换
            row.prop(setting, "expanded", text="", icon='TRIA_DOWN' if setting.expanded else 'TRIA_RIGHT', emboss=False)

            # 高亮当前分组标识：勾选框样式（勾选=当前编辑分组，点击可切换）
            is_active = (setting.group_index == self.active_group_index)
            act_op = row.operator("ssmt.select_play_group", text="", icon='CHECKBOX_HLT' if is_active else 'CHECKBOX_DEHLT', emboss=False)
            act_op.node_name = self.name
            act_op.target_group_index = setting.group_index

            sw_op = row.operator("ssmt.select_play_group", text=f"分组 {setting.group_index} - {setting.remark}", icon='GROUP')
            sw_op.node_name = self.name
            sw_op.target_group_index = setting.group_index

            set_op = row.operator("ssmt.open_group_settings", text="", icon='PREFERENCES')
            set_op.node_name = self.name
            set_op.target_group_index = setting.group_index

            row.separator()
            op_up = row.operator("ssmt.shapekey_group_move", text="", icon='TRIA_UP')
            op_up.node_name = self.name
            op_up.direction = 'UP'
            op_up.target_group_index = setting.group_index

            op_down = row.operator("ssmt.shapekey_group_move", text="", icon='TRIA_DOWN')
            op_down.node_name = self.name
            op_down.direction = 'DOWN'
            op_down.target_group_index = setting.group_index

            op_del = row.operator("ssmt.shapekey_group_remove", text="", icon='X')
            op_del.node_name = self.name
            op_del.target_group_index = setting.group_index

            # 2. 分组形态键列表
            if setting.expanded:
                child_box = group_container.box()
                has_items = False

                # 【修复】使用自然排序 (Natural Sort) 替换原有排序
                sorted_entries = sorted(self.play_group_entries, key=lambda e: self._natural_sort_key(e.shape_key_name))

                for entry in sorted_entries:
                    if entry.group_index == setting.group_index:
                        has_items = True
                        item_row = child_box.row(align=True)
                        item_row.label(text=entry.shape_key_name, icon='SHAPEKEY_DATA')

                        op_add = item_row.operator("ssmt.shapekey_assign_current_group", text="移入当前分组", icon='ADD')
                        op_add.node_name = self.name
                        op_add.shape_key_name = entry.shape_key_name
                if not has_items:
                    child_box.label(text="     (暂无形态键)", icon='INFO')
        # --- 底部：未分组区域 ---
        box = layout.box()
        row = box.row(align=True)
        row.prop(self, "show_unassigned", text="", icon='TRIA_DOWN' if self.show_unassigned else 'TRIA_RIGHT', emboss=False)
        row.label(text="未分组形态键", icon='PARTICLES')

        if self.show_unassigned:
            sub_box = box.box()
            has_unassigned = False

            # 【修复】未分组区域同样使用自然排序
            unassigned_entries = sorted([e for e in self.play_group_entries if e.group_index == 0], key=lambda e: self._natural_sort_key(e.shape_key_name))

            for entry in unassigned_entries:
                has_unassigned = True
                item_row = sub_box.row(align=True)
                item_row.label(text=entry.shape_key_name, icon='SHAPEKEY_DATA')

                op_add = item_row.operator("ssmt.shapekey_assign_current_group", text="移入当前分组", icon='ADD')
                op_add.node_name = self.name
                op_add.shape_key_name = entry.shape_key_name
            if not has_unassigned:
                sub_box.label(text="     (暂无未分配的形态键)", icon='INFO')
        # --- 底部：全局快捷键设置 ---
        box = layout.box()
        box.label(text="快捷键设置", icon='KEYINGSET')
        box.prop(self, "auto_play_toggle_key", text="自动播放开关")
        box.prop(self, "auto_play_key_global", text="全局生效")

    def _draw_slider_ui(self, layout):
        layout.prop(self, "use_slider_panel", text="启用滑块面板")
        if not self.use_slider_panel:
            return

        box = layout.box()
        box.label(text="滑块面板-自定义", icon='GRIP')
        box.prop(self, "create_cumulative_backup")

        box = layout.box()
        box.label(text="快捷键设置", icon='KEYINGSET')
        col = box.column(align=True)
        col.prop(self, "help_key", text="显示/隐藏面板")
        col.prop(self, "reset_key", text="重置位置")
        col.prop(self, "zoom_in_key", text="放大")
        col.prop(self, "zoom_out_key", text="缩小")
        col.prop(self, "drag_key", text="拖拽键")

        box = layout.box()
        box.label(text="UI尺寸设置", icon='PROPERTIES')
        col = box.column(align=True)
        col.prop(self, "panel_default_scale", text="面板默认缩放")
        col.separator()
        col.prop(self, "slider_height", text="滑块高度")
        col.prop(self, "button_height", text="按钮高度")
        col.prop(self, "panel_min_height", text="面板最小高度")
        col.label(text="背景/滑块/按钮宽度按高度×图片比例自动计算", icon='INFO')

        box = layout.box()
        box.label(text="角色检测设置", icon='VIEWZOOM')
        row = box.row(align=True)
        row.prop_search(self, "target_object", bpy.data, "objects", text="物体", icon='OBJECT_DATA')
        row.operator("ssmt.shapekey_ext_parse_slider_object", text="", icon='FILE_REFRESH').node_name = self.name
        box.prop(self, "detect_hash", text="哈希值")
        box.prop(self, "detect_index_count", text="IndexCount")

        box = layout.box()
        box.label(text="滑块图片资源（自定义）", icon='TEXTURE')
        box.prop(self, "background_image", text="背景")
        box.prop(self, "slider_handle_image", text="滑块")
        box.prop(self, "left_bar_image", text="左进度条")
        box.prop(self, "right_bar_image", text="右进度条")
        box.prop(self, "button_image", text="按钮")
        box.label(text="留空则使用插件内置默认图片", icon='INFO')

        box = layout.box()
        box.label(text="文字按钮样式", icon='COLOR')
        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(self, "button_bg_color", text="背景色")
        row.prop(self, "button_border_color", text="边框色")
        col.prop(self, "button_border_width", text="边框宽度")
        col.prop(self, "button_opacity", text="透明度")
        col.label(text="背景/边框紧贴文字，内边距极小不发糊", icon='INFO')

        box = layout.box()
        box.label(text="面板背景样式（圆角/边框）", icon='MATERIAL')
        col = box.column(align=True)
        col.prop(self, "background_corner_radius", text="背景圆角")
        col.prop(self, "background_border_color", text="边框色")
        col.prop(self, "background_border_width", text="边框宽度")
        col.prop(self, "background_opacity", text="透明度")
        col.label(text="未自定义背景图片时生效", icon='INFO')

    def execute_postprocess(self, mod_export_path, _in_place=False, _ini_path=None):
        """生成 / 原地刷新 形态键扩展配置（含合并的滑块面板）。

        - 导出（_in_place=False）：在 mod ini 中生成配置（带专有标识注释）。
        - 刷新（_in_place=True）：按标识移除本节点旧配置后原地重新生成，无需重新导出整个 mod。
        """
        if _ini_path:
            target_ini_file = _ini_path
        else:
            ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
            if not ini_files:
                return False
            target_ini_file = ini_files[0]

        print(f"[形态键扩展配置] {'原地刷新' if _in_place else '开始执行'}: {target_ini_file}")

        ns = self._ensure_namespace()
        self.last_mod_ini_path = target_ini_file

        # 防重复：仅导出时检查（刷新时按标识替换旧配置）
        if not _in_place:
            try:
                with open(target_ini_file, 'r', encoding='utf-8') as f:
                    if self.DUP_GUARD in f.read():
                        print("形态键扩展配置已存在于文件中。请手动删除后再生成，或用节点上的「应用设置到Mod」原地更新。")
                        return False
            except Exception:
                pass

        if self.create_cumulative_backup:
            self._create_cumulative_backup(target_ini_file, mod_export_path)

        result = self._read_ini_to_ordered_dict(target_ini_file)
        if result is None or not result[0]:
            return False
        sections, preserved_tail_content, preserved_driver_content = result

        if _in_place:
            self._remove_owned_config(sections, ns)
            # 旧格式（无标识）无法自动移除时提示
            if any("形态键扩展配置：组内变量同步" in l for l in sections.get('[Present]', [])):
                print("检测到旧格式的形态键扩展配置（无标识标记）。请先重新导出一次 mod 以生成带标识的新配置。")
                return False

        freq_vars = self._scan_freq_vars_from_ini(sections)
        if not freq_vars and not _in_place:
            return False

        shapekey_names = self._scan_shapekey_names_from_variable_items()
        if not shapekey_names: shapekey_names = self._scan_shapekey_names_from_classification()
        if not shapekey_names: shapekey_names = list(freq_vars.keys())
        var_to_group, all_groups = self._build_var_to_group_map(freq_vars, shapekey_names)
        if '[Constants]' not in sections: sections['[Constants]'] = []
        const_lines = sections['[Constants]']
        # 块标记开始（标识本节点在 Constants 中的配置，供刷新定位）
        const_lines.append(self.BLOCK_BEGIN.format(ns=ns))
        const_content = "\n".join(const_lines)
        for setting in self.play_group_settings:
            if PIL_AVAILABLE and setting.use_remark_as_icon and setting.remark.strip():
                res_dir = os.path.join(mod_export_path, "res")
                os.makedirs(res_dir, exist_ok=True)
                icon_name = f"4_{setting.group_index}.png"
                icon_path = os.path.join(res_dir, icon_name)
                # 仅启用自动播放的分组：带背景/边框；未启用：透明背景仅文字
                generated = self._generate_remark_icon(
                    text=setting.remark,
                    dest_path=icon_path,
                    font_size=setting.remark_font_size,
                    font_family=setting.remark_font_family,
                    text_color=setting.remark_text_color,
                    stroke_width=setting.remark_stroke_width,
                    stroke_color=setting.remark_stroke_color,
                    with_style=bool(setting.enable_auto_playback)
                )
                if generated:
                    comment = f"; SLOT_ICON_{setting.group_index} = {os.path.abspath(generated)}"
                    if comment not in const_lines: const_lines.append(comment)
            elif setting.button_icon_image:
                comment = f"; SLOT_ICON_{setting.group_index} = {setting.button_icon_image}"
                if comment not in const_lines: const_lines.append(comment)
        group_strength_vars = {}
        for g in all_groups:
            group_var = f"$Freq_Group{g}"
            group_strength_vars[g] = group_var
            if group_var not in const_content:
                setting = self._find_group_setting(g)
                remark = f" ({setting.remark})" if (setting and setting.remark) else ""
                const_lines.append(f"; 控制分组{g}{remark}的形态键强度（组内统一）")
                const_lines.append(f"global persist {group_var} = 0.0")
        for g in all_groups:
            setting = self._find_group_setting(g)
            default_val = 1 if (setting and setting.enable_auto_playback) else 0
            auto_enabled_line = f"global persist $auto_play_enabled_group{g} = {default_val}"
            if "$auto_play_enabled_group" + str(g) not in const_content: const_lines.append(auto_enabled_line)
            if setting and setting.enable_auto_playback:
                speed_percent_line = f"global persist $speed_percent_group{g} = {setting.speed_percent}"
                if f"$speed_percent_group{g}" not in const_content: const_lines.append(speed_percent_line)
                speed_min_line = f"global persist $speed_percent_min_group{g} = {setting.speed_percent_min}"
                speed_max_line = f"global persist $speed_percent_max_group{g} = {setting.speed_percent_max}"
                if f"$speed_percent_min_group{g}" not in const_content: const_lines.append(speed_min_line)
                if f"$speed_percent_max_group{g}" not in const_content: const_lines.append(speed_max_line)
                frame_end_line = f"global persist $frameEnd_group{g} = {setting.auto_playback_frame_count}"
                if f"$frameEnd_group{g}" not in const_content: const_lines.append(frame_end_line)
        # 块标记结束
        const_lines.append(self.BLOCK_END.format(ns=ns))

        if '[Present]' not in sections: sections['[Present]'] = []
        present_lines = sections['[Present]']
        first_code_pos = len(present_lines)
        for i, line in enumerate(present_lines):
            stripped = line.strip()
            if stripped and not stripped.startswith(';'):
                first_code_pos = i
                break
        # ---- 组内变量同步块（插入式，带标识注释）----
        sync_lines = [f"; @@ShapeKeyExt:SYNC:{ns}@@", ""]
        sync_lines.append("; ===== 形态键扩展配置：组内变量同步 ===== ")
        for g in all_groups:
            group_var = group_strength_vars[g]
            group_member_vars = [v for v, grp in var_to_group.items() if grp == g]
            if not group_member_vars: continue
            group_member_vars.sort()
            num_members = len(group_member_vars)
            setting = self._find_group_setting(g)
            remark = f" ({setting.remark})" if (setting and setting.remark) else ""
            if setting and setting.group_mode == 'SEQUENCE':
                sync_lines.append(f"; 分组{g}{remark}：序列模式（{num_members} 个形态键依次控制）")
                for idx, member_var in enumerate(group_member_vars):
                    sync_lines.append(f"    {member_var} = {group_var} * {num_members} - {idx}")
                    sync_lines.append(f"    if ({member_var} > 1)")
                    sync_lines.append(f"        {member_var} = 1")
                    sync_lines.append(f"    endif")
                    sync_lines.append(f"    if ({member_var} < 0)")
                    sync_lines.append(f"        {member_var} = 0")
                    sync_lines.append(f"    endif")
            else:
                sync_lines.append(f"; 分组{g}{remark}：同步模式（{group_var} 控制 {num_members} 个形态键）")
                for member_var in group_member_vars:
                    sync_line = f"    {member_var} = {group_var}"
                    if sync_line not in present_lines: sync_lines.append(sync_line)
        sync_lines.append("; ===== 结束组内变量同步 ===== ")
        sync_lines.append("")
        for code_line in reversed(sync_lines): present_lines.insert(first_code_pos, code_line)
        # ---- 自动播放块（插入式，每个启用组一个，带标识注释）----
        for g in all_groups:
            setting = self._find_group_setting(g)
            if not setting or not setting.enable_auto_playback: continue
            if len(setting.speed_intervals) == 0:
                interval = setting.speed_intervals.add()
                interval.start = 0.0
                interval.end = 100.0
                interval.base_step = 10
            self._add_auto_playback_logic_for_group(
                sections, g, group_strength_vars[g],
                setting.auto_playback_frame_count, setting.auto_playback_cycle_mode,
                setting.speed_percent_min, setting.speed_percent_max,
                list(setting.speed_intervals), setting.max_step_unroll
            )

        auto_play_key = self.auto_play_toggle_key.strip() or "space"
        key_section = f"[KeyToggleAutoPlay_{ns}]"
        if key_section not in sections:
            condition = ""
            if not self.auto_play_key_global: condition = "condition = $help == 1 && $ui_active == 1\n"
            sections[key_section] = []
            if condition: sections[key_section].append(condition.rstrip())
            sections[key_section].append(f"key = {auto_play_key}")
            sections[key_section].append("type = press")
            toggle_lines = [f"$auto_play_enabled_group{g} = 1 - $auto_play_enabled_group{g}" for g in all_groups]
            sections[key_section].append(f"run = CommandListToggleAutoPlay_{ns}")
            cmd_section = f"[CommandListToggleAutoPlay_{ns}]"
            if cmd_section not in sections: sections[cmd_section] = toggle_lines
            for g in all_groups:
                cmd_g = f"[CommandListToggleAutoPlayGroup{g}_{ns}]"
                if cmd_g not in sections: sections[cmd_g] = [f"$auto_play_enabled_group{g} = 1 - $auto_play_enabled_group{g}"]
        # 【已合并】滑块面板-自定义
        if self.use_slider_panel:
            self._apply_slider_panel(mod_export_path, target_ini_file, sections)
        self._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)
        print(f"形态键扩展配置已{'原地更新' if _in_place else '合并到'}: {os.path.basename(target_ini_file)}")
        return True


classes = (
    ShapeKeyPlayGroupItem, ShapeKeySpeedIntervalItem, ShapeKeyPlayGroupSettings,
    SSMTNode_PostProcess_ShapeKeyExt, SSMT_OT_ShapeKeyExtSetGroup,
    SSMT_OT_ScanShapeKeyExt, SSMT_OT_SpeedIntervalAdd, SSMT_OT_SpeedIntervalRemove,
    SSMT_OT_SelectPlayGroup, SSMT_OT_OpenGroupSettings, SSMT_OT_ShapeKeyMoveUpDown,
    SSMT_OT_ShapeKeyAssignToCurrentGroup, SSMT_OT_ShapeKeyGroupAdd, 
    SSMT_OT_ShapeKeyGroupRemove, SSMT_OT_ShapeKeyGroupMoveUpDown,
    SSMT_OT_ShapeKeyExt_ParseSliderObject, SSMT_OT_ShapeKeyExt_Refresh,
)

def register():
    for cls in classes: bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
