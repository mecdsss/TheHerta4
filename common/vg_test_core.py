from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Set


_MAPPING_VERSION = "TH4_VGTEST v1"
_SECTION_RE = re.compile(r"^\[Prefix:(?P<prefix>.+)\]$")
_LINE_RE = re.compile(r"^(?P<left>\d+)=(?P<right>\d+)$")

MAPPING_TEXT_PROP = "th4_vgtest_mapping_text"
MAPPING_ID_PROP = "th4_vgtest_mapping_id"
PREFIX_PROP = "th4_vgtest_prefix"
SOURCE_NAME_PROP = "th4_vgtest_source_name"


class VGTestError(RuntimeError):
    pass


@dataclass(frozen=True)
class VGTestObjectInfo:
    name: str
    prefix: str
    numeric_groups: List[int]


@dataclass(frozen=True)
class VGTestPrefixMapping:
    prefix: str
    local_to_global: Dict[int, int]

    @property
    def global_to_local(self) -> Dict[int, int]:
        return {global_value: local_value for local_value, global_value in self.local_to_global.items()}

    @property
    def global_set(self) -> Set[int]:
        return set(self.local_to_global.values())


@dataclass(frozen=True)
class VGTestMappingDocument:
    mapping_id: str
    object_order: List[str]
    prefix_order: List[str]
    prefixes: Dict[str, VGTestPrefixMapping]

    def get_prefix_for_global_group(self, group_number: int) -> str:
        for prefix in self.prefix_order:
            if group_number in self.prefixes[prefix].global_set:
                return prefix
        return ""

    def get_unique_prefix_for_groups(self, groups: Iterable[int]) -> str:
        resolved_prefixes = {
            self.get_prefix_for_global_group(group_number)
            for group_number in groups
            if self.get_prefix_for_global_group(group_number)
        }
        if len(resolved_prefixes) != 1:
            return ""
        return next(iter(resolved_prefixes))


@dataclass(frozen=True)
class VGTestVertexSplitProfile:
    owner_prefix: str
    compatible_prefixes: Set[str]
    is_boundary: bool


def build_runtime_vgtest_copy_name(object_name: str) -> str:
    name = str(object_name or "").strip()
    if not name:
        raise VGTestError("Cannot build VG Test runtime name from an empty object name.")

    if name.endswith("_copy"):
        return name[:-5] + "_vgtest" + "_copy"
    return name + "_vgtest" + "_copy"


def strip_runtime_vgtest_suffix(object_name: str) -> str:
    name = str(object_name or "").strip()
    if not name:
        return name
    patterns = (
        r"_chain\d+_dup\d+_vgtest_unassigned_copy$",
        r"_chain\d+_vgtest_unassigned_copy$",
        r"_dup\d+_vgtest_unassigned_copy$",
        r"_vgtest_unassigned_copy$",
        r"_chain\d+_dup\d+_vgtest_copy$",
        r"_chain\d+_vgtest_copy$",
        r"_dup\d+_vgtest_copy$",
        r"_vgtest_copy$",
    )
    result = name
    for pattern in patterns:
        result = re.sub(pattern, "", result)
    return result


def replace_runtime_object_prefix(object_name: str, current_prefix: str, target_prefix: str) -> str:
    clean_name = str(object_name or "").strip()
    clean_current_prefix = str(current_prefix or "").strip()
    clean_target_prefix = str(target_prefix or "").strip()

    if not clean_target_prefix:
        raise VGTestError("Target prefix cannot be empty.")
    if not clean_name or not clean_current_prefix:
        return clean_target_prefix

    dotted_token = clean_current_prefix + "."
    if clean_name.startswith(dotted_token):
        return clean_target_prefix + clean_name[len(clean_current_prefix):]
    if clean_name.startswith(clean_current_prefix):
        return clean_target_prefix + clean_name[len(clean_current_prefix):]
    return clean_target_prefix


