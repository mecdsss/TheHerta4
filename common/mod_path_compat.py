import os
import re
from collections import OrderedDict


_RESOURCE_POSITION_PATTERN = re.compile(
    r"^\[(Resource_?[A-Za-z0-9.]+(?:_[A-Za-z0-9.]+)*_?Position)(\d*)\]$"
)
_TEXTURE_OVERRIDE_POSITION_ALIAS_PATTERN = re.compile(
    r"^\[TextureOverride[^\]]*Position_1\]$"
)


def iter_hash_buffer_candidates(folder_path: str, hash_filter: str, file_suffix: str) -> list[dict]:
    normalized_filter = str(hash_filter or "").strip()
    normalized_suffix = str(file_suffix or "").strip()
    if not normalized_filter or not normalized_suffix or not os.path.isdir(folder_path):
        return []

    candidates = []
    normalized_prefix = _normalize_hash_prefix(normalized_filter)
    for filename in os.listdir(folder_path):
        if not filename.endswith(normalized_suffix):
            continue

        stem = filename[: -len(normalized_suffix)]
        stem_prefix = _normalize_hash_prefix(stem)
        if not _stem_matches_filter(stem, stem_prefix, normalized_filter, normalized_prefix):
            continue

        candidates.append(
            {
                "filename": filename,
                "stem": stem,
                "path": os.path.join(folder_path, filename),
                "prefix": stem_prefix,
            }
        )

    candidates.sort(key=lambda item: (0 if item["stem"] == normalized_filter else 1, item["filename"].lower()))
    return candidates


def resolve_hash_buffer_candidate(folder_path: str, hash_filter: str, file_suffix: str, preferred_hashes=None) -> tuple[str, str]:
    normalized_filter = str(hash_filter or "").strip()
    normalized_suffix = str(file_suffix or "").strip()
    preferred_values = []
    for candidate in list(preferred_hashes or []) + [normalized_filter]:
        normalized_candidate = str(candidate or "").strip()
        if normalized_candidate and normalized_candidate not in preferred_values:
            preferred_values.append(normalized_candidate)

    candidate_map = OrderedDict()
    for preferred_value in preferred_values:
        for candidate in iter_hash_buffer_candidates(folder_path, preferred_value, normalized_suffix):
            candidate_map.setdefault(candidate["stem"], candidate)

    if candidate_map:
        best_match = next(iter(candidate_map.values()))
        return best_match["path"], best_match["stem"]

    fallback_stem = preferred_values[0] if preferred_values else normalized_filter
    fallback_path = os.path.join(folder_path, f"{fallback_stem}{normalized_suffix}")
    return fallback_path, fallback_stem


def iter_position_buffer_candidates(folder_path: str, hash_filter: str) -> list[dict]:
    return iter_hash_buffer_candidates(folder_path, hash_filter, "-Position.buf")


def resolve_position_buffer_candidate(folder_path: str, hash_filter: str, preferred_hashes=None) -> tuple[str, str]:
    return resolve_hash_buffer_candidate(
        folder_path,
        hash_filter,
        "-Position.buf",
        preferred_hashes=preferred_hashes,
    )


def collect_base_position_resource_map(sections, prefix_extractor) -> dict:
    resource_map = {}
    for section_name in sections.keys():
        match = _RESOURCE_POSITION_PATTERN.match(str(section_name or "").strip())
        if not match:
            continue

        resource_name, numeric_suffix = match.groups()
        if numeric_suffix:
            continue

        hash_fragment = resource_name
        if hash_fragment.startswith("Resource_"):
            hash_fragment = hash_fragment[len("Resource_") :]
            if hash_fragment.endswith("_Position"):
                hash_fragment = hash_fragment[: -len("_Position")]
        elif hash_fragment.startswith("Resource") and hash_fragment.endswith("Position"):
            hash_fragment = hash_fragment[len("Resource") : -len("Position")]
        else:
            continue

        normalized_hash = hash_fragment.replace("_", "-")
        hash_prefix = prefix_extractor(normalized_hash)
        if not hash_prefix:
            continue

        resource_entries = resource_map.setdefault(hash_prefix, [])
        if resource_name not in resource_entries:
            resource_entries.append(resource_name)

    return resource_map


