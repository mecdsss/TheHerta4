import os
import re

import bpy

from ..utils.ssmt_error_utils import SSMTErrorUtils


_PREFIX_START_PATTERN = re.compile(r"^[A-Za-z0-9]{6,}$")
_PREFIX_PART_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
_KNOWN_SEPARATORS = ()
_PREFIX_CHECK_SEPARATORS = (".",)
_BLENDER_SUFFIX_PATTERN = re.compile(r"\.\d{3,}$")
_BLENDER_SUFFIX_INNER_PATTERN = re.compile(r"\.\d{3,}")
_LOD_PREFIX_PATTERN = re.compile(r"^(LOD\d+)\.(.+)$", re.IGNORECASE)
_RUNTIME_SUFFIX_PATTERNS = (
    re.compile(r"_chain\d+_dup\d+_vgtest_unassigned_copy$"),
    re.compile(r"_chain\d+_vgtest_unassigned_copy$"),
    re.compile(r"_dup\d+_vgtest_unassigned_copy$"),
    re.compile(r"_vgtest_unassigned_copy$"),
    re.compile(r"_chain\d+_dup\d+_vgtest_copy$"),
    re.compile(r"_chain\d+_vgtest_copy$"),
    re.compile(r"_dup\d+_vgtest_copy$"),
    re.compile(r"_vgtest_copy$"),
    re.compile(r"_chain\d+_dup\d+_copy_temp$"),
    re.compile(r"_chain\d+_dup\d+_copy$"),
    re.compile(r"_chain\d+_dup\d+$"),
    re.compile(r"_chain\d+_copy_temp$"),
    re.compile(r"_chain\d+_copy$"),
    re.compile(r"_chain\d+$"),
    re.compile(r"_dup\d+_copy_temp$"),
    re.compile(r"_dup\d+_copy$"),
    re.compile(r"_dup\d+$"),
    re.compile(r"_copy_temp$"),
    re.compile(r"_copy$"),
)


