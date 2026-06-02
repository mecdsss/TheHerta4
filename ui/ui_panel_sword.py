import bpy
import hashlib
import os
from bpy.props import StringProperty, CollectionProperty, IntProperty, BoolProperty, EnumProperty
from bpy.types import Operator, Panel, PropertyGroup, UIList
from bpy_extras.io_utils import ImportHelper
import bpy.utils.previews

from ..common.mesh_import_helper import MigotoBinaryFile, MeshImportHelper
from ..common.global_config import GlobalConfig
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.workspace_helper import WorkSpaceHelper

from ..utils.translate_utils import TR
from ..utils.collection_utils import CollectionUtils, CollectionColor


preview_collections = {}
sword_reversed_workspace_items_cache = []
sword_workspace_lod_items_cache = []
_sword_reversed_safe_identifier_map: dict[str, str] = {}


def _sword_make_safe_identifier(name: str) -> str:
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:16]


def _sword_resolve_reversed_identifier(identifier: str) -> str:
    return _sword_reversed_safe_identifier_map.get(identifier, identifier)


def _load_preview_images(context, folder_path: str, target_collection_name: str = "sword_image_list") -> int:
    image_collection = getattr(context.scene, target_collection_name)
    image_collection.clear()

    pcoll = preview_collections["main"]
    pcoll.clear()

    image_extensions = ('.jpg', '.jpeg', '.png', '.tiff', '.bmp', '.tga', '.exr', '.hdr', '.dds')
    image_count = 0

    for filename in os.listdir(folder_path):
        if not filename.lower().endswith(image_extensions):
            continue

        full_path = os.path.join(folder_path, filename)
        if not os.path.isfile(full_path):
            continue

        item = image_collection.add()
        item.name = filename
        item.filepath = full_path

        try:
            pcoll.load(filename, full_path, 'IMAGE')
            image_count += 1
        except Exception as exc:
            print(f"Could not load preview for {filename}: {exc}")

    return image_count


def _get_component_texture_folder(selected_obj_name: str) -> tuple[str, str]:
    prefix_info = ObjectPrefixHelper.extract_prefix_info(selected_obj_name)
    prefix = prefix_info[0] if prefix_info else ""
    prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix)
    draw_ib = str(prefix_parts.get("draw_ib", "") or "").strip()

    candidate_base_paths = []
    if prefix:
        candidate_base_paths.append(WorkSpaceHelper.get_submesh_folder_path(prefix))
    if draw_ib:
        candidate_base_paths.append(os.path.join(GlobalConfig.path_workspace_folder(), draw_ib + "\\"))

    seen_paths = set()
    for base_path in candidate_base_paths:
        normalized_base_path = os.path.normpath(base_path)
        if normalized_base_path in seen_paths:
            continue
        seen_paths.add(normalized_base_path)

        candidates = [
            ("DedupedTextures_jpg", os.path.join(base_path, "DedupedTextures_jpg\\")),
            ("DedupedTextures_png", os.path.join(base_path, "DedupedTextures_png\\")),
            ("DedupedTextures_tga", os.path.join(base_path, "DedupedTextures_tga\\")),
        ]

        for folder_name, folder_path in candidates:
            if os.path.exists(folder_path):
                return folder_path, folder_name

    return "", ""


def _get_sword_workspace_lod_items(self, context):
    global sword_workspace_lod_items_cache

    try:
        GlobalConfig.read_from_main_json_ssmt4()
        lod_folder_paths = WorkSpaceHelper.get_lod_folderpath_list()
        sword_workspace_lod_items_cache = [
            ("", "工作空间根目录", "读取工作空间根目录下的 DedupedTextures"),
        ]
        sword_workspace_lod_items_cache.extend(
            [(os.path.basename(path), os.path.basename(path), "") for path in lod_folder_paths]
        )
        return sword_workspace_lod_items_cache
    except Exception:
        sword_workspace_lod_items_cache = [
            ("", "工作空间根目录", "读取 LOD 列表失败，回退到根目录"),
        ]
        return sword_workspace_lod_items_cache


