# -*- coding: utf-8 -*-

import bpy
import numpy as np

from ..utils.shapekey_rebase_utils import rebase_shape_key_coordinates
from .at_shape_key_control import refresh_shape_key_list


def _snapshot_shape_key_coordinates(key_blocks):
    coordinates = {}
    for key_block in key_blocks:
        key_coords = np.empty(len(key_block.data) * 3, dtype=np.float32)
        key_block.data.foreach_get("co", key_coords)
        coordinates[key_block.name] = key_coords.reshape(-1, 3)
    return coordinates


def _restore_shape_key_coordinates(key_blocks, coordinates_by_name):
    for key_block in key_blocks:
        key_coords = coordinates_by_name.get(key_block.name)
        if key_coords is None:
            continue
        key_block.data.foreach_set("co", np.asarray(key_coords, dtype=np.float32).reshape(-1))


def _refresh_shape_key_list_from_context(context):
    refresh_targets = list(context.selected_objects) if context.selected_objects else []
    if context.active_object and context.active_object not in refresh_targets:
        refresh_targets.append(context.active_object)
    refresh_shape_key_list(context.scene.atp_props, refresh_targets)


def _allocate_shape_key_temp_name(existing_names, base_name):
    candidate = f"__tmp__{base_name or 'ShapeKey'}"
    if candidate not in existing_names:
        existing_names.add(candidate)
        return candidate

    index = 1
    while True:
        numbered = f"{candidate}_{index}"
        if numbered not in existing_names:
            existing_names.add(numbered)
            return numbered
        index += 1


def _build_shape_key_rename_plan(key_blocks, old_text, new_text, exact_match=False):
    rename_candidates = []
    existing_names = {
        str(getattr(key_block, "name", "") or "")
        for key_block in key_blocks
    }

    for key_block in key_blocks:
        current_name = str(getattr(key_block, "name", "") or "")
        if exact_match:
            if current_name != old_text:
                continue
            replaced_name = new_text
        else:
            if old_text not in current_name:
                continue
            replaced_name = current_name.replace(old_text, new_text)

        rename_candidates.append((key_block, current_name, replaced_name))

    if not rename_candidates:
        return []

    rename_plan = []
    final_targets = {}
    current_candidate_names = {
        current_name
        for _key_block, current_name, _replaced_name in rename_candidates
    }

    for key_block, current_name, replaced_name in rename_candidates:
        occupied_by_unrenamed = replaced_name in (existing_names - current_candidate_names)
        occupied_by_other_target = replaced_name in final_targets.values()
        has_conflict = occupied_by_unrenamed or occupied_by_other_target
        temp_name = ""
        if not has_conflict:
            temp_name = _allocate_shape_key_temp_name(existing_names, current_name)
            final_targets[current_name] = replaced_name
        rename_plan.append((key_block, current_name, temp_name, replaced_name, has_conflict))

    return rename_plan


class ATP_OT_BatchAddShapeKey(bpy.types.Operator):
    """为所有选中的物体批量添加一个新的形态键，并将其设为活动项"""
    bl_idname = "atp.batch_add_shape_key"
    bl_label = "批量添加形态键"
    bl_description = "为所有选中的网格物体添加一个指定名称的新形态键，并设为活动项"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects and context.scene.atp_props.sk_add_new_name

    def execute(self, context):
        props = context.scene.atp_props
        shape_key_name = props.sk_add_new_name
        selected_objects = context.selected_objects
        
        if not shape_key_name:
            self.report({'ERROR'}, "新形态键名称不能为空。")
            return {'CANCELLED'}

        processed_count = 0
        skipped_count = 0
        
        for obj in selected_objects:
            if obj.type == 'MESH':
                if not obj.data.shape_keys:
                    obj.shape_key_add(name="Basis")

                if shape_key_name in obj.data.shape_keys.key_blocks:
                    skipped_count += 1
                    continue
                
                try:
                    new_key = obj.shape_key_add(name=shape_key_name)
                    obj.active_shape_key_index = len(obj.data.shape_keys.key_blocks) - 1
                    processed_count += 1
                except Exception as e:
                    self.report({'WARNING'}, f"为物体 '{obj.name}' 添加形态键时出错: {e}")
        
        report_message = f"成功为 {processed_count} 个物体添加了形态键 '{shape_key_name}'。"
        if skipped_count > 0:
            report_message += f" 跳过了 {skipped_count} 个已存在同名键的物体。"
        
        self.report({'INFO'}, report_message)
        _refresh_shape_key_list_from_context(context)

        return {'FINISHED'}


