import bpy
import re
import math
import os
from pathlib import Path

class TT_MaterialPreviewItem(bpy.types.PropertyGroup):
    material: bpy.props.PointerProperty(type=bpy.types.Material, name="材质")
    plane_object: bpy.props.StringProperty(name="平面物体名称", default="")
    source_objects: bpy.props.StringProperty(name="源物体名称列表", default="")
    is_visible: bpy.props.BoolProperty(name="显示平面", default=True)
    original_uvs: bpy.props.StringProperty(name="原始UV数据", default="")
    
    def _save_original_uvs(self):
        if not self.source_objects:
            return
        
        source_obj_names = self.source_objects.split("|")
        uv_data = {}
        
        for obj_name in source_obj_names:
            if obj_name not in bpy.data.objects:
                continue
            
            source_obj = bpy.data.objects[obj_name]
            if not source_obj.data or not source_obj.data.uv_layers:
                continue
            
            uv_layer = source_obj.data.uv_layers.active.data
            uv_list = []
            
            for loop in source_obj.data.loops:
                uv = uv_layer[loop.index].uv
                uv_list.append(f"{uv.x},{uv.y}")
            
            uv_data[obj_name] = ";".join(uv_list)
        
        self.original_uvs = "|".join([f"{name}:{uv_data[name]}" for name in uv_data.keys()])
    
    def _restore_original_uvs(self):
        if not self.source_objects or not self.original_uvs:
            return
        
        source_obj_names = self.source_objects.split("|")
        original_uv_parts = self.original_uvs.split("|")
        
        for uv_part in original_uv_parts:
            if ":" not in uv_part:
                continue
            
            obj_name, uv_data = uv_part.split(":", 1)
            
            if obj_name not in bpy.data.objects:
                continue
            
            source_obj = bpy.data.objects[obj_name]
            if not source_obj.data or not source_obj.data.uv_layers:
                continue
            
            uv_layer = source_obj.data.uv_layers.active.data
            original_uv_list = uv_data.split(";")
            
            for i, loop in enumerate(source_obj.data.loops):
                if i >= len(original_uv_list):
                    break
                
                uv_str = original_uv_list[i]
                uv_parts = uv_str.split(",")
                if len(uv_parts) != 2:
                    continue
                
                uv_u = float(uv_parts[0])
                uv_v = float(uv_parts[1])
                
                uv_layer[loop.index].uv = (uv_u, uv_v)


def _calculate_bounds(items):
    min_x = float('inf')
    max_x = float('-inf')
    min_y = float('inf')
    max_y = float('-inf')
    
    for item in items:
        if not item.plane_object or item.plane_object not in bpy.data.objects:
            continue
        
        plane = bpy.data.objects[item.plane_object]
        scale = plane.scale.x
        
        half_size = 0.5 * scale
        left = plane.location.x - half_size
        right = plane.location.x + half_size
        bottom = plane.location.y - half_size
        top = plane.location.y + half_size
        
        min_x = min(min_x, left)
        max_x = max(max_x, right)
        min_y = min(min_y, bottom)
        max_y = max(max_y, top)
    
    if min_x == float('inf'):
        return None
    
    total_width = max_x - min_x
    total_height = max_y - min_y
    
    return {
        'min_x': min_x,
        'max_x': max_x,
        'min_y': min_y,
        'max_y': max_y,
        'total_width': total_width,
        'total_height': total_height
    }

def _update_source_uv(item, bounds):
    if not item.source_objects:
        return
    
    if not item.plane_object or item.plane_object not in bpy.data.objects:
        return
    
    if not item.original_uvs:
        item._save_original_uvs()
    
    if not bounds or bounds['total_width'] == 0 or bounds['total_height'] == 0:
        return
    
    plane = bpy.data.objects[item.plane_object]
    scale = plane.scale.x
    
    half_size = 0.5 * scale
    left = plane.location.x - half_size
    right = plane.location.x + half_size
    bottom = plane.location.y - half_size
    top = plane.location.y + half_size
    
    uv_min_x = (left - bounds['min_x']) / bounds['total_width']
    uv_max_x = (right - bounds['min_x']) / bounds['total_width']
    uv_min_y = (bottom - bounds['min_y']) / bounds['total_height']
    uv_max_y = (top - bounds['min_y']) / bounds['total_height']
    
    uv_width = uv_max_x - uv_min_x
    uv_height = uv_max_y - uv_min_y
    
    source_obj_names = item.source_objects.split("|")
    original_uv_parts = item.original_uvs.split("|")
    
    for uv_part in original_uv_parts:
        if ":" not in uv_part:
            continue
        
        obj_name, uv_data = uv_part.split(":", 1)
        
        if obj_name not in bpy.data.objects:
            continue
        
        source_obj = bpy.data.objects[obj_name]
        if not source_obj.data or not source_obj.data.uv_layers:
            continue
        
        uv_layer = source_obj.data.uv_layers.active.data
        original_uv_list = uv_data.split(";")
        
        for i, loop in enumerate(source_obj.data.loops):
            if i >= len(original_uv_list):
                break
            
            uv_str = original_uv_list[i]
            uv_parts = uv_str.split(",")
            if len(uv_parts) != 2:
                continue
            
            uv_u = float(uv_parts[0])
            uv_v = float(uv_parts[1])
            
            uv_u = uv_min_x + uv_u * uv_width
            uv_v = uv_min_y + uv_v * uv_height
            
            uv_layer[loop.index].uv = (uv_u, uv_v)