def ensure_resource_alias_section(sections, resource_name: str, alias_suffix: str, source_candidates=None) -> str:
    """确保资源别名 section 存在，不存在则从源 section 复制"""
    alias_section_name = f"[{resource_name}{alias_suffix}]"
    if alias_section_name in sections:
        return alias_section_name

    source_section_name = None
    candidate_section_names = [f"[{resource_name}]"]
    for candidate in list(source_candidates or []):
        clean_candidate = str(candidate or "").strip()
        if not clean_candidate:
            continue
        if not clean_candidate.startswith("["):
            clean_candidate = f"[{clean_candidate}]"
        candidate_section_names.append(clean_candidate)

    for candidate_section_name in candidate_section_names:
        if candidate_section_name in sections:
            source_section_name = candidate_section_name
            break

    if source_section_name is None:
        return alias_section_name

    sections[alias_section_name] = list(sections.get(source_section_name, []))
    return alias_section_name


def find_base_position_resource_name(
    sections,
    hash_filter: str,
    base_name: str = "",
    preferred_names=None,
    fallback_name: str = "",
) -> str:
    matched_resource_names = []
    match_tokens = _build_resource_match_tokens(hash_filter, base_name, *(preferred_names or []))

    for section_name in sections.keys():
        match = _RESOURCE_POSITION_PATTERN.match(str(section_name or "").strip())
        if not match:
            continue

        resource_name, numeric_suffix = match.groups()
        if numeric_suffix:
            continue
        if _resource_name_matches_tokens(resource_name, match_tokens):
            matched_resource_names.append(resource_name)

    for preferred_name in list(preferred_names or []):
        clean_preferred_name = section_to_resource_name(preferred_name)
        if clean_preferred_name in matched_resource_names:
            return clean_preferred_name

    if matched_resource_names:
        return matched_resource_names[0]

    for preferred_name in list(preferred_names or []):
        clean_preferred_name = section_to_resource_name(preferred_name)
        if clean_preferred_name:
            return clean_preferred_name

    return section_to_resource_name(fallback_name)


def is_stale_texture_override_position_alias_section(section_name: str, hash_filter: str = "") -> bool:
    clean_section_name = str(section_name or "").strip()
    if not _TEXTURE_OVERRIDE_POSITION_ALIAS_PATTERN.match(clean_section_name):
        return False
    return _text_matches_hash_filter(clean_section_name, hash_filter)


def is_stale_texture_override_position_copy_desc_line(line: str, hash_filter: str = "") -> bool:
    clean_line = str(line or "").strip()
    if not clean_line.startswith("post TextureOverride"):
        return False
    marker = " = copy_desc "
    if marker not in clean_line:
        return False
    left_name, right_name = clean_line[len("post ") :].split(marker, 1)
    if not left_name.startswith("TextureOverride") or not right_name.startswith("TextureOverride"):
        return False
    if "Position" not in left_name:
        return False
    return is_stale_texture_override_position_alias_section(
        resource_name_to_section(right_name),
        hash_filter,
    )


def collect_stale_texture_override_position_alias_names(lines, hash_filter: str = "") -> list[str]:
    alias_names = []
    for line in list(lines or []):
        alias_name = _extract_stale_texture_override_position_copy_desc_alias(line, hash_filter)
        if alias_name and alias_name not in alias_names:
            alias_names.append(alias_name)
    return alias_names


def resource_name_to_section(resource_name: str) -> str:
    clean_name = str(resource_name or "").strip()
    if clean_name.startswith("[") and clean_name.endswith("]"):
        return clean_name
    return f"[{clean_name}]"


def section_to_resource_name(section_name: str) -> str:
    """将 INI section 名称转换为资源名称（移除方括号）"""
    clean_name = str(section_name or "").strip()
    if clean_name.startswith("[") and clean_name.endswith("]"):
        return clean_name[1:-1]
    return clean_name


def derive_shapekey_base_resource_name(base_resource_name: str) -> str:
    clean_name = section_to_resource_name(base_resource_name)
    if clean_name.endswith("_Position"):
        return clean_name + "0000"
    if clean_name.endswith("Position"):
        return clean_name + "0000"
    return clean_name + "_Position0000"


