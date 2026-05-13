
'''
导入模型配置面板
'''
import os

import bpy
from bpy_extras.io_utils import ImportHelper

from ..utils.collection_utils import CollectionColor, CollectionUtils
from ..utils.json_utils import JsonUtils
from ..utils.timer_utils import TimerUtils
from ..utils.translate_utils import TR
from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.non_mirror_workflow import NonMirrorWorkflowHelper
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.ssmt_import_helper import SSMTImportHelper
from ..common.workspace_helper import WorkSpaceHelper
from .ui_prefix_quick_ops import PrefixQuickOpsHelper


def _build_workspace_import_targets(workspace_collection):
    partition_folder_paths = WorkSpaceHelper.get_workspace_partition_folderpath_list()
    target_base_paths = partition_folder_paths or [GlobalConfig.path_workspace_folder()]

    for base_path in target_base_paths:
        base_name = os.path.basename(os.path.normpath(base_path))
        base_collection = workspace_collection
        if partition_folder_paths:
            base_collection = CollectionUtils.create_new_collection(
                collection_name=base_name,
                color_tag=CollectionColor.Orange,
            )
            workspace_collection.children.link(base_collection)

        lod_submesh_dict = WorkSpaceHelper.get_lod_submesh_folderpath_dict(base_path)
        if lod_submesh_dict:
            for lod_name, submesh_folder_paths in lod_submesh_dict.items():
                lod_collection = CollectionUtils.create_new_collection(
                    collection_name=lod_name,
                    color_tag=CollectionColor.Blue,
                )
                base_collection.children.link(lod_collection)

                lod_folder_path = os.path.join(base_path, lod_name)
                drawib_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict_for_path(base_path)
                lod_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict_for_path(lod_folder_path)
                drawib_aliasname_dict.update(lod_aliasname_dict)

                for submesh_folder_path in submesh_folder_paths:
                    submesh_folder_name = os.path.basename(submesh_folder_path)
                    parts = submesh_folder_name.split("-")
                    yield {
                        "import_key": lod_name + "." + submesh_folder_name,
                        "submesh_folder_path": submesh_folder_path,
                        "submesh_folder_name": submesh_folder_name,
                        "display_name": WorkSpaceHelper._compose_lod_name(
                            lod_name,
                            WorkSpaceHelper.get_display_submesh_name(
                                submesh_folder_name,
                                drawib_aliasname_dict=drawib_aliasname_dict,
                            ),
                        ),
                        "alias_name": WorkSpaceHelper.get_object_display_name(
                            submesh_folder_name,
                            drawib_aliasname_dict=drawib_aliasname_dict,
                        ),
                        "draw_ib": parts[0] if len(parts) >= 1 else "",
                        "component": parts[1] if len(parts) >= 2 else "1",
                        "import_collection": lod_collection,
                    }
            continue

        drawib_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict_for_path(base_path)
        for submesh_folder_path in WorkSpaceHelper._get_submesh_folderpath_list_from(base_path):
            submesh_folder_name = os.path.basename(submesh_folder_path)
            parts = submesh_folder_name.split("-")
            yield {
                "import_key": submesh_folder_name,
                "submesh_folder_path": submesh_folder_path,
                "submesh_folder_name": submesh_folder_name,
                "display_name": WorkSpaceHelper.get_display_submesh_name(
                    submesh_folder_name,
                    drawib_aliasname_dict=drawib_aliasname_dict,
                ),
                "alias_name": WorkSpaceHelper.get_object_display_name(
                    submesh_folder_name,
                    drawib_aliasname_dict=drawib_aliasname_dict,
                ),
                "draw_ib": parts[0] if len(parts) >= 1 else "",
                "component": parts[1] if len(parts) >= 2 else "1",
                "import_collection": base_collection,
            }