class ATP_OT_BatchRemoveShapeKey(bpy.types.Operator):
    """为所有选中的物体批量删除一个指定名称的形态键"""
    bl_idname = "atp.batch_remove_shape_key"
    bl_label = "批量删除形态键"
    bl_description = "从所有选中的网格物体中，删除指定名称的形态键；仅当物体没有其他形态键时，才允许删除 Basis"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects and context.scene.atp_props.sk_add_new_name

    def execute(self, context):
        props = context.scene.atp_props
        shape_key_name_to_remove = props.sk_add_new_name
        selected_objects = context.selected_objects
        
        if not shape_key_name_to_remove:
            self.report({'ERROR'}, "要删除的形态键名称不能为空。")
            return {'CANCELLED'}
        
        removed_count = 0
        skipped_with_other_keys = 0

        for obj in selected_objects:
            if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
                key_blocks = obj.data.shape_keys.key_blocks
                key_block_to_remove = key_blocks.get(shape_key_name_to_remove)
                if key_block_to_remove is None and shape_key_name_to_remove.lower() == 'basis':
                    reference_key = getattr(obj.data.shape_keys, "reference_key", None)
                    key_block_to_remove = reference_key if reference_key is not None else (key_blocks[0] if key_blocks else None)

                if key_block_to_remove:
                    is_basis_key = key_block_to_remove == getattr(obj.data.shape_keys, "reference_key", None)
                    if is_basis_key and len(key_blocks) > 1:
                        skipped_with_other_keys += 1
                        self.report({'WARNING'}, f"物体 '{obj.name}' 还有其他形态键，不能单独删除 Basis（请先删除其他形态键）。")
                        continue
                    try:
                        obj.shape_key_remove(key_block_to_remove)
                        removed_count += 1
                    except Exception as e:
                        self.report({'WARNING'}, f"为物体 '{obj.name}' 删除形态键时出错: {e}")

        if removed_count > 0:
            self.report({'INFO'}, f"成功移除了 {removed_count} 个名为 '{shape_key_name_to_remove}' 的形态键。")
        else:
            self.report({'INFO'}, f"在选中的物体中未找到名为 '{shape_key_name_to_remove}' 的形态键。")

        if skipped_with_other_keys > 0:
            self.report({'WARNING'}, f"跳过了 {skipped_with_other_keys} 个仍包含其他形态键的物体，Basis 仅可在没有其他形态键时单独删除。")

        _refresh_shape_key_list_from_context(context)

        return {'FINISHED'}


class ATP_OT_ResetAllShapeKeys(bpy.types.Operator):
    """将所有选中物体的所有形态键值归零"""
    bl_idname = "atp.reset_all_shape_keys"
    bl_label = "归零所有形态键"
    bl_description = "将所有选中物体的所有形态键值设置为0"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects

    def execute(self, context):
        selected_objects = context.selected_objects
        reset_count = 0
        skipped_count = 0
        
        for obj in selected_objects:
            if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
                for key_block in obj.data.shape_keys.key_blocks:
                    if key_block.name.lower() != 'basis':
                        key_block.value = 0.0
                        reset_count += 1
                obj.data.update_tag()
            else:
                skipped_count += 1
        
        if reset_count > 0:
            self.report({'INFO'}, f"成功归零了 {reset_count} 个形态键。")
            _refresh_shape_key_list_from_context(context)
        else:
            self.report({'INFO'}, "在选中的物体中未找到任何形态键。")
            
        if skipped_count > 0:
            self.report({'WARNING'}, f"跳过了 {skipped_count} 个没有形态键的物体。")
            
        return {'FINISHED'}