def get_workspace_preview_texture_folder(lod_name: str = ""):
    GlobalConfig.read_from_main_json_ssmt4()

    workspace_folder_path = GlobalConfig.path_workspace_folder()
    preferred_folders = []
    if bpy.app.version <= (4, 2, 0):
        preferred_folders.extend(["DedupedTextures_jpg", "DedupedTextures_png", "DedupedTextures_tga", "DedupedTextures"])
    else:
        preferred_folders.extend(["DedupedTextures", "DedupedTextures_jpg", "DedupedTextures_png", "DedupedTextures_tga"])

    candidate_base_paths = []
    if lod_name:
        candidate_base_paths.append(os.path.join(workspace_folder_path, lod_name))
    candidate_base_paths.append(workspace_folder_path)

    seen_paths = set()
    for base_path in candidate_base_paths:
        normalized_base_path = os.path.normpath(base_path)
        if normalized_base_path in seen_paths:
            continue
        seen_paths.add(normalized_base_path)

        for folder_name in preferred_folders:
            preview_folder_path = os.path.join(base_path, folder_name + "\\")
            if os.path.exists(preview_folder_path):
                return preview_folder_path, folder_name

    return "", preferred_folders[0]


def _get_sword_reversed_workspace_items(self, context):
    global sword_reversed_workspace_items_cache

    try:
        GlobalConfig.read_from_main_json_ssmt4()
        reversed_root = os.path.join(GlobalConfig.ssmtlocation, "Reversed")
        if not reversed_root or not os.path.isdir(reversed_root):
            _sword_reversed_safe_identifier_map.clear()
            sword_reversed_workspace_items_cache = [
                ("", "当前没有可用逆向工作空间", "请确认 SSMT 缓存目录下存在 Reversed 文件夹"),
            ]
            return sword_reversed_workspace_items_cache

        folder_names = sorted(
            [entry.name for entry in os.scandir(reversed_root) if entry.is_dir()]
        )
        if not folder_names:
            _sword_reversed_safe_identifier_map.clear()
            sword_reversed_workspace_items_cache = [
                ("", "当前没有可用逆向工作空间", "Reversed 文件夹下未找到子文件夹"),
            ]
            return sword_reversed_workspace_items_cache

        _sword_reversed_safe_identifier_map.clear()
        items = []
        for name in folder_names:
            identifier = _sword_make_safe_identifier(name)
            _sword_reversed_safe_identifier_map[identifier] = name
            items.append((identifier, name, ""))
        sword_reversed_workspace_items_cache = items
        return sword_reversed_workspace_items_cache
    except Exception:
        _sword_reversed_safe_identifier_map.clear()
        sword_reversed_workspace_items_cache = [
            ("", "当前没有可用逆向工作空间", "读取 Reversed 文件夹失败"),
        ]
        return sword_reversed_workspace_items_cache


class Sword_ImportTexture_ImageListItem(PropertyGroup):
    name: StringProperty(name="Image Name") # type: ignore
    filepath: StringProperty(name="File Path") # type: ignore


class SWORD_UL_FastImportTextureList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        pcoll = preview_collections["main"]

        if self.layout_type in {'DEFAULT', 'Expand'}:
            if item.name in pcoll:
                layout.template_icon(icon_value=pcoll[item.name].icon_id, scale=1.0)
            else:
                layout.label(text="", icon='IMAGE_DATA')
            layout.label(text=item.name)

        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            if item.name in pcoll:
                layout.template_icon(icon_value=pcoll[item.name].icon_id, scale=6.0)
            else:
                layout.label(text="", icon='IMAGE_DATA')


class Sword_ImportTexture_WM_OT_SelectImageFolder(Operator, ImportHelper):
    bl_idname = "wm.select_image_folder"
    bl_label = TR.translate("选择预览贴图所在的文件夹位置")

    directory: StringProperty(subtype='DIR_PATH') # type: ignore
    filter_folder: BoolProperty(default=True, options={'HIDDEN'}) # type: ignore
    filter_image: BoolProperty(default=False, options={'HIDDEN'}) # type: ignore

    def execute(self, context):
        image_count = _load_preview_images(context, self.directory)
        self.report({'INFO'}, f"Scanned {image_count} images.")
        return {'FINISHED'}


class Sword_ImportTexture_WM_OT_AutoDetectWorkspaceTextureFolder(Operator):
    bl_idname = "ssmt.auto_detect_workspace_texture_folder"
    bl_label = "读取工作空间 DedupedTextures"

    def execute(self, context):
        lod_name = str(getattr(context.scene, "sword_workspace_preview_lod", "") or "").strip()
        deduped_textures_folder_path, folder_name = get_workspace_preview_texture_folder(lod_name=lod_name)

        if not deduped_textures_folder_path:
            self.report({'ERROR'}, f"未找到当前工作空间下的 {folder_name} 文件夹")
            return {'CANCELLED'}

        image_count = _load_preview_images(context, deduped_textures_folder_path)
        self.report({'INFO'}, f"已从当前工作空间的 {folder_name} 文件夹加载 {image_count} 张图片。")
        return {'FINISHED'}


