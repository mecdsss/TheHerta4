from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set, Tuple

import bpy
import bmesh

from ..common.object_prefix_helper import ObjectPrefixHelper
from ..utils.log_utils import LOG
from ..common.vg_test_core import (
    MAPPING_ID_PROP,
    MAPPING_TEXT_PROP,
    PREFIX_PROP,
    SOURCE_NAME_PROP,
    VGTestError,
    VGTestMappingDocument,
    VGTestObjectInfo,
    build_target_prefix_vertex_weights,
    build_mapping_document,
    build_runtime_vgtest_copy_name,
    ensure_numeric_group_names,
    filter_vertex_weights_for_prefix,
    parse_mapping_document,
    replace_runtime_object_prefix,
    resolve_dominant_prefix,
    resolve_vertex_split_profile,
    serialize_mapping_document,
)


VG_TEST_TEXT_PREFIX = "VGTestMapping"
VG_TEST_PREVIEW_COLLECTION = "TH4_VGTestPreview"
_VERTEX_MERGE_DECIMALS = 7


def _build_runtime_vgtest_unassigned_copy_name(object_name: str) -> str:
    name = str(object_name or "").strip()
    if not name:
        raise VGTestError("Cannot build VG Test runtime name from an empty object name.")
    if name.endswith("_vgtest_copy"):
        return name[:-12] + "_vgtest_unassigned_copy"
    if name.endswith("_copy"):
        return name[:-5] + "_vgtest_unassigned_copy"
    return name + "_vgtest_unassigned_copy"


def _build_runtime_unassigned_copy_name(object_name: str) -> str:
    runtime_copy_name = build_runtime_vgtest_copy_name(object_name)
    if runtime_copy_name.endswith("_copy"):
        return runtime_copy_name[:-5] + "_unassigned_copy"
    return runtime_copy_name + "_unassigned_copy"


@dataclass(frozen=True)
class _VertexPlan:
    global_weights: Dict[int, float]


@dataclass
class _VertexAssignment:
    owner_prefix: str
    compatible_prefixes: Set[str] = field(default_factory=set)
    is_boundary: bool = False
    shared_local_group: int | None = None
    diagnostic: bool = False


@dataclass
class _TriangleSoup:
    vertices: List[Tuple[float, float, float]]
    faces: List[Tuple[int, int, int]]
    vertex_weights: List[Dict[int, float]]
    uv_layers: Dict[str, List[Tuple[float, float]]]
    color_layers: Dict[str, List[Tuple[float, float, float, float]]]
    loop_normals: List[Tuple[float, float, float]]
    material_indices: List[int]
    smooth_flags: List[bool]
    vertex_lookup: Dict[Tuple[float, float, float], int] = field(default_factory=dict)
    face_lookup: Set[Tuple[int, ...]] = field(default_factory=set)
    emitted_triangle_count: int = 0
    boundary_point_count: int = 0


def _iter_selected_mesh_objects(context) -> List[bpy.types.Object]:
    return [obj for obj in getattr(context, "selected_objects", []) or [] if getattr(obj, "type", "") == "MESH"]


def _extract_prefix_from_object(obj: bpy.types.Object) -> str:
    prefix_info = ObjectPrefixHelper.extract_prefix_info(getattr(obj, "name", ""))
    if not prefix_info:
        raise VGTestError(f"Cannot resolve prefix from object name '{getattr(obj, 'name', '')}'.")
    return str(prefix_info[0])


def _get_numeric_group_names(obj: bpy.types.Object) -> List[str]:
    return [str(getattr(vertex_group, "name", "") or "") for vertex_group in getattr(obj, "vertex_groups", []) or []]


def _sorted_unique_numeric_groups(obj: bpy.types.Object) -> List[int]:
    return sorted(set(ensure_numeric_group_names(_get_numeric_group_names(obj), object_name=getattr(obj, "name", ""))))


def _get_or_create_text(text_name: str):
    text = bpy.data.texts.get(text_name)
    if text is None:
        text = bpy.data.texts.new(text_name)
    return text


def _build_text_name(mapping_id: str) -> str:
    return f"{VG_TEST_TEXT_PREFIX}_{mapping_id[:12]}"


def _write_text_document(document: VGTestMappingDocument):
    text_name = _build_text_name(document.mapping_id)
    text = _get_or_create_text(text_name)
    text.clear()
    text.write(serialize_mapping_document(document))
    return text


def _parse_text_block(text_name: str) -> VGTestMappingDocument:
    text = bpy.data.texts.get(text_name)
    if text is None:
        raise VGTestError(f"Mapping text '{text_name}' does not exist.")
    return parse_mapping_document(text.as_string())


def _tag_object_with_mapping(obj: bpy.types.Object, document: VGTestMappingDocument, text_name: str, prefix: str, source_name: str):
    obj[MAPPING_TEXT_PROP] = text_name
    obj[MAPPING_ID_PROP] = document.mapping_id
    obj[PREFIX_PROP] = prefix
    obj[SOURCE_NAME_PROP] = source_name


