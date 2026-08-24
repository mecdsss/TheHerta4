
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
from ..common.logic_name import LogicName
from ..common.non_mirror_workflow import NonMirrorWorkflowHelper
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.ssmt_import_helper import SSMTImportHelper
from ..common.workspace_helper import WorkSpaceHelper
from .ntmi_modimp.ntemi_importer import (
    NTEMIImportHelper,
    NtemiDrawCallMeta,
    _discover_draw_calls,
    _load_frame_analysis_dir_map,
    _resolve_deduped_texture_dir,
    _resolve_frame_analysis_dir,
    _load_component_name_map,
    _perform_bone_merge_postprocess,
    NTEMI_PROFILE_ID,
)
from .ui_prefix_quick_ops import PrefixQuickOpsHelper


def _create_original_model_frame(tree, label="原始模型"):
    """创建一个用于容纳原始模型节点的框架"""
    frame = tree.nodes.new('NodeFrame')
    frame.label = label
    frame.use_custom_color = True
    frame.color = (0.2, 0.35, 0.2)
    return frame


def _parent_nodes_to_frame(frame, nodes):
    """将一组节点设置为指定框架的子节点"""
    for node in nodes:
        if node is None:
            continue
        node.parent = frame


def _extract_gametype_name(import_folder_path: str) -> str:
    """从导入路径中提取游戏类型名称（如 TYPE_XXX 中的 XXX）"""
    normalized = str(import_folder_path or "").replace("\\", "/")
    marker = "TYPE_"
    index = normalized.rfind(marker)
    if index == -1:
        return ""
    return normalized[index + len(marker):].strip("/").strip()


# 骨架组合集的轮换颜色（COLOR_01~08 轮换，便于在大纲视图区分不同组；
# 直接用字符串字面量而非 CollectionColor 枚举——测试环境 stub 没有完整枚举值）
_SKELETON_GROUP_COLORS = [
    "COLOR_02", "COLOR_04", "COLOR_06", "COLOR_03",
    "COLOR_07", "COLOR_08", "COLOR_05", "COLOR_01",
]


def _zzmi_move_to_skeleton_group_collection(obj, import_key: str, parent_collection) -> None:
    """ZZMI 分组版骨骼合并：把导入对象移入其骨架组合集（SkeletonGroup_<N>）。

    合集挂在该对象的父合集（LOD 合集）下；对象从原合集移出、链接进组集合。
    仅在子网格 json 已有 SkeletonGroup 字段（ensure_skeleton_data 分组版写回）时生效；
    无字段（旧缓存/未生成）保持原合集归属不变。失败不影响导入。
    """
    from ..common.zzmi_skeleton import ZZMISkeletonMergeHelper

    workspace_root = GlobalConfig.path_workspace_folder()
    json_path = ZZMISkeletonMergeHelper._resolve_submesh_json_path(workspace_root, import_key)
    if not json_path:
        return
    submesh_json = JsonUtils.LoadFromFile(json_path)
    if not isinstance(submesh_json, dict):
        return
    skeleton_group = submesh_json.get("SkeletonGroup")
    if skeleton_group is None:
        return
    try:
        skeleton_group = int(skeleton_group)
    except (TypeError, ValueError):
        return

    collection_name = f"SkeletonGroup_{skeleton_group}"
    group_collection = parent_collection.children.get(collection_name)
    if group_collection is None:
        group_collection = CollectionUtils.create_new_collection(
            collection_name=collection_name,
            color_tag=_SKELETON_GROUP_COLORS[skeleton_group % len(_SKELETON_GROUP_COLORS)],
        )
        parent_collection.children.link(group_collection)

    for coll in list(obj.users_collection):
        coll.objects.unlink(obj)
    group_collection.objects.link(obj)


def _build_workspace_import_targets(workspace_collection):
    """构建工作空间中所有可导入子模型的迭代器，每个条目包含路径、显示名等信息"""
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