class Sword_ImportTexture_WM_OT_AutoDetectTextureFolder(Operator):
    bl_idname = "wm.auto_detect_texture_folder"
    bl_label = TR.translate("自动检测提取的贴图文件夹")

    def execute(self, context):
        selected_objects = context.selected_objects
        if not selected_objects:
            self.report({'ERROR'}, "No objects selected.")
            return {'CANCELLED'}

        obj = selected_objects[0]
        deduped_folder_path, folder_name = _get_component_texture_folder(obj.name)
        if not deduped_folder_path:
            prefix_info = ObjectPrefixHelper.extract_prefix_info(obj.name)
            prefix = prefix_info[0] if prefix_info else obj.name
            draw_ib = ObjectPrefixHelper.parse_prefix_parts(prefix).get("draw_ib", "") or prefix
            self.report({'ERROR'}, TR.translate("未找到当前DrawIB: " + str(draw_ib) + "的DedupedTextures转换后的贴图文件夹，请确保此IB在当前工作空间中已经正常提取出来了"))
            return {'CANCELLED'}

        image_count = _load_preview_images(context, deduped_folder_path)
        self.report({'INFO'}, f"Auto-detected and loaded {image_count} images from {folder_name}.")
        return {'FINISHED'}


class SWORD4RefreshWorkspaceLODList(bpy.types.Operator):
    bl_idname = "ssmt4.sword_refresh_workspace_lod_list"
    bl_label = "Refresh workspace preview LOD list"
    bl_description = "Refresh available workspace LOD folders for texture preview"

    def execute(self, context):
        GlobalConfig.read_from_main_json_ssmt4()
        _get_sword_workspace_lod_items(self, context)

        valid_values = {item[0] for item in sword_workspace_lod_items_cache}
        if getattr(context.scene, "sword_workspace_preview_lod", "") not in valid_values:
            context.scene.sword_workspace_preview_lod = ""

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

        self.report({'INFO'}, "Workspace preview LOD list refreshed.")
        return {'FINISHED'}


def reload_textures_from_folder(picture_folder_path: str):
    return _load_preview_images(bpy.context, picture_folder_path)


class Sword_ImportTexture_WM_OT_ApplyImageToMaterial(Operator):
    bl_idname = "wm.apply_image_to_material"
    bl_label = "应用贴图到选中的物体"

    def execute(self, context):
        scene = context.scene
        selected_index = scene.sword_image_list_index

        if selected_index < 0 or selected_index >= len(scene.sword_image_list):
            self.report({'ERROR'}, "No image selected in the list.")
            return {'CANCELLED'}

        selected_image = scene.sword_image_list[selected_index]
        image_path = selected_image.filepath
        image_data = bpy.data.images.load(image_path, check_existing=True)
        image_data.colorspace_settings.name = 'sRGB'
        image_data.alpha_mode = 'CHANNEL_PACKED'

        selected_objects = context.selected_objects
        if not selected_objects:
            self.report({'ERROR'}, "No objects selected.")
            return {'CANCELLED'}

        applied_count = 0
        for obj in selected_objects:
            if obj.type != 'MESH':
                continue

            if not obj.data.materials:
                mat = bpy.data.materials.new(name=f"Mat_{selected_image.name}")
                obj.data.materials.append(mat)
            else:
                mat = obj.data.materials[0]
                if mat is None:
                    mat = bpy.data.materials.new(name=f"Mat_{selected_image.name}")
                    obj.data.materials[0] = mat

            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            nodes.clear()

            tex_image = nodes.new('ShaderNodeTexImage')
            tex_image.image = image_data
            tex_image.location = (-500, 0)

            transparent_node = nodes.new(type='ShaderNodeBsdfTransparent')
            transparent_node.location = (-220, 120)

            diffuse_node = nodes.new(type='ShaderNodeBsdfDiffuse')
            diffuse_node.location = (-220, -80)

            mix_shader = nodes.new(type='ShaderNodeMixShader')
            mix_shader.location = (20, 0)

            output_node = nodes.new(type='ShaderNodeOutputMaterial')
            output_node.location = (260, 0)

            mat.blend_method = 'BLEND'
            if hasattr(mat, "use_transparency_overlap"):
                mat.use_transparency_overlap = False
            elif hasattr(mat, "show_transparent_back"):
                mat.show_transparent_back = False
            links.new(tex_image.outputs['Color'], diffuse_node.inputs['Color'])
            links.new(tex_image.outputs['Alpha'], mix_shader.inputs['Fac'])
            links.new(transparent_node.outputs['BSDF'], mix_shader.inputs[1])
            links.new(diffuse_node.outputs['BSDF'], mix_shader.inputs[2])
            links.new(mix_shader.outputs['Shader'], output_node.inputs['Surface'])
            applied_count += 1

        self.report({'INFO'}, f"Applied {selected_image.name} to {applied_count} object(s).")
        return {'FINISHED'}


