"""NTEMI(异环·安魂曲) specific importer module.

This module creates Blender mesh objects using the **exact same pipeline** as
mod_importer-main's `_import_single_slice()`, reading workspace buf/ib files
with the reference plugin's io functions and building meshes identically.

Vertex groups, weights, normals, UVs, tangent frames, custom attributes —
every detail matches the reference plugin.
"""

from __future__ import annotations

import importlib
import json
import os
import struct
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bpy

from .runtime_cache import (
    MODIMP_COLLECTOR_PROPS,
    MODIMP_PATH_PROPS,
    localize_runtime_path_props,
    object_workspace_dir_from_type_dir,
    object_workspace_dir_from_unique,
)
from .modimp_core import (
    ensure_mod_importer_package,
    resolve_mod_importer_root,
)

NTEMI_PROFILE_ID = "yihuan"
RESULT_NODE_TYPE_NTEMI_MODIMP = "SSMTNode_Result_Output_NTMIModImp"


# ── game data converter injection ──────────────────────────────────────

def _ensure_ntemi_game_data_converter(configured_root: str = ""):
    package = ensure_mod_importer_package(configured_root)
    game_data_module = importlib.import_module(f"{package.__name__}.core.game_data")
    yihuan_converter = game_data_module._CONVERTERS.get("yihuan")
    if yihuan_converter is None:
        raise RuntimeError("Yihuan converter not found in mod_importer-main")
    return yihuan_converter


def get_ntemi_game_data_converter(configured_root: str = ""):
    return _ensure_ntemi_game_data_converter(configured_root)


# ── reference plugin io function access ─────────────────────────────────

def _get_ref_module(configured_root: str = ""):
    package = ensure_mod_importer_package(configured_root)
    return importlib.import_module(f"{package.__name__}.core.io")


def _read_vb0_positions(path: str) -> list:
    io_module = _get_ref_module()
    return io_module.read_vb0_positions(path)


def _read_half2x4_records(path: str) -> list:
    io_module = _get_ref_module()
    return io_module.read_half2x4_records(path)


def _read_weight_pairs(path: str, *, vertex_count=None) -> tuple:
    io_module = _get_ref_module()
    return io_module.read_weight_pairs(path, vertex_count=vertex_count)


def _read_pre_cs_frame_pairs(path: str, *, vertex_count=None) -> tuple:
    io_module = _get_ref_module()
    return io_module.read_pre_cs_frame_pairs(path, vertex_count=vertex_count)


def _build_compacted_geometry(positions, triangles, packed_uv_entries):
    io_module = _get_ref_module()
    return io_module.build_compacted_geometry(positions, triangles, packed_uv_entries)


# ── helpers ────────────────────────────────────────────────────────────