def _update_all_source_uvs(context):
    if not hasattr(context.scene, 'material_preview_list'):
        return
    
    items = context.scene.material_preview_list
    if not items:
        return
    
    bounds = _calculate_bounds(items)
    
    for item in items:
        _update_source_uv(item, bounds)

class TT_OT_refresh_materials(bpy.types.Operator):
    bl_idname = "toolkit.tt_refresh_materials"
    bl_label = "刷新材质列表"
    bl_description = "根据正则表达式匹配选中物体的材质并创建预览平面"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        pattern = props.material_preview_pattern
        
        try:
            regex = re.compile(pattern)
        except re.error:
            self.report({'ERROR'}, "无效的正则表达式")
            return {'CANCELLED'}
        
        context.scene.material_preview_list.clear()
        
        selected_objects = context.selected_objects
        matched_materials = set()
        material_to_objects = {}
        
        for obj in selected_objects:
            if obj.type != 'MESH':
                continue
            
            for mat_slot in obj.material_slots:
                mat = mat_slot.material
                if not mat:
                    continue
                
                if regex.search(mat.name):
                    matched_materials.add(mat)
                    if mat not in material_to_objects:
                        material_to_objects[mat] = []
                    material_to_objects[mat].append(obj)
        
        if not matched_materials:
            self.report({'WARNING'}, "未找到匹配的材质")
            return {'CANCELLED'}
        
        context.scene.material_preview_list.clear()
        
        spacing = 1.2
        start_x = -((len(matched_materials) - 1) * spacing) / 2
        
        for i, material in enumerate(sorted(matched_materials, key=lambda m: m.name)):
            item = context.scene.material_preview_list.add()
            item.material = material
            item.plane_object = f"_Preview_{material.name}"
            item.is_visible = True
            
            source_objs = material_to_objects.get(material, [])
            if source_objs:
                item.source_objects = "|".join([obj.name for obj in source_objs])
        
        self._create_planes(context)
        self.report({'INFO'}, f"已找到 {len(matched_materials)} 个材质并创建预览平面")
        return {'FINISHED'}
    
    def _create_planes(self, context):
        for item in context.scene.material_preview_list:
            if item.plane_object in bpy.data.objects:
                bpy.data.objects.remove(bpy.data.objects[item.plane_object])
        
        spacing = 1.2
        start_x = -((len(context.scene.material_preview_list) - 1) * spacing) / 2
        
        for i, item in enumerate(context.scene.material_preview_list):
            bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, 0))
            plane = context.active_object
            plane.name = item.plane_object
            
            if plane.data.materials:
                plane.data.materials[0] = item.material
            else:
                plane.data.materials.append(item.material)
            
            plane.location = (start_x + i * spacing, 0.0, 0.0)
            plane.scale = (1.0, 1.0, 1.0)
            
            if item.source_objects:
                source_obj_names = item.source_objects.split("|")
                for obj_name in source_obj_names:
                    if obj_name in bpy.data.objects:
                        bpy.data.objects[obj_name].hide_set(True)
        
        _update_all_source_uvs(context)


class TT_OT_clear_all_previews(bpy.types.Operator):
    bl_idname = "toolkit.tt_clear_all_previews"
    bl_label = "清除所有预览"
    bl_description = "清除所有材质预览平面"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        for item in context.scene.material_preview_list:
            if item.source_objects:
                item._restore_original_uvs()
            if item.plane_object in bpy.data.objects:
                bpy.data.objects.remove(bpy.data.objects[item.plane_object])
            if item.source_objects:
                source_obj_names = item.source_objects.split("|")
                for obj_name in source_obj_names:
                    if obj_name in bpy.data.objects:
                        bpy.data.objects[obj_name].hide_set(False)
        
        context.scene.material_preview_list.clear()
        self.report({'INFO'}, "已清除所有材质预览")
        return {'FINISHED'}