def _find_candidate_mapping_texts_for_objects(objects: Sequence[bpy.types.Object]) -> List[str]:
    candidate_names: List[str] = []
    seen_names: Set[str] = set()

    for obj in objects:
        text_name = str(obj.get(MAPPING_TEXT_PROP, "") or "").strip()
        if text_name and text_name not in seen_names:
            seen_names.add(text_name)
            candidate_names.append(text_name)

    if candidate_names:
        return candidate_names

    global_groups: Set[int] = set()
    for obj in objects:
        global_groups.update(_sorted_unique_numeric_groups(obj))

    matching_texts = []
    for text in bpy.data.texts:
        try:
            document = parse_mapping_document(text.as_string())
        except Exception:
            continue
        mapped_groups = set()
        for prefix_mapping in document.prefixes.values():
            mapped_groups.update(prefix_mapping.global_set)
        if global_groups and global_groups.issubset(mapped_groups):
            matching_texts.append(text.name)

    return matching_texts


def _resolve_single_mapping_document_for_objects(objects: Sequence[bpy.types.Object]) -> tuple[str, VGTestMappingDocument]:
    candidate_texts = _find_candidate_mapping_texts_for_objects(objects)
    if not candidate_texts:
        raise VGTestError("Could not resolve a VG Test mapping text for the selected objects.")
    if len(candidate_texts) > 1:
        raise VGTestError(f"Multiple VG Test mapping texts match the selected objects: {', '.join(candidate_texts)}")
    text_name = candidate_texts[0]
    return text_name, _parse_text_block(text_name)


def _sort_vertex_groups_by_name(context, obj: bpy.types.Object):
    original_active = getattr(context.view_layer.objects, "active", None)
    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    context.view_layer.objects.active = obj
    bpy.ops.object.vertex_group_sort(sort_type='NAME')
    context.view_layer.objects.active = original_active


def _rename_vertex_groups_with_mapping(obj: bpy.types.Object, name_mapping: Dict[str, str]):
    temp_prefix = f"__vgtest_tmp_{uuid.uuid4().hex[:8]}__"
    staged = []

    for vertex_group in list(getattr(obj, "vertex_groups", []) or []):
        old_name = str(getattr(vertex_group, "name", "") or "")
        if old_name not in name_mapping:
            continue
        vertex_group.name = temp_prefix + old_name
        staged.append((vertex_group, old_name))

    for vertex_group, old_name in staged:
        vertex_group.name = name_mapping[old_name]


def _remove_vertex_groups_by_name(obj: bpy.types.Object, names_to_remove: Set[str]):
    for vertex_group in list(getattr(obj, "vertex_groups", []) or []):
        if str(getattr(vertex_group, "name", "") or "") in names_to_remove:
            obj.vertex_groups.remove(vertex_group)


def _collect_face_group_sets(obj: bpy.types.Object) -> List[Set[int]]:
    face_group_sets: List[Set[int]] = []
    vertex_group_names = {vertex_group.index: str(vertex_group.name) for vertex_group in obj.vertex_groups}
    for polygon in obj.data.polygons:
        group_set: Set[int] = set()
        for vertex_index in polygon.vertices:
            vertex = obj.data.vertices[vertex_index]
            for group in vertex.groups:
                group_name = vertex_group_names.get(group.group, "")
                if group.weight > 0 and group_name.isdigit():
                    group_set.add(int(group_name))
        face_group_sets.append(group_set)
    return face_group_sets


def _collect_vertex_group_sets(obj: bpy.types.Object) -> List[Set[int]]:
    vertex_group_sets: List[Set[int]] = []
    vertex_group_names = {vertex_group.index: str(vertex_group.name) for vertex_group in obj.vertex_groups}
    for vertex in obj.data.vertices:
        group_set: Set[int] = set()
        for group in vertex.groups:
            group_name = vertex_group_names.get(group.group, "")
            if group.weight > 0 and group_name.isdigit():
                group_set.add(int(group_name))
        vertex_group_sets.append(group_set)
    return vertex_group_sets


def _collect_vertex_weight_maps(obj: bpy.types.Object) -> List[Dict[int, float]]:
    vertex_weight_maps: List[Dict[int, float]] = []
    vertex_group_names = {vertex_group.index: str(vertex_group.name) for vertex_group in obj.vertex_groups}
    for vertex in obj.data.vertices:
        weight_map: Dict[int, float] = {}
        for group in vertex.groups:
            group_name = vertex_group_names.get(group.group, "")
            if group.weight > 0 and group_name.isdigit():
                weight_map[int(group_name)] = float(group.weight)
        vertex_weight_maps.append(weight_map)
    return vertex_weight_maps


def _collect_face_vertex_indices(obj: bpy.types.Object) -> List[List[int]]:
    return [list(polygon.vertices) for polygon in obj.data.polygons]


def _build_vertex_plans(
    vertex_weight_maps: Sequence[Dict[int, float]],
    document: VGTestMappingDocument,
) -> List[_VertexPlan]:
    plans: List[_VertexPlan] = []
    for weight_map in vertex_weight_maps:
        plans.append(
            _VertexPlan(
                global_weights={
                    int(group_number): float(weight)
                    for group_number, weight in dict(weight_map or {}).items()
                    if float(weight) > 0.0
                },
            )
        )
    return plans


