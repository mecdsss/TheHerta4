import bpy
import os
import re
import traceback
import bmesh
from pathlib import Path

BAKE_RESOLUTION_DEFAULT_RULES = [
    {"pattern": r"^DiffuseMap_high", "resolution": 4096, "enabled": True},
    {"pattern": r"^DiffuseMap", "resolution": 2048, "enabled": True},
    {"pattern": r"^NormalMap", "resolution": 2048, "enabled": True},
    {"pattern": r"^MaterialMap", "resolution": 1024, "enabled": True},
    {"pattern": r"^LightMap", "resolution": 1024, "enabled": True},
]


def _pick_preview_render_engine():
    engine_ids = {item.identifier for item in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
    for engine_id in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE', 'CYCLES'):
        if engine_id in engine_ids:
            return engine_id
    return bpy.context.scene.render.engine


def _configure_bake_render_scene(temp_scene, original_scene):
    temp_scene.world = original_scene.world
    temp_scene.render.engine = _pick_preview_render_engine()
    if hasattr(temp_scene, "eevee"):
        temp_scene.eevee.taa_render_samples = 64
        # 新建场景默认开启 Bloom；Emission 白色区域（alpha=1）会被辉光逐级模糊，
        # 溢出到本应为 0 的区域形成阶梯状光晕，破坏 alpha 灰度读数（对颜色 pass 同样有害）
        if hasattr(temp_scene.eevee, "use_bloom"):
            temp_scene.eevee.use_bloom = False
    temp_scene.view_settings.view_transform = 'Standard'
    # Alpha pass 把 ShaderToRGB 的 Alpha 当作颜色渲染再读回做合成，
    # 若背景不透明，世界底色（默认 0.051 灰）会在所有未覆盖区域留下约 5% 的淡残留
    temp_scene.render.film_transparent = True


def _configure_alpha_pass_scene(temp_scene):
    # Alpha pass 以灰度颜色读出 ShaderToRGB 的 Alpha，必须叠在不透明黑底上。
    # 若胶片透明，轮廓抗锯齿边缘的半透明像素在存 PNG（预乘→直通）时会被还原成纯白，
    # 读回 R 通道变成 1.0，在 UV 岛外轮廓留下一圈不透明亮环；胶片不透明时无此转换失真
    temp_scene.render.film_transparent = False
    temp_scene.world = None


def _link_spec(link):
    return (
        link.from_node.name, link.from_socket.identifier,
        link.to_node.name, link.to_socket.identifier,
    )


def _snapshot_link_specs(node_tree):
    return {_link_spec(link) for link in node_tree.links}


def _restore_node_tree(node_tree, saved_node_names, saved_link_specs):
    """恢复节点树到保存时的拓扑：删除新增节点/连线，补回缺失的原始连线。

    只按节点名与 socket identifier 操作，绝不持有节点/连线的 Python 包装对象跨变更使用：
    连线被 links.remove() 后其包装对象即成悬垂引用，再访问 from_socket 会读已释放内存，
    在批量处理时以 EXCEPTION_ACCESS_VIOLATION 闪退（try/except 拦不住 native 崩溃）。
    """
    for node in list(node_tree.nodes):
        if node.name not in saved_node_names:
            try:
                node_tree.nodes.remove(node)
            except Exception:
                pass

    for link in list(node_tree.links):
        if _link_spec(link) not in saved_link_specs:
            try:
                node_tree.links.remove(link)
            except Exception:
                pass

    live_specs = _snapshot_link_specs(node_tree)
    for from_name, from_sock_id, to_name, to_sock_id in saved_link_specs - live_specs:
        from_node = node_tree.nodes.get(from_name)
        to_node = node_tree.nodes.get(to_name)
        if from_node is None or to_node is None:
            continue
        from_socket = next((s for s in from_node.outputs if s.identifier == from_sock_id), None)
        to_socket = next((s for s in to_node.inputs if s.identifier == to_sock_id), None)
        if from_socket is None or to_socket is None:
            continue
        try:
            node_tree.links.new(from_socket, to_socket)
        except Exception:
            pass


def _linear_to_srgb(value):
    value = min(max(value, 0.0), 1.0)
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * (value ** (1.0 / 2.4)) - 0.055


def _reserve_material_output_path(output_dir, material_name, used_paths):
    """为本次批处理分配稳定且不冲突的 PNG 路径。"""
    stem = "".join(
        char for char in str(material_name or "")
        if char.isalnum() or char in ("-", "_", ".")
    ).rstrip(" .") or "Material"
    suffix = 1
    while True:
        filename = f"{stem}.png" if suffix == 1 else f"{stem}_{suffix}.png"
        output_path = os.path.join(output_dir, filename)
        collision_key = os.path.abspath(output_path).casefold()
        if collision_key not in used_paths:
            used_paths.add(collision_key)
            return output_path
        suffix += 1


def _find_active_output_node(node_tree):
    return next(
        (n for n in node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and getattr(n, 'is_active_output', False)),
        next((n for n in node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None),
    )


def _find_last_mix_shader(node_tree):
    mix_shaders = [n for n in node_tree.nodes if n.type == 'MIX_SHADER']
    return mix_shaders[-1] if mix_shaders else None


def _find_first_image_texture_upstream(output_node):
    if output_node is None:
        return None

    surface_input = output_node.inputs.get('Surface')
    if not surface_input or not getattr(surface_input, 'is_linked', False):
        return None

    visited = set()
    queue = [link.from_node for link in surface_input.links if getattr(link, 'from_node', None) is not None]
    while queue:
        node = queue.pop(0)
        node_key = id(node)
        if node_key in visited:
            continue
        visited.add(node_key)
        if getattr(node, 'type', '') == 'TEX_IMAGE' and getattr(node, 'image', None):
            return node
        for input_socket in getattr(node, 'inputs', []):
            if getattr(input_socket, 'is_linked', False):
                queue.extend(
                    link.from_node
                    for link in getattr(input_socket, 'links', [])
                    if getattr(link, 'from_node', None) is not None
                )
    return None


class TT_OT_add_bake_resolution_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_add_bake_resolution_rule"
    bl_label = "添加烘焙分辨率规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        rule = props.bake_resolution_rules.add()
        rule.pattern = ".*"
        rule.resolution = 2048
        rule.enabled = True
        return {'FINISHED'}


class TT_OT_remove_bake_resolution_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_remove_bake_resolution_rule"
    bl_label = "移除烘焙分辨率规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        props.bake_resolution_rules.remove(self.index)
        return {'FINISHED'}


class TT_OT_reset_bake_resolution_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_reset_bake_resolution_rules"
    bl_label = "重置烘焙分辨率规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        props.bake_resolution_rules.clear()
        
        for rule_data in BAKE_RESOLUTION_DEFAULT_RULES:
            rule = props.bake_resolution_rules.add()
            rule.pattern = rule_data["pattern"]
            rule.resolution = rule_data["resolution"]
            rule.enabled = rule_data["enabled"]
        
        return {'FINISHED'}


class TT_OT_bake_color_maps(bpy.types.Operator):
    bl_idname = "toolkit.tt_bake_color_maps"
    bl_label = "烘焙颜色贴图"
    bl_description = "将选中物体的材质颜色烘焙为贴图"
    bl_options = {'REGISTER', 'UNDO'}
    
    def has_complex_nodes(self, material, node_types):
        if not material or not material.use_nodes:
            return False
        output_node = next((n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and getattr(n, 'is_active_output', False)), None)
        if not output_node:
            output_node = next((n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if not output_node:
            return False
        check_shader = 'MIX_SHADER' in node_types or 'COMPLEX' in node_types
        check_color = 'MIX_COLOR' in node_types or 'COMPLEX' in node_types
        nodes_to_visit = {link.from_node for inp in output_node.inputs if inp.is_linked for link in inp.links}
        visited_nodes = {output_node}
        while nodes_to_visit:
            node = nodes_to_visit.pop()
            if node in visited_nodes:
                continue
            visited_nodes.add(node)
            if check_shader and node.type == 'MIX_SHADER':
                return True
            if check_color and (node.type == 'MIX_RGB' or (node.type == 'MIX' and getattr(node, 'data_type', '') == 'RGBA')):
                return True
            for inp in node.inputs:
                if inp.is_linked:
                    nodes_to_visit.update(link.from_node for link in inp.links if link.from_node not in visited_nodes)
        return False
    
    def unfold_mesh_by_uv(self, obj):
        """按照UV坐标展开网格顶点位置"""
        if obj.type != 'MESH':
            return None, None, None
        
        mesh = obj.data
        if not mesh.uv_layers.active:
            return None, None, None
        
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.transform(obj.matrix_world)
        bm.faces.ensure_lookup_table()
        
        uv_layer = bm.loops.layers.uv.verify()
        
        original_positions = {}
        for vert in bm.verts:
            original_positions[vert.index] = vert.co.copy()
        
        scale = 10.0
        for face in bm.faces:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                loop.vert.co = (uv[0] * scale, uv[1] * scale, 0.0)
        
        new_mesh = bpy.data.meshes.new(f"{obj.name}_unfolded")
        bm.to_mesh(new_mesh)
        bm.free()
        
        return new_mesh, original_positions, None
    
    def render_material_preview(self, material, output_path, preview_type, size, unfold_by_uv=False, source_obj=None):
        """Render the material preview to an RGBA PNG.

        If the material graph contains a Mix Shader, use the dual-pass preview path
        and temporarily toggle only the first image texture found upstream of the
        active Surface output.

        If no Mix Shader exists, fall back to a normal single-pass render instead
        of failing.
        """
        original_scene = bpy.context.window.scene
        original_engine = original_scene.render.engine
        before_data = {
            'scenes': set(bpy.data.scenes),
            'objects': set(bpy.data.objects),
            'meshes': set(bpy.data.meshes),
            'cameras': set(bpy.data.cameras),
            'lights': set(bpy.data.lights),
        }

        created_images = []
        created_files = []
        rgb_path = output_path + ".rgb.tmp.exr"
        a_path = output_path + ".a.tmp.exr"
        created_files.append(rgb_path)
        created_files.append(a_path)

        node_tree = material.node_tree
        # 只保存可序列化的拓扑规格（节点名 + socket identifier），不保存对象包装引用：
        # 批量处理时悬垂引用会读已释放内存导致 ACCESS_VIOLATION 闪退
        saved_node_names = {node.name for node in node_tree.nodes}
        saved_link_specs = _snapshot_link_specs(node_tree)
        saved_alpha_modes = {}

        try:
            temp_scene = bpy.data.scenes.new("TempMaterialRender_Scene")
            bpy.context.window.scene = temp_scene
            _configure_bake_render_scene(temp_scene, original_scene)

            if source_obj and unfold_by_uv:
                new_mesh, _, _ = self.unfold_mesh_by_uv(source_obj)
                if new_mesh:
                    new_obj = bpy.data.objects.new(f"{source_obj.name}_temp", new_mesh)
                    temp_scene.collection.objects.link(new_obj)
                    new_obj.data.materials.append(material)
                    render_obj = new_obj
                    cam_loc = (5.0, 5.0, 10.0)
                    cam_rot = (0.0, 0.0, 0.0)
                else:
                    render_obj, cam_loc, cam_rot = self._create_preview_primitive(preview_type)
                    for coll in list(render_obj.users_collection):
                        coll.objects.unlink(render_obj)
                    temp_scene.collection.objects.link(render_obj)
                    render_obj.data.materials.append(material)
            else:
                render_obj, cam_loc, cam_rot = self._create_preview_primitive(preview_type)
                for coll in list(render_obj.users_collection):
                    coll.objects.unlink(render_obj)
                temp_scene.collection.objects.link(render_obj)
                render_obj.data.materials.append(material)

            bpy.ops.object.camera_add(location=cam_loc, rotation=cam_rot)
            camera = bpy.context.active_object
            camera.data.type = 'ORTHO'
            if source_obj and unfold_by_uv:
                camera.data.ortho_scale = 10.0
            else:
                camera.data.ortho_scale = 2.0
            temp_scene.camera = camera
            temp_scene.render.resolution_x = size
            temp_scene.render.resolution_y = size
            # 中间 pass 用 EXR（线性浮点）往返：PNG 会把线性值做 sRGB 伽马编码再量化到 8 位，
            # alpha 的平滑灰度渐变经编码抬亮 + 量化后出现台阶状亮带，读回就像白色溢出到黑色区域
            temp_scene.render.image_settings.file_format = 'OPEN_EXR'
            temp_scene.render.image_settings.color_depth = '32'
            temp_scene.render.image_settings.color_mode = 'RGBA'
            # 临时文件不压缩：32 位浮点大图 ZIP 压缩耗时远超磁盘 I/O
            temp_scene.render.image_settings.exr_codec = 'NONE'

            output_node = _find_active_output_node(node_tree)
            if output_node is None:
                raise RuntimeError("Material has no Material Output node.")

            target_mix = _find_last_mix_shader(node_tree)
            primary_tex_node = _find_first_image_texture_upstream(output_node)

            if primary_tex_node and primary_tex_node.image:
                saved_alpha_modes[primary_tex_node.image] = primary_tex_node.image.alpha_mode

            if target_mix is None:
                # 单 pass 直出最终 PNG
                temp_scene.render.image_settings.file_format = 'PNG'
                temp_scene.render.image_settings.color_depth = '8'
                temp_scene.render.filepath = output_path
                bpy.ops.render.render(write_still=True)
                return True

            shader_to_rgb = node_tree.nodes.new('ShaderNodeShaderToRGB')
            shader_to_rgb.location = (target_mix.location.x + 280, target_mix.location.y)

            emission = node_tree.nodes.new('ShaderNodeEmission')
            emission.location = (shader_to_rgb.location.x + 200, shader_to_rgb.location.y)

            for link in list(node_tree.links):
                if link.from_node == target_mix and link.to_node == output_node:
                    node_tree.links.remove(link)
            node_tree.links.new(target_mix.outputs['Shader'], shader_to_rgb.inputs['Shader'])
            node_tree.links.new(emission.outputs['Emission'], output_node.inputs['Surface'])

            if primary_tex_node and primary_tex_node.image:
                primary_tex_node.image.alpha_mode = 'NONE'

            node_tree.links.new(shader_to_rgb.outputs['Color'], emission.inputs['Color'])

            temp_scene.render.filepath = rgb_path
            bpy.ops.render.render(write_still=True)

            if primary_tex_node and primary_tex_node.image:
                primary_tex_node.image.alpha_mode = 'CHANNEL_PACKED'

            for link in list(node_tree.links):
                if link.from_node == shader_to_rgb and link.to_node == emission:
                    node_tree.links.remove(link)
            node_tree.links.new(shader_to_rgb.outputs['Alpha'], emission.inputs['Color'])

            _configure_alpha_pass_scene(temp_scene)
            temp_scene.render.filepath = a_path
            bpy.ops.render.render(write_still=True)

            rgb_image = bpy.data.images.load(rgb_path)
            a_image = bpy.data.images.load(a_path)
            created_images.extend([rgb_image, a_image])

            width, height = rgb_image.size
            final_image = bpy.data.images.new(
                name="TT_ColorBake_Combined",
                width=width,
                height=height,
                alpha=True,
                float_buffer=False,
            )
            created_images.append(final_image)
            self._combine_rgb_alpha(rgb_image, a_image, final_image)
            final_image.filepath_raw = output_path
            final_image.file_format = 'PNG'
            final_image.save()

            return True

        except Exception as e:
            self.report({'ERROR'}, f"渲染预览失败: {e}")
            from ..utils.log_utils import LOG
            LOG.exception(e)
            return False

        finally:
            if node_tree:
                try:
                    _restore_node_tree(node_tree, saved_node_names, saved_link_specs)
                except Exception:
                    pass

            if saved_alpha_modes:
                for img, mode in saved_alpha_modes.items():
                    try:
                        if img and img.alpha_mode != mode:
                            img.alpha_mode = mode
                    except Exception:
                        pass

            bpy.context.window.scene = original_scene
            original_scene.render.engine = original_engine

            for img in created_images:
                try:
                    bpy.data.images.remove(img)
                except Exception:
                    pass

            for file_path in created_files:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception:
                    pass

            for data_type, old_items in before_data.items():
                current_items = set(getattr(bpy.data, data_type))
                new_items = current_items - old_items
                for item in new_items:
                    try:
                        getattr(bpy.data, data_type).remove(item, do_unlink=True)
                    except Exception:
                        pass

    @staticmethod
    def _combine_rgb_alpha(rgb_image, a_image, final_image):
        """RGB 取颜色 pass、Alpha 取 alpha pass 的 R 通道，合成进 final_image。

        EXR/浮点图 pixels 为线性且预乘 alpha；字节图 pixels 为原始编码值不做转换，
        因此需手动反预乘并做线性→sRGB 编码，否则最终 PNG 整体变暗、边缘有暗环。
        （旧 PNG 中间图路径是 sRGB 字节原样往返，碰巧等效于已编码。）
        批量 foreach_get/foreach_set + numpy 向量化，避免逐像素 Python 循环。
        """
        rgb_size = tuple(rgb_image.size)
        alpha_size = tuple(a_image.size)
        final_size = tuple(final_image.size)
        if rgb_size != alpha_size or rgb_size != final_size:
            raise ValueError(
                "RGB、Alpha 与输出图像尺寸必须一致："
                f"RGB={rgb_size}, Alpha={alpha_size}, Output={final_size}"
            )

        width, height = rgb_size
        num_pixels = width * height

        try:
            import numpy as np
        except ImportError:
            np = None

        if np is not None:
            rgb = np.empty(num_pixels * 4, dtype=np.float32)
            rgb_image.pixels.foreach_get(rgb)
            rgb = rgb.reshape(num_pixels, 4)
            alpha = np.empty(num_pixels * 4, dtype=np.float32)
            a_image.pixels.foreach_get(alpha)

            coverage = rgb[:, 3:4]
            safe_coverage = np.where(coverage > 0.0, coverage, 1.0)
            straight = np.where(
                coverage > 0.0,
                np.clip(rgb[:, 0:3] / safe_coverage, 0.0, 1.0),
                0.0,
            )
            encoded = np.where(
                straight <= 0.0031308,
                straight * 12.92,
                1.055 * np.power(straight, 1.0 / 2.4) - 0.055,
            )

            combined = np.empty((num_pixels, 4), dtype=np.float32)
            combined[:, 0:3] = encoded
            combined[:, 3] = alpha.reshape(num_pixels, 4)[:, 0]
            final_image.pixels.foreach_set(combined.ravel())
            return

        # 无 numpy 兜底：逐像素循环（仅极端环境；Blender 内置 Python 自带 numpy）
        rgb_pixels = list(rgb_image.pixels[:])
        a_pixels = list(a_image.pixels[:])
        combined = [0.0] * (num_pixels * 4)
        for i in range(num_pixels):
            base = i * 4
            coverage = rgb_pixels[base + 3]
            if coverage > 0.0:
                inv_coverage = 1.0 / coverage
                r = rgb_pixels[base + 0] * inv_coverage
                g = rgb_pixels[base + 1] * inv_coverage
                b = rgb_pixels[base + 2] * inv_coverage
            else:
                r = g = b = 0.0
            combined[base + 0] = _linear_to_srgb(r)
            combined[base + 1] = _linear_to_srgb(g)
            combined[base + 2] = _linear_to_srgb(b)
            combined[base + 3] = a_pixels[base + 0]
        final_image.pixels[:] = combined

    def _create_preview_primitive(self, preview_type):
        if preview_type == 'FLAT':
            bpy.ops.mesh.primitive_plane_add(size=2)
            cam_loc, cam_rot = (0, 0, 2), (0, 0, 0)
        elif preview_type == 'SPHERE':
            bpy.ops.mesh.primitive_uv_sphere_add(radius=1)
            bpy.ops.object.shade_smooth()
            cam_loc, cam_rot = (0, -3, 0), (1.5708, 0, 0)
        elif preview_type == 'CUBE':
            bpy.ops.mesh.primitive_cube_add(size=1.5)
            cam_loc, cam_rot = (0, -3, 0), (1.5708, 0, 0)
        else:
            bpy.ops.mesh.primitive_monkey_add(size=1.5)
            cam_loc, cam_rot = (0, -3, 0), (1.5708, 0, 0)
        
        render_obj = bpy.context.active_object
        return render_obj, cam_loc, cam_rot
    
    def import_preview_to_material(self, material, preview_path):
        if not material.use_nodes:
            material.use_nodes = True
        
        preview_image = bpy.data.images.load(preview_path, check_existing=True)
        preview_image.colorspace_settings.name = 'sRGB'
        preview_image.alpha_mode = 'CHANNEL_PACKED'
        node_tree = material.node_tree
        node_tree.nodes.clear()

        tex_node = node_tree.nodes.new('ShaderNodeTexImage')
        tex_node.image = preview_image
        tex_node.location = (-500, 0)

        transparent_node = node_tree.nodes.new('ShaderNodeBsdfTransparent')
        transparent_node.location = (-220, 120)

        diffuse_node = node_tree.nodes.new('ShaderNodeBsdfDiffuse')
        diffuse_node.location = (-220, -80)

        mix_shader = node_tree.nodes.new('ShaderNodeMixShader')
        mix_shader.location = (20, 0)

        output_node = node_tree.nodes.new('ShaderNodeOutputMaterial')
        output_node.location = (260, 0)

        # 透明排序用抖动而非混合：避免 BLEND 的排序开销与半透明重叠问题
        # （4.2+ 称 DITHERED，旧版称 HASHED）
        try:
            material.blend_method = 'DITHERED'
        except TypeError:
            material.blend_method = 'HASHED'
        if hasattr(material, "use_transparency_overlap"):
            material.use_transparency_overlap = False
        elif hasattr(material, "show_transparent_back"):
            material.show_transparent_back = False
        node_tree.links.new(tex_node.outputs['Color'], diffuse_node.inputs['Color'])
        node_tree.links.new(tex_node.outputs['Alpha'], mix_shader.inputs['Fac'])
        node_tree.links.new(transparent_node.outputs['BSDF'], mix_shader.inputs[1])
        node_tree.links.new(diffuse_node.outputs['BSDF'], mix_shader.inputs[2])
        node_tree.links.new(mix_shader.outputs['Shader'], output_node.inputs['Surface'])

    def execute(self, context):
        props = context.scene.texture_tools_props
        
        if not props.output_dir:
            self.report({'ERROR'}, "请先设置输出目录")
            return {'CANCELLED'}
        
        output_dir = bpy.path.abspath(props.output_dir)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objects:
            self.report({'ERROR'}, "未找到任何选中的网格物体")
            return {'CANCELLED'}
        
        mat_to_objects = {}
        for obj in selected_objects:
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    if slot.material not in mat_to_objects:
                        mat_to_objects[slot.material] = []
                    mat_to_objects[slot.material].append(obj)
        
        if not mat_to_objects:
            self.report({'ERROR'}, "未找到任何使用节点的选定材质")
            return {'CANCELLED'}
        
        compiled_resolution_rules = []
        if props.bake_resolution_use_rules:
            for rule in props.bake_resolution_rules:
                if not rule.enabled:
                    continue
                try:
                    compiled_resolution_rules.append((re.compile(rule.pattern), rule.resolution))
                except re.error as exc:
                    self.report(
                        {'WARNING'},
                        f"分辨率规则正则表达式无效，已跳过：{rule.pattern!r}（{exc}）",
                    )

        exported_count = 0
        processed_material_names = set()
        used_output_paths = set()
        
        for material, objects in mat_to_objects.items():
            if material.name in processed_material_names:
                continue
            
            should_bake = (props.color_bake_node_types == 'ALL') or self.has_complex_nodes(material, props.color_bake_node_types)
            
            if should_bake:
                output_path = _reserve_material_output_path(
                    output_dir, material.name, used_output_paths
                )
                processed_material_names.add(material.name)
                
                bake_size = props.color_bake_size
                
                for pattern, resolution in compiled_resolution_rules:
                    if pattern.match(material.name):
                        bake_size = resolution
                        break
                
                source_obj = objects[0]
                
                if self.render_material_preview(material, output_path, props.color_bake_preview_type, bake_size,
                                                  unfold_by_uv=props.color_bake_unfold_by_uv,
                                                  source_obj=source_obj):
                    exported_count += 1
                    
                    if props.color_bake_import_to_material:
                        self.import_preview_to_material(material, output_path)
        
        self.report({'INFO'}, f"成功导出 {exported_count}/{len(mat_to_objects)} 个材质的贴图。")
        return {'FINISHED'}


tt_color_bake_list = (
    TT_OT_add_bake_resolution_rule,
    TT_OT_remove_bake_resolution_rule,
    TT_OT_reset_bake_resolution_rules,
    TT_OT_bake_color_maps,
)