def ImprotFromWorkSpaceFull(self, context):
    workspace_collection = WorkSpaceHelper.create_and_get_workspace_collection()
    import_targets = list(_build_workspace_import_targets(workspace_collection))
    if not import_targets:
        self.report({'ERROR'}, "当前工作空间未找到可导入的子模型目录。")
        return

    foldername_gametypename_dict = {}
    imported_objects = []
    import_records = []

    for target in import_targets:
        submesh_folder_name = target["submesh_folder_name"]
        print("Import FolderName: " + target["import_key"])

        final_import_folder_path_list = WorkSpaceHelper.get_ordered_gpu_cpu_import_folderpath_list(
            target["submesh_folder_path"],
        )
        print("Final Import Folder Path List: " + str(final_import_folder_path_list))

        for import_folder_path in final_import_folder_path_list:
            gametype_name = import_folder_path.split("TYPE_")[1]

            try:
                print("尝试导入路径: " + import_folder_path)
                json_file_path = os.path.join(import_folder_path, submesh_folder_name + ".json")
                imported_obj = SSMTImportHelper.create_mesh_from_json(
                    json_file_path=json_file_path,
                    import_collection=target["import_collection"],
                )
                if imported_obj is None:
                    continue

                display_name = target["display_name"]
                workspace_unique_str = str(imported_obj.get("3DMigoto:WorkspaceUniqueStr", "") or "").strip()
                if workspace_unique_str:
                    prefix_info = ObjectPrefixHelper.extract_prefix_info(display_name)
                    if prefix_info:
                        display_name = ObjectPrefixHelper.replace_prefix(
                            display_name,
                            workspace_unique_str,
                            ".",
                            prefix_info[0],
                            prefix_info[1],
                        )
                    else:
                        display_name = workspace_unique_str

                imported_obj.name = display_name
                imported_obj.data.name = imported_obj.name
                imported_objects.append(imported_obj)
                foldername_gametypename_dict[target["import_key"]] = gametype_name
                import_records.append(
                    {
                        **target,
                        "imported_obj": imported_obj,
                        "gametype_name": gametype_name,
                    }
                )
                self.report({'INFO'}, "成功导入 " + target["import_key"] + " 的数据类型: " + gametype_name)
            except Exception as e:
                print(f"Failed to import from {import_folder_path}: {e}")
                continue
            break

    if not import_records:
        self.report({'ERROR'}, "当前工作空间没有成功导入任何模型，已跳过蓝图生成。")
        return

    save_import_json_path = os.path.join(GlobalConfig.path_workspace_folder(), "Import.json")
    JsonUtils.SaveToFile(json_dict=foldername_gametypename_dict, filepath=save_import_json_path)

    if GlobalProterties.enable_non_mirror_workflow():
        NonMirrorWorkflowHelper.process_imported_objects(imported_objects)

    CollectionUtils.select_collection_objects(workspace_collection)
    PrefixQuickOpsHelper.merge_prefixes_from_objects(context, imported_objects)

    try:
        tree_name = GlobalConfig.get_workspace_name()

        try:
            tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')
        except Exception as e:
            print(f"Failed to create new node tree: {e}. Check if SSMTBlueprintTreeType is registered.")
            return
        tree.use_fake_user = True

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        if global_properties and getattr(global_properties, "selected_blueprint_name", "") != tree.name:
            global_properties.selected_blueprint_name = tree.name

        drawib_tabname_dict = WorkSpaceHelper.get_drawib_tabname_dict()

        tab_group_nodes = {}
        tab_node_lists = {}
        default_group_node = tree.nodes.new('SSMTNode_Object_Group')
        default_group_node.label = "Default Group"
        default_node_list = []

        y_gap = 200
        tab_gap = 400

        for record in import_records:
            obj = record["imported_obj"]
            if obj.type != 'MESH':
                continue

            draw_ib = record["draw_ib"]
            tab_name = drawib_tabname_dict.get(draw_ib)

            if tab_name and tab_name not in tab_group_nodes:
                group_node = tree.nodes.new('SSMTNode_Object_Group')
                group_node.label = tab_name
                tab_group_nodes[tab_name] = group_node
                tab_node_lists[tab_name] = []

            target_group = tab_group_nodes.get(tab_name, default_group_node) if tab_name else default_group_node

            node = tree.nodes.new('SSMTNode_Object_Info')
            node.object_name = obj.name
            node.object_id = str(obj.as_pointer())
            if hasattr(node, "original_object_name"):
                node.original_object_name = obj.name
            node.draw_ib = draw_ib
            node.component = record["component"] or "1"
            node.alias_name = record["alias_name"]
            node.label = obj.name

            if target_group.inputs[-1].is_linked:
                target_group.inputs.new('SSMTSocketObject', f"Input {len(target_group.inputs) + 1}")

            tree.links.new(node.outputs[0], target_group.inputs[-1])

            if tab_name:
                tab_node_lists[tab_name].append(node)
            else:
                default_node_list.append(node)

        all_group_nodes = []
        tab_order = list(tab_group_nodes.keys())

        current_y = 0
        for tab_name in tab_order:
            nodes = tab_node_lists.get(tab_name, [])
            for node in nodes:
                node.location = (0, current_y)
                current_y -= y_gap
            current_y -= tab_gap - y_gap

        for node in default_node_list:
            node.location = (0, current_y)
            current_y -= y_gap

        has_default_links = any(inp.is_linked for inp in default_group_node.inputs)
        if has_default_links:
            all_group_nodes.append(default_group_node)
        elif not all_group_nodes:
            all_group_nodes.append(default_group_node)
        else:
            tree.nodes.remove(default_group_node)

        for tab_name in tab_order:
            all_group_nodes.append(tab_group_nodes[tab_name])

        group_x = 400
        group_current_y = 0

        for grp_node in all_group_nodes:
            grp_node.location = (group_x, group_current_y)
            group_current_y -= 300

        output_node = tree.nodes.new('SSMTNode_Result_Output')
        output_node.location = (800, 0)
        output_node.label = "Generate Mod"

        if len(output_node.inputs) > 0 and len(all_group_nodes) > 0:
            if len(all_group_nodes) == 1:
                tree.links.new(all_group_nodes[0].outputs[0], output_node.inputs[0])
            else:
                merge_node = tree.nodes.new('SSMTNode_Object_Group')
                merge_node.label = "Merge"
                merge_node.location = (600, 0)

                for grp_node in all_group_nodes:
                    if merge_node.inputs[-1].is_linked:
                        merge_node.inputs.new('SSMTSocketObject', f"Input {len(merge_node.inputs) + 1}")
                    tree.links.new(grp_node.outputs[0], merge_node.inputs[-1])

                tree.links.new(merge_node.outputs[0], output_node.inputs[0])

        for grp_node in all_group_nodes:
            if hasattr(grp_node, "update"):
                grp_node.update()

        print(f"Blueprint {tree_name} updated with imported objects, grouped by workspace tabs.")

    except Exception as e:
        print(f"Error generating blueprint nodes: {e}")
        import traceback
        traceback.print_exc()


