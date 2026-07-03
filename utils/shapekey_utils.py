
import os
import bpy
import time
import numpy
import re
import math
from contextlib import contextmanager
from mathutils import Matrix

from .timer_utils import TimerUtils

class ShapeKeyUtils:
    # Github: https://github.com/przemir/ApplyModifierForObjectWithShapeKeys
    _AUTO_FIX_WARNING_PATTERNS = (
        "has an invalid 'from' pointer",
        "it will be deleted",
    )
    _AUTO_FIX_WARNING_TOKEN_GROUPS = (
        ("invalid", "无效", "失效"),
        ("pointer", "from", "指针", "shape key", "shape keys", "形态键"),
        ("delete", "deleted", "删除", "移除"),
    )

    @staticmethod
    def is_basis_shape_key_name(name) -> bool:
        """判断名称是否为 Basis 形态键"""
        return str(name or "").strip().lower() == "basis"

    @classmethod
    def iter_exportable_shape_keys(cls, obj):
        key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
        if not key_blocks:
            return

        for index, key_block in enumerate(key_blocks):
            if index == 0:
                continue
            if cls.is_basis_shape_key_name(getattr(key_block, "name", "")):
                continue
            yield key_block

    @classmethod
    def count_exportable_shape_keys(cls, obj) -> int:
        return sum(1 for _key_block in cls.iter_exportable_shape_keys(obj) or ())

    @classmethod
    def has_exportable_shape_keys(cls, obj) -> bool:
        return cls.count_exportable_shape_keys(obj) > 0

    @staticmethod
    def get_basis_shape_key_block(obj):
        key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
        if not key_blocks:
            return None
        try:
            return key_blocks[0]
        except Exception:
            return None

    @staticmethod
    def _safe_set_active_object(obj):
        """安全设置活动对象"""
        bpy.context.view_layer.objects.active = obj

    @staticmethod
    def _update_view_layer():
        view_layer = getattr(bpy.context, "view_layer", None)
        update_method = getattr(view_layer, "update", None)
        if callable(update_method):
            update_method()

    @staticmethod
    def _ensure_object_mode_for_active_object(obj):
        """确保活动对象处于 OBJECT 模式"""
        
        active_object = bpy.context.view_layer.objects.active
        if active_object is None:
            return

        current_mode = getattr(active_object, "mode", None)
        if current_mode == 'EDIT':
            try:
                active_object.update_from_editmode()
            except Exception:
                pass

        if current_mode and current_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

    @staticmethod
    def _safe_deselect_all_objects():
        for selected_obj in list(bpy.context.selected_objects):
            try:
                selected_obj.select_set(False)
            except Exception:
                pass
        bpy.context.view_layer.objects.active = None

    @classmethod
    @contextmanager
    def _shape_key_operator_context(cls, obj):
        original_active = bpy.context.view_layer.objects.active
        original_selected = [
            selected_obj
            for selected_obj in bpy.context.selected_objects
            if selected_obj is not None
        ]
        original_mode = getattr(original_active, "mode", None) if original_active is not None else None

        try:
            if original_active is not None:
                cls._ensure_object_mode_for_active_object(original_active)
            cls._safe_deselect_all_objects()
            cls._safe_set_active_object(obj)
            obj.select_set(True)
            cls._ensure_object_mode_for_active_object(obj)

            yield
        finally:
            try:
                cls._safe_deselect_all_objects()
            except Exception:
                pass

            for selected_obj in original_selected:
                try:
                    if selected_obj.name in bpy.data.objects:
                        selected_obj.select_set(True)
                except Exception:
                    pass

            if original_active is not None:
                try:
                    if original_active.name in bpy.data.objects:
                        cls._safe_set_active_object(original_active)
                        if original_mode and getattr(original_active, "mode", None) != original_mode:
                            bpy.ops.object.mode_set(mode=original_mode)
                except Exception:
                    pass

    @classmethod
    @contextmanager
    def operator_context(cls, obj):
        with cls._shape_key_operator_context(obj):
            yield obj

    @classmethod
    def remove_shape_keys(cls, obj, all=True, apply_mix=None, active_shape_key_index=None):
        kwargs = {"all": all}
        if apply_mix is not None:
            kwargs["apply_mix"] = apply_mix

        with cls._shape_key_operator_context(obj):
            if active_shape_key_index is not None:
                obj.active_shape_key_index = active_shape_key_index
            bpy.ops.object.shape_key_remove(**kwargs)

    @classmethod
    @contextmanager
    def temporarily_disable_visible_modifiers(cls, obj):
        """临时禁用所有可见修改器"""
        modifiers = list(getattr(obj, "modifiers", []) or [])
        original_states = [
            (modifier, bool(getattr(modifier, "show_viewport", False)))
            for modifier in modifiers
        ]

        try:
            for modifier, show_viewport in original_states:
                if show_viewport:
                    modifier.show_viewport = False
            if original_states:
                cls._update_view_layer()
            yield
        finally:
            for modifier, show_viewport in original_states:
                try:
                    modifier.show_viewport = show_viewport
                except Exception:
                    pass
            if original_states:
                cls._update_view_layer()

    @classmethod
    def sync_basis_shape_key_to_mesh(cls, obj) -> bool:
        basis_key = cls.get_basis_shape_key_block(obj)
        vertices = getattr(getattr(obj, "data", None), "vertices", None)
        basis_points = getattr(basis_key, "data", None) if basis_key is not None else None
        if basis_points is None or vertices is None:
            return False

        try:
            coords = numpy.empty((len(vertices), 3), dtype=numpy.float32)
            vertices.foreach_get("co", coords.ravel())
            if len(basis_points) != len(vertices):
                return False
            basis_points.foreach_set("co", coords.ravel())
            return True
        except Exception:
            return False

    @classmethod
    def bake_current_shape_key_mix_to_mesh(cls, obj, stage_label: str = "") -> bool:
        if not obj or getattr(obj, "type", None) != 'MESH' or not getattr(obj, "data", None):
            return False

        coords = None
        with cls.temporarily_disable_visible_modifiers(obj):
            depsgraph = bpy.context.evaluated_depsgraph_get()
            evaluated_obj = obj.evaluated_get(depsgraph)
            evaluated_mesh = evaluated_obj.to_mesh()
            try:
                coords = numpy.empty((len(evaluated_mesh.vertices), 3), dtype=numpy.float32)
                evaluated_mesh.vertices.foreach_get("co", coords.ravel())
            finally:
                evaluated_obj.to_mesh_clear()

        if coords is None:
            return False

        obj.data.vertices.foreach_set("co", coords.ravel())
        cls.sync_basis_shape_key_to_mesh(obj)
        obj.data.update()
        return True

    @classmethod
    def remove_non_basis_shape_keys(cls, obj, stage_label: str = "") -> int:
        key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
        if not key_blocks:
            return 0

        clear_method = getattr(obj, "shape_key_clear", None)
        if callable(clear_method):
            clear_method()
            remaining_key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
            if not remaining_key_blocks or len(remaining_key_blocks) <= 1:
                return max(len(key_blocks) - 1, 0)

        if callable(getattr(obj, "shape_key_remove", None)):
            removed_count = 0
            for shape_key in reversed(list(key_blocks)[1:]):
                obj.shape_key_remove(shape_key)
                removed_count += 1
            return removed_count

        removed_count = 0
        remaining_key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
        try:
            while remaining_key_blocks and len(remaining_key_blocks) > 1:
                cls.remove_shape_keys(obj, all=False, active_shape_key_index=len(remaining_key_blocks) - 1)
                removed_count += 1
                remaining_key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
        except Exception as exc:
            stage_prefix = f"{stage_label}: " if stage_label else ""
            raise RuntimeError(
                f"{stage_prefix}当前对象不支持 shape_key_remove，无法移除非 Basis 形态键。"
            ) from exc
        return removed_count

    @classmethod
    def capture_shape_key_state(cls, obj) -> list[dict]:
        key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
        if not key_blocks:
            return []

        state = []
        for key_block in key_blocks:
            state.append(
                {
                    "name": getattr(key_block, "name", ""),
                    "value": float(getattr(key_block, "value", 0.0)),
                    "mute": bool(getattr(key_block, "mute", False)),
                }
            )
        return state

    @classmethod
    def restore_shape_key_state(cls, obj, shape_key_state: list[dict]):
        key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
        if not key_blocks:
            return

        state_by_name = {
            entry.get("name", ""): entry
            for entry in shape_key_state
            if entry.get("name", "")
        }
        for key_block in key_blocks:
            state_entry = state_by_name.get(getattr(key_block, "name", ""))
            if getattr(key_block, "name", "") != 'Basis':
                key_block.value = float(state_entry.get("value", 0.0)) if state_entry else 0.0
            if state_entry is not None and hasattr(key_block, 'mute'):
                key_block.mute = bool(state_entry.get("mute", False))

    @classmethod
    @contextmanager
    def preserve_shape_key_state(cls, obj):
        shape_key_state = cls.capture_shape_key_state(obj)
        try:
            yield shape_key_state
        finally:
            if shape_key_state:
                cls.restore_shape_key_state(obj, shape_key_state)
                cls._update_view_layer()

    @staticmethod
    def _get_shape_key_coordinate_snapshot(obj):
        shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        key_blocks = getattr(shape_keys, "key_blocks", None)
        if not key_blocks:
            return {}

        snapshot = {}
        for key_block in key_blocks:
            coords = numpy.empty((len(key_block.data), 3), dtype=numpy.float32)
            key_block.data.foreach_get("co", coords.ravel())
            snapshot[key_block.name] = coords
        return snapshot

    @staticmethod
    def _build_applied_transform_matrix(obj, location=True, rotation=True, scale=True):
        if rotation and scale:
            matrix = obj.matrix_basis.copy()
            if not location:
                matrix.translation = (0.0, 0.0, 0.0)
            return matrix

        loc, rot, scl = obj.matrix_basis.decompose()
        loc_matrix = Matrix.Translation(loc if location else (0.0, 0.0, 0.0))
        rot_matrix = rot.to_matrix().to_4x4() if rotation else Matrix.Identity(4)
        scale_matrix = Matrix.Diagonal((
            float(scl.x) if scale else 1.0,
            float(scl.y) if scale else 1.0,
            float(scl.z) if scale else 1.0,
            1.0,
        ))
        return loc_matrix @ rot_matrix @ scale_matrix

    @staticmethod
    def _restore_transformed_shape_key_coordinates(obj, snapshot, matrix):
        if not snapshot:
            return

        shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        key_blocks = getattr(shape_keys, "key_blocks", None)
        if not key_blocks:
            return

        matrix_np = numpy.array(matrix, dtype=numpy.float64)
        linear = matrix_np[:3, :3]
        translation = matrix_np[:3, 3]

        for key_block in key_blocks:
            coords = snapshot.get(key_block.name)
            if coords is None or len(coords) != len(key_block.data):
                continue
            transformed = coords.astype(numpy.float64) @ linear.T + translation
            key_block.data.foreach_set("co", transformed.astype(numpy.float32).ravel())

        obj.data.update()

    @classmethod
    def transform_apply_preserve_shape_keys(cls, obj, location=True, rotation=True, scale=True):
        """应用变换时保留形态键坐标"""
        
        snapshot = cls._get_shape_key_coordinate_snapshot(obj)
        matrix = cls._build_applied_transform_matrix(
            obj,
            location=location,
            rotation=rotation,
            scale=scale,
        )
        original_active = bpy.context.view_layer.objects.active
        original_selection = list(bpy.context.selected_objects)
        original_mode = getattr(original_active, "mode", None) if original_active is not None else None

        try:
            with cls.operator_context(obj):
                bpy.ops.object.transform_apply(
                    location=location,
                    rotation=rotation,
                    scale=scale,
                )
            cls._restore_transformed_shape_key_coordinates(obj, snapshot, matrix)
        finally:
            cls._safe_deselect_all_objects()
            for selected_obj in original_selection:
                if selected_obj.name in bpy.data.objects:
                    selected_obj.select_set(True)
            if original_active and original_active.name in bpy.data.objects:
                cls._safe_set_active_object(original_active)
                if original_mode and getattr(original_active, "mode", None) != original_mode:
                    try:
                        bpy.ops.object.mode_set(mode=original_mode)
                    except Exception:
                        pass

    @classmethod
    def apply_modifiers_for_object_with_shape_keys_optimized(cls, context, selected_modifiers, disable_armatures=False):
        """
        当前保留一个兼容入口，实际统一回退到经典算法。
        之前的 numpy 快路径会在部分修改器栈下产生错误结果，因此先禁用，
        避免直出和普通导出出现静默数据偏差。
        """
        if len(selected_modifiers) == 0:
            return (True, None)

        print("[ShapeKeyOptimized] Fast path disabled for correctness, falling back to classic apply path")
        return cls.apply_modifiers_for_object_with_shape_keys(
            context,
            selected_modifiers,
            disable_armatures,
        )

    @classmethod
    def apply_modifiers_for_object_with_shape_keys(cls,context, selectedModifiers, disable_armatures):
        """对含形态键的对象逐个应用修改器（经典算法，来自 Przemysław Bągard）"""
        
        # The MIT License (MIT)
        #
        # Copyright (c) 2015 Przemysław Bągard
        #
        # Permission is hereby granted, free of charge, to any person obtaining a copy
        # of this software and associated documentation files (the "Software"), to deal
        # in the Software without restriction, including without limitation the rights
        # to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
        # copies of the Software, and to permit persons to whom the Software is
        # furnished to do so, subject to the following conditions:
        #
        # The above copyright notice and this permission notice shall be included in
        # all copies or substantial portions of the Software.
        #
        # THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
        # IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
        # FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
        # AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
        # LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
        # OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
        # THE SOFTWARE.

        # Date: 01 February 2015
        # Blender script
        # Description: Apply modifier and remove from the stack for object with shape keys
        # (Pushing 'Apply' button in 'Object modifiers' tab result in an error 'Modifier cannot be applied to a mesh with shape keys').

        # Algorithm (old):
        # - Duplicate active object as many times as the number of shape keys
        # - For each copy remove all shape keys except one
        # - Removing last shape does not change geometry data of object
        # - Apply modifier for each copy
        # - Join objects as shapes and restore shape keys names
        # - Delete all duplicated object except one
        # - Delete old object
        # Original object should be preserved (to keep object name and other data associated with object/mesh). 

        # Algorithm (new):
        # Don't make list of copies, handle it one shape at time.
        # In this algorithm there shouldn't be more than 3 copy of object at time, so it should be more memory-friendly.
        #
        # - Copy object which will hold shape keys
        # - For original object (which will be also result object), remove all shape keys, then apply modifiers. Add "base" shape key
        # - For each shape key except base copy temporary object from copy. Then for temporaryObject:
        #     - remove all shape keys except one (done by removing all shape keys, then transfering the right one from copyObject)
        #     - apply modifiers
        #     - merge with originalObject
        #     - delete temporaryObject
        # - Delete copyObject.
        
        if len(selectedModifiers) == 0:
            return (True, None)

        list_properties = []
        properties = ["interpolation", "mute", "name", "relative_key", "slider_max", "slider_min", "value", "vertex_group"]
        shapesCount = 0
        vertCount = -1
        startTime = time.time()
        
        # Inspect modifiers for hints used in error message if needed.
        contains_mirror_with_merge = False
        for modifier in context.object.modifiers:
            if modifier.name in selectedModifiers:
                if modifier.type == 'MIRROR' and modifier.use_mirror_merge == True:
                    contains_mirror_with_merge = True

        # Disable armature modifiers.
        disabled_armature_modifiers = []
        if disable_armatures:
            for modifier in context.object.modifiers:
                if modifier.name not in selectedModifiers and modifier.type == 'ARMATURE' and modifier.show_viewport == True:
                    disabled_armature_modifiers.append(modifier)
                    modifier.show_viewport = False
        
        # Calculate shape keys count.
        if context.object.data.shape_keys:
            shapesCount = len(context.object.data.shape_keys.key_blocks)
        
        # If there are no shape keys, just apply modifiers.
        if(shapesCount == 0):
            for modifierName in selectedModifiers:
                bpy.ops.object.modifier_apply(modifier=modifierName)
            return (True, None)
        
        # We want to preserve original object, so all shapes will be joined to it.
        originalObject = context.view_layer.objects.active
        bpy.ops.object.select_all(action='DESELECT')
        originalObject.select_set(True)
        
        # Copy object which will holds all shape keys.
        bpy.ops.object.duplicate_move(OBJECT_OT_duplicate={"linked":False, "mode":'TRANSLATION'}, TRANSFORM_OT_translate={"value":(0, 0, 0), "orient_type":'GLOBAL', "orient_matrix":((1, 0, 0), (0, 1, 0), (0, 0, 1)), "orient_matrix_type":'GLOBAL', "constraint_axis":(False, False, False), "mirror":True, "use_proportional_edit":False, "proportional_edit_falloff":'SMOOTH', "proportional_size":1, "use_proportional_connected":False, "use_proportional_projected":False, "snap":False, "snap_target":'CLOSEST', "snap_point":(0, 0, 0), "snap_align":False, "snap_normal":(0, 0, 0), "gpencil_strokes":False, "cursor_transform":False, "texture_space":False, "remove_on_cancel":False, "release_confirm":False, "use_accurate":False})
        copyObject = context.view_layer.objects.active
        copyObject.select_set(False)
        
        # Return selection to originalObject.
        context.view_layer.objects.active = originalObject
        originalObject.select_set(True)
        
        # Save key shape properties
        for i in range(0, shapesCount):
            key_b = originalObject.data.shape_keys.key_blocks[i]
            print (originalObject.data.shape_keys.key_blocks[i].name, key_b.name)
            properties_object = {p:None for p in properties}
            properties_object["name"] = key_b.name
            properties_object["mute"] = key_b.mute
            properties_object["interpolation"] = key_b.interpolation
            properties_object["relative_key"] = key_b.relative_key.name
            properties_object["slider_max"] = key_b.slider_max
            properties_object["slider_min"] = key_b.slider_min
            properties_object["value"] = key_b.value
            properties_object["vertex_group"] = key_b.vertex_group
            list_properties.append(properties_object)

        # Handle base shape in "originalObject"
        print("applyModifierForObjectWithShapeKeys: Applying base shape key")
        cls.remove_shape_keys(originalObject, all=True)
        for modifierName in selectedModifiers:
            bpy.ops.object.modifier_apply(modifier=modifierName)
        vertCount = len(originalObject.data.vertices)
        bpy.ops.object.shape_key_add(from_mix=False)
        originalObject.select_set(False)
        
        # Handle other shape-keys: copy object, get right shape-key, apply modifiers and merge with originalObject.
        # We handle one object at time here.
        for i in range(1, shapesCount):
            currTime = time.time()
            elapsedTime = currTime - startTime

            print("applyModifierForObjectWithShapeKeys: Applying shape key %d/%d ('%s', %0.2f seconds since start)" % (i+1, shapesCount, list_properties[i]["name"], elapsedTime))
            context.view_layer.objects.active = copyObject
            copyObject.select_set(True)
            
            # Copy temp object.
            bpy.ops.object.duplicate_move(OBJECT_OT_duplicate={"linked":False, "mode":'TRANSLATION'}, TRANSFORM_OT_translate={"value":(0, 0, 0), "orient_type":'GLOBAL', "orient_matrix":((1, 0, 0), (0, 1, 0), (0, 0, 1)), "orient_matrix_type":'GLOBAL', "constraint_axis":(False, False, False), "mirror":True, "use_proportional_edit":False, "proportional_edit_falloff":'SMOOTH', "proportional_size":1, "use_proportional_connected":False, "use_proportional_projected":False, "snap":False, "snap_target":'CLOSEST', "snap_point":(0, 0, 0), "snap_align":False, "snap_normal":(0, 0, 0), "gpencil_strokes":False, "cursor_transform":False, "texture_space":False, "remove_on_cancel":False, "release_confirm":False, "use_accurate":False})
            tmpObject = context.view_layer.objects.active
            cls.remove_shape_keys(tmpObject, all=True)
            copyObject.select_set(True)
            copyObject.active_shape_key_index = i
            
            # Get right shape-key.
            bpy.ops.object.shape_key_transfer()
            context.object.active_shape_key_index = 0
            cls.remove_shape_keys(tmpObject, all=False)
            cls.remove_shape_keys(tmpObject, all=True)
            
            # Time to apply modifiers.
            for modifierName in selectedModifiers:
                bpy.ops.object.modifier_apply(modifier=modifierName)
            
            # Verify number of vertices.
            if vertCount != len(tmpObject.data.vertices):
            
                errorInfoHint = ""
                if contains_mirror_with_merge == True:
                    errorInfoHint = "There is mirror modifier with 'Merge' property enabled. This may cause a problem."
                if errorInfoHint:
                    errorInfoHint = "\n\nHint: " + errorInfoHint
                errorInfo = ("Shape keys ended up with different number of vertices!\n"
                            "All shape keys needs to have the same number of vertices after modifier is applied.\n"
                            "Otherwise joining such shape keys will fail!%s" % errorInfoHint)
                if disable_armatures:
                    for modifier in disabled_armature_modifiers:
                        modifier.show_viewport = True
                for cleanup_obj in (tmpObject, copyObject):
                    try:
                        cleanup_mesh = cleanup_obj.data
                        bpy.data.objects.remove(cleanup_obj, do_unlink=True)
                        if cleanup_mesh and cleanup_mesh.users == 0:
                            bpy.data.meshes.remove(cleanup_mesh)
                    except Exception:
                        pass
                context.view_layer.objects.active = originalObject
                originalObject.select_set(True)
                return (False, errorInfo)
        
            # Join with originalObject
            copyObject.select_set(False)
            context.view_layer.objects.active = originalObject
            originalObject.select_set(True)
            bpy.ops.object.join_shapes()
            originalObject.select_set(False)
            context.view_layer.objects.active = tmpObject
            
            # Remove tmpObject
            tmpMesh = tmpObject.data
            bpy.ops.object.delete(use_global=False)
            bpy.data.meshes.remove(tmpMesh)
        
        # Restore shape key properties like name, mute etc.
        context.view_layer.objects.active = originalObject
        for i in range(0, shapesCount):
            key_b = context.view_layer.objects.active.data.shape_keys.key_blocks[i]
            # name needs to be restored before relative_key
            key_b.name = list_properties[i]["name"]
            
        for i in range(0, shapesCount):
            key_b = context.view_layer.objects.active.data.shape_keys.key_blocks[i]
            key_b.interpolation = list_properties[i]["interpolation"]
            key_b.mute = list_properties[i]["mute"]
            key_b.slider_max = list_properties[i]["slider_max"]
            key_b.slider_min = list_properties[i]["slider_min"]
            key_b.value = list_properties[i]["value"]
            key_b.vertex_group = list_properties[i]["vertex_group"]
            rel_key = list_properties[i]["relative_key"]
        
            for j in range(0, shapesCount):
                key_brel = context.view_layer.objects.active.data.shape_keys.key_blocks[j]
                if rel_key == key_brel.name:
                    key_b.relative_key = key_brel
                    break
        
        # Remove copyObject.
        originalObject.select_set(False)
        context.view_layer.objects.active = copyObject
        copyObject.select_set(True)
        tmpMesh = copyObject.data
        bpy.ops.object.delete(use_global=False)
        bpy.data.meshes.remove(tmpMesh)
        
        # Select originalObject.
        context.view_layer.objects.active = originalObject
        context.view_layer.objects.active.select_set(True)
        
        if disable_armatures:
            for modifier in disabled_armature_modifiers:
                modifier.show_viewport = True
        
        return (True, None)


    @classmethod
    def extract_shapekey_data(cls,merged_obj,index_vertex_id_dict):
        '''
        Build WWMI native shape key buffers.

        WWMI stores shape keys in batches of 127 keys. Each batch has 128
        uint offsets: a leading 0, one offset after every key, and the final
        value acts as the sentinel used by ShapeKeyLoader.
        '''
        shapekey_cache = cls.get_shapekey_cache(merged_obj,index_vertex_id_dict)
        if not shapekey_cache:
            return [], [], []

        shapekey_offsets = []
        shapekey_vertex_ids = []
        shapekey_vertex_offsets = []

        max_shapekey_id = max(shapekey_cache.keys())
        batch_count = max(1, math.ceil((max_shapekey_id + 1) / 127))

        for batch_id in range(batch_count):
            shapekey_verts_count = 0
            shapekey_offsets.append(0)
            shapekey_id_offset = batch_id * 127

            for group_id in range(shapekey_id_offset, shapekey_id_offset + 127):
                shapekey = shapekey_cache.get(group_id, None)
                if shapekey is not None and len(shapekey) != 0:
                    for draw_index, vertex_offsets in shapekey.items():
                        shapekey_vertex_ids.append(draw_index)
                        shapekey_vertex_offsets.extend(vertex_offsets + [0, 0, 0])
                        shapekey_verts_count += 1

                shapekey_offsets.append(shapekey_verts_count)

        return shapekey_offsets,shapekey_vertex_ids,shapekey_vertex_offsets
    


    @classmethod
    def get_shapekey_cache(cls, merged_obj, index_vertex_id_dict):
        obj = merged_obj
        mesh = obj.data
        mesh_shapekeys = mesh.shape_keys
        
        if mesh_shapekeys is None:
            print(f"obj: {obj.name} 不含有形态键，跳过处理")
            TimerUtils.End("shapekey_cache")
            return {}

        # 构建顶点索引到全局index_id的反向映射
        vertex_to_indices = {}
        for index_id, vertex_id in index_vertex_id_dict.items():
            if vertex_id not in vertex_to_indices:
                vertex_to_indices[vertex_id] = []
            vertex_to_indices[vertex_id].append(index_id)

        # 获取基础坐标
        base_data = mesh_shapekeys.key_blocks['Basis'].data
        base_coords = numpy.empty((len(mesh.vertices), 3), dtype=numpy.float32)
        base_data.foreach_get('co', base_coords.ravel())

        shapekey_cache = {}
        shapekey_pattern = re.compile(r'.*(?:deform|custom)[_ -]*(\d+).*')

        # 处理每个形态键
        for shapekey in mesh_shapekeys.key_blocks:
            # 跳过基础形态键
            if shapekey.name == 'Basis':
                continue
            
            # print(shapekey.name)

            # 提取形态键ID
            match = shapekey_pattern.findall(shapekey.name.lower())
            if not match:
                print(f"当前形态键名称:{shapekey.name} 不符合命名规范，跳过")
                continue
                
            shapekey_idx = int(match[0])
            # print("process: " + str(shapekey_idx))

            # 获取形态键坐标数据
            sk_coords = numpy.empty((len(mesh.vertices), 3), dtype=numpy.float32)
            shapekey.data.foreach_get('co', sk_coords.ravel())
            
            # 计算偏移量 (向量化操作)
            offsets = sk_coords - base_coords
            
            # 计算向量长度并过滤小偏移
            lengths = numpy.linalg.norm(offsets, axis=1)
            valid_mask = lengths >= 1e-9
            valid_vertex_ids = numpy.where(valid_mask)[0]
            
            if not valid_vertex_ids.size:
                # 这里一般不会触发
                # print("valid_vertex_ids.size not, continue!")
                continue
                
            # 按形态键初始化缓存
            if shapekey_idx not in shapekey_cache:
                shapekey_cache[shapekey_idx] = {}
                
            # 处理有效顶点
            for v_idx in valid_vertex_ids:
                offset_list = offsets[v_idx].tolist()
                # 获取关联的全局index_id
                if v_idx in vertex_to_indices:
                    for index_id in vertex_to_indices[v_idx]:
                        shapekey_cache[shapekey_idx][index_id] = offset_list

        return shapekey_cache
    
    @staticmethod
    def reset_shapekey_values(obj, configured_shapekey_names=None, current_shapekey_name=None):
        """重置形态键值：将配置列表中非当前的形态键归零"""


        if obj.data.shape_keys:
            reset_all_non_basis = configured_shapekey_names is None
            configured_shapekey_names = set(configured_shapekey_names or [])
            
            for key_block in obj.data.shape_keys.key_blocks:
                if key_block.name == 'Basis':
                    continue

                if reset_all_non_basis or key_block.name in configured_shapekey_names:
                    if key_block.name != current_shapekey_name:
                        key_block.value = 0.0

    @classmethod
    def cleanup_invalid_shapekeys(cls, obj_names=None):
        """清理损坏的形态键（relative_key 指针无效的形态键）"""

        cleaned_count = 0
        
        if obj_names is None:
            objects_to_check = list(bpy.data.objects)
        else:
            objects_to_check = [bpy.data.objects.get(name) for name in obj_names]
            objects_to_check = [obj for obj in objects_to_check if obj is not None]
        
        for obj in objects_to_check:
            if obj.type != 'MESH':
                continue
            
            if not obj.data.shape_keys:
                continue
            
            key_blocks = obj.data.shape_keys.key_blocks
            if not key_blocks or len(key_blocks) == 0:
                continue
            
            valid_key_names = set(kb.name for kb in key_blocks)
            keys_to_remove = []
            
            for key_block in key_blocks:
                try:
                    relative_key = key_block.relative_key
                    if relative_key is None:
                        keys_to_remove.append(key_block.name)
                        print(f"[ShapeKeyUtils] 检测到损坏的形态键: {obj.name}.{key_block.name} (relative_key 为 None)")
                        continue
                    
                    rel_name = relative_key.name
                    if rel_name not in valid_key_names:
                        keys_to_remove.append(key_block.name)
                        print(f"[ShapeKeyUtils] 检测到损坏的形态键: {obj.name}.{key_block.name} (relative_key '{rel_name}' 不在有效列表中)")
                except ReferenceError:
                    keys_to_remove.append(key_block.name)
                    print(f"[ShapeKeyUtils] 检测到损坏的形态键: {obj.name}.{key_block.name} (ReferenceError: 指针已失效)")
                except Exception as e:
                    print(f"[ShapeKeyUtils] 检查形态键时发生异常: {obj.name}.{key_block.name} - {e} (跳过，由 Blender 自动处理)")
            
            if keys_to_remove:
                for key_name in reversed(keys_to_remove):
                    try:
                        key_index = -1
                        for i, kb in enumerate(obj.data.shape_keys.key_blocks):
                            if kb.name == key_name:
                                key_index = i
                                break
                        
                        if key_index >= 0:
                            cls.remove_shape_keys(
                                obj,
                                all=False,
                                active_shape_key_index=key_index,
                            )
                            cleaned_count += 1
                            print(f"[ShapeKeyUtils] 已删除损坏的形态键: {obj.name}.{key_name}")
                    except Exception as e:
                        print(f"[ShapeKeyUtils] 删除形态键失败: {obj.name}.{key_name} - {e}")
        
        if cleaned_count > 0:
            print(f"[ShapeKeyUtils] 共清理 {cleaned_count} 个损坏的形态键")
        
        return cleaned_count

    @classmethod
    def is_blender_auto_fix_warning(cls, error_msg: str) -> bool:
        error_lower = (error_msg or "").lower()
        if not error_lower:
            return False

        if all(pattern in error_lower for pattern in cls._AUTO_FIX_WARNING_PATTERNS):
            return True

        # Blender's save-time auto-fix warning is localized on some builds.
        # Accept the warning when it carries the same semantic signals:
        # invalid/broken data -> pointer/from/shapekey context -> auto deletion.
        return all(
            any(token in error_lower for token in token_group)
            for token_group in cls._AUTO_FIX_WARNING_TOKEN_GROUPS
        )

    @classmethod
    def save_as_mainfile_with_shape_key_recovery(cls, filepath: str, copy: bool = False, check_existing: bool = False):
        try:
            bpy.ops.wm.save_as_mainfile(filepath=filepath, copy=copy, check_existing=check_existing)
            return
        except RuntimeError as exc:
            if not cls.is_blender_auto_fix_warning(str(exc)):
                raise

            if filepath and os.path.exists(filepath):
                print(f"[ShapeKeyUtils] Blender auto-fixed invalid shape key pointers while saving '{filepath}'.")
                return

            cleaned_count = cls.cleanup_invalid_shapekeys()
            print(f"[ShapeKeyUtils] Retrying save_as_mainfile after cleaning {cleaned_count} invalid shape keys.")
            bpy.ops.wm.save_as_mainfile(filepath=filepath, copy=copy, check_existing=check_existing)

    @classmethod
    def save_mainfile_with_shape_key_recovery(cls):
        """保存当前 Blender 文件，遇到形态键自动修复后重试"""
        
        try:
            bpy.ops.wm.save_mainfile()
            return
        except RuntimeError as exc:
            if not cls.is_blender_auto_fix_warning(str(exc)):
                raise

            cleaned_count = cls.cleanup_invalid_shapekeys()
            print(f"[ShapeKeyUtils] Retrying save_mainfile after cleaning {cleaned_count} invalid shape keys.")
            bpy.ops.wm.save_mainfile()