@dataclass
class _SplitSoupResult:
    prefix_soups: Dict[str, _TriangleSoup]
    unassigned_soup: _TriangleSoup


def _collect_loop_normals(mesh) -> List[Tuple[float, float, float]]:
    corner_normals = getattr(mesh, "corner_normals", None)
    if corner_normals:
        try:
            return [_point_tuple(corner_normal.vector) for corner_normal in corner_normals]
        except Exception:
            pass

    try:
        return [_point_tuple(loop.normal) for loop in mesh.loops]
    except Exception:
        pass

    polygon_normals: List[Tuple[float, float, float]] = []
    for polygon in mesh.polygons:
        polygon_normal = _point_tuple(polygon.normal)
        for _loop_index in range(polygon.loop_start, polygon.loop_start + polygon.loop_total):
            polygon_normals.append(polygon_normal)
    if polygon_normals:
        return polygon_normals

    return [
        _point_tuple(mesh.vertices[loop.vertex_index].normal)
        for loop in mesh.loops
    ]


def _point_tuple(value) -> Tuple[float, float, float]:
    return (float(value[0]), float(value[1]), float(value[2]))


def _uv_tuple(value) -> Tuple[float, float]:
    return (float(value[0]), float(value[1]))


def _color_tuple(value) -> Tuple[float, float, float, float]:
    if len(value) >= 4:
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    if len(value) == 3:
        return (float(value[0]), float(value[1]), float(value[2]), 1.0)
    if len(value) == 2:
        return (float(value[0]), float(value[1]), 0.0, 1.0)
    if len(value) == 1:
        return (float(value[0]), 0.0, 0.0, 1.0)
    return (0.0, 0.0, 0.0, 1.0)