def _detect_ntemi_workspace() -> bool:
    """检测当前工作空间是否为 NTEMI（异环·安魂曲）工作空间"""
    if GlobalConfig.logic_name == LogicName.NTEMI:
        return True
    workspace_root = GlobalConfig.path_workspace_folder()
    if not workspace_root or not os.path.isdir(workspace_root):
        return False
    lod0_dir = os.path.join(workspace_root, "LOD0")
    if not os.path.isdir(lod0_dir):
        return False
    import json as _json
    from pathlib import Path as _Path
    for entry in os.scandir(lod0_dir):
        if not entry.is_dir():
            continue
        type_subdirs = sorted(_Path(entry.path).glob("TYPE_*"))
        for type_dir in type_subdirs:
            for json_file in type_dir.glob("*.json"):
                try:
                    payload = _json.loads(json_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if str(payload.get("GamePreset", "") or "").strip().upper() == "NTEMI":
                    return True
    return False


def ImprotFromWorkSpaceFull(self, context):
    """从工作空间完整导入所有子模型并构建蓝图节点树"""
    workspace_collection = WorkSpaceHelper.create_and_get_workspace_collection()
    is_ntemi = _detect_ntemi_workspace()

    if is_ntemi:
        return _import_workspace_full_ntemi(self, context, workspace_collection)

    import_targets = list(_build_workspace_import_targets(workspace_collection))
    if not import_targets:
        self.report({'ERROR'}, "当前工作空间未找到可导入的子模型目录。")
        return False

    # EFMI 骨骼合并数据预生成：导入前把 FrameAnalysis 反查的 VGMap 写回工作空间 json，
    # 使导入流程走全局骨骼索引（json 有 VGMap 且 import_merged_vgmap 开启时自动生效）。
    if (
        GlobalConfig.logic_name == LogicName.EFMI
        and GlobalProterties.import_merged_vgmap()
    ):
        try:
            from ..common.efmi_skeleton import EFMISkeletonMergeHelper
            import_keys = [target["import_key"] for target in import_targets]
            ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=GlobalConfig.path_workspace_folder(),
                unique_str_list=import_keys,
                lod_group_projection=GlobalProterties.efmi_lod_group_projection(),
            )
            print(f"[EFMI骨骼合并] {message}")
            if ok:
                self.report({'INFO'}, message)
            else:
                print(f"[EFMI骨骼合并] 未生成骨骼数据：{message}")
        except Exception as e:
            import traceback
            print(f"[EFMI骨骼合并] 预生成失败（不阻断导入）: {e}")
            traceback.print_exc()

    # ZZMI 骨骼合并数据预生成（与 EFMI 同构的分支选项）：
    # 复选框（import_merged_vgmap，「使用融合统一顶点组」）关闭时完全不执行，保持旧逻辑；
    # 开启时把 FrameAnalysis 反查的 VGMap/VGOffset/VGCount 写回工作空间 json，
    # 导入流程经 create_mesh_from_json 的既有双条件路径自动走全局骨骼索引。
    is_zzmi_merged = (
        GlobalConfig.logic_name == LogicName.ZZMI
        and GlobalProterties.import_merged_vgmap()
    )
    if is_zzmi_merged:
        try:
            from ..common.zzmi_skeleton import ZZMISkeletonMergeHelper
            import_keys = [target["import_key"] for target in import_targets]
            ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=GlobalConfig.path_workspace_folder(),
                unique_str_list=import_keys,
            )
            print(f"[ZZMI骨骼合并] {message}")
            if ok:
                self.report({'INFO'}, message)
            else:
                print(f"[ZZMI骨骼合并] 未生成骨骼数据：{message}")
        except Exception as e:
            import traceback
            print(f"[ZZMI骨骼合并] 预生成失败（不阻断导入）: {e}")
            traceback.print_exc()

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
            gametype_name = _extract_gametype_name(import_folder_path)
            if not gametype_name:
                self.report({'WARNING'}, f"跳过无法识别游戏类型的导入目录：{import_folder_path}")
                continue

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
                if is_zzmi_merged:
                    # 分组版骨骼合并：导入对象按 json SkeletonGroup 归入对应骨架组合集，
                    # 让"同一对象空间的部件"在大纲视图里聚在一起（跨组不共享骨架）。
                    try:
                        _zzmi_move_to_skeleton_group_collection(
                            imported_obj,
                            target["import_key"],
                            target["import_collection"],
                        )
                    except Exception as e:
                        print(f"[ZZMI骨骼合并] 分组合集归组失败（不影响导入）: {e}")
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
                print(f"导入目录失败：{import_folder_path}，错误：{e}")
                continue
            break

    if not import_records:
        self.report({'ERROR'}, "当前工作空间没有成功导入任何模型，已跳过蓝图生成。")
        return False

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
            print(f"创建蓝图节点树失败：{e}。请检查 SSMTBlueprintTreeType 是否已正确注册。")
            self.report({'ERROR'}, "创建蓝图失败，请确认 SSMT 蓝图节点类型已正确注册。")
            return False
        tree.use_fake_user = True

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        if global_properties and getattr(global_properties, "selected_blueprint_name", "") != tree.name:
            global_properties.selected_blueprint_name = tree.name

        drawib_tabname_dict = WorkSpaceHelper.get_drawib_tabname_dict()
        original_model_frame = _create_original_model_frame(tree)

        tab_group_nodes = {}
        tab_node_lists = {}
        default_group_node = tree.nodes.new('SSMTNode_Object_Group')
        default_group_node.label = "默认分组"
        default_group_node.parent = original_model_frame
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
                group_node.parent = original_model_frame
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
            node.parent = original_model_frame

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
        output_node.label = "生成 Mod"

        if len(output_node.inputs) > 0 and len(all_group_nodes) > 0:
            if len(all_group_nodes) == 1:
                tree.links.new(all_group_nodes[0].outputs[0], output_node.inputs[0])
            else:
                merge_node = tree.nodes.new('SSMTNode_Object_Group')
                merge_node.label = "合并"
                merge_node.location = (600, 0)
                merge_node.parent = original_model_frame

                for grp_node in all_group_nodes:
                    if merge_node.inputs[-1].is_linked:
                        merge_node.inputs.new('SSMTSocketObject', f"Input {len(merge_node.inputs) + 1}")
                    tree.links.new(grp_node.outputs[0], merge_node.inputs[-1])

                tree.links.new(merge_node.outputs[0], output_node.inputs[0])

        for grp_node in all_group_nodes:
            if hasattr(grp_node, "update"):
                grp_node.update()
        if hasattr(original_model_frame, "update"):
            original_model_frame.update()

        print(f"蓝图 {tree_name} 已按工作空间标签完成导入对象分组更新。")
        return True

    except Exception as e:
        print(f"生成蓝图节点时发生错误：{e}")
        import traceback
        traceback.print_exc()
        self.report({'ERROR'}, f"生成导入蓝图失败：{e}")
        return False


