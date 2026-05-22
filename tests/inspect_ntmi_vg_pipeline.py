import importlib
import json
import os
import sys

import bpy


TARGET_BASE_NAMES = [
    "LOD0.ae1ab184-71202-29187.袜子",
    "LOD0.ae1ab184-71202-29187.脖子装饰",
    "LOD0.ae1ab184-71202-29187.头发装饰",
    "LOD0.ae1ab184-71202-29187.耳朵",
    "LOD0.ae1ab184-71202-29187.袜子.001",
    "LOD0.ae1ab184-71202-29187.脖子装饰.001",
    "LOD0.ae1ab184-71202-29187.衣服手臂",
    "LOD0.ae1ab184-71202-29187.上半身内衬.001",
]


def _numeric_groups(obj):
    return [vg.name for vg in getattr(obj, "vertex_groups", []) if str(vg.name).isdigit()]


def _all_groups(obj, limit=40):
    return [vg.name for vg in list(getattr(obj, "vertex_groups", []))[:limit]]


def _find_tree():
    for node_group in bpy.data.node_groups:
        if getattr(node_group, "bl_idname", "") == "SSMTBlueprintTreeType":
            return node_group
    return None


def _find_target_nodes(tree):
    matched = []
    for node in getattr(tree, "nodes", []):
        if getattr(node, "bl_idname", "") != "SSMTNode_Object_Info":
            continue
        object_name = str(getattr(node, "object_name", "") or "")
        if object_name in TARGET_BASE_NAMES:
            matched.append(node)
    return matched


def _collect_mapping_sources(vg_node):
    payload = []
    get_connected_mapping_nodes = getattr(vg_node, "get_connected_mapping_nodes", None)
    if not callable(get_connected_mapping_nodes):
        return payload
    for item in get_connected_mapping_nodes() or []:
        node = item.get("node")
        if node is None:
            continue
        entry = {
            "node_name": getattr(node, "name", ""),
            "node_type": getattr(node, "bl_idname", ""),
            "target_hash": item.get("target_hash", ""),
            "high_priority": bool(item.get("high_priority", False)),
        }
        if hasattr(node, "mapping_text"):
            entry["mapping_text"] = getattr(node, "mapping_text", "")
        if hasattr(node, "mapping_text_name"):
            entry["mapping_text_name"] = getattr(node, "mapping_text_name", "")
        if hasattr(node, "get_mapping_dict"):
            try:
                mapping = node.get_mapping_dict() or {}
            except Exception as exc:
                mapping = {"__error__": str(exc)}
            entry["mapping_size"] = len(mapping)
            sample_items = list(mapping.items())[:12]
            entry["mapping_sample"] = sample_items
        payload.append(entry)
    return payload


def _collect_before_state():
    report = []
    for base_name in TARGET_BASE_NAMES:
        obj = bpy.data.objects.get(base_name)
        report.append(
            {
                "name": base_name,
                "exists": obj is not None,
                "numeric_groups": _numeric_groups(obj) if obj else [],
                "all_groups": _all_groups(obj) if obj else [],
            }
        )
    return report


def _build_blueprint_model(tree):
    addon_root = os.path.dirname(os.path.dirname(__file__))
    if addon_root not in sys.path:
        sys.path.insert(0, addon_root)

    export_helper_module = importlib.import_module("TheHerta4.blueprint.export_helper")
    model_module = importlib.import_module("TheHerta4.blueprint.model")
    previous_result_type = export_helper_module.BlueprintExportHelper.runtime_result_output_node_type
    try:
        export_helper_module.BlueprintExportHelper.set_runtime_result_output_node_type(
            "SSMTNode_Result_Output_NTMIModImp"
        )
        return model_module.BluePrintModel(tree=tree, context=bpy.context)
    finally:
        export_helper_module.BlueprintExportHelper.set_runtime_result_output_node_type(previous_result_type)


def _collect_nested_trees(tree):
    nested = []
    visited = set()

    def walk(current_tree):
        if current_tree is None or current_tree.name in visited:
            return
        visited.add(current_tree.name)
        for node in getattr(current_tree, "nodes", []):
            if getattr(node, "bl_idname", "") != "SSMTNode_Blueprint_Nest":
                continue
            if getattr(node, "mute", False):
                continue
            nested_tree_name = str(getattr(node, "blueprint_name", "") or "").strip()
            nested_tree = bpy.data.node_groups.get(nested_tree_name) if nested_tree_name else None
            if nested_tree is None:
                continue
            nested.append(nested_tree)
            walk(nested_tree)

    walk(tree)
    return nested


def _collect_chain_state(blueprint_model):
    rows = []
    for chain in getattr(blueprint_model, "processing_chains", []) or []:
        original_object_name = str(getattr(chain, "original_object_name", "") or "")
        object_name = str(getattr(chain, "object_name", "") or "")
        export_name = ""
        getter = getattr(chain, "get_export_object_name", None)
        if callable(getter):
            try:
                export_name = getter() or ""
            except Exception as exc:
                export_name = f"__error__:{exc}"
        candidate_names = {original_object_name, object_name, export_name}
        if not candidate_names.intersection(TARGET_BASE_NAMES):
            continue
        rows.append(
            {
                "object_name": object_name,
                "original_object_name": original_object_name,
                "export_object_name": export_name,
                "reached_output": bool(getattr(chain, "reached_output", False)),
                "is_valid": bool(getattr(chain, "is_valid", False)),
                "vg_process_nodes": [getattr(node, "name", "") for node in getattr(chain, "vertex_group_process_nodes", []) or []],
                "vg_mapping_nodes": [getattr(node, "name", "") for node in getattr(chain, "vertex_group_mapping_nodes", []) or []],
            }
        )
    return rows


