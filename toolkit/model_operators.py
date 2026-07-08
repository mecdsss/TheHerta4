import bmesh
import bpy
import math
from mathutils import Vector

from bpy.props import BoolProperty, CollectionProperty

from ..utils.obj_utils import ObjUtils
from ..utils.collection_utils import CollectionUtils
from ..utils.vertexgroup_utils import VertexGroupUtils
from ..utils.shapekey_utils import ShapeKeyUtils
from ..utils.algorithm_utils import AlgorithmUtils


MERGE_SPLIT_FACE_SOURCE_ATTR = "TH4_MergeSplitFaceSource"


def _get_single_selected_mesh():
    if len(bpy.context.selected_objects) == 0:
        return None
    obj = bpy.context.selected_objects[0]
    if obj.type != 'MESH':
        return None
    return obj


def _get_collection_mesh_objects(collection):
    """获取集合中所有网格物体"""
    return [obj for obj in collection.objects if obj.type == 'MESH']


def _iter_selected_mesh_objects(context):
    for obj in getattr(context, "selected_objects", []) or []:
        if obj is not None and getattr(obj, "type", "") == "MESH":
            yield obj


def _ensure_object_mode():
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')


def _select_only_objects(context, objects, active_obj=None):
    bpy.ops.object.select_all(action='DESELECT')
    for obj in objects:
        obj.select_set(True)
    context.view_layer.objects.active = active_obj or (objects[0] if objects else None)


def _prepare_shape_keys_for_merge(obj):
    if obj is None or getattr(obj, "type", "") != "MESH":
        return
    shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    key_blocks = getattr(shape_keys, "key_blocks", None)
    if not key_blocks:
        return
    # If the object currently shows a deformed mesh because of shape key values,
    # bake that current state into Basis before join, otherwise merged Basis drifts.
    ShapeKeyUtils.bake_current_shape_key_mix_to_mesh(obj, stage_label=f"MergeSplit prepare: {obj.name}")


def _sanitize_merge_split_group_name(name):
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in name).strip("._") or "Object"


def _clear_merge_split_item_record(item):
    item.marker_group_name = ""
    item.face_start = 0
    item.face_count = 0
    item.vertex_count = 0


def _parse_merge_split_source_id(marker_group_name):
    marker = str(marker_group_name or "").strip()
    if not marker:
        return None
    source_id_text = marker.rsplit("_", 1)[-1]
    if not source_id_text.isdigit():
        return None
    return int(source_id_text)


def _collect_recorded_split_entries(items):
    split_items = []
    seen_source_ids = set()
    fallback_source_id = 0
    for item in items:
        object_name = str(getattr(item, "object_name", "") or "").strip()
        face_start = int(getattr(item, "face_start", 0) or 0)
        face_count = int(getattr(item, "face_count", 0) or 0)
        vertex_count = int(getattr(item, "vertex_count", 0) or 0)
        marker_group_name = str(getattr(item, "marker_group_name", "") or "").strip()
        if not object_name or vertex_count <= 0 or face_count <= 0:
            continue

        parsed_source_id = _parse_merge_split_source_id(marker_group_name)
        if parsed_source_id is not None:
            if parsed_source_id in seen_source_ids:
                continue
            source_id = parsed_source_id
            seen_source_ids.add(parsed_source_id)
        else:
            source_id = fallback_source_id

        split_items.append((source_id, object_name, face_start, face_count, marker_group_name))
        fallback_source_id += 1
    return split_items


def _snapshot_merge_split_item_records(items):
    return [
        {
            "item": item,
            "marker_group_name": str(getattr(item, "marker_group_name", "") or ""),
            "face_start": int(getattr(item, "face_start", 0) or 0),
            "face_count": int(getattr(item, "face_count", 0) or 0),
            "vertex_count": int(getattr(item, "vertex_count", 0) or 0),
        }
        for item in items
    ]


def _restore_merge_split_item_records(snapshot):
    for payload in snapshot or []:
        item = payload.get("item")
        if item is None:
            continue
        item.marker_group_name = payload.get("marker_group_name", "")
        item.face_start = payload.get("face_start", 0)
        item.face_count = payload.get("face_count", 0)
        item.vertex_count = payload.get("vertex_count", 0)