def _import_workspace_full_ntemi(self, context, workspace_collection):
    """NTEMI 工作空间的完整导入流程，含骨骼合并后处理"""
    workspace_root = GlobalConfig.path_workspace_folder()
    drawib_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict()
    draw_calls = _discover_draw_calls(workspace_root, drawib_aliasname_dict)
    if not draw_calls:
        self.report({'ERROR'}, "NTEMI工作空间未找到可导入的DrawCall数据。")
        return False

    default_frame_analysis_dir = _resolve_frame_analysis_dir(workspace_root)
    frame_analysis_dir_map = _load_frame_analysis_dir_map(workspace_root)
    lod_collection_map = {}
    deduped_texture_dir_map = {}
    component_map_by_lod = {}

    foldername_gametypename_dict = {}
    imported_objects = []
    import_records = []

    for draw_call in draw_calls:
        lod_name = str(draw_call.lod_name or "LOD0").strip() or "LOD0"
        lod_collection = lod_collection_map.get(lod_name)
        if lod_collection is None:
            lod_collection = CollectionUtils.create_new_collection(
                collection_name=lod_name,
                color_tag=CollectionColor.Blue,
            )
            workspace_collection.children.link(lod_collection)
            lod_collection_map[lod_name] = lod_collection

        if lod_name not in deduped_texture_dir_map:
            deduped_texture_dir_map[lod_name] = _resolve_deduped_texture_dir(workspace_root, lod_name)
        if lod_name not in component_map_by_lod:
            component_map_by_lod[lod_name] = _load_component_name_map(os.path.join(workspace_root, lod_name))

        frame_analysis_dir = frame_analysis_dir_map.get(
            str(draw_call.draw_ib or "").strip().lower(),
            default_frame_analysis_dir,
        )
        json_file_path = os.path.join(
            draw_call.folder_path, f"{draw_call.submesh_folder_name}.json"
        )

        workspace_unique_str = f"{lod_name}.{draw_call.submesh_folder_name}"

        try:
            imported_obj = NTEMIImportHelper.create_mesh_with_modimp_props(
                json_file_path=json_file_path,
                draw_call_meta=draw_call,
                import_collection=lod_collection,
                deduped_texture_dir=deduped_texture_dir_map.get(lod_name, ""),
                component_map=component_map_by_lod.get(lod_name, {}),
                workspace_unique_str=workspace_unique_str,
                frame_analysis_dir=frame_analysis_dir,
            )
            if imported_obj is None:
                continue

            display_name = workspace_unique_str
            if draw_call.alias_name:
                prefix_info = ObjectPrefixHelper.extract_prefix_info(draw_call.display_name)
                if prefix_info:
                    display_name = ObjectPrefixHelper.replace_prefix(
                        draw_call.display_name,
                        workspace_unique_str,
                        ".",
                        prefix_info[0],
                        prefix_info[1],
                    )
                else:
                    display_name = workspace_unique_str

            imported_obj.name = display_name
            imported_obj.data.name = display_name
            imported_objects.append(imported_obj)

            gametype_name = "GPU_P12_BI8_BW8_T8_T1-8_TA4_N4_"
            foldername_gametypename_dict[workspace_unique_str] = gametype_name
            import_records.append(
                {
                    "import_key": workspace_unique_str,
                    "submesh_folder_name": draw_call.submesh_folder_name,
                    "submesh_folder_path": draw_call.folder_path,
                    "display_name": display_name,
                    "alias_name": draw_call.alias_name,
                    "draw_ib": draw_call.draw_ib,
                    "component": draw_call.component,
                    "lod_name": lod_name,
                    "frame_analysis_dir": frame_analysis_dir,
                    "import_collection": lod_collection,
                    "imported_obj": imported_obj,
                    "gametype_name": gametype_name,
                }
            )
            self.report(
                {'INFO'},
                f"NTEMI 成功导入 {workspace_unique_str}"
            )
        except Exception as e:
            print(f"导入 NTEMI DrawCall 失败：{workspace_unique_str}，错误：{e}")
            import traceback
            traceback.print_exc()
            continue

    if not import_records:
        self.report({'ERROR'}, "NTEMI工作空间没有成功导入任何模型，已跳过蓝图生成。")
        return False

    save_import_json_path = os.path.join(workspace_root, "Import.json")
    JsonUtils.SaveToFile(json_dict=foldername_gametypename_dict, filepath=save_import_json_path)

    if GlobalProterties.enable_non_mirror_workflow():
        NonMirrorWorkflowHelper.process_imported_objects(imported_objects)

    CollectionUtils.select_collection_objects(workspace_collection)
    PrefixQuickOpsHelper.merge_prefixes_from_objects(context, imported_objects)

    imported_by_source: dict[tuple[str, str], list] = {}
    for record in import_records:
        imported_obj = record.get("imported_obj")
        draw_ib = str(record.get("draw_ib", "") or "").strip()
        frame_analysis_dir = str(record.get("frame_analysis_dir", "") or "").strip()
        if imported_obj is None or not draw_ib or not frame_analysis_dir:
            continue
        imported_by_source.setdefault((frame_analysis_dir, draw_ib), []).append(imported_obj)

    for (frame_analysis_dir, draw_ib), draw_ib_objects in imported_by_source.items():
        try:
            _perform_bone_merge_postprocess(
                objects=draw_ib_objects,
                frame_analysis_dir=frame_analysis_dir,
                draw_ib=draw_ib,
                workspace_root=workspace_root,
            )
        except Exception as e:
            print(f"NTEMI骨骼合并后处理失败：DrawIB={draw_ib}，FrameAnalysis={frame_analysis_dir}，错误：{e}")
            import traceback
            traceback.print_exc()
            self.report(
                {'WARNING'},
                f"已跳过 DrawIB {draw_ib} 的骨骼合并后处理：{os.path.basename(frame_analysis_dir)}"
            )

    try:
        tree_name = GlobalConfig.get_workspace_name()

        try:
            tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')
        except Exception as e:
            print(f"创建 NTEMI 蓝图节点树失败：{e}。请检查 SSMTBlueprintTreeType 是否已正确注册。")
            self.report({'ERROR'}, "创建 NTEMI 蓝图失败，请确认 SSMT 蓝图节点类型已正确注册。")
            return False
        tree.use_fake_user = True

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        if global_properties and getattr(global_properties, "selected_blueprint_name", "") != tree.name:
            global_properties.selected_blueprint_name = tree.name

        drawib_tabname_dict = WorkSpaceHelper.get_drawib_tabname_dict()
        original_model_frame = _create_original_model_frame(tree)

        tab_group_nodes = {}
        tab_node_lists = {}
        default_group_node = tree.nodes.new('SSMTNode_Object_Group')
        default_group_node.label = "默认分组"
        default_group_node.parent = original_model_frame
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
                group_node.parent = original_model_frame
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
            node.parent = original_model_frame

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

        output_node = tree.nodes.new('SSMTNode_Result_Output_NTMIModImp')
        output_node.location = (800, 0)
        output_node.label = "NTMI ModImp 输出"

        if len(output_node.inputs) > 0 and len(all_group_nodes) > 0:
            if len(all_group_nodes) == 1:
                tree.links.new(all_group_nodes[0].outputs[0], output_node.inputs[0])
            else:
                merge_node = tree.nodes.new('SSMTNode_Object_Group')
                merge_node.label = "合并"
                merge_node.location = (600, 0)
                merge_node.parent = original_model_frame

                for grp_node in all_group_nodes:
                    if merge_node.inputs[-1].is_linked:
                        merge_node.inputs.new('SSMTSocketObject', f"Input {len(merge_node.inputs) + 1}")
                    tree.links.new(grp_node.outputs[0], merge_node.inputs[-1])

                tree.links.new(merge_node.outputs[0], output_node.inputs[0])

        for grp_node in all_group_nodes:
            if hasattr(grp_node, "update"):
                grp_node.update()
        if hasattr(original_model_frame, "update"):
            original_model_frame.update()

        print(f"已创建 NTEMI 蓝图 {tree_name}，并挂载 NTMI ModImp 输出节点。")
        return True

    except Exception as e:
        print(f"生成 NTEMI 蓝图节点时发生错误：{e}")
        import traceback
        traceback.print_exc()
        self.report({'ERROR'}, f"生成 NTEMI 导入蓝图失败：{e}")
        return False