class SwordImportAllReversed(bpy.types.Operator):
    bl_idname = "ssmt.import_all_reverse"
    bl_label = TR.translate("一键导入逆向出来的全部模型")
    bl_description = "把上一次一键逆向出来的所有模型全部导入到 Blender，然后你可以手动筛选并删除错误的数据类型，流程上更加方便。"

    def _resolve_reverse_output_folder_path(self, context):
        GlobalConfig.read_from_main_json_ssmt4()

        source_mode = context.scene.sword_reverse_source_mode
        if source_mode == "SPECIFIC":
            selected_workspace_name = context.scene.sword_specific_reversed_workspace_name
            if not selected_workspace_name:
                self.report({"ERROR"}, "当前未选择指定工作空间，请先选择 Reversed 下的子文件夹")
                return ""
            resolved_name = _sword_resolve_reversed_identifier(selected_workspace_name)
            return os.path.join(GlobalConfig.ssmtlocation, "Reversed", resolved_name)

        if source_mode == "CUSTOM":
            custom_folder_path = str(context.scene.sword_custom_reverse_output_folder_path).strip()
            if not custom_folder_path:
                self.report({"ERROR"}, "自定义目录为空，请先选择目录")
                return ""
            return custom_folder_path

        return GlobalConfig.path_reverse_output_folder()

    def execute(self, context):
        reverse_output_folder_path = self._resolve_reverse_output_folder_path(context)
        if not reverse_output_folder_path:
            return {'FINISHED'}

        if not os.path.exists(reverse_output_folder_path) or not os.path.isdir(reverse_output_folder_path):
            self.report({"ERROR"}, "当前一键逆向结果中标注的文件夹位置不存在，请重新运行一键逆向")
            return {'FINISHED'}

        total_folder_name = os.path.basename(reverse_output_folder_path)
        reverse_collection = CollectionUtils.create_new_collection(collection_name=total_folder_name, color_tag=CollectionColor.Red)
        bpy.context.scene.collection.children.link(reverse_collection)

        subfolder_path_list = [f.path for f in os.scandir(reverse_output_folder_path) if f.is_dir()]
        if not subfolder_path_list:
            self.report({"ERROR"}, "目标目录下未找到可导入的子文件夹")
            return {'FINISHED'}

        for subfolder_path in subfolder_path_list:
            datatype_folder_name = os.path.basename(subfolder_path)
            datatype_collection = CollectionUtils.create_new_collection(
                collection_name=datatype_folder_name,
                color_tag=CollectionColor.White,
                link_to_parent_collection_name=reverse_collection.name,
            )

            fmt_files = []
            for file in os.listdir(subfolder_path):
                if file.endswith('.fmt'):
                    fmt_files.append(os.path.join(subfolder_path, file))

            for fmt_filepath in fmt_files:
                filename_with_extension = os.path.basename(fmt_filepath)
                filename_without_extension = os.path.splitext(filename_with_extension)[0]
                try:
                    mbf = MigotoBinaryFile(fmt_path=fmt_filepath, mesh_name=filename_without_extension)
                    MeshImportHelper.create_mesh_obj_from_mbf(mbf=mbf, import_collection=datatype_collection)
                except Exception as exc:
                    error_msg = f"导入失败，已跳过: {fmt_filepath} | 错误: {exc}"
                    print(error_msg)
                    self.report({'WARNING'}, error_msg)
                    continue

        reload_textures_from_folder(reverse_output_folder_path)
        return {'FINISHED'}


class SWORD4RefreshReversedWorkspaceList(bpy.types.Operator):
    bl_idname = "ssmt4.sword_refresh_reversed_workspace_list"
    bl_label = "刷新逆向工作空间列表"
    bl_description = "刷新当前 SSMT 缓存目录中 Reversed 文件夹的子文件夹列表"

    def execute(self, context):
        GlobalConfig.read_from_main_json_ssmt4()

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

        self.report({'INFO'}, "已刷新逆向工作空间列表")
        return {'FINISHED'}


