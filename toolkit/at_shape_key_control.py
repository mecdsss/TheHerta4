# -*- coding: utf-8 -*-

import bpy
import math
import mathutils
import time
import numpy as np


def refresh_shape_key_list(scene_props, objects):
    scene_props.shape_key_list.clear()

    found_keys = set()
    target_objects = [obj for obj in objects if obj and obj.type == 'MESH' and obj.data.shape_keys]

    for obj in target_objects:
        reference_key = obj.data.shape_keys.reference_key
        for key_block in obj.data.shape_keys.key_blocks:
            if key_block != reference_key:
                found_keys.add(key_block.name)

    for key_name in sorted(found_keys):
        item = scene_props.shape_key_list.add()
        item.name = key_name
        for obj in target_objects:
            if key_name in obj.data.shape_keys.key_blocks:
                item.value = obj.data.shape_keys.key_blocks[key_name].value
                break

    return found_keys


class ATP_OT_RefreshShapeKeys(bpy.types.Operator):
    bl_idname = "atp.refresh_shape_keys"
    bl_label = "刷新形态键列表"
    bl_description = "根据当前选中的物体刷新统一形态键控制列表"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects)

    def execute(self, context):
        props = context.scene.atp_props
        found_keys = refresh_shape_key_list(props, context.selected_objects)
        if not found_keys:
            self.report({'INFO'}, "当前选中物体中没有可控制的形态键。")
            return {'CANCELLED'}

        self.report({'INFO'}, f"找到 {len(found_keys)} 个唯一形态键。")
        return {'FINISHED'}


class ATP_OT_CopyShapeKeys(bpy.types.Operator):
    """将活动物体的形态键相对位移复制到其他选中的同拓扑物体上。"""
    bl_idname = "atp.copy_shape_keys"
    bl_label = "复制形态键到选中项"
    bl_description = "将活动物体的全部形态键复制到其他顶点数相同的选中网格物体"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active_obj = context.active_object
        return (
            active_obj is not None
            and active_obj.type == 'MESH'
            and active_obj.data.shape_keys is not None
            and len(context.selected_objects) > 1
        )

    def execute(self, context):
        source_obj = context.active_object
        all_targets = [obj for obj in context.selected_objects if obj != source_obj]

        if not source_obj.data.shape_keys or not source_obj.data.shape_keys.key_blocks:
            self.report({'ERROR'}, "源物体没有可复制的形态键。")
            return {'CANCELLED'}

        source_mesh = source_obj.data
        source_vtx_count = len(source_mesh.vertices)

        valid_targets = [
            target for target in all_targets
            if target.type == 'MESH' and len(target.data.vertices) == source_vtx_count
        ]
        skipped_count = len(all_targets) - len(valid_targets)

        if not valid_targets:
            self.report({'ERROR'}, f"未找到顶点数为 {source_vtx_count} 的目标物体。")
            return {'CANCELLED'}

        start_time = time.time()

        source_keys = source_mesh.shape_keys
        if not source_keys.reference_key:
            self.report({'ERROR'}, f"源物体 '{source_obj.name}' 缺少 Basis。")
            return {'CANCELLED'}

        basis_key = source_keys.reference_key
        source_basis_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
        basis_key.data.foreach_get("co", source_basis_coords)
        source_basis_coords = source_basis_coords.reshape(-1, 3)

        keys_deltas = {}
        for key_block in source_keys.key_blocks:
            if key_block == basis_key:
                continue

            key_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
            key_block.data.foreach_get("co", key_coords)
            key_coords = key_coords.reshape(-1, 3)

            keys_deltas[key_block.name] = {
                "delta": key_coords - source_basis_coords,
                "value": key_block.value,
                "slider_min": key_block.slider_min,
                "slider_max": key_block.slider_max,
                "mute": key_block.mute,
            }

        for target in valid_targets:
            target_mesh = target.data
            if not target_mesh.shape_keys:
                target.shape_key_add(name="Basis")

            target_keys = target_mesh.shape_keys
            target_basis_key = target_keys.reference_key
            if not target_basis_key:
                self.report({'WARNING'}, f"跳过目标 '{target.name}'，因为它缺少 Basis。")
                continue

            target_basis_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
            target_basis_key.data.foreach_get("co", target_basis_coords)
            target_basis_coords = target_basis_coords.reshape(-1, 3)

            props = context.scene.atp_props
            if props.copy_sk_use_manual_rotation:
                rot_x = math.radians(props.copy_sk_rotation_x)
                rot_y = math.radians(props.copy_sk_rotation_y)
                rot_z = math.radians(props.copy_sk_rotation_z)

                rot_matrix_x = mathutils.Matrix.Rotation(rot_x, 4, 'X').to_3x3()
                rot_matrix_y = mathutils.Matrix.Rotation(rot_y, 4, 'Y').to_3x3()
                rot_matrix_z = mathutils.Matrix.Rotation(rot_z, 4, 'Z').to_3x3()
                transform_matrix = rot_matrix_z @ rot_matrix_y @ rot_matrix_x
            else:
                source_matrix = source_obj.matrix_world
                target_matrix_inv = target.matrix_world.inverted()
                transform_matrix = (target_matrix_inv @ source_matrix).to_3x3()

            for key_name, key_info in keys_deltas.items():
                target_key_block = target_keys.key_blocks.get(key_name)
                if not target_key_block:
                    target_key_block = target.shape_key_add(name=key_name, from_mix=False)

                transformed_delta = key_info["delta"] @ transform_matrix.transposed()
                new_coords = (target_basis_coords + transformed_delta).reshape(-1)

                target_key_block.data.foreach_set("co", new_coords)
                target_key_block.value = key_info["value"]
                target_key_block.slider_min = key_info["slider_min"]
                target_key_block.slider_max = key_info["slider_max"]
                target_key_block.mute = key_info["mute"]

            target_mesh.update()

        elapsed = time.time() - start_time
        self.report({'INFO'}, f"已复制 {len(keys_deltas)} 个形态键到 {len(valid_targets)} 个物体，耗时 {elapsed:.3f}s。")
        if skipped_count > 0:
            self.report({'WARNING'}, f"跳过了 {skipped_count} 个顶点数不匹配的物体。")

        return {'FINISHED'}


at_shape_key_control_list = (
    ATP_OT_RefreshShapeKeys,
    ATP_OT_CopyShapeKeys,
)