def _ensure_preview_collection():
    collection = bpy.data.collections.get(VG_TEST_PREVIEW_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(VG_TEST_PREVIEW_COLLECTION)
        bpy.context.scene.collection.children.link(collection)
    elif bpy.context.scene.collection.children.get(collection.name) is None:
        bpy.context.scene.collection.children.link(collection)
    return collection


def _clear_preview_collection(collection):
    for obj in list(collection.objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            continue


def _filter_and_restore_groups(obj: bpy.types.Object, document: VGTestMappingDocument, target_prefix: str):
    prefix_mapping = document.prefixes.get(target_prefix)
    if prefix_mapping is None:
        raise VGTestError(f"Missing prefix mapping for '{target_prefix}'.")

    vertex_weight_maps = _collect_vertex_weight_maps(obj)

    for vertex_group in list(getattr(obj, "vertex_groups", []) or []):
        obj.vertex_groups.remove(vertex_group)

    sorted_local_groups = sorted(prefix_mapping.local_to_global.keys())
    local_group_to_index: Dict[int, int] = {}
    for local_group in sorted_local_groups:
        local_group_to_index[local_group] = obj.vertex_groups.new(name=str(local_group)).index

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        deform_layer = bm.verts.layers.deform.verify()

        for vert in bm.verts:
            weight_map = vertex_weight_maps[vert.index] if vert.index < len(vertex_weight_maps) else {}
            target_weights = filter_vertex_weights_for_prefix(weight_map, target_prefix, document)
            deform_data = vert[deform_layer]
            for group_index in list(deform_data.keys()):
                del deform_data[group_index]
            for global_group, weight in target_weights.items():
                local_group = prefix_mapping.global_to_local[int(global_group)]
                local_group_index = local_group_to_index[local_group]
                deform_data[local_group_index] = weight

        bm.to_mesh(obj.data)
        obj.data.update()
    finally:
        bm.free()


def _copy_object_and_mesh(obj: bpy.types.Object, new_name: str):
    obj_copy = obj.copy()
    obj_copy.data = obj.data.copy()
    obj_copy.name = new_name
    return obj_copy


def _prune_mesh_to_face_mask(obj: bpy.types.Object, face_mask: Sequence[bool]):
    mesh = obj.data
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        faces_to_delete = [face for index, face in enumerate(bm.faces) if index >= len(face_mask) or not face_mask[index]]
        if faces_to_delete:
            bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES')
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()


def _collect_mesh_loop_payload(obj: bpy.types.Object) -> dict:
    mesh = obj.data

    uv_payload: Dict[str, List[Tuple[float, float]]] = {}
    for uv_layer in getattr(mesh, "uv_layers", []) or []:
        uv_payload[uv_layer.name] = [_uv_tuple(loop_uv.uv) for loop_uv in uv_layer.data]

    color_payload: Dict[str, List[Tuple[float, float, float, float]]] = {}
    color_attributes = getattr(mesh, "color_attributes", None)
    if color_attributes:
        for color_attr in color_attributes:
            if getattr(color_attr, "domain", "") != "CORNER":
                continue
            color_payload[color_attr.name] = [_color_tuple(color.color) for color in color_attr.data]
    else:
        for vertex_color in getattr(mesh, "vertex_colors", []) or []:
            color_payload[vertex_color.name] = [_color_tuple(color.color) for color in vertex_color.data]

    loops = mesh.loops
    loop_normals = _collect_loop_normals(mesh)
    positions = [_point_tuple(vertex.co) for vertex in mesh.vertices]

    polygons = []
    for polygon in mesh.polygons:
        loop_indices = list(range(polygon.loop_start, polygon.loop_start + polygon.loop_total))
        polygons.append(
            {
                "vertices": list(polygon.vertices),
                "loop_indices": loop_indices,
                "material_index": int(getattr(polygon, "material_index", 0)),
                "use_smooth": bool(getattr(polygon, "use_smooth", False)),
            }
        )

    return {
        "positions": positions,
        "polygons": polygons,
        "uv_layers": uv_payload,
        "color_layers": color_payload,
        "loop_normals": loop_normals,
    }


def _make_triangle_soup() -> _TriangleSoup:
    return _TriangleSoup(
        vertices=[],
        faces=[],
        vertex_weights=[],
        uv_layers={},
        color_layers={},
        loop_normals=[],
        material_indices=[],
        smooth_flags=[],
        vertex_lookup={},
        face_lookup=set(),
        emitted_triangle_count=0,
        boundary_point_count=0,
    )


def _classify_sign_for_global_group(plan: _VertexPlan, global_group: int) -> int:
    if float(plan.global_weights.get(int(global_group), 0.0)) > 0.0:
        return 1
    return -1


def _remap_weight_map_to_local(
    weight_map: Dict[int, float],
    prefix: str,
    document: VGTestMappingDocument,
) -> Dict[int, float]:
    prefix_mapping = document.prefixes[prefix]
    local_weights: Dict[int, float] = {}
    for global_group, weight in dict(weight_map or {}).items():
        local_group = prefix_mapping.global_to_local.get(int(global_group))
        if local_group is None:
            continue
        local_weights[int(local_group)] = float(weight)
    return local_weights


def _resolve_unique_shared_local_group_for_prefixes(
    prefixes: Set[str],
    document: VGTestMappingDocument,
) -> int | None:
    local_sets = [
        set(document.prefixes[prefix].local_to_global.keys())
        for prefix in prefixes
        if prefix in document.prefixes
    ]
    if len(local_sets) <= 1:
        return None
    shared_local_groups = set.intersection(*local_sets)
    if len(shared_local_groups) != 1:
        return None
    return next(iter(shared_local_groups))


def _collect_numeric_mapped_weights(
    weight_map: Dict[int, float],
    document: VGTestMappingDocument,
) -> Dict[int, float]:
    return {
        int(group_number): float(weight)
        for group_number, weight in dict(weight_map or {}).items()
        if float(weight) > 0.0 and document.get_prefix_for_global_group(int(group_number))
    }


def _collect_touched_prefixes(
    weight_map: Dict[int, float],
    document: VGTestMappingDocument,
) -> Set[str]:
    return {
        document.get_prefix_for_global_group(group_number)
        for group_number in _collect_numeric_mapped_weights(weight_map, document)
        if document.get_prefix_for_global_group(group_number)
    }


def _collect_local_weight_totals(
    weight_map: Dict[int, float],
    document: VGTestMappingDocument,
) -> Dict[int, float]:
    local_totals: Dict[int, float] = {}
    for group_number, weight in _collect_numeric_mapped_weights(weight_map, document).items():
        prefix = document.get_prefix_for_global_group(group_number)
        if not prefix:
            continue
        local_group = document.prefixes[prefix].global_to_local[int(group_number)]
        local_totals[int(local_group)] = local_totals.get(int(local_group), 0.0) + float(weight)
    return local_totals


def _resolve_vertex_owner_prefix(
    weight_map: Dict[int, float],
    document: VGTestMappingDocument,
) -> str:
    numeric_weights = _collect_numeric_mapped_weights(weight_map, document)
    if not numeric_weights:
        return ""

    touched_prefixes = _collect_touched_prefixes(numeric_weights, document)
    if len(touched_prefixes) == 1:
        return next(iter(touched_prefixes))
    return resolve_dominant_prefix(numeric_weights, document)


def _collect_mesh_vertex_adjacency(payload: dict) -> List[Set[int]]:
    positions = payload.get("positions", [])
    adjacency: List[Set[int]] = [set() for _ in positions]
    for polygon in payload.get("polygons", []):
        vertices = list(polygon.get("vertices", []))
        count = len(vertices)
        for index in range(count):
            current = int(vertices[index])
            other = int(vertices[(index + 1) % count])
            if current == other:
                continue
            if 0 <= current < len(adjacency) and 0 <= other < len(adjacency):
                adjacency[current].add(other)
                adjacency[other].add(current)
    return adjacency


def _build_vertex_assignments(
    payload: dict,
    vertex_plans: Sequence[_VertexPlan],
    document: VGTestMappingDocument,
) -> List[_VertexAssignment]:
    assignments: List[_VertexAssignment] = []
    for _vertex_index, plan in enumerate(vertex_plans):
        numeric_weights = _collect_numeric_mapped_weights(plan.global_weights, document)
        if not numeric_weights:
            assignments.append(_VertexAssignment(owner_prefix="", diagnostic=True))
            continue

        try:
            profile = resolve_vertex_split_profile(numeric_weights, document)
        except VGTestError:
            assignments.append(
                _VertexAssignment(
                    owner_prefix="",
                    compatible_prefixes=set(),
                    is_boundary=False,
                    shared_local_group=None,
                    diagnostic=True,
                )
            )
            continue

        touched_prefixes = _collect_touched_prefixes(numeric_weights, document)
        shared_local_group = None
        if profile.is_boundary:
            shared_local_group = _resolve_unique_shared_local_group_for_prefixes(touched_prefixes, document)
        assignments.append(
            _VertexAssignment(
                owner_prefix=str(profile.owner_prefix or ""),
                compatible_prefixes=set(profile.compatible_prefixes or set()),
                is_boundary=bool(profile.is_boundary),
                shared_local_group=int(shared_local_group) if shared_local_group is not None else None,
                diagnostic=not bool(profile.owner_prefix or profile.compatible_prefixes),
            )
        )

    return assignments


def _choose_owner_fill_local_group(
    numeric_weights: Dict[int, float],
    target_prefix: str,
    document: VGTestMappingDocument,
) -> int | None:
    touched_prefixes = _collect_touched_prefixes(numeric_weights, document)
    shared_local_groups = set()
    if len(touched_prefixes) > 1:
        local_sets = [
            set(document.prefixes[prefix].local_to_global.keys())
            for prefix in touched_prefixes
            if prefix in document.prefixes
        ]
        if len(local_sets) > 1:
            shared_local_groups = set.intersection(*local_sets)

    owner_local_totals: Dict[int, float] = {}
    for group_number, weight in numeric_weights.items():
        prefix = document.get_prefix_for_global_group(group_number)
        if prefix != target_prefix:
            continue
        local_group = document.prefixes[prefix].global_to_local[int(group_number)]
        owner_local_totals[int(local_group)] = owner_local_totals.get(int(local_group), 0.0) + float(weight)

    shared_candidates = {
        local_group: weight
        for local_group, weight in owner_local_totals.items()
        if local_group in shared_local_groups
    }
    if shared_candidates:
        return max(shared_candidates.items(), key=lambda item: (float(item[1]), -int(item[0])))[0]

    exclusive_candidates = {
        local_group: weight
        for local_group, weight in owner_local_totals.items()
        if local_group not in shared_local_groups
    }
    if exclusive_candidates:
        return max(exclusive_candidates.items(), key=lambda item: (float(item[1]), -int(item[0])))[0]
    if owner_local_totals:
        return max(owner_local_totals.items(), key=lambda item: (float(item[1]), -int(item[0])))[0]
    return None


def _build_output_local_weight_map(
    weight_map: Dict[int, float],
    assignment: _VertexAssignment,
    target_prefix: str,
    document: VGTestMappingDocument,
) -> Dict[int, float]:
    numeric_weights = _collect_numeric_mapped_weights(weight_map, document)
    if not numeric_weights:
        return {}
    if target_prefix not in assignment.compatible_prefixes:
        return {}

    if assignment.is_boundary:
        if assignment.shared_local_group is None:
            return {}
        return {int(assignment.shared_local_group): 1.0}

    if assignment.owner_prefix != target_prefix:
        return {}

    touched_prefixes = _collect_touched_prefixes(numeric_weights, document)
    if len(touched_prefixes) > 1:
        try:
            collapsed_global_weights = build_target_prefix_vertex_weights(
                numeric_weights,
                target_prefix,
                document,
            )
        except VGTestError:
            collapsed_global_weights = {}

        fill_local_group = _choose_owner_fill_local_group(numeric_weights, target_prefix, document)
        if collapsed_global_weights:
            local_weights: Dict[int, float] = {}
            target_mapping = document.prefixes[target_prefix]
            for global_group, weight in collapsed_global_weights.items():
                local_group = target_mapping.global_to_local.get(int(global_group))
                if local_group is None:
                    continue
                local_weights[int(local_group)] = local_weights.get(int(local_group), 0.0) + float(weight)
            if local_weights:
                return {
                    int(local_group): min(1.0, float(weight))
                    for local_group, weight in local_weights.items()
                    if float(weight) > 1e-8
                }
        if fill_local_group is None:
            return {}
        return {int(fill_local_group): 1.0}

    return _remap_weight_map_to_local(
        filter_vertex_weights_for_prefix(numeric_weights, target_prefix, document),
        target_prefix,
        document,
    )


def _append_diagnostic_point_to_soup(
    soup: _TriangleSoup,
    co: Tuple[float, float, float],
    weight_map: Dict[int, float],
) -> None:
    _get_or_add_soup_vertex(
        soup,
        {
            "co": co,
            "weights": dict(weight_map),
            "uvs": {},
            "colors": {},
            "normal": (0.0, 0.0, 1.0),
            "is_boundary": False,
        },
    )


def _vertex_merge_key(co: Tuple[float, float, float]) -> Tuple[float, float, float]:
    return tuple(round(float(axis), _VERTEX_MERGE_DECIMALS) for axis in co)


def _merge_vertex_weight_map(target: Dict[int, float], source: Dict[int, float]) -> None:
    for group_number, weight in dict(source or {}).items():
        weight_value = float(weight)
        if weight_value <= 0.0:
            continue
        group_key = int(group_number)
        target[group_key] = max(float(target.get(group_key, 0.0)), weight_value)


def _get_or_add_soup_vertex(soup: _TriangleSoup, corner: dict) -> int:
    key = _vertex_merge_key(corner["co"])
    existing_index = soup.vertex_lookup.get(key)
    if existing_index is not None:
        _merge_vertex_weight_map(soup.vertex_weights[existing_index], corner["weights"])
        return existing_index

    vertex_index = len(soup.vertices)
    soup.vertex_lookup[key] = vertex_index
    soup.vertices.append(corner["co"])
    soup.vertex_weights.append(dict(corner["weights"]))
    return vertex_index


def _record_soup_loop_payload(soup: _TriangleSoup, corners: Sequence[dict]) -> None:
    for corner in corners:
        for layer_name, uv_value in corner["uvs"].items():
            soup.uv_layers.setdefault(layer_name, []).append(uv_value)
        for layer_name, color_value in corner["colors"].items():
            soup.color_layers.setdefault(layer_name, []).append(color_value)
        soup.loop_normals.append(corner["normal"])
        if corner.get("is_boundary"):
            soup.boundary_point_count += 1


def _append_triangle_to_soup(
    soup: _TriangleSoup,
    corners: Sequence[dict],
    polygon_meta: dict,
) -> None:
    if len(corners) != 3:
        raise VGTestError("VG Test internal error: expected triangles after clipping.")

    face = tuple(_get_or_add_soup_vertex(soup, corner) for corner in corners)
    if len(set(face)) != 3:
        return

    face_key = tuple(sorted(face))
    if face_key in soup.face_lookup:
        for corner in corners:
            if corner.get("is_boundary"):
                soup.boundary_point_count += 1
        return

    soup.face_lookup.add(face_key)
    soup.faces.append(face)
    soup.material_indices.append(int(polygon_meta["material_index"]))
    soup.smooth_flags.append(bool(polygon_meta["use_smooth"]))
    soup.emitted_triangle_count += 1

    _record_soup_loop_payload(soup, corners)


def _append_unassigned_polygon_to_soup(
    soup: _TriangleSoup,
    corners: Sequence[dict],
    polygon_meta: dict,
) -> None:
    if len(corners) != 3:
        return

    face = tuple(_get_or_add_soup_vertex(soup, corner) for corner in corners)
    if len(set(face)) != 3:
        return

    face_key = tuple(sorted(face))
    if face_key in soup.face_lookup:
        for corner in corners:
            if corner.get("is_boundary"):
                soup.boundary_point_count += 1
        return

    soup.face_lookup.add(face_key)
    soup.faces.append(face)
    soup.material_indices.append(int(polygon_meta["material_index"]))
    soup.smooth_flags.append(False)
    soup.emitted_triangle_count += 1

    _record_soup_loop_payload(soup, corners)


def _build_prefix_triangle_soups(
    obj: bpy.types.Object,
    document: VGTestMappingDocument,
    vertex_weight_maps: Sequence[Dict[int, float]],
) -> _SplitSoupResult:
    payload = _collect_mesh_loop_payload(obj)
    vertex_plans = _build_vertex_plans(vertex_weight_maps, document)
    vertex_assignments = _build_vertex_assignments(payload, vertex_plans, document)
    soups: Dict[str, _TriangleSoup] = {
        prefix: _make_triangle_soup()
        for prefix in document.prefix_order
    }
    unassigned_soup = _make_triangle_soup()

    for vertex_index, assignment in enumerate(vertex_assignments):
        if not assignment.diagnostic or vertex_index >= len(payload["positions"]):
            continue
        _append_diagnostic_point_to_soup(
            unassigned_soup,
            payload["positions"][vertex_index],
            vertex_plans[vertex_index].global_weights,
        )

    for polygon in payload["polygons"]:
        vertex_indices = polygon["vertices"]
        loop_indices = polygon["loop_indices"]
        if len(vertex_indices) != 3 or len(loop_indices) != 3:
            raise VGTestError("VG Test only supports triangulated runtime meshes during split.")

        compatible_prefixes: Set[str] | None = None
        for vertex_index in vertex_indices:
            if vertex_index >= len(vertex_assignments):
                compatible_prefixes = set()
                break
            assignment = vertex_assignments[vertex_index]
            if assignment.diagnostic:
                compatible_prefixes = set()
                break
            current_prefixes = set(assignment.compatible_prefixes)
            if compatible_prefixes is None:
                compatible_prefixes = current_prefixes
            else:
                compatible_prefixes &= current_prefixes

        if compatible_prefixes:
            prefix = next((candidate for candidate in document.prefix_order if candidate in compatible_prefixes), "")
        else:
            prefix = ""

        if prefix:
            group_corners: List[dict] = []
            for corner_index, vertex_index in enumerate(vertex_indices):
                plan = vertex_plans[vertex_index]
                assignment = vertex_assignments[vertex_index]
                loop_index = loop_indices[corner_index]
                group_corners.append(
                    {
                        "co": payload["positions"][vertex_index],
                        "weights": _build_output_local_weight_map(plan.global_weights, assignment, prefix, document),
                        "source_weights": dict(plan.global_weights),
                        "uvs": {name: values[loop_index] for name, values in payload["uv_layers"].items()},
                        "colors": {name: values[loop_index] for name, values in payload["color_layers"].items()},
                        "normal": payload["loop_normals"][loop_index],
                        "sign": 1,
                        "is_boundary": assignment.is_boundary,
                    }
                )

            _append_triangle_to_soup(soups[prefix], group_corners, polygon)

    return _SplitSoupResult(prefix_soups=soups, unassigned_soup=unassigned_soup)


def _replace_object_mesh_from_soup(obj: bpy.types.Object, soup: _TriangleSoup) -> None:
    mesh = obj.data
    mesh.clear_geometry()

    if not soup.faces:
        mesh.update()
        return

    mesh.from_pydata(soup.vertices, [], soup.faces)

    for material in list(getattr(mesh, "materials", []) or []):
        pass

    for layer_name, values in soup.uv_layers.items():
        uv_layer = mesh.uv_layers.get(layer_name)
        if uv_layer is None:
            uv_layer = mesh.uv_layers.new(name=layer_name)
        flat_values: List[float] = []
        for value in values:
            flat_values.extend([float(value[0]), float(value[1])])
        uv_layer.data.foreach_set("uv", flat_values)

    color_attributes = getattr(mesh, "color_attributes", None)
    if color_attributes is not None:
        for layer_name, values in soup.color_layers.items():
            color_layer = color_attributes.get(layer_name)
            if color_layer is None:
                color_layer = color_attributes.new(name=layer_name, type='BYTE_COLOR', domain='CORNER')
            flat_values: List[float] = []
            for value in values:
                flat_values.extend([float(value[0]), float(value[1]), float(value[2]), float(value[3])])
            color_layer.data.foreach_set("color", flat_values)
    else:
        for layer_name, values in soup.color_layers.items():
            color_layer = mesh.vertex_colors.get(layer_name)
            if color_layer is None:
                color_layer = mesh.vertex_colors.new(name=layer_name)
            flat_values: List[float] = []
            for value in values:
                flat_values.extend([float(value[0]), float(value[1]), float(value[2]), float(value[3])])
            color_layer.data.foreach_set("color", flat_values)

    for polygon_index, polygon in enumerate(mesh.polygons):
        if polygon_index < len(soup.material_indices):
            polygon.material_index = int(soup.material_indices[polygon_index])
        if polygon_index < len(soup.smooth_flags):
            polygon.use_smooth = bool(soup.smooth_flags[polygon_index])

    for vertex_group in list(getattr(obj, "vertex_groups", []) or []):
        obj.vertex_groups.remove(vertex_group)

    used_groups = sorted({
        int(group_number)
        for vertex_weight_map in soup.vertex_weights
        for group_number, weight in vertex_weight_map.items()
        if float(weight) > 0.0
    })
    group_index_map: Dict[int, int] = {}
    for local_group in used_groups:
        group_index_map[local_group] = obj.vertex_groups.new(name=str(local_group)).index

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        deform_layer = bm.verts.layers.deform.verify()
        for vert in bm.verts:
            deform_data = vert[deform_layer]
            for group_index in list(deform_data.keys()):
                del deform_data[group_index]
            vertex_weight_map = soup.vertex_weights[vert.index] if vert.index < len(soup.vertex_weights) else {}
            for local_group, weight in vertex_weight_map.items():
                if float(weight) <= 0.0:
                    continue
                deform_data[group_index_map[int(local_group)]] = float(weight)
        bm.to_mesh(mesh)
    finally:
        bm.free()

    try:
        mesh.normals_split_custom_set(soup.loop_normals)
    except Exception:
        pass
    mesh.update()


class VGTestRuntime:
    @staticmethod
    def build_selected_mapping_document(context) -> tuple[VGTestMappingDocument, str]:
        selected_mesh_objects = _iter_selected_mesh_objects(context)
        if not selected_mesh_objects:
            raise VGTestError("Select at least one mesh object.")

        items = []
        for obj in selected_mesh_objects:
            items.append(
                VGTestObjectInfo(
                    name=obj.name,
                    prefix=_extract_prefix_from_object(obj),
                    numeric_groups=_sorted_unique_numeric_groups(obj),
                )
            )

        document = build_mapping_document(items)
        text = _write_text_document(document)
        return document, text.name

    @staticmethod
    def unify_selected_objects(context) -> tuple[str, int]:
        document, text_name = VGTestRuntime.build_selected_mapping_document(context)
        selected_mesh_objects = _iter_selected_mesh_objects(context)

        for obj in selected_mesh_objects:
            prefix = _extract_prefix_from_object(obj)
            prefix_mapping = document.prefixes[prefix]
            rename_map = {str(local_group): str(global_group) for local_group, global_group in prefix_mapping.local_to_global.items()}
            _rename_vertex_groups_with_mapping(obj, rename_map)
            _sort_vertex_groups_by_name(context, obj)
            _tag_object_with_mapping(obj, document, text_name, prefix, obj.name)

        return text_name, len(selected_mesh_objects)

    @staticmethod
    def restore_selected_objects(context) -> tuple[str, int]:
        selected_mesh_objects = _iter_selected_mesh_objects(context)
        if not selected_mesh_objects:
            raise VGTestError("Select at least one mesh object.")

        text_name, document = _resolve_single_mapping_document_for_objects(selected_mesh_objects)

        for obj in selected_mesh_objects:
            prefix = str(obj.get(PREFIX_PROP, "") or "").strip() or _extract_prefix_from_object(obj)
            _filter_and_restore_groups(obj, document, prefix)
            _sort_vertex_groups_by_name(context, obj)
            _tag_object_with_mapping(obj, document, text_name, prefix, str(obj.get(SOURCE_NAME_PROP, "") or obj.name))

        return text_name, len(selected_mesh_objects)

    @staticmethod
    def split_objects_for_preview(context) -> list[str]:
        selected_mesh_objects = _iter_selected_mesh_objects(context)
        if not selected_mesh_objects:
            raise VGTestError("VG Test split preview requires at least one selected mesh object.")

        collection = _ensure_preview_collection()
        _clear_preview_collection(collection)

        created_names: list[str] = []
        for obj in selected_mesh_objects:
            text_name, document = _resolve_single_mapping_document_for_objects([obj])
            created = VGTestRuntime._split_object(
                obj,
                document,
                text_name,
                preview_only=True,
                clear_preview=False,
            )
            created_names.extend(entry["object_name"] for entry in created)
        return created_names

    @staticmethod
    def expand_chain_object_for_export(
        source_object_name: str,
        original_object_name: str = "",
    ) -> list[dict]:
        obj = bpy.data.objects.get(source_object_name)
        if obj is None or obj.type != "MESH":
            raise VGTestError(f"Cannot split export object '{source_object_name}'.")
        text_name, document = _resolve_single_mapping_document_for_objects([obj])
        split_results = VGTestRuntime._split_object(
            obj,
            document,
            text_name,
            preview_only=False,
            original_object_name=original_object_name or source_object_name,
        )
        return [entry for entry in split_results if not entry.get("diagnostic")]

    @staticmethod
    def _split_object(
        obj: bpy.types.Object,
        document: VGTestMappingDocument,
        text_name: str,
        *,
        preview_only: bool,
        original_object_name: str = "",
        clear_preview: bool = True,
    ) -> list[dict]:
        vertex_weight_maps = _collect_vertex_weight_maps(obj)
        split_result = _build_prefix_triangle_soups(obj, document, vertex_weight_maps)
        prefix_soups = split_result.prefix_soups

        collection = _ensure_preview_collection()
        if preview_only and clear_preview:
            _clear_preview_collection(collection)

        created: list[dict] = []
        source_name = original_object_name or obj.name
        base_name = build_runtime_vgtest_copy_name(obj.name)
        source_prefix = str(obj.get(PREFIX_PROP, "") or "").strip() or _extract_prefix_from_object(obj)

        for prefix in document.prefix_order:
            soup = prefix_soups[prefix]
            if not soup.faces:
                continue

            export_name = replace_runtime_object_prefix(base_name, source_prefix, prefix)
            obj_copy = _copy_object_and_mesh(obj, export_name)
            collection.objects.link(obj_copy)
            _replace_object_mesh_from_soup(obj_copy, soup)
            _tag_object_with_mapping(obj_copy, document, text_name, prefix, source_name)
            LOG.info(
                f"[VGTEST-RUNTIME] split '{obj.name}' -> '{obj_copy.name}' "
                f"prefix='{prefix}' triangles={soup.emitted_triangle_count} "
                f"boundary_points={soup.boundary_point_count} "
                f"local_groups={','.join(str(group.name) for group in obj_copy.vertex_groups)}"
            )

            created.append(
                {
                    "object_name": obj_copy.name,
                    "original_object_name": source_name,
                    "export_name": export_name,
                    "prefix": prefix,
                }
            )

        unassigned_soup = split_result.unassigned_soup
        if unassigned_soup.faces:
            unassigned_name = _build_runtime_unassigned_copy_name(obj.name)
            obj_copy = _copy_object_and_mesh(obj, unassigned_name)
            collection.objects.link(obj_copy)
            _replace_object_mesh_from_soup(obj_copy, unassigned_soup)
            try:
                obj_copy.display_type = 'WIRE'
                obj_copy.show_in_front = True
            except Exception:
                pass
            _tag_object_with_mapping(obj_copy, document, text_name, "VGTEST_UNASSIGNED", source_name)
            LOG.warning(
                f"[VGTEST-UNASSIGNED] split '{obj.name}' -> '{obj_copy.name}' "
                f"diagnostic_faces={unassigned_soup.emitted_triangle_count} "
                f"diagnostic_points={unassigned_soup.boundary_point_count}"
            )
            created.append(
                {
                    "object_name": obj_copy.name,
                    "original_object_name": source_name,
                    "export_name": unassigned_name,
                    "prefix": "VGTEST_UNASSIGNED",
                    "diagnostic": True,
                }
            )

        if not created:
            raise VGTestError(f"VG Test split produced no objects for '{obj.name}'.")

        return created