def _resolve_frame_analysis_dir(workspace_root: str) -> str:
    config_path = os.path.join(workspace_root, "Config", "FrameAnalysisPath.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            path = str(payload.get("frameAnalysisFolderPath", "") or "").strip()
            if path and os.path.isdir(path):
                return path
        except Exception:
            pass

    config_directory = os.path.join(workspace_root, "Config", "Tabs")
    if os.path.isdir(config_directory):
        for tab_file in sorted(Path(config_directory).glob("ws-tab-*.json")):
            try:
                payload = json.loads(tab_file.read_text(encoding="utf-8"))
                path = str(payload.get("frameAnalysisFolderPath", "") or "").strip()
                if path and os.path.isdir(path):
                    return path
            except Exception:
                continue
    return ""


def _resolve_deduped_texture_dir(workspace_root: str) -> str:
    lod0_dir = os.path.join(workspace_root, "LOD0")
    deduped_dir = os.path.join(lod0_dir, "DedupedTextures")
    if os.path.isdir(deduped_dir):
        return deduped_dir
    return ""


def _read_index_binary(ib_path: str) -> list:
    data = Path(ib_path).read_bytes()
    if len(data) == 0:
        raise ValueError(f"IB buffer is empty: {ib_path}")
    if len(data) % 4 != 0:
        raise ValueError(f"IB buffer size is not a multiple of 4 bytes: {ib_path}")

    indices = [value[0] for value in struct.iter_unpack("<I", data)]
    if len(indices) % 3 != 0:
        raise ValueError(f"IB index count is not a multiple of 3: {ib_path}")

    triangles = []
    for i in range(0, len(indices), 3):
        triangles.append((int(indices[i]), int(indices[i + 1]), int(indices[i + 2])))
    return triangles


def _load_component_name_map(lod0_dir: str) -> Dict[str, list]:
    component_file = os.path.join(lod0_dir, "ComponentName_DrawCallIndexList.json")
    if os.path.isfile(component_file):
        try:
            with open(component_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ── texture resolution ───────────────────────────────────────────────

def _find_texture_in_type_dir(type_dir: str, submesh_folder_name: str, mark_name: str) -> str | None:
    candidate = os.path.join(type_dir, f"{submesh_folder_name}-{mark_name}.dds")
    if os.path.isfile(candidate):
        return candidate
    return None


def _find_texture_in_deduped(deduped_dir: str, hash_value: str) -> str | None:
    if not deduped_dir or not os.path.isdir(deduped_dir) or not hash_value:
        return None
    for entry in os.scandir(deduped_dir):
        if not entry.is_file() or not entry.name.lower().endswith(".dds"):
            continue
        if hash_value.lower() in entry.name.lower():
            return entry.path
    return None


def _build_texture_slots_from_workspace(
    type_dir: str,
    submesh_folder_name: str,
    deduped_texture_dir: str,
    texture_marks: list,
) -> dict:
    slots: dict = {}
    for mark in texture_marks or []:
        mark_name = str(mark.get("MarkName", "") or "").strip()
        mark_hash = str(mark.get("MarkHash", "") or "").strip()
        mark_slot = str(mark.get("MarkSlot", "") or "").strip()
        mark_type = str(mark.get("MarkType", "") or "").strip()
        mark_filename = str(mark.get("MarkFileName", "") or "").strip()
        if not mark_name or not mark_hash:
            continue

        path = _find_texture_in_type_dir(type_dir, submesh_folder_name, mark_name)
        if path is None:
            path = _find_texture_in_deduped(deduped_texture_dir, mark_hash)
        if path is None:
            continue

        ext = os.path.splitext(path)[1].lstrip(".")
        if not mark_filename:
            mark_filename = f"{mark_hash}-{mark_name}.{ext}"
        slots[mark_slot] = {
            "hash": mark_hash,
            "source_path": path,
            "extension": ext,
            "mark_type": mark_type,
            "mark_name": mark_name,
            "mark_filename": mark_filename,
        }
    return slots


# ── material ───────────────────────────────────────────────────────────

_MARK_NAME_TO_BSDF_INPUT = {
    "DiffuseMap": "Base Color",
    "NormalMap": "Normal",
    "LightMap": "Emission Color",
    "SpecularMap": "Specular",
    "RoughnessMap": "Roughness",
    "MetallicMap": "Metallic",
    "EmissionMap": "Emission Color",
    "OpacityMap": "Alpha",
    "AOMap": "Ambient Occlusion",
}

_NORMAL_MARK_NAMES = {"NormalMap", "Normal", "BumpMap", "Bump"}


def _apply_material_from_texture_slots(obj: bpy.types.Object, texture_slots: dict):
    if not texture_slots:
        _ensure_material(obj)
        return

    mat_name = f"{obj.name}_Material"
    material = bpy.data.materials.get(mat_name)
    if material is None:
        material = bpy.data.materials.new(mat_name)
        material.use_nodes = True

    if not material.use_nodes:
        material.use_nodes = True

    bsdf = None
    for node in material.node_tree.nodes:
        if node.bl_idname == 'ShaderNodeBsdfPrincipled':
            bsdf = node
            break
    if bsdf is None:
        bsdf = material.node_tree.nodes.new(type='ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)
        output = None
        for node in material.node_tree.nodes:
            if node.bl_idname == 'ShaderNodeOutputMaterial':
                output = node
                break
        if output is None:
            output = material.node_tree.nodes.new(type='ShaderNodeOutputMaterial')
            output.location = (300, 0)
        material.node_tree.links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    offset_x = -400
    offset_y = 0
    for slot_name, slot_data in sorted(texture_slots.items()):
        source_path = str(slot_data.get("source_path", "") or "").strip()
        mark_name = str(slot_data.get("mark_name", "") or "").strip()
        if not source_path or not os.path.isfile(source_path):
            continue

        tex_node = material.node_tree.nodes.new('ShaderNodeTexImage')
        try:
            tex_node.image = bpy.data.images.load(source_path)
        except Exception:
            material.node_tree.nodes.remove(tex_node)
            continue
        tex_node.location.x = offset_x
        tex_node.location.y = offset_y
        offset_y -= 300

        is_normal = mark_name in _NORMAL_MARK_NAMES or "normal" in mark_name.lower() or "bump" in mark_name.lower()
        if is_normal:
            tex_node.image.colorspace_settings.is_data = True
            tex_node.image.colorspace_settings.name = 'Non-Color'
            norm_map = material.node_tree.nodes.new('ShaderNodeNormalMap')
            norm_map.location.x = offset_x + 300
            norm_map.location.y = tex_node.location.y
            material.node_tree.links.new(norm_map.inputs['Color'], tex_node.outputs['Color'])
            material.node_tree.links.new(bsdf.inputs['Normal'], norm_map.outputs['Normal'])
        else:
            bsdf_input_name = _MARK_NAME_TO_BSDF_INPUT.get(mark_name, "")
            if bsdf_input_name and bsdf_input_name in bsdf.inputs:
                if bsdf_input_name == "Base Color":
                    material.node_tree.links.new(bsdf.inputs['Base Color'], tex_node.outputs['Color'])
                    material.node_tree.links.new(bsdf.inputs['Alpha'], tex_node.outputs['Alpha'])
                elif bsdf_input_name == "Alpha":
                    material.node_tree.links.new(bsdf.inputs['Alpha'], tex_node.outputs['Color'])
                else:
                    material.node_tree.links.new(bsdf.inputs[bsdf_input_name], tex_node.outputs['Color'])
            else:
                material.node_tree.links.new(bsdf.inputs['Base Color'], tex_node.outputs['Color'])

    if not obj.material_slots:
        obj.data.materials.append(material)


def _ensure_material(obj: bpy.types.Object):
    if obj.material_slots:
        return
    mat_name = f"{obj.name}_Material"
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(mat_name)
        mat.use_nodes = True
    obj.data.materials.append(mat)


# ── outline param helpers ──────────────────────────────────────────────

def _store_outline_param_attributes(mesh: bpy.types.Mesh, values: list):
    if not values:
        return
    for channel_index, channel_name in enumerate(("r", "g", "b", "a")):
        _store_int_attribute(
            mesh,
            f"modimp_outline_{channel_name}",
            [int(record[channel_index]) for record in values],
        )
    color_attributes = getattr(mesh, "color_attributes", None)
    if color_attributes is None:
        return
    try:
        attribute = color_attributes.new(name="NTMI_OutlineParam", type="BYTE_COLOR", domain="POINT")
    except TypeError:
        return
    for item, value in zip(attribute.data, values):
        item.color = tuple(max(0, min(255, int(component))) / 255.0 for component in value)


def _read_outline_buffer(outline_buf_path: str) -> list | None:
    if not os.path.isfile(outline_buf_path):
        return None
    data = Path(outline_buf_path).read_bytes()
    if len(data) == 0 or len(data) % 4 != 0:
        return None
    return [tuple(values) for values in struct.iter_unpack("<4B", data)]


def _resolve_outline_param_buffer(frame_analysis_dir: str, draw_indices: list, outline_hash: str) -> str | None:
    if not outline_hash or not frame_analysis_dir:
        return None
    deduped = os.path.join(frame_analysis_dir, "deduped")
    if not os.path.isdir(deduped):
        return None
    for draw_index in sorted(int(v) for v in (draw_indices or []) if str(v).strip()):
        prefix = f"{draw_index:06d}-vs-t"
        for entry in sorted(os.scandir(deduped), key=lambda e: e.name):
            if entry.is_file() and entry.name.startswith(prefix) and f"={outline_hash}" in entry.name:
                return entry.path
    return None

def _store_vector_attribute(mesh: bpy.types.Mesh, name: str, values: list):
    attribute = mesh.attributes.new(name=name, type="FLOAT_VECTOR", domain="POINT")
    for item, value in zip(attribute.data, values):
        item.vector = value


def _store_float_attribute(mesh: bpy.types.Mesh, name: str, values: list):
    attribute = mesh.attributes.new(name=name, type="FLOAT", domain="POINT")
    for item, value in zip(attribute.data, values):
        item.value = float(value)


def _store_int_attribute(mesh: bpy.types.Mesh, name: str, values: list):
    attribute = mesh.attributes.new(name=name, type="INT", domain="POINT")
    for item, value in zip(attribute.data, values):
        item.value = int(value)


def _mirror_x_vector(vector):
    return (-float(vector[0]), float(vector[1]), float(vector[2]))


def _reverse_triangle_winding(triangle):
    return (triangle[0], triangle[2], triangle[1])


def _normalize3(vector):
    length = (vector[0] * vector[0] + vector[1] * vector[1] + vector[2] * vector[2]) ** 0.5
    if length <= 1e-8:
        return (0.0, 0.0, 0.0)
    return (vector[0] / length, vector[1] / length, vector[2] / length)


# ── vertex groups ──────────────────────────────────────────────────────

def _assign_palette_groups(
    imported_object: bpy.types.Object,
    blend_indices: list,
    blend_weights: list,
):
    vertex_groups: dict = {}
    for vertex_index, (index_record, weight_record) in enumerate(zip(blend_indices, blend_weights)):
        for palette_index, bone_weight in zip(index_record, weight_record):
            if bone_weight <= 0.0:
                continue
            vertex_group = vertex_groups.get(palette_index)
            if vertex_group is None:
                vertex_group = imported_object.vertex_groups.new(name=str(palette_index))
                vertex_groups[palette_index] = vertex_group
            vertex_group.add([vertex_index], bone_weight, "ADD")


# ── core import: replicates _import_single_slice exactly ────────────────

def _import_slice_ntemi(
    *,
    vb0_buf_path: str,
    t5_buf_path: str,
    weight_buf_path: str,
    frame_buf_path: str,
    outline_buf_path: str = "",
    ib_path: str,
    object_name: str,
    collection: bpy.types.Collection,
    shade_smooth: bool = True,
    store_orig_vertex_id: bool = True,
) -> tuple:
    converter = _ensure_ntemi_game_data_converter()

    positions = _read_vb0_positions(vb0_buf_path)
    packed_uv_entries = _read_half2x4_records(t5_buf_path)

    triangles = _read_index_binary(ib_path)

    has_weights = os.path.isfile(weight_buf_path)
    has_frames = os.path.isfile(frame_buf_path)

    blend_indices_u8 = None
    blend_weights_u8 = None
    pre_frame_a = None
    pre_frame_b = None

    if has_weights:
        blend_indices_u8, blend_weights_u8 = _read_weight_pairs(weight_buf_path, vertex_count=len(positions))
    if has_frames:
        pre_frame_a, pre_frame_b = _read_pre_cs_frame_pairs(frame_buf_path, vertex_count=len(positions))

    outline_records = _read_outline_buffer(outline_buf_path)

    geometry = _build_compacted_geometry(positions, triangles, packed_uv_entries)

    blender_positions = [converter.to_blender_position(position) for position in geometry.positions]

    compact_blend_indices = None
    compact_blend_weights = None
    decoded_tangents = None
    decoded_normals = None
    decoded_bitangent_signs = None
    compact_normals = None

    if blend_indices_u8 is not None and blend_weights_u8 is not None:
        compact_blend_indices = [blend_indices_u8[vertex_id] for vertex_id in geometry.original_vertex_ids]
        compact_blend_weights = [
            tuple(component / 255.0 for component in blend_weights_u8[vertex_id])
            for vertex_id in geometry.original_vertex_ids
        ]

    if pre_frame_a is not None and pre_frame_b is not None:
        compact_frame_a = [pre_frame_a[vertex_id] for vertex_id in geometry.original_vertex_ids]
        compact_frame_b = [pre_frame_b[vertex_id] for vertex_id in geometry.original_vertex_ids]
        decoded_frames = converter.decode_pre_cs_frames(compact_frame_a, compact_frame_b)
        decoded_tangents = [frame.tangent for frame in decoded_frames]
        decoded_normals = [frame.normal for frame in decoded_frames]
        decoded_bitangent_signs = [frame.bitangent_sign for frame in decoded_frames]
        compact_normals = decoded_normals

    mesh = bpy.data.meshes.new(object_name)
    imported_object = bpy.data.objects.new(object_name, mesh)
    collection.objects.link(imported_object)

    blender_triangles = [_reverse_triangle_winding(triangle) for triangle in geometry.triangles]
    mesh.from_pydata(blender_positions, [], blender_triangles)
    mesh.validate(verbose=False, clean_customdata=False)
    mesh.update()

    if shade_smooth:
        for polygon in mesh.polygons:
            polygon.use_smooth = True

    for uv_index in range(4):
        uv_layer = mesh.uv_layers.new(name=f"UV{uv_index}")
        for polygon in mesh.polygons:
            for loop_index in polygon.loop_indices:
                vertex_index = mesh.loops[loop_index].vertex_index
                u_coord, v_coord = geometry.packed_uv_entries[vertex_index][uv_index]
                uv_layer.data[loop_index].uv = (u_coord, 1.0 - v_coord)
    if mesh.uv_layers:
        mesh.uv_layers.active = mesh.uv_layers[0]

    for entry_index in range(1, 4):
        _store_vector_attribute(
            mesh,
            f"packed_uv{entry_index}",
            [
                (float(record[entry_index][0]), float(record[entry_index][1]), 0.0)
                for record in geometry.packed_uv_entries
            ],
        )

    if compact_normals is not None:
        mesh.normals_split_custom_set_from_vertices(compact_normals)

    if decoded_tangents is not None and decoded_normals is not None and decoded_bitangent_signs is not None:
        _store_vector_attribute(mesh, "modimp_tangent", decoded_tangents)
        _store_vector_attribute(mesh, "modimp_normal", decoded_normals)
        _store_float_attribute(mesh, "modimp_bitangent_sign", decoded_bitangent_signs)

    if compact_blend_indices is not None and compact_blend_weights is not None:
        for slot_index in range(4):
            _store_int_attribute(mesh, f"blend_index_{slot_index}", [record[slot_index] for record in compact_blend_indices])
            _store_float_attribute(mesh, f"blend_weight_{slot_index}", [record[slot_index] for record in compact_blend_weights])
        _assign_palette_groups(imported_object, compact_blend_indices, compact_blend_weights)

    if outline_records is not None:
        max_vertex_id = max(geometry.original_vertex_ids, default=-1)
        if max_vertex_id < len(outline_records):
            compact_outline_params = [outline_records[vertex_id] for vertex_id in geometry.original_vertex_ids]
            _store_outline_param_attributes(mesh, compact_outline_params)

    if store_orig_vertex_id:
        _store_int_attribute(mesh, "orig_vertex_id", [int(value) for value in geometry.original_vertex_ids])

    return imported_object, {
        "vertex_count": len(mesh.vertices),
        "triangle_count": len(geometry.triangles),
    }


# ── DrawCall discovery ─────────────────────────────────────────────────

@dataclass
class NtemiDrawCallMeta:
    lod_name: str
    submesh_folder_name: str
    folder_path: str
    draw_ib: str
    first_index: int
    index_count: int
    display_name: str
    alias_name: str
    component: str


def _discover_draw_calls(workspace_root: str, drawib_aliasname_dict: dict = None) -> List[NtemiDrawCallMeta]:
    draw_calls: List[NtemiDrawCallMeta] = []
    lod0_dir = os.path.join(workspace_root, "LOD0")
    if not os.path.isdir(lod0_dir):
        return draw_calls
    if drawib_aliasname_dict is None:
        drawib_aliasname_dict = {}

    for entry in sorted(os.scandir(lod0_dir), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        folder_name = entry.name
        parts = folder_name.split("-")
        if len(parts) < 3:
            continue
        draw_ib = parts[0]
        index_count = int(parts[1])
        first_index = int(parts[2])

        type_subdirs = sorted(Path(entry.path).glob("TYPE_*"))
        if not type_subdirs:
            continue
        type_dir = str(type_subdirs[0])

        json_file = os.path.join(type_dir, f"{folder_name}.json")
        if not os.path.isfile(json_file):
            continue

        alias_name = drawib_aliasname_dict.get(draw_ib, "")
        display_name = folder_name
        if alias_name:
            display_name = f"{alias_name}-{parts[1]}-{parts[2]}"

        draw_calls.append(NtemiDrawCallMeta(
            lod_name="LOD0",
            submesh_folder_name=folder_name,
            folder_path=type_dir,
            draw_ib=draw_ib,
            first_index=first_index,
            index_count=index_count,
            display_name=display_name,
            alias_name=alias_name,
            component=parts[1],
        ))
    return draw_calls


# ── modimp properties ──────────────────────────────────────────────────

def _apply_ntemi_modimp_properties(
    obj: bpy.types.Object,
    *,
    draw_call_meta: NtemiDrawCallMeta,
    json_dict: dict,
    vb0_buf_path: str = "",
    t5_buf_path: str = "",
    weight_buf_path: str = "",
    frame_buf_path: str = "",
    draw_indices: list = None,
    workspace_unique_str: str = "",
    frame_analysis_dir: str = "",
):
    draw_ib = draw_call_meta.draw_ib
    first_index = draw_call_meta.first_index
    index_count = draw_call_meta.index_count
    category_hash = json_dict.get("CategoryHash", {})

    obj["modimp_profile_id"] = NTEMI_PROFILE_ID
    obj["modimp_ib_hash"] = draw_ib
    obj["modimp_source_ib_hash"] = draw_ib
    obj["modimp_region_hash"] = draw_ib
    obj["modimp_region_index_count"] = index_count
    obj["modimp_region_first_index"] = first_index
    obj["modimp_display_ib_hash"] = draw_ib

    ib_file = os.path.join(draw_call_meta.folder_path, f"{draw_call_meta.submesh_folder_name}.ib")
    obj["modimp_ib_txt_path"] = ib_file if os.path.isfile(ib_file) else ""
    obj["modimp_vb0_buf_path"] = vb0_buf_path
    obj["modimp_t5_buf_path"] = t5_buf_path
    obj["modimp_weight_buf_path"] = weight_buf_path
    obj["modimp_frame_buf_path"] = frame_buf_path

    vg_count = int(json_dict.get("VGCount", 0))
    obj["modimp_import_variant"] = "pre_cs" if vg_count > 0 else "post_cs"
    obj["modimp_first_index"] = first_index
    obj["modimp_index_count"] = index_count
    obj["modimp_slice_order"] = first_index

    vertex_min = int(category_hash.get("VertexMin", 0) or 0)
    vertex_max = int(category_hash.get("VertexMax", 0) or 0)
    if vertex_max > 0:
        obj["modimp_used_vertex_start"] = vertex_min
        obj["modimp_used_vertex_end"] = vertex_max
    else:
        vertex_offset = int(json_dict.get("VertexOffset", 0))
        vertex_count = int(json_dict.get("VertexCount", 0) or 0)
        obj["modimp_used_vertex_start"] = vertex_offset
        obj["modimp_used_vertex_end"] = vertex_offset + vertex_count

    if draw_indices:
        obj["modimp_draw_indices"] = ",".join(str(int(v)) for v in draw_indices)
    else:
        obj["modimp_draw_indices"] = ""

    if category_hash.get("Texcoord"):
        obj["modimp_match_vs_texcoord_hash"] = str(category_hash["Texcoord"])
    if category_hash.get("Position"):
        obj["modimp_match_vs_position_hash"] = str(category_hash["Position"])

    obj["modimp_mirror_flip"] = False
    obj["modimp_root_vb0_path"] = ""
    obj["modimp_root_vb0_note"] = ""

    if frame_analysis_dir:
        deduped = os.path.join(frame_analysis_dir, "deduped")
        if os.path.isdir(deduped):
            if category_hash.get("Position"):
                pos_hash = str(category_hash["Position"]).lower()
                for entry in os.scandir(deduped):
                    if entry.is_file() and pos_hash in entry.name.lower() and not entry.name.endswith(".txt"):
                        obj["modimp_root_vb0_path"] = entry.path
                        obj["modimp_root_vb0_note"] = "Closest bind/rest-like source traced dynamically from the producer dispatch chain."
                        break

    if workspace_unique_str:
        obj["modimp_workspace_unique_str"] = workspace_unique_str


def _resolve_vb1_layout_path(frame_analysis_dir: str) -> str:
    if not frame_analysis_dir:
        return ""
    deduped = os.path.join(frame_analysis_dir, "deduped")
    if not os.path.isdir(deduped):
        return ""
    candidates = []
    for entry in os.scandir(deduped):
        if entry.is_file() and "vb1-layout" in entry.name.lower():
            candidates.append(entry.path)
    return candidates[0] if candidates else ""


def _resolve_ib_txt_path(frame_analysis_dir: str, draw_ib: str, first_index: int, index_count: int) -> str:
    if not frame_analysis_dir:
        return ""
    deduped = os.path.join(frame_analysis_dir, "deduped")
    if not os.path.isdir(deduped):
        return ""
    for entry in os.scandir(deduped):
        if not entry.is_file():
            continue
        name = entry.name.lower()
        if not name.startswith(draw_ib.lower()):
            continue
        if "ib-format" not in name:
            continue
        if f"first={first_index}" in name or f"count={index_count}" in name:
            return entry.path
    return ""


def _resolve_buf_path_in_frame_analysis(frame_analysis_dir: str, hash_value: str, suffix: str = ".buf") -> str:
    if not frame_analysis_dir or not hash_value:
        return ""
    deduped = os.path.join(frame_analysis_dir, "deduped")
    if not os.path.isdir(deduped):
        return ""
    for entry in os.scandir(deduped):
        if entry.is_file() and hash_value.lower() in entry.name.lower() and entry.name.lower().endswith(suffix):
            return entry.path
    return ""


# ── high-level import helper ───────────────────────────────────────────

def _resolve_root_vb0_path(frame_analysis_dir: str, position_hash: str) -> str:
    if not frame_analysis_dir or not position_hash:
        return ""
    deduped = os.path.join(frame_analysis_dir, "deduped")
    if not os.path.isdir(deduped):
        return ""
    normalized_hash = str(position_hash).lower()
    for entry in os.scandir(deduped):
        if entry.is_file() and normalized_hash in entry.name.lower() and not entry.name.endswith(".txt"):
            return entry.path
    return ""


def _set_object_props(obj: bpy.types.Object, props: dict[str, str]):
    for key, value in props.items():
        if value:
            obj[key] = value


def _localize_object_runtime_paths(obj: bpy.types.Object, object_workspace_dir: str):
    path_props = {
        key: str(obj.get(key, "") or "").strip()
        for key in MODIMP_PATH_PROPS
    }
    localized = localize_runtime_path_props(path_props, object_workspace_dir)
    _set_object_props(obj, localized)


def _build_runtime_path_props(
    *,
    draw_call_meta: NtemiDrawCallMeta,
    category_hash: dict,
    vb0_buf_path: str,
    t5_buf_path: str,
    weight_buf_path: str,
    frame_buf_path: str,
    frame_analysis_dir: str = "",
) -> dict[str, str]:
    ib_file = os.path.join(draw_call_meta.folder_path, f"{draw_call_meta.submesh_folder_name}.ib")
    path_props = {
        "modimp_ib_txt_path": ib_file if os.path.isfile(ib_file) else "",
        "modimp_vb0_buf_path": vb0_buf_path,
        "modimp_t5_buf_path": t5_buf_path,
        "modimp_weight_buf_path": weight_buf_path,
        "modimp_frame_buf_path": frame_buf_path,
    }

    if not frame_analysis_dir:
        return path_props

    ib_txt_path = _resolve_ib_txt_path(
        frame_analysis_dir,
        draw_call_meta.draw_ib,
        draw_call_meta.first_index,
        draw_call_meta.index_count,
    )
    if ib_txt_path:
        path_props["modimp_ib_txt_path"] = ib_txt_path

    hash_to_prop = (
        ("Position", "modimp_vb0_buf_path"),
        ("Texcoord", "modimp_t5_buf_path"),
        ("Blend", "modimp_weight_buf_path"),
        ("Normal", "modimp_frame_buf_path"),
    )
    for hash_key, prop_name in hash_to_prop:
        hash_value = category_hash.get(hash_key, "")
        if not hash_value:
            continue
        resolved_path = _resolve_buf_path_in_frame_analysis(frame_analysis_dir, hash_value)
        if resolved_path:
            path_props[prop_name] = resolved_path

    vb1_layout = _resolve_vb1_layout_path(frame_analysis_dir)
    if vb1_layout:
        path_props["modimp_vb1_layout_path"] = vb1_layout

    root_vb0_path = _resolve_root_vb0_path(frame_analysis_dir, category_hash.get("Position", ""))
    if root_vb0_path:
        path_props["modimp_root_vb0_path"] = root_vb0_path

    return path_props


class NTEMIImportHelper:
    @staticmethod
    def create_mesh_with_modimp_props(
        *,
        json_file_path: str,
        draw_call_meta: NtemiDrawCallMeta,
        import_collection: bpy.types.Collection | None = None,
        deduped_texture_dir: str = "",
        component_map: dict = None,
        workspace_unique_str: str = "",
        frame_analysis_dir: str = "",
    ):
        json_dict = {}
        try:
            with open(json_file_path, "r", encoding="utf-8") as f:
                json_dict = json.load(f)
        except Exception:
            pass

        if import_collection is None:
            import_collection = bpy.context.scene.collection

        vb0_buf_path = os.path.join(draw_call_meta.folder_path, f"{draw_call_meta.submesh_folder_name}-Position.buf")
        t5_buf_path = os.path.join(draw_call_meta.folder_path, f"{draw_call_meta.submesh_folder_name}-Texcoord.buf")
        weight_buf_path = os.path.join(draw_call_meta.folder_path, f"{draw_call_meta.submesh_folder_name}-Blend.buf")
        frame_buf_path = os.path.join(draw_call_meta.folder_path, f"{draw_call_meta.submesh_folder_name}-Normal.buf")
        outline_buf_path = os.path.join(draw_call_meta.folder_path, f"{draw_call_meta.submesh_folder_name}-Color.buf")
        ib_path = os.path.join(draw_call_meta.folder_path, f"{draw_call_meta.submesh_folder_name}.ib")

        if not os.path.isfile(vb0_buf_path) or not os.path.isfile(ib_path):
            return None

        imported_obj, stats = _import_slice_ntemi(
            vb0_buf_path=vb0_buf_path,
            t5_buf_path=t5_buf_path if os.path.isfile(t5_buf_path) else "",
            weight_buf_path=weight_buf_path if os.path.isfile(weight_buf_path) else "",
            frame_buf_path=frame_buf_path if os.path.isfile(frame_buf_path) else "",
            outline_buf_path=outline_buf_path if os.path.isfile(outline_buf_path) else "",
            ib_path=ib_path,
            object_name=draw_call_meta.display_name,
            collection=import_collection,
            shade_smooth=True,
            store_orig_vertex_id=True,
        )

        category_hash = json_dict.get("CategoryHash", {})

        texture_marks = json_dict.get("TextureMarkUpInfoList", [])
        workspace_slots = _build_texture_slots_from_workspace(
            draw_call_meta.folder_path,
            draw_call_meta.submesh_folder_name,
            deduped_texture_dir,
            texture_marks,
        )
        if workspace_slots:
            imported_obj["modimp_texture_slots"] = json.dumps(workspace_slots, ensure_ascii=False)

        _apply_material_from_texture_slots(imported_obj, workspace_slots)

        draw_indices = None
        if component_map:
            unique_str = f"{draw_call_meta.draw_ib}-{draw_call_meta.index_count}-{draw_call_meta.first_index}"
            draw_indices = component_map.get(unique_str)
            if draw_indices is None:
                draw_indices = component_map.get(draw_call_meta.submesh_folder_name)

        _apply_ntemi_modimp_properties(
            imported_obj,
            draw_call_meta=draw_call_meta,
            json_dict=json_dict,
            vb0_buf_path=vb0_buf_path,
            t5_buf_path=t5_buf_path,
            weight_buf_path=weight_buf_path,
            frame_buf_path=frame_buf_path,
            draw_indices=draw_indices,
            workspace_unique_str=workspace_unique_str,
            frame_analysis_dir=frame_analysis_dir,
        )

        runtime_path_props = _build_runtime_path_props(
            draw_call_meta=draw_call_meta,
            category_hash=category_hash,
            vb0_buf_path=vb0_buf_path,
            t5_buf_path=t5_buf_path,
            weight_buf_path=weight_buf_path,
            frame_buf_path=frame_buf_path,
            frame_analysis_dir=frame_analysis_dir,
        )
        localized_path_props = localize_runtime_path_props(
            runtime_path_props,
            object_workspace_dir_from_type_dir(draw_call_meta.folder_path),
        )
        _set_object_props(imported_obj, localized_path_props)

        return imported_obj


def _perform_bone_merge_postprocess(
    objects: list,
    *,
    frame_analysis_dir: str,
    draw_ib: str,
    workspace_root: str = "",
):
    if not objects or not frame_analysis_dir or not draw_ib:
        return

    package = ensure_mod_importer_package()
    discovery_module = importlib.import_module(f"{package.__name__}.core.discovery")
    operators_module = importlib.import_module(f"{package.__name__}.operators")

    print(f"[NTEMI BoneMerge] discovering model for IB={draw_ib} from {frame_analysis_dir}")
    detected_model = discovery_module.discover_yihuan_model(
        frame_dump_dir=frame_analysis_dir,
        ib_hash=draw_ib,
    )
    print(f"[NTEMI BoneMerge] discovered model with {len(detected_model.slices)} slices")

    summary = discovery_module.analyze_yihuan_frame_stages(
        frame_dump_dir=frame_analysis_dir,
        ib_hash=draw_ib,
    )
    print(f"[NTEMI BoneMerge] analyzed frame stages, dispatches={summary.get('dispatch_count', 0)}")

    collector_contract = operators_module._build_collector_runtime_contract(summary, detected_model)
    print(f"[NTEMI BoneMerge] built collector contract with {len(collector_contract)} fields")

    if collector_contract:
        seen_collections = set()
        for obj in objects:
            for key in MODIMP_COLLECTOR_PROPS:
                value = collector_contract.get(key)
                if value is not None:
                    obj[key] = value
            for coll in getattr(obj, "users_collection", []) or []:
                if coll.name in seen_collections:
                    continue
                seen_collections.add(coll.name)
                for key, value in collector_contract.items():
                    if value is not None:
                        coll[key] = value

    bone_merge_map = operators_module._build_bone_merge_map(summary, detected_model)
    entries = bone_merge_map.get("entries", [])
    print(f"[NTEMI BoneMerge] built bone merge map with {len(entries)} entries")

    if not entries:
        print("[NTEMI BoneMerge] bone merge map has no entries, skipping bone merge.")
    else:
        renamed_count = operators_module._apply_bone_merge_map_to_objects(
            objects,
            bone_merge_map,
        )
        print(f"[NTEMI BoneMerge] renamed {renamed_count} vertex groups across {len(objects)} objects")

    for detected_slice in detected_model.slices:
        for obj in objects:
            if not (
                int(obj.get("modimp_region_first_index", 0) or 0) == int(detected_slice.first_index)
                and int(obj.get("modimp_region_index_count", 0) or 0) == int(detected_slice.index_count)
            ):
                continue

            if detected_slice.draw_indices:
                obj["modimp_draw_indices"] = ",".join(str(int(v)) for v in detected_slice.draw_indices)
            if detected_slice.vb1_layout_path is not None:
                obj["modimp_vb1_layout_path"] = detected_slice.vb1_layout_path
            if detected_slice.match_vs_outline_hash is not None:
                obj["modimp_match_vs_outline_hash"] = detected_slice.match_vs_outline_hash
            if detected_slice.match_vs_texcoord_hash is not None:
                obj["modimp_match_vs_texcoord_hash"] = detected_slice.match_vs_texcoord_hash
            if detected_slice.match_vs_position_hash is not None:
                obj["modimp_match_vs_position_hash"] = detected_slice.match_vs_position_hash

            if workspace_root:
                object_workspace_dir = object_workspace_dir_from_unique(
                    workspace_root,
                    str(obj.get("modimp_workspace_unique_str", "") or ""),
                )
                _localize_object_runtime_paths(obj, object_workspace_dir)

    print(f"[NTEMI BoneMerge] completed successfully")