class SSMT4ImportAllFromCurrentWorkSpaceBlueprint(bpy.types.Operator):
    """一键导入当前 SSMT 工作空间下所有内容的 Blender 算子"""
    bl_idname = "ssmt4.import_all_from_workspace"
    bl_label = TR.translate("一键导入SSMT工作空间内容")
    bl_description = "一键导入当前工作空间文件夹下所有的内容"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        success = False
        if GlobalConfig.get_workspace_name() == "":
            self.report({"ERROR"}, "请先在 SSMT 中选择工作空间后再导入。")
        elif not os.path.exists(GlobalConfig.path_workspace_folder()):
            self.report(
                {"ERROR"},
                "工作空间目录不存在，请先在 SSMT 中创建工作空间后再导入："
                + GlobalConfig.path_workspace_folder(),
            )
        else:
            TimerUtils.Start("ImportFromWorkSpaceBlueprint")
            success = bool(ImprotFromWorkSpaceFull(self, context))
            TimerUtils.End("ImportFromWorkSpaceBlueprint")

        if success:
            return {'FINISHED'}
        return {'CANCELLED'}


class SSMT4ImportRaw(bpy.types.Operator, ImportHelper):
    """导入 SSMT 格式模型文件的 Blender 算子，支持批量选择 JSON 文件"""
    bl_idname = "ssmt4.import_raw"
    bl_label = TR.translate("导入SSMT格式模型")
    bl_description = "导入SSMT格式的模型文件，只需选择.json文件即可"
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
        """执行导入操作，遍历选中的 JSON 文件并创建网格对象"""
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

        if imported_objects:
            return {'FINISHED'}
        self.report({'ERROR'}, "没有成功导入任何模型，请检查所选 JSON 或目录是否有效。")
        return {'CANCELLED'}


def register():
    bpy.utils.register_class(SSMT4ImportRaw)
    bpy.utils.register_class(SSMT4ImportAllFromCurrentWorkSpaceBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMT4ImportRaw)
    bpy.utils.unregister_class(SSMT4ImportAllFromCurrentWorkSpaceBlueprint)
