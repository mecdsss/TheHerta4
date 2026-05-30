import importlib
import json
import os
import sys

import bpy


TARGET_OBJECT = "LOD0.ae1ab184-71202-29187.00下半身裙子替换"
TARGET_COPY = f"{TARGET_OBJECT}_copy"
TARGET_HASH_EXACT = "LOD0.ae1ab184-71202-29187.00"


def _numeric_groups(obj):
    """获取对象中所有数字命名的顶点组"""
    return [vg.name for vg in getattr(obj, "vertex_groups", []) if str(vg.name).isdigit()]


def _all_groups(obj, limit=80):
    """获取对象中前N个顶点组名称"""
    return [vg.name for vg in list(getattr(obj, "vertex_groups", []))[:limit]]


def _find_tree():
    """在场景中查找名为"娜娜丽"的 SSMTBlueprintTreeType 蓝图树"""
    for node_group in bpy.data.node_groups:
        if getattr(node_group, "bl_idname", "") == "SSMTBlueprintTreeType" and node_group.name == "娜娜丽":
            return node_group
    return None


def _collect_nested_trees(tree):
    """递归收集蓝图树中所有嵌套的子蓝图树"""
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


def _prepare_pipeline(tree):
    """准备并执行预处理管线，返回构建的 BluePrintModel"""
    addon_root = os.path.dirname(os.path.dirname(__file__))
    if addon_root not in sys.path:
        sys.path.insert(0, addon_root)

    export_helper_module = importlib.import_module("TheHerta4.blueprint.export_helper")
    model_module = importlib.import_module("TheHerta4.blueprint.model")
    ntmi_export_modimp = importlib.import_module("TheHerta4.blueprint.ntmi_export_modimp")
    preprocess_module = importlib.import_module("TheHerta4.blueprint.preprocess")

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
            raise RuntimeError("No NTMI ModImp output node found")

        session = ntmi_export_modimp.NTMIModImpExportSession(context=bpy.context, tree=tree, node=node)
        object_names = session._collect_object_names()
        preprocess_module.PreProcessHelper.recover_blueprint_node_references(tree, nested_trees)
        original_to_copy_map = preprocess_module.PreProcessHelper.execute_preprocess(object_names)
        if original_to_copy_map:
            ntmi_export_modimp._sync_modimp_mirror_flags_after_preprocess(original_to_copy_map)
            preprocess_module.PreProcessHelper.update_blueprint_node_references(tree, nested_trees)

        blueprint_model = model_module.BluePrintModel(tree=tree, context=bpy.context)
        return blueprint_model
    finally:
        export_helper_module.BlueprintExportHelper.runtime_result_output_node_type = previous_result_type
        export_helper_module.BlueprintExportHelper.set_current_buffer_folder_name(previous_buffer_folder)
        export_helper_module.BlueprintExportHelper.set_current_export_index(previous_export_index)


def _find_vg_process_node(blueprint_model):
    """在 BluePrintModel 的处理链中查找匹配目标副本的顶点组处理节点"""
    for chain in getattr(blueprint_model, "processing_chains", []) or []:
        if str(getattr(chain, "object_name", "") or "") != TARGET_COPY:
            continue
        vg_nodes = getattr(chain, "vertex_group_process_nodes", []) or []
        if vg_nodes:
            return vg_nodes[0]
    return None


def _clone_object(obj, suffix):
    """克隆对象及其网格数据，链接到场景集合"""
    clone = obj.copy()
    clone.data = obj.data.copy()
    clone.name = f"{obj.name}{suffix}"
    bpy.context.scene.collection.objects.link(clone)
    return clone


def _cleanup_object(obj):
    """删除对象及其未使用的网格数据"""
    if obj is None:
        return
    mesh = getattr(obj, "data", None)
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and getattr(mesh, "users", 0) == 0:
        bpy.data.meshes.remove(mesh)


def _apply_process(node, obj, mapping_nodes):
    """对对象应用顶点组处理管线的所有步骤（重命名、合并、清理、排序等）"""
    merged_mapping = node.get_merged_mapping_for_object(obj.name, mapping_nodes)
    stats = {
        "mapping_size": len(merged_mapping),
        "mapping_sample": list(merged_mapping.items())[:20],
        "renamed": node._rename_vertex_groups(obj, merged_mapping) if merged_mapping else 0,
        "merged": node._merge_vertex_groups_by_prefix(obj),
        "cleaned": node._remove_non_numeric_vertex_groups(obj),
        "filled": node._fill_vertex_group_gaps(obj) if getattr(node, "fill_missing_groups", False) else 0,
    }
    node._sort_vertex_groups(obj)
    stats["numeric_groups"] = _numeric_groups(obj)
    stats["all_groups"] = _all_groups(obj)
    return stats


def main():
    """精确匹配案例分析主函数：比较有/无精确匹配节点的顶点组处理结果"""
    tree = _find_tree()
    if tree is None:
        sys.stdout.write(json.dumps({"error": "tree_not_found"}, ensure_ascii=False, indent=2))
        return

    blueprint_model = _prepare_pipeline(tree)
    process_node = _find_vg_process_node(blueprint_model)
    if process_node is None:
        sys.stdout.write(json.dumps({"error": "vg_process_node_not_found"}, ensure_ascii=False, indent=2))
        return

    mapping_nodes = process_node.get_connected_mapping_nodes() or []
    exact_nodes = []
    normal_nodes = []
    for item in mapping_nodes:
        node = item.get("node")
        row = {
            "node_name": getattr(node, "name", ""),
            "target_hash": item.get("target_hash", ""),
            "exact_hash_match": bool(getattr(node, "exact_hash_match", False)),
            "mapping_text_name": getattr(node, "mapping_text_name", ""),
        }
        if row["exact_hash_match"]:
            exact_nodes.append(row)
        else:
            normal_nodes.append(row)

    target_obj = bpy.data.objects.get(TARGET_COPY)
    if target_obj is None:
        sys.stdout.write(json.dumps({"error": "target_copy_not_found"}, ensure_ascii=False, indent=2))
        return

    current_match_nodes = [
        {
            "node_name": getattr(item.get("node"), "name", ""),
            "target_hash": item.get("target_hash", ""),
            "exact_hash_match": bool(getattr(item.get("node"), "exact_hash_match", False)),
            "matches": process_node._matches_target_hash(TARGET_COPY, item.get("target_hash", "")),
        }
        for item in mapping_nodes
    ]

    full_clone = _clone_object(target_obj, "__full_case")
    full_stats = _apply_process(process_node, full_clone, mapping_nodes)

    filtered_nodes = [
        item for item in mapping_nodes
        if not (
            bool(getattr(item.get("node"), "exact_hash_match", False))
            and str(item.get("target_hash", "") or "").strip() == TARGET_HASH_EXACT
        )
    ]
    filtered_clone = _clone_object(target_obj, "__no_exact_case")
    filtered_stats = _apply_process(process_node, filtered_clone, filtered_nodes)

    payload = {
        "target_object": TARGET_OBJECT,
        "target_copy": TARGET_COPY,
        "before_copy_numeric_groups": _numeric_groups(target_obj),
        "before_copy_all_groups": _all_groups(target_obj),
        "exact_nodes": exact_nodes,
        "normal_nodes_count": len(normal_nodes),
        "current_match_nodes": current_match_nodes,
        "with_exact": full_stats,
        "without_exact": filtered_stats,
    }

    _cleanup_object(full_clone)
    _cleanup_object(filtered_clone)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