def ensure_numeric_group_names(group_names: Sequence[str], object_name: str = "") -> List[int]:
    numeric_groups: List[int] = []
    invalid_groups = [str(group_name or "") for group_name in group_names if not str(group_name or "").isdigit()]
    if invalid_groups:
        suffix = f" '{object_name}'" if object_name else ""
        invalid_preview = ", ".join(invalid_groups[:8])
        remaining = len(invalid_groups) - 8
        if remaining > 0:
            invalid_preview = f"{invalid_preview}, ... (+{remaining})"
        raise VGTestError(f"VG Test only supports numeric vertex groups. Object{suffix} contains: {invalid_preview}")

    for group_name in group_names:
        numeric_groups.append(int(str(group_name)))
    return numeric_groups


def build_mapping_document(items: Sequence[VGTestObjectInfo], mapping_id: str = "") -> VGTestMappingDocument:
    if not items:
        raise VGTestError("VG Test mapping requires at least one object.")

    seen_prefixes: Set[str] = set()
    prefix_order: List[str] = []
    prefixes: Dict[str, VGTestPrefixMapping] = {}
    object_order: List[str] = []
    next_group = 0

    for item in items:
        clean_prefix = str(item.prefix or "").strip()
        if not clean_prefix:
            raise VGTestError(f"Object '{item.name}' is missing a resolvable prefix.")
        if clean_prefix in seen_prefixes:
            raise VGTestError(f"VG Test requires unique prefixes per mapping run. Duplicate prefix: {clean_prefix}")

        seen_prefixes.add(clean_prefix)
        prefix_order.append(clean_prefix)
        object_order.append(item.name)

        local_to_global: Dict[int, int] = {}
        for local_group in sorted(set(item.numeric_groups)):
            local_to_global[local_group] = next_group
            next_group += 1

        prefixes[clean_prefix] = VGTestPrefixMapping(prefix=clean_prefix, local_to_global=local_to_global)

    return VGTestMappingDocument(
        mapping_id=str(mapping_id or uuid.uuid4().hex),
        object_order=object_order,
        prefix_order=prefix_order,
        prefixes=prefixes,
    )