class Sword_ExtractSubmesh(bpy.types.Operator):
    bl_idname = "mesh.extract_submesh"
    bl_label = "提取子网格"
    bl_description = "根据起始索引和索引数量提取子网格"
    bl_options = {'REGISTER', 'UNDO'}

    start_index: IntProperty(
        name="起始索引",
        description="起始顶点索引",
        default=0,
        min=0
    ) # type: ignore

    index_count: IntProperty(
        name="索引数量",
        description="要提取的索引数量",
        default=0,
        min=0
    ) # type: ignore

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "请选择一个网格物体")
            return {'CANCELLED'}

        if self.index_count <= 0:
            self.report({'ERROR'}, "索引数量必须大于 0")
            return {'CANCELLED'}

        self.report({'WARNING'}, "此功能尚未完全实现")
        return {'FINISHED'}


class Sword_ImportTexture_VIEW3D_PT_ImageMaterialPanel(Panel):
    bl_label = "3Dmigoto-Sword面板"
    bl_idname = "VIEW3D_PT_image_material_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sword'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        reverse_box = layout.box()
        reverse_box.label(text="逆向导入", icon='IMPORT')
        reverse_box.prop(scene, "sword_reverse_source_mode")
        if scene.sword_reverse_source_mode == "SPECIFIC":
            reversed_workspace_row = reverse_box.row(align=True)
            reversed_workspace_row.prop(scene, "sword_specific_reversed_workspace_name", text="指定工作空间")
            reversed_workspace_row.operator(SWORD4RefreshReversedWorkspaceList.bl_idname, text="", icon='FILE_REFRESH')
        elif scene.sword_reverse_source_mode == "CUSTOM":
            reverse_box.prop(scene, "sword_custom_reverse_output_folder_path", text="自定义目录")

        reverse_box.operator("ssmt.import_all_reverse", icon='IMPORT')
        reverse_box.operator("import_mesh.migoto_raw_buffers_mmt", icon='IMPORT')

        texture_box = layout.box()
        texture_box.label(text="快速贴图预览", icon='TEXTURE')
        lod_row = texture_box.row(align=True)
        lod_row.prop(scene, "sword_workspace_preview_lod", text="Workspace LOD")
        lod_row.operator(SWORD4RefreshWorkspaceLODList.bl_idname, text="", icon='FILE_REFRESH')

        button_row = texture_box.row(align=True)
        button_row.operator("ssmt.auto_detect_workspace_texture_folder", icon='FILE_FOLDER', text="工作空间贴图")
        button_row.operator("wm.auto_detect_texture_folder", icon='OUTLINER_OB_IMAGE', text="当前组件贴图")
        button_row.operator("wm.select_image_folder", icon='FILEBROWSER', text="手动选目录")

        if scene.sword_image_list:
            texture_box.label(text=f"Found {len(scene.sword_image_list)} images")
            row = texture_box.row()
            row.template_list(
                "SWORD_UL_FastImportTextureList",
                "Image List",
                scene,
                "sword_image_list",
                scene,
                "sword_image_list_index",
                rows=6
            )
        else:
            texture_box.label(text="No images found. Load a folder first.")

        texture_box.operator("wm.apply_image_to_material", icon='MATERIAL_DATA')

        if scene.sword_image_list and 0 <= scene.sword_image_list_index < len(scene.sword_image_list):
            selected_item = scene.sword_image_list[scene.sword_image_list_index]
            pcoll = preview_collections["main"]

            if selected_item.name in pcoll:
                box = texture_box.box()
                box.label(text="Preview:")
                box.template_icon(icon_value=pcoll[selected_item.name].icon_id, scale=10.0)


class Sword_SplitModel_By_DrawIndexed_Panel(Panel):
    bl_label = "手动逆向后根据DrawIndexed值分割模型"
    bl_idname = "VIEW3D_PT_Sword_SplitModel_By_DrawIndexed_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sword'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "submesh_start")
        layout.prop(scene, "submesh_count")

        op = layout.operator("mesh.extract_submesh")
        op.start_index = scene.submesh_start
        op.index_count = scene.submesh_count