class ATP_OT_SetActiveShapeKey(bpy.types.Operator):
    """将指定名称的形态键设置为所有选中物体的活动形态键"""
    bl_idname = "atp.set_active_shape_key"
    bl_label = "设置活动形态键"
    bl_description = "将指定名称的形态键设置为所有选中物体的活动形态键"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.atp_props
        return context.selected_objects and props.sk_set_active_name

    def execute(self, context):
        props = context.scene.atp_props
        shape_key_name = props.sk_set_active_name
        selected_objects = context.selected_objects
        
        if not shape_key_name:
            self.report({'ERROR'}, "形态键名称不能为空。")
            return {'CANCELLED'}

        success_count = 0
        not_found_count = 0
        
        for obj in selected_objects:
            if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
                key_block = obj.data.shape_keys.key_blocks.get(shape_key_name)
                
                if key_block:
                    obj.active_shape_key_index = list(obj.data.shape_keys.key_blocks).index(key_block)
                    success_count += 1
                else:
                    not_found_count += 1
            else:
                not_found_count += 1
        
        if success_count > 0:
            self.report({'INFO'}, f"成功为 {success_count} 个物体设置了活动形态键 '{shape_key_name}'。")
        else:
            self.report({'WARNING'}, f"未在任何选中物体中找到名为 '{shape_key_name}' 的形态键。")
            
        if not_found_count > 0:
            self.report({'INFO'}, f"在 {not_found_count} 个物体中未找到指定形态键。")
            
        return {'FINISHED'}


class ATP_OT_BatchRenameShapeKey(bpy.types.Operator):
    """批量替换选中物体中的形态键名称片段"""
    bl_idname = "atp.batch_rename_shape_key"
    bl_label = "批量重命名形态键"
    bl_description = "对选中物体中的形态键名称执行文本替换式批量重命名"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.atp_props
        return context.selected_objects and props.sk_rename_old_name and props.sk_rename_new_name

    def execute(self, context):
        props = context.scene.atp_props
        old_name = props.sk_rename_old_name
        new_name = props.sk_rename_new_name
        exact_match = bool(getattr(props, "sk_rename_exact_match", False))
        selected_objects = context.selected_objects
        
        if not old_name or not new_name:
            self.report({'ERROR'}, "原名称和新名称不能为空。")
            return {'CANCELLED'}
            
        if old_name == new_name:
            self.report({'WARNING'}, "原名称和新名称相同，无需重命名。")
            return {'CANCELLED'}

        renamed_key_count = 0
        affected_object_count = 0
        no_match_object_count = 0
        conflict_count = 0
        
        for obj in selected_objects:
            if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
                rename_plan = _build_shape_key_rename_plan(
                    obj.data.shape_keys.key_blocks,
                    old_name,
                    new_name,
                    exact_match=exact_match,
                )
                if not rename_plan:
                    no_match_object_count += 1
                    continue

                renamed_in_object = 0
                for key_block, _current_name, temp_name, _replaced_name, has_conflict in rename_plan:
                    if has_conflict:
                        conflict_count += 1
                        continue
                    key_block.name = temp_name

                for key_block, _current_name, _temp_name, replaced_name, has_conflict in rename_plan:
                    if has_conflict:
                        continue
                    key_block.name = replaced_name
                    renamed_key_count += 1
                    renamed_in_object += 1

                if renamed_in_object > 0:
                    affected_object_count += 1
            else:
                no_match_object_count += 1
        
        if renamed_key_count > 0:
            self.report(
                {'INFO'},
                f"成功在 {affected_object_count} 个物体中替换了 {renamed_key_count} 个形态键名称片段：'{old_name}' -> '{new_name}'。"
            )
            _refresh_shape_key_list_from_context(context)
        else:
            self.report({'WARNING'}, f"未在任何选中物体中找到包含 '{old_name}' 的形态键名称片段。")
            
        if no_match_object_count > 0:
            self.report({'INFO'}, f"在 {no_match_object_count} 个物体中未找到包含指定名称片段的形态键。")
            
        if conflict_count > 0:
            self.report(
                {'WARNING'},
                f"有 {conflict_count} 个形态键在替换后会与现有名称冲突，已跳过。"
            )
            
        return {'FINISHED'}


