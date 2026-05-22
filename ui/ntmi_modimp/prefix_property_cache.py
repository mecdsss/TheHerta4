from __future__ import annotations

import json
from typing import Iterable

import bpy

from ...common.object_prefix_helper import ObjectPrefixHelper
from .runtime_cache import MODIMP_COLLECTOR_PROPS, MODIMP_PATH_PROPS, prefix_identity_matches


PREFIX_PROPERTY_CACHE_TEXT_NAME = "NTMI_ModImp_PrefixPropertyCache"

_RUNTIME_PROP_KEYS = (
    "modimp_profile_id",
    "modimp_ib_hash",
    "modimp_source_ib_hash",
    "modimp_region_hash",
    "modimp_region_index_count",
    "modimp_region_first_index",
    "modimp_display_ib_hash",
    "modimp_import_variant",
    "modimp_first_index",
    "modimp_index_count",
    "modimp_slice_order",
    "modimp_used_vertex_start",
    "modimp_used_vertex_end",
    "modimp_draw_indices",
    "modimp_match_vs_texcoord_hash",
    "modimp_match_vs_position_hash",
    "modimp_match_vs_outline_hash",
    "modimp_mirror_flip",
    "modimp_root_vb0_note",
    "modimp_workspace_unique_str",
    "modimp_texture_slots",
)

_CACHE_PROP_KEYS = tuple(dict.fromkeys(_RUNTIME_PROP_KEYS + MODIMP_PATH_PROPS + MODIMP_COLLECTOR_PROPS))


def _text_block():
    text = bpy.data.texts.get(PREFIX_PROPERTY_CACHE_TEXT_NAME)
    if text is None:
        text = bpy.data.texts.new(PREFIX_PROPERTY_CACHE_TEXT_NAME)
    return text


def _read_payload() -> dict:
    text = bpy.data.texts.get(PREFIX_PROPERTY_CACHE_TEXT_NAME)
    if text is None:
        return {"version": 1, "entries": []}
    raw = text.as_string()
    if not raw.strip():
        return {"version": 1, "entries": []}
    try:
        payload = json.loads(raw)
    except Exception:
        return {"version": 1, "entries": []}
    if not isinstance(payload, dict):
        return {"version": 1, "entries": []}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        payload["entries"] = []
    return payload


def _write_payload(payload: dict):
    text = _text_block()
    text.clear()
    text.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _object_prefix_identity(obj_name: str) -> tuple[str, str] | None:
    parsed_prefix = ObjectPrefixHelper.extract_prefix_info(obj_name)
    if not parsed_prefix:
        return None
    prefix, _separator = parsed_prefix
    prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix)
    lod_name = str(prefix_parts.get("lod_name", "") or "").strip().lower()
    bare_prefix = ObjectPrefixHelper.normalize_prefix(
        str(prefix_parts.get("bare_unique_str", "") or "")
    ).lower()
    if not bare_prefix:
        return None
    return lod_name, bare_prefix


def _collect_object_props(obj) -> dict:
    props = {}
    for key in _CACHE_PROP_KEYS:
        try:
            value = obj.get(key)
        except Exception:
            value = None
        if value in (None, "", [], {}, ()):
            continue
        props[key] = value
    return props


def update_prefix_record_for_object(obj, extra_owners: Iterable[object] = ()):
    identity = _object_prefix_identity(getattr(obj, "name", ""))
    if identity is None:
        return

    merged_props = {}
    for owner in (obj, *tuple(extra_owners)):
        for key, value in _collect_object_props(owner).items():
            merged_props[key] = value

    if not merged_props:
        return

    payload = _read_payload()
    entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
    updated_entry = {
        "lod_name": identity[0],
        "bare_prefix": identity[1],
        "object_name": str(getattr(obj, "name", "") or ""),
        "props": merged_props,
    }

    replaced = False
    for index, entry in enumerate(entries):
        candidate_identity = (
            str(entry.get("lod_name", "") or "").strip().lower(),
            str(entry.get("bare_prefix", "") or "").strip().lower(),
        )
        if prefix_identity_matches(identity, candidate_identity):
            entries[index] = updated_entry
            replaced = True
            break
    if not replaced:
        entries.append(updated_entry)

    payload["entries"] = entries
    _write_payload(payload)


def get_prefix_record_props(obj_name: str) -> dict:
    identity = _object_prefix_identity(obj_name)
    if identity is None:
        return {}

    payload = _read_payload()
    matches = []
    for entry in payload.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        candidate_identity = (
            str(entry.get("lod_name", "") or "").strip().lower(),
            str(entry.get("bare_prefix", "") or "").strip().lower(),
        )
        if prefix_identity_matches(identity, candidate_identity):
            matches.append(entry)

    if not matches:
        return {}

    matches.sort(
        key=lambda entry: (
            str(entry.get("lod_name", "") or "").strip().lower() not in {"", identity[0]},
            str(entry.get("object_name", "") or "").endswith("_copy"),
            str(entry.get("object_name", "") or ""),
        )
    )
    props = matches[0].get("props", {})
    return dict(props) if isinstance(props, dict) else {}