def register():
    pcoll = bpy.utils.previews.new()
    preview_collections["main"] = pcoll

    bpy.utils.register_class(Sword_ImportTexture_ImageListItem)
    bpy.utils.register_class(SWORD_UL_FastImportTextureList)
    bpy.utils.register_class(Sword_ImportTexture_WM_OT_SelectImageFolder)
    bpy.utils.register_class(Sword_ImportTexture_WM_OT_AutoDetectWorkspaceTextureFolder)
    bpy.utils.register_class(Sword_ImportTexture_WM_OT_AutoDetectTextureFolder)
    bpy.utils.register_class(Sword_ImportTexture_WM_OT_ApplyImageToMaterial)
    bpy.utils.register_class(SwordImportAllReversed)
    bpy.utils.register_class(SWORD4RefreshReversedWorkspaceList)
    bpy.utils.register_class(SWORD4RefreshWorkspaceLODList)
    bpy.utils.register_class(Sword_ExtractSubmesh)
    bpy.utils.register_class(Sword_ImportTexture_VIEW3D_PT_ImageMaterialPanel)
    bpy.utils.register_class(Sword_SplitModel_By_DrawIndexed_Panel)

    bpy.types.Scene.sword_image_list = CollectionProperty(type=Sword_ImportTexture_ImageListItem)
    bpy.types.Scene.sword_image_list_index = IntProperty(default=0)
    bpy.types.Scene.sword_workspace_preview_lod = EnumProperty(
        name="Workspace LOD",
        description="Select the LOD folder used for workspace texture preview, empty means workspace root",
        items=_get_sword_workspace_lod_items,
    )
    bpy.types.Scene.submesh_start = IntProperty(name="起始索引", default=0, min=0)
    bpy.types.Scene.submesh_count = IntProperty(name="索引数量", default=0, min=0)
    bpy.types.Scene.sword_reverse_source_mode = EnumProperty(
        name="导入模式",
        description="控制一键导入逆向结果时的目录来源",
        items=[
            ("LAST", "上次逆向结果", "使用全局配置中记录的上次逆向输出目录"),
            ("SPECIFIC", "指定工作空间", "使用 SSMT 缓存目录中 Reversed 下指定的子文件夹"),
            ("CUSTOM", "自定义目录", "使用你手动指定的目录"),
        ],
        default="LAST",
    )
    bpy.types.Scene.sword_specific_reversed_workspace_name = EnumProperty(
        name="指定工作空间",
        description="当前 SSMT 缓存目录中 Reversed 的子文件夹列表",
        items=_get_sword_reversed_workspace_items,
    )
    bpy.types.Scene.sword_custom_reverse_output_folder_path = StringProperty(
        name="自定义目录",
        description="手动指定用于一键导入逆向结果的目录",
        default="",
        subtype='DIR_PATH',
    )


def unregister():
    try:
        del bpy.types.Scene.sword_image_list
        del bpy.types.Scene.sword_image_list_index
        del bpy.types.Scene.sword_workspace_preview_lod
        del bpy.types.Scene.submesh_start
        del bpy.types.Scene.submesh_count
        del bpy.types.Scene.sword_reverse_source_mode
        del bpy.types.Scene.sword_specific_reversed_workspace_name
        del bpy.types.Scene.sword_custom_reverse_output_folder_path
    except Exception:
        pass

    for pcoll in preview_collections.values():
        try:
            bpy.utils.previews.remove(pcoll)
        except Exception:
            pass
    preview_collections.clear()

    bpy.utils.unregister_class(Sword_SplitModel_By_DrawIndexed_Panel)
    bpy.utils.unregister_class(Sword_ImportTexture_VIEW3D_PT_ImageMaterialPanel)
    bpy.utils.unregister_class(Sword_ExtractSubmesh)
    bpy.utils.unregister_class(SWORD4RefreshWorkspaceLODList)
    bpy.utils.unregister_class(SWORD4RefreshReversedWorkspaceList)
    bpy.utils.unregister_class(SwordImportAllReversed)
    bpy.utils.unregister_class(Sword_ImportTexture_WM_OT_ApplyImageToMaterial)
    bpy.utils.unregister_class(Sword_ImportTexture_WM_OT_AutoDetectTextureFolder)
    bpy.utils.unregister_class(Sword_ImportTexture_WM_OT_AutoDetectWorkspaceTextureFolder)
    bpy.utils.unregister_class(Sword_ImportTexture_WM_OT_SelectImageFolder)
    bpy.utils.unregister_class(SWORD_UL_FastImportTextureList)
    bpy.utils.unregister_class(Sword_ImportTexture_ImageListItem)