class ObjectPrefixHelper:
    @staticmethod
    def _compose_lod_prefix(lod_name: str, bare_prefix: str) -> str:
        normalized_lod_name = (lod_name or "").strip()
        normalized_bare_prefix = (bare_prefix or "").strip()
        if normalized_lod_name and normalized_bare_prefix:
            return normalized_lod_name + "." + normalized_bare_prefix
        return normalized_bare_prefix

    @staticmethod
    def normalize_prefix(prefix: str) -> str:
        return (prefix or "").strip().strip("-_. ")

    @classmethod
    def split_lod_prefix(cls, name: str) -> tuple[str, str]:
        clean_name = (name or "").strip()
        match = _LOD_PREFIX_PATTERN.match(clean_name)
        if not match:
            return "", clean_name
        return match.group(1), match.group(2)

    @staticmethod
    def _strip_blender_suffix(name: str) -> tuple[str, str]:
        match = _BLENDER_SUFFIX_PATTERN.search(name)
        if match:
            return name[:match.start()], match.group()
        return name, ""

    @staticmethod
    def _strip_runtime_suffix(name: str) -> tuple[str, str]:
        for pattern in _RUNTIME_SUFFIX_PATTERNS:
            match = pattern.search(name)
            if match:
                return name[:match.start()], match.group()
        return name, ""

    @classmethod
    def _is_structured_prefix(cls, prefix_candidate: str) -> bool:
        clean_prefix = cls.normalize_prefix(prefix_candidate)
        if not clean_prefix:
            return False

        _lod_name, bare_prefix = cls.split_lod_prefix(clean_prefix)
        parsed = cls._extract_hyphen_prefix(bare_prefix)
        if not parsed:
            return False

        parsed_prefix, _ = parsed
        return parsed_prefix == bare_prefix

    @classmethod
    def _extract_hyphen_prefix(cls, object_name: str):
        clean_name = (object_name or "").strip()
        name_without_suffix, _blender_suffix = cls._strip_blender_suffix(clean_name)
        name_without_suffix, _runtime_suffix = cls._strip_runtime_suffix(name_without_suffix)
        prefix_candidate = name_without_suffix.split(".", 1)[0]
        parts = [part.strip() for part in prefix_candidate.split("-") if part.strip()]
        if len(parts) < 2:
            return None
        if not _PREFIX_START_PATTERN.fullmatch(parts[0]):
            return None

        prefix_parts = [parts[0]]
        for part in parts[1:3]:
            if not _PREFIX_PART_PATTERN.fullmatch(part):
                break
            prefix_parts.append(part)

        return "-".join(prefix_parts), "-"

    @classmethod
    def _get_workspace_unique_str_from_object(cls, object_name: str) -> str:
        try:
            obj = bpy.data.objects.get(object_name)
        except Exception:
            obj = None

        if obj is None:
            return ""

        return cls.normalize_prefix(str(obj.get("3DMigoto:WorkspaceUniqueStr", "") or ""))

    @classmethod
    def _resolve_incomplete_prefix_from_workspace(cls, object_name: str, lod_name: str, prefix_candidate: str) -> str:
        clean_prefix = cls.normalize_prefix(prefix_candidate)
        if not clean_prefix:
            return clean_prefix

        prefix_parts = cls.parse_prefix_parts(clean_prefix)
        draw_ib = str(prefix_parts.get("draw_ib", "") or "").strip()
        index_count = str(prefix_parts.get("index_count", "") or "").strip()
        first_index = str(prefix_parts.get("first_index", "") or "").strip()
        if not draw_ib or not index_count or first_index:
            return clean_prefix

        workspace_unique_str = cls._get_workspace_unique_str_from_object(object_name)
        if workspace_unique_str:
            workspace_lod_name, workspace_bare_unique_str = cls.split_lod_prefix(workspace_unique_str)
            workspace_parts = cls.parse_prefix_parts(workspace_unique_str)
            if (
                str(workspace_parts.get("draw_ib", "") or "").strip() == draw_ib
                and str(workspace_parts.get("index_count", "") or "").strip() == index_count
                and str(workspace_parts.get("first_index", "") or "").strip()
                and (not lod_name or not workspace_lod_name or workspace_lod_name == lod_name)
            ):
                return workspace_bare_unique_str

        try:
            from .global_config import GlobalConfig
            from .workspace_helper import WorkSpaceHelper
        except Exception:
            return clean_prefix

        workspace_folder = str(GlobalConfig.path_workspace_folder() or "").strip()
        if not workspace_folder:
            return clean_prefix

        candidate_base_paths = [workspace_folder, *WorkSpaceHelper.get_workspace_partition_folderpath_list()]
        matched_prefixes = []

        for base_path in candidate_base_paths:
            search_roots = []
            if lod_name:
                search_roots.append(os.path.join(base_path, lod_name))
            search_roots.append(base_path)

            for search_root in search_roots:
                if not os.path.isdir(search_root):
                    continue

                for submesh_folder_path in WorkSpaceHelper._get_submesh_folderpath_list_from(search_root):
                    folder_name = os.path.basename(submesh_folder_path)
                    folder_lod_name, folder_bare_name = cls.split_lod_prefix(folder_name)
                    if lod_name and folder_lod_name and folder_lod_name != lod_name:
                        continue

                    folder_prefix = cls.normalize_prefix(folder_bare_name.split(".", 1)[0])
                    folder_parts = cls.parse_prefix_parts(folder_prefix)
                    if (
                        str(folder_parts.get("draw_ib", "") or "").strip() != draw_ib
                        or str(folder_parts.get("index_count", "") or "").strip() != index_count
                        or not str(folder_parts.get("first_index", "") or "").strip()
                    ):
                        continue

                    resolved_prefix = cls._compose_lod_prefix(lod_name or folder_lod_name, folder_prefix)
                    if resolved_prefix not in matched_prefixes:
                        matched_prefixes.append(resolved_prefix)

        if len(matched_prefixes) != 1:
            return clean_prefix

        _resolved_lod_name, resolved_bare_prefix = cls.split_lod_prefix(matched_prefixes[0])
        return resolved_bare_prefix

    @classmethod
    def extract_prefix_info(cls, object_name: str):
        clean_name = (object_name or "").strip()
        if not clean_name:
            return None

        lod_name, bare_name = cls.split_lod_prefix(clean_name)

        def with_lod(prefix: str, separator: str):
            clean_prefix = cls.normalize_prefix(prefix)
            if not clean_prefix:
                return None
            if lod_name:
                return f"{lod_name}.{clean_prefix}", separator
            return clean_prefix, separator

        if "." in bare_name:
            prefix = cls.normalize_prefix(bare_name.split(".", 1)[0])
            prefix = cls._resolve_incomplete_prefix_from_workspace(clean_name, lod_name, prefix)
            if cls._is_structured_prefix(prefix):
                return with_lod(prefix, ".")

        name_without_suffix, blender_suffix = cls._strip_blender_suffix(bare_name)

        for separator in _KNOWN_SEPARATORS:
            if separator not in name_without_suffix:
                continue
            prefix_candidate = cls.normalize_prefix(name_without_suffix.rsplit(separator, 1)[0])
            if not prefix_candidate:
                continue
            if _BLENDER_SUFFIX_INNER_PATTERN.search(prefix_candidate):
                continue
            return with_lod(prefix_candidate, separator)

        parsed = cls._extract_hyphen_prefix(bare_name)
        if not parsed:
            return None

        prefix, _separator = parsed
        prefix = cls._resolve_incomplete_prefix_from_workspace(clean_name, lod_name, prefix)
        bare_name_without_runtime_suffix, _runtime_suffix = cls._strip_runtime_suffix(bare_name)
        if (
            cls.normalize_prefix(bare_name) == prefix
            or cls.normalize_prefix(bare_name_without_runtime_suffix) == prefix
        ):
            return with_lod(prefix, ".")
        return with_lod(prefix, "-")

    @classmethod
    def split_name_and_prefix(cls, object_name: str, prefix: str = "", separator: str = ""):
        clean_name = object_name or ""
        clean_prefix = cls.normalize_prefix(prefix)
        clean_separator = separator or "."

        if clean_prefix and clean_name == clean_prefix:
            return clean_prefix, clean_separator, ""

        if clean_prefix and clean_name.startswith(clean_prefix + clean_separator):
            return clean_prefix, clean_separator, clean_name[len(clean_prefix + clean_separator):]

        parsed = cls.extract_prefix_info(clean_name)
        if parsed:
            parsed_prefix, parsed_separator = parsed
            if clean_name == parsed_prefix:
                return parsed_prefix, parsed_separator, ""
            token = parsed_prefix + parsed_separator
            if clean_name.startswith(token):
                return parsed_prefix, parsed_separator, clean_name[len(token):]

        return "", clean_separator, clean_name

    @classmethod
    def has_prefix(cls, object_name: str, prefix: str) -> bool:
        clean_prefix = cls.normalize_prefix(prefix)
        if not clean_prefix:
            return False
        if object_name == clean_prefix:
            return True
        return any(object_name.startswith(clean_prefix + separator) for separator in _PREFIX_CHECK_SEPARATORS)

    @classmethod
    def replace_prefix(cls, object_name: str, new_prefix: str, separator: str = ".", old_prefix: str = "", old_separator: str = "") -> str:
        _, _, base_name = cls.split_name_and_prefix(object_name, old_prefix, old_separator)
        clean_prefix = cls.normalize_prefix(new_prefix)
        clean_separator = separator or "."
        if not clean_prefix:
            return base_name
        if not base_name:
            return clean_prefix
        return f"{clean_prefix}{clean_separator}{base_name}"

    @classmethod
    def parse_prefix_parts(cls, prefix: str) -> dict:
        clean_prefix = cls.normalize_prefix(prefix)
        lod_name, bare_prefix = cls.split_lod_prefix(clean_prefix)
        parts = [part.strip() for part in bare_prefix.split("-") if part.strip()]
        unique_str = f"{lod_name}.{bare_prefix}" if lod_name else bare_prefix
        return {
            "lod_name": lod_name,
            "unique_str": unique_str,
            "bare_unique_str": bare_prefix,
            "draw_ib": parts[0] if len(parts) >= 1 else "",
            "index_count": parts[1] if len(parts) >= 2 else "",
            "first_index": parts[2] if len(parts) >= 3 else "",
            "component": parts[1] if len(parts) >= 2 else "",
        }

    @classmethod
    def get_node_prefix_info(cls, node):
        object_name = getattr(node, "object_name", "")
        parsed_prefix_info = cls.extract_prefix_info(object_name)
        if parsed_prefix_info:
            return parsed_prefix_info

        stored_prefix = cls.normalize_prefix(getattr(node, "object_prefix", ""))
        stored_separator = getattr(node, "prefix_separator", "") or "."
        if stored_prefix:
            return stored_prefix, stored_separator

        return None

    @classmethod
    def get_node_prefix_info_with_source(cls, node):
        object_name = getattr(node, "object_name", "")
        parsed_prefix_info = cls.extract_prefix_info(object_name)
        if parsed_prefix_info:
            return parsed_prefix_info[0], parsed_prefix_info[1], "object_name"

        stored_prefix = cls.normalize_prefix(getattr(node, "object_prefix", ""))
        stored_separator = getattr(node, "prefix_separator", "") or "."
        if stored_prefix:
            return stored_prefix, stored_separator, "node_storage"

        return None

    @classmethod
    def require_node_prefix_info(cls, node):
        prefix_info = cls.get_node_prefix_info_with_source(node)
        if prefix_info:
            return prefix_info[0], prefix_info[1]

        object_name = getattr(node, "object_name", "") or getattr(node, "name", "<未命名节点>")
        SSMTErrorUtils.raise_fatal(
            f"物体 '{object_name}' 缺少前缀信息：既无法从物体名称解析前缀，也没有可用的节点内存储前缀"
        )

    @classmethod
    def build_virtual_object_name_for_node(cls, node, strict: bool = False) -> str:
        object_name = getattr(node, "object_name", "")
        if object_name:
            parsed_prefix_info = cls.extract_prefix_info(object_name)
            if parsed_prefix_info:
                return object_name
        prefix_info = cls.get_node_prefix_info_with_source(node)
        if not prefix_info and strict:
            prefix, separator = cls.require_node_prefix_info(node)
            prefix_info = (prefix, separator, "required")
        if not prefix_info:
            return object_name
        prefix, separator, source = prefix_info
        effective_separator = "." if source == "node_storage" else separator
        return cls.replace_prefix(object_name, prefix, effective_separator, prefix, separator)

    @classmethod
    def build_effective_object_name(cls, object_name: str, stored_prefix: str = "", stored_separator: str = ".", strict: bool = False) -> str:
        parsed_prefix_info = cls.extract_prefix_info(object_name)
        if parsed_prefix_info:
            return object_name

        clean_prefix = cls.normalize_prefix(stored_prefix)
        if clean_prefix:
            return cls.replace_prefix(object_name, clean_prefix, ".", clean_prefix, stored_separator or ".")

        if strict:
            SSMTErrorUtils.raise_fatal(
                f"物体 '{object_name or '<空名称>'}' 缺少前缀信息：既无法从物体名称解析前缀，也没有可用的节点内存储前缀"
            )

        return object_name

    @classmethod
    def resolve_source_object_name(cls, object_name: str) -> str:
        if not object_name:
            return object_name

        prefix, separator, base_name = cls.split_name_and_prefix(object_name)
        if prefix and separator == "." and base_name:
            if bpy.data.objects.get(base_name) is not None:
                return base_name

        if prefix:
            for obj in bpy.data.objects:
                if obj is None:
                    continue
                try:
                    current_name = obj.name
                except (AttributeError, ReferenceError):
                    continue

                if current_name == object_name:
                    return current_name
                if cls.has_prefix(current_name, prefix):
                    return current_name

        return object_name