class SSMT4ImportAllFromCurrentWorkSpaceBlueprint(bpy.types.Operator):
    bl_idname = "ssmt4.import_all_from_workspace"
    bl_label = TR.translate("涓€閿鍏SMT宸ヤ綔绌洪棿鍐呭")
    bl_description = "涓€閿鍏ュ綋鍓嶅伐浣滅┖闂存枃浠跺す涓嬫墍鏈夌殑鍐呭"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if GlobalConfig.get_workspace_name() == "":
            self.report({"ERROR"}, "Please select your WorkSpace in SSMT before import.")
        elif not os.path.exists(GlobalConfig.path_workspace_folder()):
            self.report(
                {"ERROR"},
                "WorkSpace Folder Didn't exists, Please create a WorkSpace in SSMT before import "
                + GlobalConfig.path_workspace_folder(),
            )
        else:
            TimerUtils.Start("ImportFromWorkSpaceBlueprint")
            ImprotFromWorkSpaceFull(self, context)
            TimerUtils.End("ImportFromWorkSpaceBlueprint")

        return {'FINISHED'}


class SSMT4ImportRaw(bpy.types.Operator, ImportHelper):
    bl_idname = "ssmt4.import_raw"
    bl_label = TR.translate("瀵煎叆SSMT鏍煎紡妯″瀷")
    bl_description = "瀵煎叆SSMT鏍煎紡鐨勬ā鍨嬫枃浠? 鍙渶閫夋嫨.json鏂囦欢鍗冲彲"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: bpy.props.StringProperty(
        default='*.json',
        options={'HIDDEN'},
    )  # type: ignore

    files: bpy.props.CollectionProperty(
        name="File Path",
        type=bpy.types.OperatorFileListElement,
    )  # type: ignore

    def execute(self, context):
        dirname = os.path.dirname(self.filepath)

        collection_name = os.path.basename(dirname)
        collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(collection)
        imported_objects = []

        import_filename_list = []
        if len(self.files) == 1:
            if str(self.filepath).endswith(".json"):
                import_filename_list.append(self.filepath)
            else:
                for filename in os.listdir(self.filepath):
                    if filename.endswith(".json"):
                        import_filename_list.append(filename)
        else:
            for json_file in self.files:
                import_filename_list.append(json_file.name)

        for json_file_name in import_filename_list:
            if os.path.isabs(json_file_name):
                json_file_path = json_file_name
            else:
                json_file_path = os.path.join(dirname, json_file_name)
            imported_obj = SSMTImportHelper.create_mesh_from_json(
                json_file_path=json_file_path,
                import_collection=collection,
            )
            if imported_obj is not None:
                imported_objects.append(imported_obj)

        if GlobalProterties.enable_non_mirror_workflow():
            NonMirrorWorkflowHelper.process_imported_objects(imported_objects)

        CollectionUtils.select_collection_objects(collection)
        PrefixQuickOpsHelper.merge_prefixes_from_objects(context, imported_objects)

        return {'FINISHED'}


def register():
    bpy.utils.register_class(SSMT4ImportRaw)
    bpy.utils.register_class(SSMT4ImportAllFromCurrentWorkSpaceBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMT4ImportRaw)
    bpy.utils.unregister_class(SSMT4ImportAllFromCurrentWorkSpaceBlueprint)
