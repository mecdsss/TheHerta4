import bpy
import os
import glob
import datetime
import shutil

from bpy.types import Node
from bpy.props import StringProperty

from .node_postprocess_base import SSMTNode_PostProcess_Base
from .anim_driver_collector import AnimationDriverCollector

_ANIM_DRIVER_SECTION_MARKER_START = "; --- ANIMATION DRIVER SECTION ---"
_ANIM_DRIVER_SECTION_MARKER_END = "; --- END ANIMATION DRIVER SECTION ---"

_ANIM_DRIVER_BLUEPRINT_BASE_NAME = "动画驱动蓝图"


def _create_unique_anim_driver_blueprint_name() -> str:
    if _ANIM_DRIVER_BLUEPRINT_BASE_NAME not in bpy.data.node_groups:
        return _ANIM_DRIVER_BLUEPRINT_BASE_NAME

    index = 1
    while True:
        name = f"{_ANIM_DRIVER_BLUEPRINT_BASE_NAME}_{index:03d}"
        if name not in bpy.data.node_groups:
            return name
        index += 1


class SSMT_OT_CreateAnimDriverBlueprint(bpy.types.Operator):
    bl_idname = "ssmt.create_anim_driver_blueprint"
    bl_label = "新建动画驱动蓝图"
    bl_description = "创建一个新的动画驱动蓝图，仅允许放置动画驱动节点"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: StringProperty(
        name="Node Name",
        description="关联的后处理节点名称",
        default="",
    )

    def execute(self, context):
        blueprint_name = _create_unique_anim_driver_blueprint_name()
        new_tree = bpy.data.node_groups.new(name=blueprint_name, type='SSMTBlueprintTreeType')
        new_tree.use_fake_user = True
        new_tree["is_animation_driver"] = True

        if self.node_name:
            for tree in bpy.data.node_groups:
                if tree.bl_idname != 'SSMTBlueprintTreeType':
                    continue
                for node in tree.nodes:
                    if node.name == self.node_name and hasattr(node, 'blueprint_name'):
                        node.blueprint_name = blueprint_name
                        node.label = f"动画驱动: {blueprint_name}"
                        break

        self.report({'INFO'}, f"已创建动画驱动蓝图: {blueprint_name}")
        return {'FINISHED'}