class ATP_OT_SetActiveShapeKeyAsBasis(bpy.types.Operator):
    """将活动形态键重设为新的 Basis，并按原始偏移重算其他形态键。"""
    bl_idname = "atp.set_active_shape_key_as_basis"
    bl_label = "将活动形态键设为基态"
    bl_description = "把当前活动形态键烘焙为新的 Basis，并让其他形态键在新 Basis 上保持原始偏移"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active_obj = context.active_object
        if context.mode != 'OBJECT':
            return False
        if active_obj is None or active_obj.type != 'MESH':
            return False

        shape_keys = getattr(active_obj.data, "shape_keys", None)
        if shape_keys is None or len(shape_keys.key_blocks) <= 1:
            return False
        if not shape_keys.use_relative:
            return False

        active_shape_key = getattr(active_obj, "active_shape_key", None)
        return active_shape_key is not None and active_shape_key != shape_keys.reference_key

    def execute(self, context):
        props = context.scene.atp_props
        obj = context.active_object
        shape_keys = obj.data.shape_keys
        basis_key = shape_keys.reference_key
        source_key = obj.active_shape_key

        if basis_key is None or source_key is None:
            self.report({'ERROR'}, "当前物体缺少可处理的形态键。")
            return {'CANCELLED'}

        if source_key == basis_key:
            self.report({'ERROR'}, "当前活动形态键已经是 Basis。")
            return {'CANCELLED'}

        source_key_name = source_key.name
        remove_source = props.sk_rebase_remove_source
        coordinate_snapshot = _snapshot_shape_key_coordinates(shape_keys.key_blocks)

        try:
            rebased_coordinates = rebase_shape_key_coordinates(
                coordinates_by_name=coordinate_snapshot,
                basis_name=basis_key.name,
                new_basis_name=source_key_name,
                remove_new_basis_key=remove_source,
            )
        except (KeyError, ValueError) as exc:
            self.report({'ERROR'}, f"重设基态失败: {exc}")
            return {'CANCELLED'}

        _restore_shape_key_coordinates(shape_keys.key_blocks, rebased_coordinates)
        obj.data.update()

        for key_block in shape_keys.key_blocks:
            if key_block == basis_key:
                continue
            key_block.relative_key = basis_key

        if remove_source:
            refreshed_source_key = shape_keys.key_blocks.get(source_key_name)
            if refreshed_source_key is not None and refreshed_source_key != basis_key:
                obj.shape_key_remove(refreshed_source_key)
        else:
            preserved_source_key = shape_keys.key_blocks.get(source_key_name)
            if preserved_source_key is not None:
                preserved_source_key.relative_key = basis_key
                preserved_source_key.value = 0.0

        obj.active_shape_key_index = 0
        obj.data.update()
        _refresh_shape_key_list_from_context(context)

        action_text = "并删除源形态键" if remove_source else "并保留零偏移源形态键"
        self.report({'INFO'}, f"已将 '{source_key_name}' 设为新的 Basis，其他形态键已按原始偏移重算，{action_text}。")
        return {'FINISHED'}


at_shape_key_operations_list = (
    ATP_OT_BatchAddShapeKey,
    ATP_OT_BatchRemoveShapeKey,
    ATP_OT_ResetAllShapeKeys,
    ATP_OT_SetActiveShapeKey,
    ATP_OT_BatchRenameShapeKey,
    ATP_OT_SetActiveShapeKeyAsBasis,
)