def _ensure_face_source_attribute(mesh):
    if mesh is None:
        return None
    attributes = getattr(mesh, "attributes", None)
    if attributes is None:
        return None
    attribute = attributes.get(MERGE_SPLIT_FACE_SOURCE_ATTR)
    if attribute is not None:
        if getattr(attribute, "domain", "") == "FACE" and getattr(attribute, "data_type", "") == "INT":
            return attribute
        try:
            attributes.remove(attribute)
        except Exception:
            return None
    try:
        return attributes.new(name=MERGE_SPLIT_FACE_SOURCE_ATTR, type="INT", domain="FACE")
    except Exception:
        return None


def _write_face_source_id(obj, source_id):
    if obj is None or getattr(obj, "type", "") != "MESH" or getattr(obj, "data", None) is None:
        return False
    attribute = _ensure_face_source_attribute(obj.data)
    if attribute is None:
        return False
    try:
        for item in getattr(attribute, "data", []) or []:
            item.value = int(source_id)
        obj.data.update()
        return True
    except Exception:
        return False


def _read_face_source_ids(mesh):
    attribute = getattr(getattr(mesh, "attributes", None), "get", lambda _name: None)(MERGE_SPLIT_FACE_SOURCE_ATTR)
    if attribute is None:
        return []
    return [int(getattr(item, "value", -1)) for item in getattr(attribute, "data", []) or []]


def _clear_internal_merge_split_groups(obj):
    if obj is None or getattr(obj, "type", "") != "MESH":
        return
    mesh = getattr(obj, "data", None)
    attributes = getattr(mesh, "attributes", None)
    if attributes is None:
        return
    attribute = attributes.get(MERGE_SPLIT_FACE_SOURCE_ATTR)
    if attribute is not None:
        try:
            attributes.remove(attribute)
        except Exception:
            pass


def _select_faces_by_source_id(obj, source_id):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    face_layer = bm.faces.layers.int.get(MERGE_SPLIT_FACE_SOURCE_ATTR)
    if face_layer is None:
        return 0
    selected_count = 0
    for face in bm.faces:
        is_selected = int(face[face_layer]) == int(source_id)
        face.select_set(is_selected)
        if is_selected:
            selected_count += 1
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return selected_count


def _select_face_range(obj, face_start, face_count):
    if face_count <= 0:
        return 0
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    end = face_start + face_count
    selected_count = 0
    for face in bm.faces:
        is_selected = face_start <= face.index < end
        face.select_set(is_selected)
        if is_selected:
            selected_count += 1
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return selected_count


def _find_separated_object(context, source_obj, previous_object_names):
    active_obj = getattr(context.view_layer.objects, "active", None)
    if active_obj is not None and active_obj != source_obj and getattr(active_obj, "type", "") == "MESH":
        return active_obj

    for obj in getattr(context, "selected_objects", []) or []:
        if obj is not None and obj != source_obj and getattr(obj, "type", "") == "MESH":
            return obj

    for obj_name in set(bpy.data.objects.keys()) - previous_object_names:
        candidate = bpy.data.objects.get(obj_name)
        if candidate is not None and candidate.type == 'MESH':
            return candidate
    return None


def _get_object_vertex_group_name_set(obj):
    VertexGroupUtils.remove_unused_vertex_groups(obj)
    return {vg.name for vg in obj.vertex_groups}


def _get_object_world_center(obj):
    if not obj.data or len(obj.data.vertices) == 0:
        return obj.matrix_world.translation.copy()
    total = obj.matrix_world @ obj.data.vertices[0].co
    for vertex in obj.data.vertices[1:]:
        total += obj.matrix_world @ vertex.co
    return total / len(obj.data.vertices)


def _get_object_world_bounds(obj):
    if not obj.bound_box:
        origin = obj.matrix_world.translation.copy()
        return origin, origin
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    min_corner = corners[0].copy()
    max_corner = corners[0].copy()
    for corner in corners[1:]:
        min_corner.x = min(min_corner.x, corner.x)
        min_corner.y = min(min_corner.y, corner.y)
        min_corner.z = min(min_corner.z, corner.z)
        max_corner.x = max(max_corner.x, corner.x)
        max_corner.y = max(max_corner.y, corner.y)
        max_corner.z = max(max_corner.z, corner.z)
    return min_corner, max_corner


def _bounds_gap(bounds_a, bounds_b):
    min_a, max_a = bounds_a
    min_b, max_b = bounds_b

    def axis_gap(a_min, a_max, b_min, b_max):
        if a_max < b_min:
            return b_min - a_max
        if b_max < a_min:
            return a_min - b_max
        return 0.0

    gap_x = axis_gap(min_a.x, max_a.x, min_b.x, max_b.x)
    gap_y = axis_gap(min_a.y, max_a.y, min_b.y, max_b.y)
    gap_z = axis_gap(min_a.z, max_a.z, min_b.z, max_b.z)
    return math.sqrt(gap_x * gap_x + gap_y * gap_y + gap_z * gap_z)