def derive_shapekey_slot_resource_name(base_resource_name: str, slot_num: int, suffix: str = "") -> str:
    """派生形态键插槽资源名称"""
    clean_name = section_to_resource_name(base_resource_name)
    if clean_name.endswith("_Position"):
        return f"{clean_name}1{int(slot_num):03d}{suffix}"
    if clean_name.endswith("Position"):
        return f"{clean_name}1{int(slot_num):03d}{suffix}"
    return f"{clean_name}_Position1{int(slot_num):03d}{suffix}"


def derive_shapekey_slot_map_resource_name(base_resource_name: str, slot_num: int) -> str:
    return derive_shapekey_slot_resource_name(base_resource_name, slot_num, "_Map")


def derive_shapekey_freq_resource_name(base_resource_name: str) -> str:
    clean_name = section_to_resource_name(base_resource_name)
    if clean_name.endswith("_Position"):
        return clean_name + "_FreqIndices"
    if clean_name.endswith("Position"):
        return clean_name + "_FreqIndices"
    return clean_name + "_Position_FreqIndices"


def derive_shapekey_merged_data_resource_name(base_resource_name: str, use_delta: bool) -> str:
    clean_name = section_to_resource_name(base_resource_name)
    suffix = "_Merged_PackedPosDelta" if use_delta else "_Merged_Packed"
    if clean_name.endswith("_Position"):
        return clean_name + suffix
    if clean_name.endswith("Position"):
        return clean_name + suffix
    return clean_name + "_Position" + suffix


def derive_shapekey_merged_map_resource_name(base_resource_name: str) -> str:
    clean_name = section_to_resource_name(base_resource_name)
    if clean_name.endswith("_Position"):
        return clean_name + "_Merged_Map"
    if clean_name.endswith("Position"):
        return clean_name + "_Merged_Map"
    return clean_name + "_Position_Merged_Map"


def _normalize_hash_prefix(value: str) -> str:
    normalized_value = str(value or "").strip()
    if normalized_value.upper().startswith("LOD") and "." in normalized_value:
        normalized_value = normalized_value.split(".", 1)[1]
    return normalized_value.split("-", 1)[0]


def _build_resource_match_tokens(*values) -> set[str]:
    tokens = set()
    for value in values:
        clean_value = section_to_resource_name(value)
        for candidate in (clean_value, _normalize_hash_prefix(clean_value)):
            normalized_candidate = _normalize_resource_match_text(candidate)
            if normalized_candidate:
                tokens.add(normalized_candidate)
    return tokens


def _resource_name_matches_tokens(resource_name: str, match_tokens: set[str]) -> bool:
    if not match_tokens:
        return False
    resource_key = _normalize_resource_match_text(resource_name)
    return any(token and token in resource_key for token in match_tokens)


def _text_matches_hash_filter(text: str, hash_filter: str) -> bool:
    match_tokens = _build_resource_match_tokens(hash_filter)
    if not match_tokens:
        return True
    text_key = _normalize_resource_match_text(text)
    return any(token and token in text_key for token in match_tokens)


def _extract_stale_texture_override_position_copy_desc_alias(line: str, hash_filter: str = "") -> str:
    if not is_stale_texture_override_position_copy_desc_line(line, hash_filter):
        return ""
    rhs = str(line or "").strip().split(" = copy_desc ", 1)[1].strip()
    section_name = resource_name_to_section(rhs)
    if not is_stale_texture_override_position_alias_section(section_name, hash_filter):
        return ""
    return rhs


def _normalize_resource_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _stem_matches_filter(stem: str, stem_prefix: str, raw_filter: str, filter_prefix: str) -> bool:
    normalized_stem = str(stem or "").strip()
    normalized_filter = str(raw_filter or "").strip()
    if not normalized_stem or not normalized_filter:
        return False
    if normalized_stem == normalized_filter:
        return True
    if normalized_stem.startswith(normalized_filter + "-") or normalized_stem.startswith(normalized_filter + "."):
        return True
    if normalized_filter.startswith(normalized_stem + "-") or normalized_filter.startswith(normalized_stem + "."):
        return True
    return bool(stem_prefix and filter_prefix and stem_prefix == filter_prefix)