class SSMTNode_PostProcess_AnimDriver(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_AnimDriver'
    bl_label = '动画驱动蓝图'
    bl_description = '引用动画驱动蓝图，将其收集构建的INI配置插入到配置表最上方'
    bl_icon = 'ACTION'

    def update_blueprint_name(self, context):
        if self.blueprint_name and self.blueprint_name != 'NONE':
            self.label = f"动画驱动: {self.blueprint_name}"
        else:
            self.label = "动画驱动蓝图"
        self.update_node_width([self.blueprint_name])

    blueprint_name: StringProperty(
        name="蓝图名称",
        description="选择要引用的动画驱动蓝图",
        default="",
        update=update_blueprint_name,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketPostProcess', "Input")
        self.width = 300

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)
        row.prop_search(self, "blueprint_name", bpy.data, "node_groups", text="", icon='NODETREE')

        op = row.operator("ssmt.create_anim_driver_blueprint", text="", icon='ADD')
        op.node_name = self.name

        if self.blueprint_name and self.blueprint_name != 'NONE':
            blueprint = bpy.data.node_groups.get(self.blueprint_name)
            if blueprint and blueprint.bl_idname == 'SSMTBlueprintTreeType' and blueprint.get("is_animation_driver"):
                box = layout.box()
                box.label(text=f"节点数: {len(blueprint.nodes)}", icon='NODE')
                box.label(text=f"连接数: {len(blueprint.links)}", icon='LINKED')

                driver_node_count = sum(
                    1 for n in blueprint.nodes
                    if hasattr(n, "generate_ini_segment") and callable(n.generate_ini_segment)
                    and n.bl_idname != 'SSMTNode_AnimDriver_Base'
                )
                box.label(text=f"驱动节点: {driver_node_count}", icon='ACTION')

                if driver_node_count > 0:
                    collector = AnimationDriverCollector(blueprint)
                    paragraph_count = collector.count_paragraphs()
                    box.label(text=f"段落数: {paragraph_count}", icon='FILE_TEXT')

                box.separator()
                row = box.row(align=True)
                row.operator("ssmt.blueprint_nest_navigate", text="进入动画驱动蓝图", icon='FORWARD')
            elif blueprint:
                box = layout.box()
                box.label(text="警告: 选中的不是SSMT蓝图", icon='ERROR')
            else:
                box = layout.box()
                box.label(text="警告: 蓝图不存在", icon='ERROR')

    def execute_postprocess(self, mod_export_path):
        print(f"动画驱动蓝图后处理节点开始执行，Mod导出路径: {mod_export_path}")

        if not self.blueprint_name or self.blueprint_name == 'NONE':
            print("警告: 未选择动画驱动蓝图")
            return

        blueprint = bpy.data.node_groups.get(self.blueprint_name)
        if not blueprint or blueprint.bl_idname != 'SSMTBlueprintTreeType':
            print(f"错误: 蓝图 '{self.blueprint_name}' 不存在或不是SSMT蓝图")
            return

        if not blueprint.get("is_animation_driver"):
            print(f"错误: 蓝图 '{self.blueprint_name}' 不是动画驱动蓝图")
            return

        # 在 execute 上下文中修正所有驱动节点的 auto_index
        for node in blueprint.nodes:
            if hasattr(node, '_ensure_valid_index') and callable(node._ensure_valid_index):
                try:
                    node._ensure_valid_index()
                except Exception:
                    pass

        collector = AnimationDriverCollector(blueprint)
        paragraphs = collector.collect()

        if not paragraphs:
            print(f"动画驱动蓝图 '{self.blueprint_name}' 中没有找到动画驱动节点")
            return

        ini_content = self._build_ini_content(paragraphs)

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            print("路径中未找到任何.ini文件")
            return

        target_ini_file = ini_files[0]
        self._create_cumulative_backup(target_ini_file, mod_export_path)

        try:
            with open(target_ini_file, 'r', encoding='utf-8') as f:
                original_content = f.read()
        except Exception as e:
            print(f"读取目标INI文件失败: {e}")
            return

        if _ANIM_DRIVER_SECTION_MARKER_START in original_content:
            start_idx = original_content.find(_ANIM_DRIVER_SECTION_MARKER_START)
            end_idx = original_content.find(_ANIM_DRIVER_SECTION_MARKER_END, start_idx)
            if end_idx != -1:
                end_idx += len(_ANIM_DRIVER_SECTION_MARKER_END)
                remaining = original_content[:start_idx] + original_content[end_idx:]
            else:
                remaining = original_content[:start_idx]
            original_content = remaining
            print("检测到旧动画驱动段，已自动覆盖")

        base_content, tail_content = SSMTNode_PostProcess_Base.split_auto_appended_tail_content(original_content)

        new_content = f"{ini_content}\n\n{base_content}"
        if tail_content:
            new_content = f"{new_content}\n\n{tail_content}"

        try:
            with open(target_ini_file, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"动画驱动INI配置已插入到: {os.path.basename(target_ini_file)}")
            print(f"段落数: {len(paragraphs)}")
        except Exception as e:
            print(f"写入INI文件失败: {e}")

    def _build_ini_content(self, paragraphs) -> str:
        lines = [
            _ANIM_DRIVER_SECTION_MARKER_START,
            "; ==============================================================================",
            "; 动画驱动配置 (自动生成)",
            "; ==============================================================================",
            "",
        ]

        for paragraph in paragraphs:
            para_content = paragraph["ini_content"]
            if para_content:
                lines.append(para_content)
                lines.append("")
                lines.append("; ------------------------------------------------------------------------------")
                lines.append("")

        lines.append(_ANIM_DRIVER_SECTION_MARKER_END)
        return "\n".join(lines)

    def _create_cumulative_backup(self, ini_file_path, mod_export_path):
        try:
            if not os.path.exists(ini_file_path):
                print(f"文件不存在，跳过备份: {ini_file_path}")
                return

            backup_dir = os.path.join(mod_export_path, "Backups")
            os.makedirs(backup_dir, exist_ok=True)

            base_filename = os.path.basename(ini_file_path)
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_filename = f"{base_filename}.{timestamp}.bak"
            backup_path = os.path.join(backup_dir, backup_filename)

            shutil.copy2(ini_file_path, backup_path)
            print(f"已创建备份: {backup_path}")
        except Exception as e:
            print(f"创建备份失败: {e}")


classes = (
    SSMT_OT_CreateAnimDriverBlueprint,
    SSMTNode_PostProcess_AnimDriver,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
