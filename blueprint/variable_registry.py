import re
from collections import Counter
from typing import Iterable, Optional

import bpy


OBJECT_SWAP_PREFIX = "swapkey"
SHAPEKEY_PREFIX = "Freq_"
CONTINUOUS_SHAPEKEY_INDEX_PREFIX = "continuous_shapekey_frame"

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _get_scene_global_properties(context=None):
    scene = getattr(context, "scene", None) if context is not None else getattr(bpy.context, "scene", None)
    if scene is None:
        return None
    return getattr(scene, "global_properties", None)


def _sanitize_name(text: str, fallback: str = "var") -> str:
    safe_text = re.sub(r"\s+", "_", str(text or "").strip())
    safe_text = _SAFE_NAME_RE.sub("", safe_text)
    if safe_text and safe_text[0].isdigit():
        safe_text = "_" + safe_text
    return safe_text or fallback


def _split_csv(text: str) -> list[str]:
    return [item for item in str(text or "").split(",") if item]


def _join_csv(values: Iterable[str]) -> str:
    return ",".join(values)


def _iter_blueprint_nodes():
    node_groups = getattr(getattr(bpy, "data", None), "node_groups", None)
    if not node_groups:
        return

    for tree in node_groups:
        if getattr(tree, "bl_idname", "") != "SSMTBlueprintTreeType":
            continue
        for node in getattr(tree, "nodes", []):
            yield node


def _iter_node_variable_names(node):
    custom_name = normalize_variable_name(getattr(node, "custom_var_name", "") or "")
    assigned_name = normalize_variable_name(getattr(node, "assigned_variable_name", "") or "")
    if custom_name:
        yield custom_name
    if assigned_name:
        yield assigned_name
    continuous_custom_name = normalize_variable_name(getattr(node, "custom_continuous_index_variable_name", "") or "")
    continuous_assigned_name = normalize_variable_name(getattr(node, "assigned_continuous_index_variable_name", "") or "")
    if continuous_custom_name:
        yield continuous_custom_name
    if continuous_assigned_name:
        yield continuous_assigned_name
    paused_name = normalize_variable_name(getattr(node, "custom_paused_var", "") or "")
    driven_name = normalize_variable_name(getattr(node, "driven_variable", "") or "")
    if paused_name:
        yield paused_name
    if driven_name:
        yield driven_name
    for item in getattr(node, "driven_variable_list", []) or []:
        variable_name = normalize_variable_name(getattr(item, "variable_name", "") or "")
        if variable_name:
            yield variable_name


def _iter_shapekey_item_variable_names(item):
    custom_name = normalize_variable_name(getattr(item, "custom_variable_name", "") or "")
    assigned_name = normalize_variable_name(getattr(item, "assigned_variable_name", "") or "")
    if custom_name:
        yield custom_name
    if assigned_name:
        yield assigned_name


def _collect_used_variable_name_counts(context=None) -> Counter:
    counts = Counter()

    for node in _iter_blueprint_nodes() or ():
        bl_idname = getattr(node, "bl_idname", "")
        if bl_idname == "SSMTNode_PostProcess_ShapeKey":
            for item in getattr(node, "shapekey_variable_items", []):
                counts.update(_iter_shapekey_item_variable_names(item))
            continue

        node_variable_names = tuple(_iter_node_variable_names(node))
        if node_variable_names:
            counts.update(node_variable_names)

    _sync_variable_usage_cache(counts, context=context)
    return counts


def _sync_variable_usage_cache(counts: Counter, context=None):
    props = _get_scene_global_properties(context)
    if props is None:
        return

    props.allocated_variable_names_csv = _join_csv(sorted(counts.keys()))

    next_index = 0
    while counts.get(f"{OBJECT_SWAP_PREFIX}{next_index}", 0) > 0:
        next_index += 1
    props.object_swap_variable_counter = next_index


def _normalize_owned_counts(owned_names: Optional[Iterable[str]] = None) -> Counter:
    counts = Counter()
    if not owned_names:
        return counts

    for name in owned_names:
        normalized = normalize_variable_name(name)
        if normalized:
            counts[normalized] += 1
    return counts


def _is_name_used_by_other_owner(name: str, used_counts: Counter, owned_counts: Optional[Counter] = None) -> bool:
    normalized = normalize_variable_name(name)
    if not normalized:
        return False
    owned_count = owned_counts.get(normalized, 0) if owned_counts else 0
    return used_counts.get(normalized, 0) > owned_count


def get_used_variable_names(context=None) -> set[str]:
    return set(_collect_used_variable_name_counts(context).keys())


def mark_variable_name_used(var_name: str, context=None):
    normalized = normalize_variable_name(var_name)
    if not normalized:
        return
    counts = _collect_used_variable_name_counts(context)
    counts[normalized] += 1
    _sync_variable_usage_cache(counts, context=context)


def normalize_variable_name(var_name: str) -> str:
    cleaned = str(var_name or "").strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    return _sanitize_name(cleaned, fallback="")


def ensure_object_swap_variable_name(node, context=None) -> str:
    current = normalize_variable_name(getattr(node, "assigned_variable_name", ""))
    if current:
        _collect_used_variable_name_counts(context)
        return current

    used_counts = _collect_used_variable_name_counts(context)
    next_index = 0
    while True:
        candidate = f"{OBJECT_SWAP_PREFIX}{next_index}"
        next_index += 1
        if _is_name_used_by_other_owner(candidate, used_counts):
            continue
        node.assigned_variable_name = candidate
        used_counts[candidate] += 1
        _sync_variable_usage_cache(used_counts, context=context)
        return candidate


def allocate_shape_key_variable_name(
    shape_key_name: str,
    *,
    preferred: Optional[str] = None,
    context=None,
    owned_names: Optional[Iterable[str]] = None,
) -> str:
    preferred_normalized = normalize_variable_name(preferred or "")
    used_counts = _collect_used_variable_name_counts(context)
    owned_counts = _normalize_owned_counts(owned_names)

    if preferred_normalized:
        if not _is_name_used_by_other_owner(preferred_normalized, used_counts, owned_counts):
            return preferred_normalized

    base_name = _sanitize_name(shape_key_name, fallback="shape")
    candidate = f"{SHAPEKEY_PREFIX}{base_name}"
    if not _is_name_used_by_other_owner(candidate, used_counts, owned_counts):
        return candidate

    suffix = 1
    while True:
        indexed = f"{candidate}_{suffix}"
        if not _is_name_used_by_other_owner(indexed, used_counts, owned_counts):
            return indexed
        suffix += 1


def get_node_variable_name(node, context=None) -> str:
    assigned_name = ensure_object_swap_variable_name(node, context=context)
    manual_name = normalize_variable_name(getattr(node, "custom_var_name", ""))
    resolved = manual_name or assigned_name
    mark_variable_name_used(resolved, context=context)
    return f"${resolved}"


def allocate_continuous_shapekey_index_variable_name(
    *,
    preferred: Optional[str] = None,
    context=None,
    owned_names: Optional[Iterable[str]] = None,
) -> str:
    preferred_normalized = normalize_variable_name(preferred or "")
    used_counts = _collect_used_variable_name_counts(context)
    owned_counts = _normalize_owned_counts(owned_names)

    if preferred_normalized:
        if not _is_name_used_by_other_owner(preferred_normalized, used_counts, owned_counts):
            return preferred_normalized

    suffix = 1
    while True:
        candidate = f"{CONTINUOUS_SHAPEKEY_INDEX_PREFIX}{suffix}"
        if not _is_name_used_by_other_owner(candidate, used_counts, owned_counts):
            return candidate
        suffix += 1