class TT_OT_toggle_visibility(bpy.types.Operator):
    bl_idname = "toolkit.tt_toggle_visibility"
    bl_label = "切换可见性"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        items = context.scene.material_preview_list
        if self.index < 0 or self.index >= len(items):
            return {'CANCELLED'}
        
        item = items[self.index]
        item.is_visible = not item.is_visible
        
        if item.plane_object:
            plane = bpy.data.objects.get(item.plane_object)
            if plane:
                plane.hide_set(not item.is_visible)
        
        return {'FINISHED'}


class TT_OT_select_plane(bpy.types.Operator):
    bl_idname = "toolkit.tt_select_plane"
    bl_label = "选中平面"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        items = context.scene.material_preview_list
        if self.index < 0 or self.index >= len(items):
            return {'CANCELLED'}
        
        item = items[self.index]
        
        if item.plane_object:
            plane = bpy.data.objects.get(item.plane_object)
            if plane:
                bpy.ops.object.select_all(action='DESELECT')
                plane.select_set(True)
                context.view_layer.objects.active = plane
        
        return {'FINISHED'}


class TT_OT_update_from_planes(bpy.types.Operator):
    bl_idname = "toolkit.tt_update_from_planes"
    bl_label = "从平面更新UV"
    bl_description = "根据平面的实际位置和缩放更新UV"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        _update_all_source_uvs(context)
        return {'FINISHED'}


