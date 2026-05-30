import bpy
import json
import os
import sys


TARGET_NAMES = {
    "LOD0.ae1ab184-71202-29187.袜子_copy",
    "LOD0.ae1ab184-71202-29187.脖子装饰_copy",
    "LOD0.ae1ab184-71202-29187.头发装饰_copy",
    "LOD0.ae1ab184-71202-29187.耳朵_copy",
    "LOD0.ae1ab184-71202-29187.袜子.001_copy",
    "LOD0.ae1ab184-71202-29187.脖子装饰.001_copy",
    "LOD0.ae1ab184-71202-29187.衣服手臂_copy",
    "LOD0.ae1ab184-71202-29187.上半身内衬.001_copy",
}


def _numeric_groups(obj):
    """获取对象中所有数字命名的顶点组"""
    return [vg.name for vg in getattr(obj, "vertex_groups", []) if str(vg.name).isdigit()]


def _workspace_unique(obj):
    """获取对象的 3DMigoto:WorkspaceUniqueStr 自定义属性"""
    try:
        return str(obj.get("3DMigoto:WorkspaceUniqueStr", "") or "")
    except Exception:
        return ""


def _collect_object_report():
    """收集目标副本对象的属性报告（类型、顶点组等）"""
    report = []
    for name in sorted(TARGET_NAMES):
        obj = bpy.data.objects.get(name)
        if obj is None:
            report.append({"name": name, "exists": False})
            continue
        report.append(
            {
                "name": name,
                "exists": True,
                "type": getattr(obj, "type", ""),
                "workspace_unique": _workspace_unique(obj),
                "vertex_group_count": len(getattr(obj, "vertex_groups", [])),
                "numeric_vertex_groups": _numeric_groups(obj),
                "first_vertex_groups": [vg.name for vg in list(getattr(obj, "vertex_groups", []))[:20]],
            }
        )
    return report


def _iter_blueprint_trees():
    """遍历场景中所有蓝图树"""
    for node_group in bpy.data.node_groups:
        if getattr(node_group, "bl_idname", "") == "SSMTBlueprintTreeType":
            yield node_group


def _collect_node_report():
    """收集蓝图树中与目标对象相关的 Object_Info 节点信息"""
    report = []
    for tree in _iter_blueprint_trees():
        for node in getattr(tree, "nodes", []):
            if getattr(node, "bl_idname", "") != "SSMTNode_Object_Info":
                continue
            object_name = str(getattr(node, "object_name", "") or "")
            if object_name not in TARGET_NAMES and f"{object_name}_copy" not in TARGET_NAMES:
                continue
            report.append(
                {
                    "tree": tree.name,
                    "node": node.name,
                    "label": getattr(node, "label", ""),
                    "object_name": object_name,
                    "object_id": str(getattr(node, "object_id", "") or ""),
                    "original_object_name": str(getattr(node, "original_object_name", "") or ""),
                    "object_prefix": str(getattr(node, "object_prefix", "") or ""),
                    "prefix_separator": str(getattr(node, "prefix_separator", "") or ""),
                }
            )
    return report


def main():
    """Blend 文件探索主函数：收集副本对象和蓝图节点信息并输出 JSON"""
    payload = {
        "blend": bpy.data.filepath,
        "objects": _collect_object_report(),
        "nodes": _collect_node_report(),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