def _cluster_loose_parts_by_vg_similarity_and_distance(objects):
    """根据顶点组相似度和空间距离对松散块进行聚类"""
    if not objects:
        return []

    object_infos = []
    max_extent = 0.0
    for obj in objects:
        vg_set = _get_object_vertex_group_name_set(obj)
        center = _get_object_world_center(obj)
        bounds = _get_object_world_bounds(obj)
        min_corner, max_corner = bounds
        extent = (max_corner - min_corner).length
        max_extent = max(max_extent, extent)
        object_infos.append((obj, vg_set, center, bounds))

    adjacency_threshold = max(0.001, max_extent * 0.05)

    parent = list(range(len(object_infos)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left in range(len(object_infos)):
        _obj_left, vg_left, _center_left, bounds_left = object_infos[left]
        for right in range(left + 1, len(object_infos)):
            _obj_right, vg_right, _center_right, bounds_right = object_infos[right]
            if not vg_left or not vg_right:
                continue
            intersection = len(vg_left & vg_right)
            union_size = len(vg_left | vg_right)
            similarity = intersection / union_size if union_size else 0.0
            gap_distance = _bounds_gap(bounds_left, bounds_right)
            if similarity >= 0.5 and gap_distance <= adjacency_threshold:
                union(left, right)

    grouped = {}
    for index, (obj, _, _, _) in enumerate(object_infos):
        grouped.setdefault(find(index), []).append(obj)
    return list(grouped.values())


class ModelSplitByLoosePart(bpy.types.Operator):
    bl_idname = "toolkit.split_by_loose_part"
    bl_label = "根据UV松散块儿分割模型"
    bl_description = "功能与Edit界面的Split => Split by Loose Parts相似，但是分割模型为松散块儿并放入新集合。"

    def execute(self, context):
        
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        obj = bpy.context.selected_objects[0]
        collection_name = f"{obj.name}_LooseParts"
        ObjUtils.split_obj_by_loose_parts_to_collection(obj=obj,collection_name=collection_name)

        self.report({'INFO'}, "根据UV松散块儿分割模型成功!")
        return {'FINISHED'}


class ModelSplitByVertexGroup(bpy.types.Operator):
    bl_idname = "toolkit.split_by_vertex_group"
    bl_label = "根据共享与孤立顶点组分割模型"
    bl_description = "把模型根据共享的顶点组分开，方便快速分离身体上的小物件，方便后续刷权重不受小物件影响。"

    def execute(self, context):
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        obj = bpy.context.selected_objects[0]
        collection_name = f"{obj.name}_Splits"
        ObjUtils.split_obj_by_loose_parts_to_collection(obj=obj,collection_name=collection_name)
        
        collection = CollectionUtils.get_collection_by_name(collection_name=collection_name)
        CollectionUtils.select_collection_objects(collection)
        selected_objects = bpy.context.selected_objects

        number_vgnameset_dict = {}
        number_objlist_dict = {}

        for obj in selected_objects:
            VertexGroupUtils.remove_unused_vertex_groups(obj)
            vertex_group_names = [vg.name for vg in obj.vertex_groups]
            vgname_set = set()
            for vgname in vertex_group_names:
                    vgname_set.add(vgname)

            if len(number_vgnameset_dict) == 0:
                number_vgnameset_dict[1] = vgname_set
                number_objlist_dict[1] = [obj]
            else:
                exists = False
                for number, tmp_vgname_set in number_vgnameset_dict.items():
                    vgname_jiaoji = tmp_vgname_set & vgname_set
                    if len(vgname_jiaoji) != 0:
                        vgname_quanji = tmp_vgname_set.union(vgname_set)
                        number_vgnameset_dict[number] = vgname_quanji
                        exists = True
                        break
                
                if not exists:
                    number_objlist_dict[len(number_objlist_dict) + 1] = [obj]
                    number_vgnameset_dict[len(number_vgnameset_dict) + 1] = vgname_set
                else:
                    number_objlist_dict[number].append(obj)

        for number, objlist in number_objlist_dict.items():
            ObjUtils.merge_objects(obj_list=objlist,target_collection=collection)
        self.report({'INFO'}, "根据顶点组分割模型成功!")
        return {'FINISHED'}
    

class ModelSplitEachVertexGroup(bpy.types.Operator):
    bl_idname = "toolkit.split_each_vertex_group"
    bl_label = "按每个顶点组分割"
    bl_description = "为每个顶点组单独分离一个网格对象"

    def execute(self, context):
        obj = _get_single_selected_mesh()
        if obj is None:
            self.report({'ERROR'}, "请选择一个网格对象")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='OBJECT')
        VertexGroupUtils.split_mesh_by_vertex_group(obj)
        for split_obj in bpy.context.selected_objects:
            if split_obj.type == 'MESH':
                VertexGroupUtils.remove_unused_vertex_groups(split_obj)

        self.report({'INFO'}, self.bl_label + " 成功")
        return {'FINISHED'}


class ModelSplitLoosePartClusterByVertexGroup(bpy.types.Operator):
    bl_idname = "toolkit.split_loose_part_cluster_by_vertex_group"
    bl_label = "松散块按VG聚类"
    bl_description = "先按松散块分离，再按顶点组相似度和空间邻近关系聚类合并"

    def execute(self, context):
        obj = _get_single_selected_mesh()
        if obj is None:
            self.report({'ERROR'}, "请选择一个网格对象")
            return {'CANCELLED'}

        collection_name = f"{obj.name}_LoosePartClusters"
        ObjUtils.split_obj_by_loose_parts_to_collection(obj=obj, collection_name=collection_name)
        collection = CollectionUtils.get_collection_by_name(collection_name=collection_name)
        if collection is None:
            self.report({'ERROR'}, "未能创建拆分结果集合")
            return {'CANCELLED'}

        grouped_objects = _cluster_loose_parts_by_vg_similarity_and_distance(_get_collection_mesh_objects(collection))
        for object_group in grouped_objects:
            if len(object_group) <= 1:
                continue
            ObjUtils.merge_objects(obj_list=object_group, target_collection=collection)

        for merged_obj in _get_collection_mesh_objects(collection):
            VertexGroupUtils.remove_unused_vertex_groups(merged_obj)

        self.report({'INFO'}, self.bl_label + " 成功")
        return {'FINISHED'}


class ModelDeleteLoosePoint(bpy.types.Operator):
    bl_idname = "toolkit.delete_loose_point"
    bl_label = "删除模型中的松散点"
    bl_description = "删除模型中的松散点，避免影响后续的模型处理。"

    def execute(self, context):
        
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        
        ObjUtils.selected_obj_delete_loose()

        self.report({'INFO'}, "删除松散点成功!")
        return {'FINISHED'}


class BMTP_AddSelectedObjectsToMergeSplitList(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_merge_split_add_selected"
    bl_label = "添加选中物体"
    bl_description = "将当前选中的多个网格物体添加到合并/拆分列表"

    def execute(self, context):
        props = context.scene.bmtp_props
        existing_names = {item.object_name for item in props.merge_split_items}
        added_count = 0
        for obj in _iter_selected_mesh_objects(context):
            if obj.name in existing_names:
                continue
            item = props.merge_split_items.add()
            item.object_name = obj.name
            item.marker_group_name = ""
            item.face_start = 0
            item.face_count = 0
            item.vertex_count = 0
            existing_names.add(obj.name)
            added_count += 1

        self.report({'INFO'}, f"已添加 {added_count} 个物体")
        return {'FINISHED'}


class BMTP_RemoveMergeSplitListItem(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_merge_split_remove_item"
    bl_label = "移除选中项"
    bl_description = "从合并/拆分列表中移除当前选中项"

    def execute(self, context):
        props = context.scene.bmtp_props
        index = int(getattr(props, "merge_split_index", 0))
        if 0 <= index < len(props.merge_split_items):
            props.merge_split_items.remove(index)
            props.merge_split_index = max(0, min(index, len(props.merge_split_items) - 1))
        return {'FINISHED'}


class BMTP_MergeObjectsByRecordedRanges(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_merge_objects_by_recorded_ranges"
    bl_label = "合并列表物体"
    bl_description = "把列表中的多个网格物体合并成一个原地物体，并记录面级来源标记"

    def execute(self, context):
        props = context.scene.bmtp_props
        previous_records = _snapshot_merge_split_item_records(props.merge_split_items)

        targets = []
        seen = set()
        for item in props.merge_split_items:
            obj_name = str(getattr(item, "object_name", "") or "").strip()
            if not obj_name or obj_name in seen:
                continue
            obj = bpy.data.objects.get(obj_name)
            if obj is None or obj.type != 'MESH':
                continue
            targets.append((item, obj_name, obj))
            seen.add(obj_name)

        if len(targets) < 2:
            self.report({'ERROR'}, "列表中至少需要 2 个有效网格物体")
            return {'CANCELLED'}

        for item in props.merge_split_items:
            _clear_merge_split_item_record(item)

        _ensure_object_mode()
        current_face_offset = 0
        for index, (item, obj_name, obj) in enumerate(targets):
            _prepare_shape_keys_for_merge(obj)
            _clear_internal_merge_split_groups(obj)
            source_id = index
            face_count = len(getattr(obj.data, "polygons", []))
            vertex_indices = [vertex.index for vertex in getattr(obj.data, "vertices", [])]
            marker_group_name = f"{MERGE_SPLIT_FACE_SOURCE_ATTR}_{_sanitize_merge_split_group_name(obj_name)}_{source_id}"

            item.face_start = current_face_offset
            item.face_count = face_count
            item.vertex_count = len(vertex_indices)
            item.marker_group_name = marker_group_name
            current_face_offset += face_count

            if not _write_face_source_id(obj, source_id):
                _restore_merge_split_item_records(previous_records)
                self.report({'ERROR'}, f"物体 {obj_name} 无法写入面来源标记，当前 Blender 版本可能不支持该属性")
                return {'CANCELLED'}

        _base_item, _base_obj_name, base_obj = targets[0]
        _select_only_objects(context, [obj for _item, _obj_name, obj in targets], active_obj=base_obj)
        bpy.ops.object.join()

        merged_obj = context.view_layer.objects.active or base_obj
        merged_obj.name = str(getattr(props, "merge_split_target_name", "") or "").strip() or merged_obj.name
        if getattr(merged_obj.data, "name", ""):
            merged_obj.data.name = merged_obj.name
        props.merge_split_target_name = merged_obj.name

        if getattr(props, "merge_split_weld_vertices", False):
            _select_only_objects(context, [merged_obj], active_obj=merged_obj)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold=0.00001)
            bpy.ops.object.mode_set(mode='OBJECT')

        self.report({'INFO'}, f"已合并为 {merged_obj.name}")
        return {'FINISHED'}


class BMTP_SplitMergedObjectByRecordedRanges(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_split_merged_object_by_recorded_ranges"
    bl_label = "按记录拆分"
    bl_description = "按列表中记录的面级来源标记，把当前合并后的物体拆分回多个原始物体"

    def execute(self, context):
        props = context.scene.bmtp_props
        merged_name = str(getattr(props, "merge_split_target_name", "") or "").strip()
        merged_obj = bpy.data.objects.get(merged_name)
        if merged_obj is None or merged_obj.type != 'MESH':
            self.report({'ERROR'}, "未找到要拆分的合并物体")
            return {'CANCELLED'}

        split_items = _collect_recorded_split_entries(props.merge_split_items)

        if not split_items:
            self.report({'ERROR'}, "列表中没有可用的来源记录")
            return {'CANCELLED'}

        _ensure_object_mode()
        _select_only_objects(context, [merged_obj], active_obj=merged_obj)
        face_source_ids = _read_face_source_ids(merged_obj.data)
        has_face_source_attribute = bool(face_source_ids)

        split_entries = list(split_items)
        if has_face_source_attribute:
            split_entries.sort(key=lambda item: item[0])
        else:
            split_entries.sort(key=lambda item: item[2])

        renamed_count = 0
        zero_face_marker_names = []
        failed_capture_marker_names = []
        remaining_obj = merged_obj
        first_entry = split_entries[0]
        tail_entries = list(reversed(split_entries[1:]))

        for source_id, object_name, face_start, face_count, _marker_group_name in tail_entries:
            _select_only_objects(context, [remaining_obj], active_obj=remaining_obj)
            before_object_names = {obj.name for obj in bpy.data.objects}
            bpy.ops.object.mode_set(mode='EDIT')
            context.tool_settings.mesh_select_mode = (False, False, True)
            bpy.ops.mesh.select_all(action='DESELECT')
            if has_face_source_attribute:
                selected_face_count = _select_faces_by_source_id(remaining_obj, source_id)
            else:
                selected_face_count = _select_face_range(remaining_obj, face_start, face_count)
            if selected_face_count <= 0:
                bpy.ops.object.mode_set(mode='OBJECT')
                zero_face_marker_names.append(object_name)
                continue
            bpy.ops.mesh.separate(type='SELECTED')
            bpy.ops.object.mode_set(mode='OBJECT')

            target_obj = _find_separated_object(context, remaining_obj, before_object_names)
            if target_obj is None:
                failed_capture_marker_names.append(object_name)
                continue

            target_obj.name = object_name
            if getattr(target_obj.data, "name", ""):
                target_obj.data.name = object_name
            _clear_internal_merge_split_groups(target_obj)
            VertexGroupUtils.remove_unused_vertex_groups(target_obj)
            renamed_count += 1

        remaining_face_count = len(getattr(getattr(remaining_obj, "data", None), "polygons", []) or [])
        _first_source_id, first_object_name, _first_face_start, first_face_count, _first_marker_group_name = first_entry
        if remaining_face_count > 0:
            remaining_obj.name = first_object_name
            if getattr(remaining_obj.data, "name", ""):
                remaining_obj.data.name = first_object_name
            _clear_internal_merge_split_groups(remaining_obj)
            VertexGroupUtils.remove_unused_vertex_groups(remaining_obj)
            renamed_count += 1

        expected_tail_count = len(tail_entries)
        if renamed_count <= 0:
            if zero_face_marker_names and len(zero_face_marker_names) == expected_tail_count:
                self.report({'ERROR'}, "拆分失败：未能从面范围记录中选中任何面，原合并物体保持不变。")
            elif failed_capture_marker_names:
                self.report({'ERROR'}, "拆分失败：已执行分离，但未能识别分离结果物体，原合并物体保持不变。")
            else:
                self.report({'ERROR'}, "拆分失败：没有恢复出任何物体，原合并物体保持不变。")
            return {'CANCELLED'}

        if remaining_face_count <= 0 and remaining_obj is not None and remaining_obj.name in bpy.data.objects:
            bpy.data.objects.remove(remaining_obj, do_unlink=True)
        elif remaining_face_count > 0 and remaining_face_count != first_face_count:
            self.report({'WARNING'}, f"首个物体 {remaining_obj.name} 剩余 {remaining_face_count} 个面，预期 {first_face_count} 个面。")

        props.merge_split_target_name = ""
        self.report({'INFO'}, f"已拆分并恢复 {renamed_count} 个物体")
        return {'FINISHED'}
    
class ModelClearCustomSplitNormals(bpy.types.Operator):
    bl_idname = "toolkit.clear_custom_split_normals"
    bl_label = "清除自定义拆分法向"
    bl_description = "WuWa 逆向得到的模型，有时顶点法线会歪，用这个处理一下就行。"
    def execute(self, context):
        sel = context.selected_objects
        if not sel:
            self.report({'ERROR'}, "未选中对象！")
            return {'CANCELLED'}
        for obj in sel:
            if obj.type == 'MESH':
                context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.mesh.customdata_custom_splitnormals_clear()
        return {'FINISHED'}
    
class ModelRenameVertexGroupNameWithTheirSuffix(bpy.types.Operator):
    bl_idname = "toolkit.rename_vertex_group_name_with_their_suffix"
    bl_label = "用模型名称作为前缀重命名顶点组"
    bl_description = "用模型名称作为前缀重命名顶点组，方便后续合并到一个物体后同名称的顶点组不会合在一起冲突，便于后续一键绑定骨骼。"

    def execute(self, context):
        
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                model_name = obj.name
                for vertex_group in obj.vertex_groups:
                    original_name = vertex_group.name
                    new_name = f"{model_name}_{original_name}"
                    vertex_group.name = new_name

        self.report({'INFO'}, "用模型名称作为前缀重命名顶点组成功!")
        return {'FINISHED'}


class ModelRenameNumericVertexGroupsToRandomEnglish(bpy.types.Operator):
    bl_idname = "toolkit.rename_numeric_vertex_groups_to_random_english"
    bl_label = "数字顶点组改随机英文"
    bl_description = "将纯数字顶点组使用两段重命名方式改成随机英文，支持多选多个网格物体"

    def execute(self, context):
        selected_mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_mesh_objects:
            self.report({'ERROR'}, "没有选中的网格对象！")
            return {'CANCELLED'}

        processed_objects, renamed_count = VertexGroupUtils.rename_numeric_vertex_groups_to_random_english(
            selected_mesh_objects
        )
        self.report(
            {'INFO'},
            f"操作完成！处理了 {processed_objects} 个物体，重命名了 {renamed_count} 个数字顶点组。"
        )
        return {'FINISHED'}


class AddBoneFromVertexGroupV2(bpy.types.Operator):
    bl_idname = "toolkit.add_bone_from_vertex_group_v2"
    bl_label = "根据顶点组生成基础骨骼"
    bl_description = "把当前选中的obj的每个顶点组都生成一个默认位置的骨骼，方便接下来手动调整骨骼位置和父级关系来绑骨，虹汐哥改进版本"
    def execute(self, context):
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        VertexGroupUtils.create_armature_from_vertex_groups()
        self.report({'INFO'}, self.bl_label + " 成功!")
        return {'FINISHED'}


class SplitMeshByCommonVertexGroup(bpy.types.Operator):
    bl_idname = "toolkit.split_mesh_by_common_vertex_group"
    bl_label = "根据顶点组将模型打碎为松散块儿"
    bl_description = "把当前选中的obj按顶点组进行分割，适用于部分精细刷权重并重新组合模型的场景"
    
    def execute(self, context):
        for obj in bpy.context.selected_objects:
            VertexGroupUtils.split_mesh_by_vertex_group(obj)
        self.report({'INFO'}, self.bl_label + " 成功!")
        return {'FINISHED'}
    

class SmoothNormalSaveToUV(bpy.types.Operator):
    bl_idname = "toolkit.smooth_normal_save_to_uv"
    bl_label = "平滑法线存UV(近似)"
    bl_description = "平滑法线存UV算法，可用于修复ZZZ,WuWa的某些UV(只是近似实现60%的效果)" 

    def execute(self, context):
        AlgorithmUtils.smooth_normal_save_to_uv()
        return {'FINISHED'}
    


        
class PropertyCollectionModifierItem(bpy.types.PropertyGroup):
    checked: BoolProperty(
        name="", 
        default=False
    ) # type: ignore

class WWMI_ApplyModifierForObjectWithShapeKeysOperator(bpy.types.Operator):
    bl_idname = "toolkit.apply_modifier_for_object_with_shape_keys"
    bl_label = "在有形态键的模型上应用修改器"
    bl_description = "Apply selected modifiers and remove from the stack for object with shape keys (Solves 'Modifier cannot be applied to a mesh with shape keys' error when pushing 'Apply' button in 'Object modifiers'). Sourced by Przemysław Bągard"
 
    def item_list(self, context):
        return [(modifier.name, modifier.name, modifier.name) for modifier in bpy.context.object.modifiers]
    
    my_collection: CollectionProperty(
        type=PropertyCollectionModifierItem
    ) # type: ignore
    
    disable_armatures: BoolProperty(
        name="Don't include armature deformations",
        default=True,
    ) # type: ignore
 
    def execute(self, context):
        ob = bpy.context.object
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = ob
        ob.select_set(True)
        
        selectedModifiers = [o.name for o in self.my_collection if o.checked]
        
        if not selectedModifiers:
            self.report({'ERROR'}, 'No modifier selected!')
            return {'FINISHED'}
        
        success, errorInfo = ShapeKeyUtils.apply_modifiers_for_object_with_shape_keys(context, selectedModifiers, self.disable_armatures)
        
        if not success:
            self.report({'ERROR'}, errorInfo)
        
        return {'FINISHED'}
        
    def draw(self, context):
        if context.object.data.shape_keys and context.object.data.shape_keys.animation_data:
            self.layout.separator()
            self.layout.label(text="Warning:")
            self.layout.label(text="              Object contains animation data")
            self.layout.label(text="              (like drivers, keyframes etc.)")
            self.layout.label(text="              assigned to shape keys.")
            self.layout.label(text="              Those data will be lost!")
            self.layout.separator()
        box = self.layout.box()
        for prop in self.my_collection:
            box.prop(prop, "checked", text=prop["name"])
        self.layout.prop(self, "disable_armatures")
 
    def invoke(self, context, event):
        self.my_collection.clear()
        for i in range(len(bpy.context.object.modifiers)):
            item = self.my_collection.add()
            item.name = bpy.context.object.modifiers[i].name
            item.checked = False
        return context.window_manager.invoke_props_dialog(self)
    

class RecalculateTANGENTWithVectorNormalizedNormal(bpy.types.Operator):
    bl_idname = "toolkit.recalculate_tangent_arithmetic_average_normal"
    bl_label = "使用向量相加归一化算法重计算TANGENT"
    bl_description = "近似修复轮廓线算法，可以达到99%的轮廓线相似度，适用于GI,HSR,ZZZ,HI3 2.0之前的老角色" 
    def execute(self, context):
        for obj in bpy.context.selected_objects:
            if obj.type == "MESH":
                if obj.get("3DMigoto:RecalculateTANGENT",False):
                    obj["3DMigoto:RecalculateTANGENT"] = not obj["3DMigoto:RecalculateTANGENT"]
                else:
                    obj["3DMigoto:RecalculateTANGENT"] = True
                self.report({'INFO'},"重计算TANGENT设为:" + str(obj["3DMigoto:RecalculateTANGENT"]))
        return {'FINISHED'}


class RecalculateCOLORWithVectorNormalizedNormal(bpy.types.Operator):
    bl_idname = "toolkit.recalculate_color_arithmetic_average_normal"
    bl_label = "使用算术平均归一化算法重计算COLOR"
    bl_description = "近似修复轮廓线算法，可以达到99%的轮廓线相似度，仅适用于HI3 2.0新角色" 

    def execute(self, context):
        for obj in bpy.context.selected_objects:
            if obj.type == "MESH":
                if obj.get("3DMigoto:RecalculateCOLOR",False):
                    obj["3DMigoto:RecalculateCOLOR"] = not obj["3DMigoto:RecalculateCOLOR"]
                else:
                    obj["3DMigoto:RecalculateCOLOR"] = True
                self.report({'INFO'},"重计算COLOR设为:" + str(obj["3DMigoto:RecalculateCOLOR"]))
        return {'FINISHED'}
    


class RenameAmatureFromGame(bpy.types.Operator):
    bl_idname = "toolkit.rename_amature_from_game"
    bl_label = "重命名选中Amature的骨骼名称(GI)(测试)"
    bl_description = "用于把游戏里解包出来的骨骼重命名，方便我们直接一键绑定到提取出的Mod模型上，Credit to Leotorrez"
    def execute(self, context):
        armature_name = bpy.context.active_object.name

        object_name_original = 'Body'
        if not bpy.context.active_object:
            raise RuntimeError("The selected object is not an armature.")
        if bpy.context.active_object.type != "ARMATURE" or armature_name not in bpy.data.objects:
            raise RuntimeError("Error: No object selected.")

        bpy.ops.object.scale_clear()
        bpy.context.view_layer.objects.active = bpy.data.objects[armature_name]
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.transform.mirror(constraint_axis=(True, False, False))
        bpy.ops.object.transform_apply(scale=True, rotation=False)

        vertex_groups = [vg.name for vg in bpy.data.objects[object_name_original].vertex_groups]
        pairs = {old:new for old,new in zip(vertex_groups, sorted(vertex_groups))}
        name_mapping = {new: str(i) for i, (_, new) in enumerate(pairs.items())}
        for vertex_group in bpy.data.objects[object_name_original].vertex_groups:
            armature_obj = bpy.data.objects[armature_name].data
            armature_obj.bones[vertex_group.name].name = vertex_group.name = name_mapping[vertex_group.name]

        new_armature_name = f"{armature_name}_sorted"
        bpy.data.objects[armature_name].name = new_armature_name
        bpy.context.view_layer.objects.active = bpy.data.objects[new_armature_name]
        obj = bpy.data.objects.get(new_armature_name)
        obj.parent = None
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        obj.rotation_euler[0] = -1.5708
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        obj.rotation_euler[0] = 1.5708

        for obj in bpy.data.objects:
            if obj.name != new_armature_name:
                for child in obj.children:
                    bpy.data.objects.remove(child)
                bpy.data.objects.remove(obj)
        return {'FINISHED'}


model_operators_list = [
    ModelSplitByLoosePart,
    ModelSplitByVertexGroup,
    ModelSplitEachVertexGroup,
    ModelSplitLoosePartClusterByVertexGroup,
    ModelDeleteLoosePoint,
    BMTP_AddSelectedObjectsToMergeSplitList,
    BMTP_RemoveMergeSplitListItem,
    BMTP_MergeObjectsByRecordedRanges,
    BMTP_SplitMergedObjectByRecordedRanges,
    ModelClearCustomSplitNormals,
    ModelRenameVertexGroupNameWithTheirSuffix,
    ModelRenameNumericVertexGroupsToRandomEnglish,
    AddBoneFromVertexGroupV2,
    SplitMeshByCommonVertexGroup,
    SmoothNormalSaveToUV,
    PropertyCollectionModifierItem,
    WWMI_ApplyModifierForObjectWithShapeKeysOperator,
    RecalculateTANGENTWithVectorNormalizedNormal,
    RecalculateCOLORWithVectorNormalizedNormal,
    RenameAmatureFromGame,
]
