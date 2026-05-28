from __future__ import annotations

import bpy

from ..common.vg_test_core import VGTestError


class TOOLKIT_OT_VGTestUnifyNumeric(bpy.types.Operator):
    bl_idname = "toolkit.vgtest_unify_numeric"
    bl_label = "统一顶点组"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from ..blueprint.vg_test_runtime import VGTestRuntime

        try:
            text_name, object_count = VGTestRuntime.unify_selected_objects(context)
        except VGTestError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Unified numeric vertex groups for {object_count} object(s). Mapping: {text_name}")
        return {'FINISHED'}


class TOOLKIT_OT_VGTestRestoreLocalNumeric(bpy.types.Operator):
    bl_idname = "toolkit.vgtest_restore_local_numeric"
    bl_label = "恢复局部顶点组"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from ..blueprint.vg_test_runtime import VGTestRuntime

        try:
            text_name, object_count = VGTestRuntime.restore_selected_objects(context)
        except VGTestError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Restored local numeric groups for {object_count} object(s). Mapping: {text_name}")
        return {'FINISHED'}


class TOOLKIT_OT_VGTestSplitPreview(bpy.types.Operator):
    bl_idname = "toolkit.vgtest_split_preview"
    bl_label = "手动切割预览"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from ..blueprint.vg_test_runtime import VGTestRuntime

        try:
            created_names = VGTestRuntime.split_objects_for_preview(context)
        except VGTestError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Created {len(created_names)} VG Test preview object(s).")
        return {'FINISHED'}


vg_test_operators = [
    TOOLKIT_OT_VGTestUnifyNumeric,
    TOOLKIT_OT_VGTestRestoreLocalNumeric,
    TOOLKIT_OT_VGTestSplitPreview,
]