def serialize_mapping_document(document: VGTestMappingDocument) -> str:
    lines = [
        f"# {_MAPPING_VERSION}",
        f"# mapping_id={document.mapping_id}",
        f"# object_order={','.join(document.object_order)}",
        f"# prefix_order={','.join(document.prefix_order)}",
        "",
    ]

    for prefix in document.prefix_order:
        mapping = document.prefixes[prefix]
        lines.append(f"[Prefix:{prefix}]")
        for local_group, global_group in sorted(mapping.local_to_global.items()):
            lines.append(f"{local_group}={global_group}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_mapping_document(content: str) -> VGTestMappingDocument:
    mapping_id = ""
    object_order: List[str] = []
    prefix_order: List[str] = []
    prefixes: Dict[str, VGTestPrefixMapping] = {}

    current_prefix = ""
    current_mapping: Dict[int, int] = {}
    saw_header = False

    def commit_current_prefix():
        nonlocal current_prefix, current_mapping
        if not current_prefix:
            return
        prefixes[current_prefix] = VGTestPrefixMapping(
            prefix=current_prefix,
            local_to_global=dict(sorted(current_mapping.items())),
        )
        prefix_order.append(current_prefix)
        current_prefix = ""
        current_mapping = {}

    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            payload = line[1:].strip()
            if payload == _MAPPING_VERSION:
                saw_header = True
                continue
            if payload.startswith("mapping_id="):
                mapping_id = payload.split("=", 1)[1].strip()
                continue
            if payload.startswith("object_order="):
                object_order = [entry for entry in payload.split("=", 1)[1].split(",") if entry]
                continue
            continue

        section_match = _SECTION_RE.match(line)
        if section_match:
            commit_current_prefix()
            current_prefix = section_match.group("prefix").strip()
            if not current_prefix:
                raise VGTestError("VG Test mapping contains an empty prefix section.")
            if current_prefix in prefixes:
                raise VGTestError(f"VG Test mapping contains duplicate prefix section: {current_prefix}")
            continue

        line_match = _LINE_RE.match(line)
        if not line_match or not current_prefix:
            raise VGTestError(f"Invalid VG Test mapping line: {line}")
        current_mapping[int(line_match.group("left"))] = int(line_match.group("right"))

    commit_current_prefix()

    if not saw_header:
        raise VGTestError("Mapping text is not a TH4_VGTEST v1 document.")
    if not prefixes:
        raise VGTestError("VG Test mapping document contains no prefix mappings.")
    if not mapping_id:
        raise VGTestError("VG Test mapping document is missing mapping_id.")

    if not object_order:
        object_order = list(prefix_order)

    return VGTestMappingDocument(
        mapping_id=mapping_id,
        object_order=object_order,
        prefix_order=prefix_order,
        prefixes=prefixes,
    )


def remap_local_groups_to_global(group_numbers: Iterable[int], mapping: VGTestPrefixMapping) -> List[int]:
    result = []
    for local_group in group_numbers:
        if local_group not in mapping.local_to_global:
            raise VGTestError(f"Local vertex group {local_group} is not present in mapping for prefix '{mapping.prefix}'.")
        result.append(mapping.local_to_global[local_group])
    return result


def remap_global_groups_to_local(group_numbers: Iterable[int], mapping: VGTestPrefixMapping) -> List[int]:
    inverse_mapping = mapping.global_to_local
    result = []
    for global_group in group_numbers:
        if global_group not in inverse_mapping:
            raise VGTestError(f"Global vertex group {global_group} is not present in mapping for prefix '{mapping.prefix}'.")
        result.append(inverse_mapping[global_group])
    return result


def classify_face_prefixes(face_group_sets: Sequence[Set[int]], document: VGTestMappingDocument) -> List[Set[str]]:
    classified_faces: List[Set[str]] = []
    for group_set in face_group_sets:
        prefixes = {
            document.get_prefix_for_global_group(group_number)
            for group_number in group_set
            if document.get_prefix_for_global_group(group_number)
        }
        if not prefixes:
            raise VGTestError("VG Test split found geometry that does not belong to any mapped prefix.")
        classified_faces.append(prefixes)
    return classified_faces


def classify_vertex_prefixes(vertex_group_sets: Sequence[Set[int]], document: VGTestMappingDocument) -> List[Set[str]]:
    classified_vertices: List[Set[str]] = []
    for group_set in vertex_group_sets:
        prefixes = {
            document.get_prefix_for_global_group(group_number)
            for group_number in group_set
            if document.get_prefix_for_global_group(group_number)
        }
        classified_vertices.append(prefixes)
    return classified_vertices


def find_mixed_prefix_vertex_indices(vertex_group_sets: Sequence[Set[int]], document: VGTestMappingDocument) -> List[int]:
    mixed_indices: List[int] = []
    for index, prefixes in enumerate(classify_vertex_prefixes(vertex_group_sets, document)):
        if len(prefixes) > 1:
            mixed_indices.append(index)
    return mixed_indices


def collect_vertex_prefix_local_sets(
    group_numbers: Iterable[int],
    document: VGTestMappingDocument,
) -> Dict[str, Set[int]]:
    prefix_to_locals: Dict[str, Set[int]] = {}
    for group_number in group_numbers:
        prefix = document.get_prefix_for_global_group(int(group_number))
        if not prefix:
            continue
        local_group = document.prefixes[prefix].global_to_local[int(group_number)]
        prefix_to_locals.setdefault(prefix, set()).add(local_group)
    return prefix_to_locals


def collect_vertex_prefix_weight_totals(
    vertex_weights: Dict[int, float],
    document: VGTestMappingDocument,
) -> Dict[str, float]:
    prefix_to_weight: Dict[str, float] = {}
    for group_number, weight in dict(vertex_weights or {}).items():
        numeric_group = int(group_number)
        numeric_weight = float(weight)
        if numeric_weight <= 0.0:
            continue
        prefix = document.get_prefix_for_global_group(numeric_group)
        if not prefix:
            continue
        prefix_to_weight[prefix] = prefix_to_weight.get(prefix, 0.0) + numeric_weight
    return prefix_to_weight


def resolve_dominant_prefix(
    vertex_weights: Dict[int, float],
    document: VGTestMappingDocument,
) -> str:
    prefix_to_weight = collect_vertex_prefix_weight_totals(vertex_weights, document)
    if not prefix_to_weight:
        return ""

    max_total = max(prefix_to_weight.values())
    total_candidates = [
        prefix for prefix, total_weight in prefix_to_weight.items()
        if abs(total_weight - max_total) <= 1e-7
    ]
    if len(total_candidates) == 1:
        return total_candidates[0]

    prefix_to_max_single_weight: Dict[str, float] = {}
    for group_number, weight in dict(vertex_weights or {}).items():
        numeric_group = int(group_number)
        numeric_weight = float(weight)
        if numeric_weight <= 0.0:
            continue
        prefix = document.get_prefix_for_global_group(numeric_group)
        if prefix not in total_candidates:
            continue
        prefix_to_max_single_weight[prefix] = max(
            prefix_to_max_single_weight.get(prefix, 0.0),
            numeric_weight,
        )

    max_single = max(prefix_to_max_single_weight.values(), default=0.0)
    single_candidates = [
        prefix for prefix, single_weight in prefix_to_max_single_weight.items()
        if abs(single_weight - max_single) <= 1e-7
    ]
    if len(single_candidates) == 1:
        return single_candidates[0]

    for prefix in document.prefix_order:
        if prefix in single_candidates:
            return prefix
    return single_candidates[0] if single_candidates else total_candidates[0]


def resolve_vertex_split_profile(
    vertex_weights: Dict[int, float],
    document: VGTestMappingDocument,
) -> VGTestVertexSplitProfile:
    group_numbers = list(dict(vertex_weights or {}).keys())
    prefix_to_locals = collect_vertex_prefix_local_sets(group_numbers, document)
    if not prefix_to_locals:
        raise VGTestError("VG Test split found a vertex that does not belong to any mapped prefix.")

    compatible_prefixes = set(prefix_to_locals.keys())
    if len(prefix_to_locals) == 1:
        owner_prefix = next(iter(compatible_prefixes))
        return VGTestVertexSplitProfile(
            owner_prefix=owner_prefix,
            compatible_prefixes={owner_prefix},
            is_boundary=False,
        )

    owner_prefix = resolve_dominant_prefix(vertex_weights, document)
    if not owner_prefix:
        raise VGTestError("VG Test split could not resolve an owner prefix for a mixed-weight vertex.")
    return VGTestVertexSplitProfile(
        owner_prefix=owner_prefix,
        compatible_prefixes={owner_prefix},
        is_boundary=False,
    )


def filter_vertex_weights_for_prefix(
    vertex_weights: Dict[int, float],
    target_prefix: str,
    document: VGTestMappingDocument,
) -> Dict[int, float]:
    filtered: Dict[int, float] = {}
    for group_number, weight in dict(vertex_weights or {}).items():
        numeric_group = int(group_number)
        numeric_weight = float(weight)
        if numeric_weight <= 0.0:
            continue
        prefix = document.get_prefix_for_global_group(numeric_group)
        if prefix != target_prefix:
            continue
        filtered[numeric_group] = numeric_weight
    return filtered


def format_group_number_set(group_numbers: Iterable[int]) -> str:
    values = sorted({int(group_number) for group_number in group_numbers})
    if not values:
        return "(无)"
    return ", ".join(str(value) for value in values)


def format_prefix_local_sets(group_numbers: Iterable[int], document: VGTestMappingDocument) -> str:
    prefix_to_locals = collect_vertex_prefix_local_sets(group_numbers, document)
    if not prefix_to_locals:
        return "(无可识别前缀)"

    parts: List[str] = []
    for prefix in document.prefix_order:
        local_groups = prefix_to_locals.get(prefix)
        if not local_groups:
            continue
        parts.append(f"{prefix} -> [{format_group_number_set(local_groups)}]")
    return "; ".join(parts) if parts else "(无可识别前缀)"


def format_group_weight_map(group_weights: Dict[int, float]) -> str:
    if not group_weights:
        return "(无)"
    parts = [
        f"{int(group_number)}={float(weight):.4f}".rstrip("0").rstrip(".")
        for group_number, weight in sorted(group_weights.items(), key=lambda item: int(item[0]))
    ]
    return ", ".join(parts)


def describe_vertex_compatibility_reason(
    group_numbers: Iterable[int],
    document: VGTestMappingDocument,
) -> str:
    prefix_to_locals = collect_vertex_prefix_local_sets(group_numbers, document)
    if not prefix_to_locals:
        return "没有任何可识别的映射前缀"
    if len(prefix_to_locals) == 1:
        only_prefix = next(iter(prefix_to_locals.keys()))
        return f"仅属于前缀 {only_prefix}"

    shared_local_group = resolve_unique_shared_local_group(group_numbers, document)
    if shared_local_group is not None:
        prefixes = ", ".join(prefix_to_locals.keys())
        return f"命中多个前缀，但存在唯一共用局部组 {shared_local_group}，可归属前缀: {prefixes}"

    return "同时命中多个前缀，但不存在唯一共用局部组，因此无法归属到单一输出前缀"


def describe_vertex_weight_compatibility_reason(
    vertex_weights: Dict[int, float],
    document: VGTestMappingDocument,
) -> str:
    group_numbers = list(dict(vertex_weights or {}).keys())
    prefix_to_locals = collect_vertex_prefix_local_sets(group_numbers, document)
    if not prefix_to_locals:
        return "没有任何可识别的映射前缀"
    if len(prefix_to_locals) == 1:
        only_prefix = next(iter(prefix_to_locals.keys()))
        return f"仅属于前缀 {only_prefix}"

    shared_local_group = resolve_unique_shared_local_group(group_numbers, document)
    if shared_local_group is not None:
        prefixes = ", ".join(prefix_to_locals.keys())
        return f"同时命中多个前缀，但存在唯一共用局部组 {shared_local_group}，可作为边界顶点，兼容前缀: {prefixes}"

    dominant_prefix = resolve_dominant_prefix(vertex_weights, document)
    dominant_weight = collect_vertex_prefix_weight_totals(vertex_weights, document).get(dominant_prefix, 0.0)
    return f"同时命中多个前缀，且不存在唯一共用局部组，已按权重总和归属到前缀 {dominant_prefix} (总权重={dominant_weight:.4f})"


def resolve_shared_local_groups(
    group_numbers: Iterable[int],
    document: VGTestMappingDocument,
) -> Set[int]:
    prefix_to_locals = collect_vertex_prefix_local_sets(group_numbers, document)
    if len(prefix_to_locals) <= 1:
        return set()
    return set.intersection(*(set(local_groups) for local_groups in prefix_to_locals.values()))


def resolve_unique_shared_local_group(
    group_numbers: Iterable[int],
    document: VGTestMappingDocument,
) -> int | None:
    shared_local_groups = resolve_shared_local_groups(group_numbers, document)
    if len(shared_local_groups) != 1:
        return None
    return next(iter(shared_local_groups))


def get_vertex_compatible_prefixes(
    group_numbers: Iterable[int],
    document: VGTestMappingDocument,
) -> Set[str]:
    prefix_to_locals = collect_vertex_prefix_local_sets(group_numbers, document)
    if not prefix_to_locals:
        return set()
    if len(prefix_to_locals) == 1:
        return set(prefix_to_locals.keys())
    if resolve_unique_shared_local_group(group_numbers, document) is None:
        return set()
    return set(prefix_to_locals.keys())


def get_vertex_compatible_prefixes_from_weights(
    vertex_weights: Dict[int, float],
    document: VGTestMappingDocument,
) -> Set[str]:
    try:
        return set(resolve_vertex_split_profile(vertex_weights, document).compatible_prefixes)
    except VGTestError:
        return set()


def classify_faces_by_vertex_compatibility(
    face_vertex_indices: Sequence[Sequence[int]],
    vertex_group_sets: Sequence[Set[int]],
    document: VGTestMappingDocument,
) -> List[Set[str]]:
    vertex_compatible_prefixes = [
        get_vertex_compatible_prefixes(group_set, document)
        for group_set in vertex_group_sets
    ]

    classified_faces: List[Set[str]] = []
    for face_index, vertex_indices in enumerate(face_vertex_indices):
        assignable_prefixes: Set[str] | None = None
        for vertex_index in vertex_indices:
            compatible_prefixes = vertex_compatible_prefixes[vertex_index]
            if assignable_prefixes is None:
                assignable_prefixes = set(compatible_prefixes)
            else:
                assignable_prefixes &= compatible_prefixes

        if not assignable_prefixes:
            vertex_details: List[str] = []
            for vertex_index in list(vertex_indices)[:8]:
                vertex_group_set = vertex_group_sets[vertex_index] if vertex_index < len(vertex_group_sets) else set()
                compatible_prefixes = vertex_compatible_prefixes[vertex_index] if vertex_index < len(vertex_compatible_prefixes) else set()
                vertex_details.append(
                    f"顶点 {vertex_index}: 全局组[{format_group_number_set(vertex_group_set)}]，"
                    f"映射[{format_prefix_local_sets(vertex_group_set, document)}]，"
                    f"判定[{describe_vertex_compatibility_reason(vertex_group_set, document)}]，"
                    f"可分配前缀[{', '.join(sorted(compatible_prefixes)) if compatible_prefixes else '无'}]"
                )
            raise VGTestError(
                "VG Test 切割失败：发现一张面无法安全分配到任何目标前缀。\n"
                f"面索引：{face_index}\n"
                f"面顶点：{', '.join(str(vertex_index) for vertex_index in list(vertex_indices)[:8])}\n"
                "原因：这张面上的所有顶点，找不到一个共同可归属的目标前缀。"
                "通常表示这些顶点的数字顶点组映射后彼此不兼容，"
                "也就是至少有一个顶点不属于另外两个顶点共同支持的那一组前缀。\n"
                "顶点详情：\n"
                + "\n".join(vertex_details)
            )

        classified_faces.append(assignable_prefixes)

    return classified_faces


def classify_faces_by_vertex_weight_compatibility(
    face_vertex_indices: Sequence[Sequence[int]],
    vertex_weight_maps: Sequence[Dict[int, float]],
    document: VGTestMappingDocument,
) -> List[Set[str]]:
    vertex_compatible_prefixes = [
        get_vertex_compatible_prefixes_from_weights(weight_map, document)
        for weight_map in vertex_weight_maps
    ]

    classified_faces: List[Set[str]] = []
    for face_index, vertex_indices in enumerate(face_vertex_indices):
        assignable_prefixes: Set[str] | None = None
        for vertex_index in vertex_indices:
            compatible_prefixes = vertex_compatible_prefixes[vertex_index]
            if assignable_prefixes is None:
                assignable_prefixes = set(compatible_prefixes)
            else:
                assignable_prefixes &= compatible_prefixes

        if not assignable_prefixes:
            vertex_details: List[str] = []
            for vertex_index in list(vertex_indices)[:8]:
                vertex_weight_map = vertex_weight_maps[vertex_index] if vertex_index < len(vertex_weight_maps) else {}
                compatible_prefixes = vertex_compatible_prefixes[vertex_index] if vertex_index < len(vertex_compatible_prefixes) else set()
                vertex_details.append(
                    f"顶点 {vertex_index}: 权重[{format_group_weight_map(vertex_weight_map)}]，"
                    f"映射[{format_prefix_local_sets(vertex_weight_map.keys(), document)}]，"
                    f"判定[{describe_vertex_weight_compatibility_reason(vertex_weight_map, document)}]，"
                    f"可分配前缀[{', '.join(sorted(compatible_prefixes)) if compatible_prefixes else '无'}]"
                )
            raise VGTestError(
                "VG Test 切割失败：发现一张面无法安全分配到任何目标前缀。\n"
                f"面索引：{face_index}\n"
                f"面顶点：{', '.join(str(vertex_index) for vertex_index in list(vertex_indices)[:8])}\n"
                "原因：这张面上的所有顶点，找不到一个共同可归属的目标前缀。"
                "通常表示这些顶点在按权重归属后，仍然无法在同一输出物体中共存。\n"
                "顶点详情：\n"
                + "\n".join(vertex_details)
            )

        classified_faces.append(assignable_prefixes)

    return classified_faces


def build_target_prefix_vertex_weights(
    vertex_weights: Dict[int, float],
    target_prefix: str,
    document: VGTestMappingDocument,
) -> Dict[int, float]:
    numeric_weights = {
        int(group_number): float(weight)
        for group_number, weight in dict(vertex_weights or {}).items()
        if float(weight) > 0.0 and document.get_prefix_for_global_group(int(group_number))
    }
    if not numeric_weights:
        return {}

    prefix_to_locals = collect_vertex_prefix_local_sets(numeric_weights.keys(), document)
    if target_prefix not in prefix_to_locals:
        raise VGTestError(
            f"Vertex weights cannot be represented in target prefix '{target_prefix}'."
        )

    if len(prefix_to_locals) == 1:
        return {
            group_number: weight
            for group_number, weight in numeric_weights.items()
            if document.get_prefix_for_global_group(group_number) == target_prefix
        }

    shared_local_groups = resolve_shared_local_groups(numeric_weights.keys(), document)
    if not shared_local_groups:
        raise VGTestError(
            "VG Test 切割失败：接缝顶点无法折叠到目标前缀的共用局部顶点组。\n"
            f"目标前缀：{target_prefix}\n"
            f"接缝全局权重：{format_group_weight_map(numeric_weights)}\n"
            f"映射关系：{format_prefix_local_sets(numeric_weights.keys(), document)}\n"
            "原因：这个接缝点同时混合了多个前缀的权重，但这些前缀之间没有共同的局部顶点组编号。"
            "按当前规则，切割边缘只能由两侧共用的局部顶点组承接权重。"
        )

    target_mapping = document.prefixes[target_prefix]
    target_global_groups: Dict[int, int] = {}
    for shared_local_group in sorted(shared_local_groups):
        target_global_group = target_mapping.local_to_global.get(shared_local_group)
        if target_global_group is None:
            raise VGTestError(
                "VG Test 切割失败：目标前缀缺少接缝需要的共用局部顶点组。\n"
                f"目标前缀：{target_prefix}\n"
                f"缺少局部组：{shared_local_group}"
            )
        target_global_groups[shared_local_group] = target_global_group

    shared_weight_total = 0.0
    for group_number, weight in numeric_weights.items():
        prefix = document.get_prefix_for_global_group(group_number)
        if not prefix:
            continue
        local_group = document.prefixes[prefix].global_to_local[group_number]
        if local_group in shared_local_groups:
            shared_weight_total += float(weight)

    if shared_weight_total <= 0.0:
        raise VGTestError(
            "VG Test 切割失败：接缝共用局部顶点组没有可用的正权重。"
        )

    if len(target_global_groups) == 1:
        only_local_group, only_global_group = next(iter(target_global_groups.items()))
        return {int(only_global_group): 1.0}

    total_weight = sum(numeric_weights.values())
    result: Dict[int, float] = {}
    for shared_local_group, target_global_group in target_global_groups.items():
        accumulated = 0.0
        for group_number, weight in numeric_weights.items():
            prefix = document.get_prefix_for_global_group(group_number)
            if not prefix:
                continue
            local_group = document.prefixes[prefix].global_to_local[group_number]
            if local_group == shared_local_group:
                accumulated += float(weight)
        result[int(target_global_group)] = accumulated / shared_weight_total if total_weight > 0.0 else 0.0

    normalized_total = sum(result.values())
    if normalized_total > 0.0:
        for group_number in list(result.keys()):
            result[group_number] = result[group_number] / normalized_total
    return result
