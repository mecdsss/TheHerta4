import bpy
import subprocess
import sys
import threading

from .tt_texture_atlas_core import (
    AtlasError,
    build_refresh_entries,
    generate_texture_atlas,
    is_pillow_available,
)


class TT_UL_AtlasMaterials(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0, flt_flag=0):
        row = layout.row(align=True)
        row.enabled = not bool(item.skip_reason)
        row.prop(item, "enabled", text="")
        if item.material:
            row.label(text=item.material.name, icon='MATERIAL')
        else:
            row.label(text="材质缺失", icon='ERROR')
        if item.skip_reason:
            row.label(text="已跳过", icon='ERROR')


class TT_OT_atlas_refresh_materials(bpy.types.Operator):
    bl_idname = "toolkit.tt_atlas_refresh_materials"
    bl_label = "刷新 Atlas 材质"
    bl_description = "从当前选中的网格物体刷新可参与 Atlas 的材质列表"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.texture_tools_props
        entries, warnings = build_refresh_entries(context, props)
        props.atlas_materials.clear()

        for entry in entries:
            item = props.atlas_materials.add()
            item.material = entry["material"]
            item.source_objects = entry["source_objects"]
            item.enabled = entry["enabled"] and not entry["skip_reason"]
            item.skip_reason = entry["skip_reason"]

        if not entries:
            self.report({'WARNING'}, "没有在当前选中网格物体上找到可用材质")
            return {'CANCELLED'}

        props.atlas_material_index = min(max(props.atlas_material_index, 0), len(props.atlas_materials) - 1)
        for warning in warnings[:8]:
            self.report({'WARNING'}, warning)
        self.report({'INFO'}, f"已刷新 {len(entries)} 个材质")
        return {'FINISHED'}


class TT_OT_atlas_select_all_materials(bpy.types.Operator):
    bl_idname = "toolkit.tt_atlas_select_all_materials"
    bl_label = "全选 Atlas 材质"
    bl_description = "选中所有未被标记为跳过的 Atlas 材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        for item in context.scene.texture_tools_props.atlas_materials:
            item.enabled = not bool(item.skip_reason)
        return {'FINISHED'}


class TT_OT_atlas_select_no_materials(bpy.types.Operator):
    bl_idname = "toolkit.tt_atlas_select_no_materials"
    bl_label = "全不选 Atlas 材质"
    bl_description = "取消选择所有 Atlas 材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        for item in context.scene.texture_tools_props.atlas_materials:
            item.enabled = False
        return {'FINISHED'}


class TT_OT_atlas_generate(bpy.types.Operator):
    bl_idname = "toolkit.tt_atlas_generate"
    bl_label = "生成 Atlas"
    bl_description = "将选中的材质合并为 Atlas，并把对应面片重定向到新材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.texture_tools_props
        if not props.output_dir:
            self.report({'ERROR'}, "请先设置输出目录")
            return {'CANCELLED'}

        try:
            result = generate_texture_atlas(context, props)
        except AtlasError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Atlas 生成失败: {exc}")
            raise

        for warning in result["warnings"][:8]:
            self.report({'WARNING'}, warning)
        atlas_width, atlas_height = result["atlas_size"]
        self.report({'INFO'}, f"已生成 Atlas {atlas_width}x{atlas_height}，处理 {result['material_count']} 个材质")
        return {'FINISHED'}


class TT_OT_ensure_pillow(bpy.types.Operator):
    bl_idname = "toolkit.tt_ensure_pillow"
    bl_label = "安装 Pillow"
    bl_description = "安装纹理 Atlas 所需的 Pillow 依赖"

    _timer = None
    _finished = False
    _success = False
    _message = ""

    def _install(self):
        try:
            command = [sys.executable, "-m", "pip", "install", "--user", "--upgrade", "--no-cache-dir", "Pillow"]
            subprocess.run(command, capture_output=True, text=True, check=True, encoding="utf-8", errors="ignore")
            self._success = True
            self._message = "Pillow 安装完成。请重启 Blender 后再使用 Atlas 功能。"
        except Exception as exc:
            stderr = getattr(exc, "stderr", "") or str(exc)
            self._success = False
            self._message = f"Pillow 安装失败: {stderr}"
        self._finished = True

    def modal(self, context, event):
        if event.type == 'TIMER' and self._finished:
            context.window_manager.event_timer_remove(self._timer)
            self._show_popup()
            return {'FINISHED'}
        return {'PASS_THROUGH'}

    def execute(self, context):
        if is_pillow_available():
            self.report({'INFO'}, "Pillow 已安装")
            return {'FINISHED'}

        self._finished = False
        self._success = False
        self._message = ""
        threading.Thread(target=self._install, daemon=True).start()
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, "开始安装 Pillow，请稍候")
        return {'RUNNING_MODAL'}

    def _show_popup(self):
        message = self._message

        def draw(menu, context):
            for line in message.splitlines():
                menu.layout.label(text=line)

        bpy.context.window_manager.popup_menu(draw, title="Pillow 安装结果", icon='INFO' if self._success else 'ERROR')


tt_texture_atlas_list = (
    TT_UL_AtlasMaterials,
    TT_OT_atlas_refresh_materials,
    TT_OT_atlas_select_all_materials,
    TT_OT_atlas_select_no_materials,
    TT_OT_atlas_generate,
    TT_OT_ensure_pillow,
)
