import os
from dataclasses import dataclass, field
from typing import Dict, List

import bpy

from .global_config import GlobalConfig
from ..utils.collection_utils import CollectionColor, CollectionUtils
from ..utils.json_utils import JsonUtils


@dataclass
class DedupedTextureInfo:
    original_hash: str = field(default="", init=False)
    render_hash: str = field(default="", init=False)
    format: str = field(default="", init=False)
    componet_count_list_str: str = field(default="", init=False)


class WorkSpaceHelper:
    @staticmethod
    def _has_importable_content(base_folder: str) -> bool:
        if not os.path.isdir(base_folder):
            return False
        if WorkSpaceHelper._get_submesh_folderpath_list_from(base_folder):
            return True
        if WorkSpaceHelper.get_lod_folderpath_list(base_folder):
            return True
        return False

    @staticmethod
    def get_workspace_partition_folderpath_list() -> List[str]:
        workspace_folder = GlobalConfig.path_workspace_folder()
        if not os.path.isdir(workspace_folder):
            return []

        if WorkSpaceHelper._has_importable_content(workspace_folder):
            return []

        partition_folderpath_list = []
        for f in os.scandir(workspace_folder):
            if not f.is_dir():
                continue
            if f.name == "Config":
                continue
            config_json_path = os.path.join(f.path, "Config.json")
            if not os.path.exists(config_json_path):
                continue
            partition_folderpath_list.append(f.path)

        partition_folderpath_list.sort(key=lambda path: os.path.basename(path).casefold())
        return partition_folderpath_list

    @staticmethod
    def _compose_lod_name(lod_name: str, bare_name: str) -> str:
        normalized_lod_name = str(lod_name or "").strip()
        normalized_bare_name = str(bare_name or "").strip()
        if normalized_lod_name and normalized_bare_name:
            return normalized_lod_name + "." + normalized_bare_name
        return normalized_bare_name

    @staticmethod
    def parse_lod_unique_str(unique_str: str) -> tuple[str, str]:
        normalized_unique_str = str(unique_str or "").strip()
        if normalized_unique_str.upper().startswith("LOD") and "." in normalized_unique_str:
            dot_idx = normalized_unique_str.index(".")
            potential_lod_name = normalized_unique_str[:dot_idx]
            if potential_lod_name[3:].isdigit():
                return potential_lod_name, normalized_unique_str[dot_idx + 1:]
        return "", normalized_unique_str

    @staticmethod
    def get_submesh_folder_path(unique_str: str) -> str:
        lod_name, bare_unique_str = WorkSpaceHelper.parse_lod_unique_str(unique_str)
        workspace_folder = GlobalConfig.path_workspace_folder()
        candidate_base_paths = [workspace_folder, *WorkSpaceHelper.get_workspace_partition_folderpath_list()]

        for base_path in candidate_base_paths:
            if lod_name:
                candidate_path = os.path.join(base_path, lod_name, bare_unique_str)
            else:
                candidate_path = os.path.join(base_path, bare_unique_str)
            if os.path.isdir(candidate_path):
                return candidate_path

        if lod_name:
            return os.path.join(workspace_folder, lod_name, bare_unique_str)
        return os.path.join(workspace_folder, bare_unique_str)

    @staticmethod
    def get_object_display_name(submesh_folder_name: str, drawib_aliasname_dict: Dict[str, str] | None = None) -> str:
        normalized_folder_name = str(submesh_folder_name or "").strip()
        if not normalized_folder_name:
            return ""

        _lod_name, bare_folder_name = WorkSpaceHelper.parse_lod_unique_str(normalized_folder_name)
        drawib_aliasname_dict = drawib_aliasname_dict or WorkSpaceHelper.get_drawib_aliasname_dict()
        folder_prefix, _, folder_alias = bare_folder_name.partition(".")
        draw_ib = folder_prefix.split("-")[0]

        configured_alias = str(drawib_aliasname_dict.get(draw_ib, "")).strip()
        if configured_alias:
            return configured_alias

        if folder_alias.strip():
            return folder_alias.strip()

        return folder_prefix

    @staticmethod
    def get_display_submesh_name(submesh_folder_name: str, drawib_aliasname_dict: Dict[str, str] | None = None) -> str:
        normalized_folder_name = str(submesh_folder_name or "").strip()
        if not normalized_folder_name:
            return ""

        lod_name, bare_folder_name = WorkSpaceHelper.parse_lod_unique_str(normalized_folder_name)
        name_prefix, _, existing_alias = bare_folder_name.partition(".")
        alias_name = WorkSpaceHelper.get_object_display_name(
            bare_folder_name,
            drawib_aliasname_dict=drawib_aliasname_dict,
        )
        if not alias_name:
            return normalized_folder_name

        if existing_alias.strip() and alias_name == existing_alias.strip():
            return WorkSpaceHelper._compose_lod_name(lod_name, bare_folder_name)

        if alias_name == name_prefix:
            return WorkSpaceHelper._compose_lod_name(lod_name, name_prefix)

        return WorkSpaceHelper._compose_lod_name(lod_name, name_prefix + "." + alias_name)

    @staticmethod
    def get_ordered_gpu_cpu_import_folderpath_list(submesh_folderpath: str) -> List[str]:
        gpu_import_folder_path_list = []
        cpu_import_folder_path_list = []

        dirs = os.listdir(submesh_folderpath)
        for dirname in dirs:
            if not dirname.startswith("TYPE_"):
                continue
            final_import_folder_path = os.path.join(submesh_folderpath, dirname)
            if dirname.startswith("TYPE_GPU"):
                gpu_import_folder_path_list.append(final_import_folder_path)
            elif dirname.startswith("TYPE_CPU"):
                cpu_import_folder_path_list.append(final_import_folder_path)

        final_import_folder_path_list = []
        for gpu_path in gpu_import_folder_path_list:
            final_import_folder_path_list.append(gpu_path)
        for cpu_path in cpu_import_folder_path_list:
            final_import_folder_path_list.append(cpu_path)

        return final_import_folder_path_list

    @staticmethod
    def create_and_get_workspace_collection() -> bpy.types.Collection:
        workspace_collection = CollectionUtils.create_new_collection(
            collection_name=GlobalConfig.get_workspace_name(),
            color_tag=CollectionColor.Red,
        )
        bpy.context.scene.collection.children.link(workspace_collection)
        return workspace_collection

    @staticmethod
    def _get_submesh_folderpath_list_from(base_folder: str) -> List[str]:
        submesh_folderpath_list = []
        if not os.path.isdir(base_folder):
            return submesh_folderpath_list

        for f in os.scandir(base_folder):
            if not f.is_dir():
                continue
            name_splits = f.name.split("-")
            if len(name_splits) >= 3:
                submesh_folderpath_list.append(f.path)

        submesh_folderpath_list.sort(key=lambda path: os.path.basename(path).casefold())
        return submesh_folderpath_list

    @staticmethod
    def get_lod_folderpath_list(base_folder: str | None = None) -> List[str]:
        lod_folderpath_list = []
        workspace_folder = base_folder or GlobalConfig.path_workspace_folder()
        if not os.path.isdir(workspace_folder):
            return lod_folderpath_list

        for f in os.scandir(workspace_folder):
            if not f.is_dir():
                continue

            name = f.name
            if name.upper().startswith("LOD") and name[3:].isdigit():
                lod_folderpath_list.append(f.path)

        lod_folderpath_list.sort(key=lambda path: int(os.path.basename(path)[3:]))
        return lod_folderpath_list

    @staticmethod
    def get_lod_submesh_folderpath_dict(base_folder: str | None = None) -> Dict[str, List[str]]:
        lod_submesh_folderpath_dict: Dict[str, List[str]] = {}
        for lod_folder_path in WorkSpaceHelper.get_lod_folderpath_list(base_folder):
            lod_name = os.path.basename(lod_folder_path)
            lod_submesh_folderpath_dict[lod_name] = WorkSpaceHelper._get_submesh_folderpath_list_from(lod_folder_path)
        return lod_submesh_folderpath_dict

    @staticmethod
    def get_submesh_folderpath_list() -> List[str]:
        return WorkSpaceHelper._get_submesh_folderpath_list_from(GlobalConfig.path_workspace_folder())

    @staticmethod
    def get_submesh_folder_records() -> List[Dict[str, str]]:
        """枚举当前工作空间中所有子网格文件夹及其导入身份键（lod_name + bare_name）。

        枚举范围与一键导入（ui_func_import_ssmt._build_workspace_import_targets）完全一致：
        - 工作空间根目录直接含子网格文件夹时，只扫根目录；
        - 根目录按分区组织（子目录含 Config.json）时，扫各分区（含分区内 LOD 子目录）。

        每个记录的身份键与导入对象上的 3DMigoto:WorkspaceUniqueStr 解析结果一致，
        支持多 LOD 前缀：`LOD0/xxx-1-0` -> lod_name="LOD0"，根目录 `xxx-1-0` -> lod_name=""。
        """
        records: List[Dict[str, str]] = []
        workspace_folder = GlobalConfig.path_workspace_folder()
        base_paths = WorkSpaceHelper.get_workspace_partition_folderpath_list()
        if not base_paths:
            if not workspace_folder or not os.path.isdir(workspace_folder):
                return records
            base_paths = [workspace_folder]

        seen_paths = set()
        for base_path in base_paths:
            for lod_folder_path in WorkSpaceHelper.get_lod_folderpath_list(base_path):
                lod_name = os.path.basename(lod_folder_path)
                for submesh_folder_path in WorkSpaceHelper._get_submesh_folderpath_list_from(lod_folder_path):
                    if submesh_folder_path in seen_paths:
                        continue
                    seen_paths.add(submesh_folder_path)
                    submesh_folder_name = os.path.basename(submesh_folder_path)
                    identity_str = WorkSpaceHelper._compose_lod_name(lod_name, submesh_folder_name)
                    parsed_lod_name, bare_name = WorkSpaceHelper.parse_lod_unique_str(identity_str)
                    records.append(
                        {
                            "folder_path": submesh_folder_path,
                            "lod_name": parsed_lod_name.upper(),
                            "bare_name": bare_name,
                        }
                    )

            for submesh_folder_path in WorkSpaceHelper._get_submesh_folderpath_list_from(base_path):
                if submesh_folder_path in seen_paths:
                    continue
                seen_paths.add(submesh_folder_path)
                submesh_folder_name = os.path.basename(submesh_folder_path)
                parsed_lod_name, bare_name = WorkSpaceHelper.parse_lod_unique_str(submesh_folder_name)
                records.append(
                    {
                        "folder_path": submesh_folder_path,
                        "lod_name": parsed_lod_name.upper(),
                        "bare_name": bare_name,
                    }
                )

        return records

    @staticmethod
    def get_unwanted_submesh_folder_list(kept_lod_bare_pairs: set[tuple[str, str]]) -> List[str]:
        """返回工作空间中不在 kept 保留集合内的子网格文件夹路径列表（按文件夹名排序）。

        kept_lod_bare_pairs：当前场景对象保留下来的 (lod_name, bare_name) 身份键集合，
        例如 {("LOD0", "aaaabbbb-100-0"), ("", "ccccdddd-200-0")}。

        匹配按 LOD 前缀精确查找：`LOD0/aaaabbbb-100-0` 只会被 ("LOD0", "aaaabbbb-100-0")
        保留，不会被裸 ("", "aaaabbbb-100-0") 或 ("LOD1", "aaaabbbb-100-0") 保留，
        避免跨 LOD 误留/误删。调用方可用这些路径直接删除文件夹（shutil.rmtree）。
        """
        kept_keys = {
            (str(lod_name or "").upper().strip(), str(bare_name or "").strip())
            for lod_name, bare_name in (kept_lod_bare_pairs or set())
        }
        unwanted_list = []
        for record in WorkSpaceHelper.get_submesh_folder_records():
            if (record["lod_name"], record["bare_name"]) not in kept_keys:
                unwanted_list.append(record["folder_path"])
        unwanted_list.sort(key=lambda path: (os.path.basename(path).casefold(), path.casefold()))
        return unwanted_list

    @staticmethod
    def get_drawib_tabname_dict() -> Dict[str, str]:
        drawib_tabname_dict = {}

        tabs_dir = os.path.join(GlobalConfig.path_workspace_folder(), "Config", "Tabs")
        workpage_tabs_path = os.path.join(GlobalConfig.path_workspace_folder(), "Config", "WorkPageTabs.json")

        if not os.path.exists(workpage_tabs_path):
            return drawib_tabname_dict

        tab_id_to_name = {}
        workpage_tabs_json = JsonUtils.LoadFromFile(workpage_tabs_path)
        if isinstance(workpage_tabs_json, dict):
            tabs_list = workpage_tabs_json.get("tabs", [])
            for tab_info in tabs_list:
                if isinstance(tab_info, dict):
                    tab_id = str(tab_info.get("id", "")).strip()
                    tab_name = str(tab_info.get("name", "")).strip()
                    if tab_id:
                        tab_id_to_name[tab_id] = tab_name

        if not os.path.exists(tabs_dir):
            return drawib_tabname_dict

        for filename in os.listdir(tabs_dir):
            if not filename.startswith("ws-tab-") or not filename.endswith(".json"):
                continue
            tab_json_path = os.path.join(tabs_dir, filename)
            tab_json = JsonUtils.LoadFromFile(tab_json_path)
            if not isinstance(tab_json, dict):
                continue

            tab_id = filename.replace(".json", "")
            tab_name = tab_id_to_name.get(tab_id, tab_id)

            model_rows = tab_json.get("modelRows", [])
            for row in model_rows:
                if isinstance(row, dict):
                    draw_ib = str(row.get("drawIB", "")).strip()
                    if draw_ib:
                        drawib_tabname_dict[draw_ib] = tab_name

        return drawib_tabname_dict

    @staticmethod
    def get_drawib_aliasname_dict_for_path(folder_path: str) -> Dict[str, str]:
        drawib_aliasname_dict = {}
        if not folder_path:
            return drawib_aliasname_dict
        config_json_path = os.path.join(folder_path, "Config.json")
        if os.path.exists(config_json_path):
            config_json = JsonUtils.LoadFromFile(config_json_path)
            if isinstance(config_json, list):
                for item in config_json:
                    if not isinstance(item, dict):
                        continue
                    draw_ib = str(item.get("DrawIB", "")).strip()
                    alias_name = str(item.get("Alias", "")).strip()
                    if draw_ib:
                        drawib_aliasname_dict[draw_ib] = alias_name
        return drawib_aliasname_dict

    @staticmethod
    def get_drawib_aliasname_dict() -> Dict[str, str]:
        drawib_aliasname_dict = {}
        workspace_folder = GlobalConfig.path_workspace_folder()
        for draw_ib, alias_name in WorkSpaceHelper.get_drawib_aliasname_dict_for_path(workspace_folder).items():
            drawib_aliasname_dict[draw_ib] = alias_name

        for partition_folder_path in WorkSpaceHelper.get_workspace_partition_folderpath_list():
            for draw_ib, alias_name in WorkSpaceHelper.get_drawib_aliasname_dict_for_path(partition_folder_path).items():
                if draw_ib not in drawib_aliasname_dict:
                    drawib_aliasname_dict[draw_ib] = alias_name
            for lod_folder_path in WorkSpaceHelper.get_lod_folderpath_list(partition_folder_path):
                for draw_ib, alias_name in WorkSpaceHelper.get_drawib_aliasname_dict_for_path(lod_folder_path).items():
                    if draw_ib not in drawib_aliasname_dict:
                        drawib_aliasname_dict[draw_ib] = alias_name

        for lod_folder_path in WorkSpaceHelper.get_lod_folderpath_list():
            for draw_ib, alias_name in WorkSpaceHelper.get_drawib_aliasname_dict_for_path(lod_folder_path).items():
                if draw_ib not in drawib_aliasname_dict:
                    drawib_aliasname_dict[draw_ib] = alias_name
        return drawib_aliasname_dict

    @staticmethod
    def get_hash_deduped_texture_info_dict(submesh_folder_name: str) -> Dict[str, DedupedTextureInfo]:
        draw_ib_folder_path = WorkSpaceHelper.get_submesh_folder_path(submesh_folder_name) + "\\"
        component_name__drawcall_indexlist_json_path = os.path.join(draw_ib_folder_path, "ComponentName_DrawCallIndexList.json")
        trianglelist_deduped_filename_json_path = os.path.join(draw_ib_folder_path, "TrianglelistDedupedFileName.json")

        component_name__drawcall_indexlist_json_dict = JsonUtils.LoadFromFile(component_name__drawcall_indexlist_json_path)

        drawcall_component_count_dict = {}
        for component_name, drawcall_indexlist in component_name__drawcall_indexlist_json_dict.items():
            for drawcall_index in drawcall_indexlist:
                drawcall_component_count_dict[drawcall_index] = component_name.split(" ")[1]

        trianglelist_deduped_filename_json_dict = JsonUtils.LoadFromFile(trianglelist_deduped_filename_json_path)

        deduped_filename_drawcall_index_list_dict = {}
        for trianglelist_deduped_filename, deduped_kv_dict in trianglelist_deduped_filename_json_dict.items():
            deduped_filename: str = deduped_kv_dict["FALogDedupedFileName"]
            draw_call_index: str = trianglelist_deduped_filename[0:6]

            drawcall_index_list = deduped_filename_drawcall_index_list_dict.get(deduped_filename, [])
            if draw_call_index not in drawcall_index_list:
                drawcall_index_list.append(draw_call_index)

            deduped_filename_drawcall_index_list_dict[deduped_filename] = drawcall_index_list

        hash_deduped_texture_info_dict = {}

        for deduped_filename, drawcall_index_list in deduped_filename_drawcall_index_list_dict.items():
            used_component_count_list = []

            filename_parts = deduped_filename.split("_")
            original_hash = filename_parts[0] if len(filename_parts) > 0 else ""
            render_hash = filename_parts[1].split("-")[0] if len(filename_parts) > 1 else ""

            base_name = os.path.splitext(deduped_filename)[0]
            fmt = ""
            try:
                first_underscore = base_name.find("_")
                if first_underscore != -1:
                    dash_after_underscore = base_name.find("-", first_underscore + 1)
                    if dash_after_underscore != -1:
                        fmt = base_name[dash_after_underscore + 1:]
                if not fmt:
                    if "-" in base_name:
                        fmt = base_name.rsplit("-", 1)[-1]
                    else:
                        parts = base_name.split("_")
                        if len(parts) > 2:
                            fmt = parts[-1]
                        else:
                            fmt = ""
                fmt = fmt.strip()
            except Exception:
                fmt = ""

            format_name = fmt

            print(format_name)

            for draw_call_index in drawcall_index_list:
                matched_component_count = drawcall_component_count_dict.get(draw_call_index, "")
                if matched_component_count != "":
                    if matched_component_count not in used_component_count_list:
                        used_component_count_list.append(matched_component_count)

            used_component_count_list.sort()

            componet_count_list_str = ""
            for unique_component_count_str in used_component_count_list:
                componet_count_list_str = componet_count_list_str + unique_component_count_str + "."

            deduped_texture_info = DedupedTextureInfo()
            deduped_texture_info.original_hash = original_hash
            deduped_texture_info.render_hash = render_hash
            deduped_texture_info.format = format_name
            deduped_texture_info.componet_count_list_str = componet_count_list_str

            hash_deduped_texture_info_dict[original_hash] = deduped_texture_info

        return hash_deduped_texture_info_dict