class TT_OT_bake_atlas(bpy.types.Operator):
    bl_idname = "toolkit.tt_bake_atlas"
    bl_label = "烘焙贴图集"
    bl_description = "将所有材质平面烘焙为一张贴图集"
    bl_options = {'REGISTER', 'UNDO'}
    
    output_path: bpy.props.StringProperty(name="输出路径", description="贴图集输出路径", subtype='FILE_PATH')
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        if not props.output_dir:
            self.report({'ERROR'}, "请先设置输出目录")
            return {'CANCELLED'}
        
        if not hasattr(context.scene, 'material_preview_list'):
            self.report({'ERROR'}, "没有材质预览数据")
            return {'CANCELLED'}
        
        items = context.scene.material_preview_list
        if not items:
            self.report({'ERROR'}, "没有材质预览平面")
            return {'CANCELLED'}
        
        output_dir = bpy.path.abspath(props.output_dir)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        if not self.output_path:
            self.output_path = os.path.join(output_dir, "MaterialAtlas.png")
        
        base_resolution = props.material_preview_base_resolution
        bounds = _calculate_bounds(items)
        
        if not bounds:
            self.report({'ERROR'}, "无法计算边界框")
            return {'CANCELLED'}
        
        total_width_uv = bounds['total_width']
        total_height_uv = bounds['total_height']
        
        atlas_width = int(base_resolution * total_width_uv)
        atlas_height = int(base_resolution * total_height_uv)
        
        self.report({'INFO'}, f"贴图集分辨率: {atlas_width}x{atlas_height}")
        
        if atlas_width >= 4096 or atlas_height >= 4096:
            self.report({'INFO'}, f"分辨率达到4096，将分块烘焙 ({atlas_width}x{atlas_height})")
            success = self._bake_tiled(context, items, bounds, base_resolution, atlas_width, atlas_height, self.output_path)
        else:
            success = self._bake_single(context, items, bounds, base_resolution, atlas_width, atlas_height, self.output_path)
        
        if success:
            self.report({'INFO'}, f"贴图集已保存到: {self.output_path}")
        
        return {'FINISHED'}
    
    def _bake_single(self, context, items, bounds, base_resolution, atlas_width, atlas_height, output_path):
        original_scene = context.scene
        original_engine = original_scene.render.engine
        original_world = original_scene.world
        
        before_data = {
            'scenes': set(bpy.data.scenes),
            'objects': set(bpy.data.objects),
            'meshes': set(bpy.data.meshes),
            'cameras': set(bpy.data.cameras),
            'lights': set(bpy.data.lights)
        }
        
        try:
            if "TempAtlasBake_Scene" in bpy.data.scenes:
                bpy.data.scenes.remove(bpy.data.scenes["TempAtlasBake_Scene"])
            
            temp_scene = bpy.data.scenes.new("TempAtlasBake_Scene")
            context.window.scene = temp_scene
            temp_scene.world = original_world
            temp_scene.render.engine = 'BLENDER_EEVEE_NEXT' if (4, 1, 0) <= bpy.app.version else 'BLENDER_EEVEE'
            temp_scene.eevee.taa_render_samples = 64
            temp_scene.view_settings.view_transform = 'Standard'
            
            center_x = (bounds['min_x'] + bounds['max_x']) / 2
            center_y = (bounds['min_y'] + bounds['max_y']) / 2
            center_z = 0
            camera_distance = 5
            
            bpy.ops.object.camera_add(location=(center_x, center_y, center_z + camera_distance), rotation=(0, 0, 0))
            camera = context.active_object
            camera.name = "Bake_Camera"
            camera.data.type = 'ORTHO'
            camera.data.ortho_scale = max(atlas_width, atlas_height) / base_resolution
            temp_scene.camera = camera
            
            temp_scene.render.resolution_x = atlas_width
            temp_scene.render.resolution_y = atlas_height
            temp_scene.render.film_transparent = True
            temp_scene.render.image_settings.file_format = 'PNG'
            temp_scene.render.filepath = output_path
            
            for item in items:
                if not item.plane_object:
                    continue
                
                if item.plane_object not in original_scene.objects:
                    continue
                
                if not item.material:
                    continue
                
                plane = original_scene.objects[item.plane_object]
                plane_copy = plane.copy()
                plane_copy.data = plane.data.copy()
                plane_copy.animation_data_clear()
                plane_copy.name = f"Bake_{item.material.name}"
                
                for slot in plane_copy.material_slots:
                    if slot.material:
                        slot.material = item.material
                
                context.collection.objects.link(plane_copy)
                plane_copy.select_set(True)
                context.view_layer.objects.active = plane_copy
            
            bpy.ops.render.render(write_still=True)
            
            return True
        
        except Exception as e:
            self.report({'ERROR'}, f"烘焙失败: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        finally:
            context.window.scene = original_scene
            original_scene.render.engine = original_engine
            
            for data_type, old_items in before_data.items():
                current_items = set(getattr(bpy.data, data_type))
                new_items = current_items - old_items
                for item in new_items:
                    try:
                        getattr(bpy.data, data_type).remove(item, do_unlink=True)
                    except:
                        pass
    
    def _bake_tiled(self, context, items, bounds, base_resolution, atlas_width, atlas_height, output_path):
        original_scene = context.scene
        original_engine = original_scene.render.engine
        original_world = original_scene.world
        
        before_data = {
            'scenes': set(bpy.data.scenes),
            'objects': set(bpy.data.objects),
            'meshes': set(bpy.data.meshes),
            'cameras': set(bpy.data.cameras),
            'lights': set(bpy.data.lights)
        }
        
        try:
            if "TempAtlasBake_Scene" in bpy.data.scenes:
                bpy.data.scenes.remove(bpy.data.scenes["TempAtlasBake_Scene"])
            
            temp_scene = bpy.data.scenes.new("TempAtlasBake_Scene")
            context.window.scene = temp_scene
            temp_scene.world = original_world
            temp_scene.render.engine = 'BLENDER_EEVEE_NEXT' if (4, 1, 0) <= bpy.app.version else 'BLENDER_EEVEE'
            temp_scene.eevee.taa_render_samples = 64
            temp_scene.view_settings.view_transform = 'Standard'
            
            tile_size = 2048
            tiles_x = math.ceil(atlas_width / tile_size)
            tiles_y = math.ceil(atlas_height / tile_size)
            
            self.report({'INFO'}, f"分块烘焙: {tiles_x}x{tiles_y} = {tiles_x * tiles_y} 块")
            
            temp_images = []
            
            for tile_y in range(tiles_y):
                for tile_x in range(tiles_x):
                    tile_index = tile_y * tiles_x + tile_x
                    
                    tile_width = min(tile_size, atlas_width - tile_x * tile_size)
                    tile_height = min(tile_size, atlas_height - tile_y * tile_size)
                    
                    tile_offset_x = bounds['min_x'] + tile_x * (tile_size / base_resolution)
                    tile_offset_y = bounds['min_y'] + tile_y * (tile_size / base_resolution)
                    
                    tile_center_x = tile_offset_x + tile_width / (2 * base_resolution)
                    tile_center_y = tile_offset_y + tile_height / (2 * base_resolution)
                    camera_distance = 5
                    
                    bpy.ops.object.camera_add(location=(tile_center_x, tile_center_y, camera_distance), rotation=(0, 0, 0))
                    camera = context.active_object
                    camera.name = f"Tile_Camera_{tile_index}"
                    camera.data.type = 'ORTHO'
                    camera.data.ortho_scale = max(tile_width, tile_height) / base_resolution
                    temp_scene.camera = camera
                    
                    temp_scene.render.resolution_x = tile_width
                    temp_scene.render.resolution_y = tile_height
                    temp_scene.render.film_transparent = True
                    temp_scene.render.image_settings.file_format = 'PNG'
                    
                    temp_path = os.path.join(os.path.dirname(output_path), f"temp_tile_{tile_index}.png")
                    temp_scene.render.filepath = temp_path
                    
                    for item in items:
                        if not item.plane_object:
                            continue
                        
                        if item.plane_object not in original_scene.objects:
                            continue
                        
                        if not item.material:
                            continue
                        
                        plane = original_scene.objects[item.plane_object]
                        plane_copy = plane.copy()
                        plane_copy.data = plane.data.copy()
                        plane_copy.animation_data_clear()
                        plane_copy.name = f"Bake_{item.material.name}_Tile_{tile_index}"
                        
                        for slot in plane_copy.material_slots:
                            if slot.material:
                                slot.material = item.material
                        
                        context.collection.objects.link(plane_copy)
                        plane_copy.select_set(True)
                        context.view_layer.objects.active = plane_copy
                    
                    bpy.ops.render.render(write_still=True)
                    
                    temp_images.append({
                        'path': temp_path,
                        'x': tile_x * tile_size,
                        'y': tile_y * tile_size,
                        'width': tile_width,
                        'height': tile_height
                    })
                    
                    bpy.ops.object.select_all(action='DESELECT')
                    for obj in list(context.collection.objects):
                        if obj.name.startswith('Bake_') or obj.name.startswith('Tile_Camera_'):
                            try:
                                bpy.data.objects.remove(obj, do_unlink=True)
                            except:
                                pass
            
            self._merge_tiles(temp_images, atlas_width, atlas_height, output_path)
            
            return True
        
        except Exception as e:
            self.report({'ERROR'}, f"分块烘焙失败: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        finally:
            context.window.scene = original_scene
            original_scene.render.engine = original_engine
            
            for data_type, old_items in before_data.items():
                current_items = set(getattr(bpy.data, data_type))
                new_items = current_items - old_items
                for item in new_items:
                    try:
                        getattr(bpy.data, data_type).remove(item, do_unlink=True)
                    except:
                        pass
    
    def _merge_tiles(self, tiles, atlas_width, atlas_height, output_path):
        try:
            import numpy as np
            
            atlas_array = np.zeros((atlas_height, atlas_width, 4), dtype=np.uint8)
            
            for tile in tiles:
                tile_img = bpy.data.images.load(tile['path'], check_existing=True)
                tile_array = np.array(tile_img.pixels)
                tile_array = tile_array.reshape((tile['height'], tile['width'], 4))
                tile_array = (tile_array * 255).astype(np.uint8)
                
                y_end = min(tile['y'] + tile['height'], atlas_height)
                x_end = min(tile['x'] + tile['width'], atlas_width)
                
                atlas_array[tile['y']:y_end, tile['x']:x_end] = tile_array
                
                try:
                    bpy.data.images.remove(tile_img)
                except:
                    pass
                
                try:
                    os.remove(tile['path'])
                except:
                    pass
            
            atlas_image = bpy.data.images.new(os.path.basename(output_path), atlas_width, atlas_height, alpha=True)
            atlas_array = atlas_array.astype(np.float32) / 255.0
            atlas_image.pixels = atlas_array.flatten()
            atlas_image.filepath_raw = output_path
            atlas_image.save()
            
            return True
        
        except ImportError:
            self.report({'ERROR'}, "需要 numpy 库来合并分块贴图")
            return False
        except Exception as e:
            self.report({'ERROR'}, f"合并贴图失败: {e}")
            import traceback
            traceback.print_exc()
            return False


tt_material_preview_list = (
    TT_MaterialPreviewItem,
    TT_OT_refresh_materials,
    TT_OT_clear_all_previews,
    TT_OT_toggle_visibility,
    TT_OT_select_plane,
    TT_OT_update_from_planes,
    TT_OT_bake_atlas,
)