def _collect_vg_nodes(blueprint_model):
    rows = []
    seen = set()
    for chain in getattr(blueprint_model, "processing_chains", []) or []:
        candidate_names = {
            str(getattr(chain, "original_object_name", "") or ""),
            str(getattr(chain, "object_name", "") or ""),
        }
        if not candidate_names.intersection(TARGET_BASE_NAMES):
            continue
        for vg_node in getattr(chain, "vertex_group_process_nodes", []) or []:
            key = getattr(vg_node, "name", "")
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "node_name": getattr(vg_node, "name", ""),
                    "connected_mappings": _collect_mapping_sources(vg_node),
                }
            )
    return rows


def _collect_copy_state():
    report = []
    for base_name in TARGET_BASE_NAMES:
        copy_name = f"{base_name}_copy"
        obj = bpy.data.objects.get(copy_name)
        report.append(
            {
                "name": copy_name,
                "exists": obj is not None,
                "numeric_groups": _numeric_groups(obj) if obj else [],
                "all_groups": _all_groups(obj) if obj else [],
            }
        )
    return report


def _run_preprocess_pipeline(tree):
    addon_root = os.path.dirname(os.path.dirname(__file__))
    if addon_root not in sys.path:
        sys.path.insert(0, addon_root)

    ntmi_export_modimp = importlib.import_module("TheHerta4.blueprint.ntmi_export_modimp")
    preprocess_module = importlib.import_module("TheHerta4.blueprint.preprocess")
    export_helper_module = importlib.import_module("TheHerta4.blueprint.export_helper")
    model_module = importlib.import_module("TheHerta4.blueprint.model")

    previous_result_type = export_helper_module.BlueprintExportHelper.runtime_result_output_node_type
    previous_buffer_folder = export_helper_module.BlueprintExportHelper.get_current_buffer_folder_name()
    previous_export_index = export_helper_module.BlueprintExportHelper.current_export_index
    nested_trees = _collect_nested_trees(tree)
    try:
        export_helper_module.BlueprintExportHelper.set_runtime_result_output_node_type(
            "SSMTNode_Result_Output_NTMIModImp"
        )
        export_helper_module.BlueprintExportHelper.set_runtime_blueprint_tree(tree)
        export_helper_module.BlueprintExportHelper.set_current_export_index(1)
        export_helper_module.BlueprintExportHelper.set_current_buffer_folder_name("Buffer")
        model_module.BluePrintModel.clear_object_name_mapping()

        node = None
        for candidate in tree.nodes:
            if getattr(candidate, "bl_idname", "") == "SSMTNode_Result_Output_NTMIModImp":
                node = candidate
                break
        if node is None:
            return {"error": "no_ntmi_modimp_output_node"}

        session = ntmi_export_modimp.NTMIModImpExportSession(context=bpy.context, tree=tree, node=node)
        object_names = session._collect_object_names()
        preprocess_module.PreProcessHelper.recover_blueprint_node_references(tree, nested_trees)
        original_to_copy_map = preprocess_module.PreProcessHelper.execute_preprocess(object_names)
        if original_to_copy_map:
            ntmi_export_modimp._sync_modimp_mirror_flags_after_preprocess(original_to_copy_map)
            preprocess_module.PreProcessHelper.update_blueprint_node_references(tree, nested_trees)
        return {
            "object_names": object_names,
            "original_to_copy_map": dict(original_to_copy_map or {}),
        }
    finally:
        export_helper_module.BlueprintExportHelper.runtime_result_output_node_type = previous_result_type
        export_helper_module.BlueprintExportHelper.set_current_buffer_folder_name(previous_buffer_folder)
        export_helper_module.BlueprintExportHelper.set_current_export_index(previous_export_index)


def main():
    tree = _find_tree()
    payload = {
        "blend": bpy.data.filepath,
        "tree": getattr(tree, "name", None),
        "before_objects": _collect_before_state(),
        "target_nodes": [],
        "chains": [],
        "vg_nodes": [],
        "preprocess": {},
        "copy_objects": [],
    }
    if tree is None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    payload["target_nodes"] = [
        {
            "node_name": getattr(node, "name", ""),
            "object_name": getattr(node, "object_name", ""),
            "object_id": getattr(node, "object_id", ""),
            "object_prefix": getattr(node, "object_prefix", ""),
            "prefix_separator": getattr(node, "prefix_separator", ""),
        }
        for node in _find_target_nodes(tree)
    ]

    payload["preprocess"] = _run_preprocess_pipeline(tree)
    blueprint_model = _build_blueprint_model(tree)
    payload["chains"] = _collect_chain_state(blueprint_model)
    payload["vg_nodes"] = _collect_vg_nodes(blueprint_model)
    payload["copy_objects"] = _collect_copy_state()
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
