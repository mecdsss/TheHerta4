import bpy


TEMPLATE_TYPES = (
    ("LightMap", "lightmap_generate_lightmap"),
    ("HighLightMap", "lightmap_generate_highlightmap"),
    ("RampMap", "lightmap_generate_rampmap"),
    ("MaterialMap", "lightmap_generate_materialmap"),
    ("StockingMap", "lightmap_generate_stockingmap"),
)


class TT_OT_generate_lightmap_template(bpy.types.Operator):
    bl_idname = "toolkit.tt_generate_lightmap_template"
    bl_label = "生成光照模板"
    bl_description = "为选中的物体创建 LightMap / HighLightMap / RampMap / MaterialMap / StockingMap 材质模板"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.texture_tools_props
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}

        active_obj = context.active_object
        if not active_obj or active_obj.type != 'MESH':
            self.report({'ERROR'}, "请确保活动物体是网格物体")
            return {'CANCELLED'}

        mat_prefix = active_obj.name
        selected_template_types = [
            template_type
            for template_type, prop_name in TEMPLATE_TYPES
            if bool(getattr(props, prop_name, False))
        ]

        if not selected_template_types:
            self.report({'ERROR'}, "请至少选择一种材质类型")
            return {'CANCELLED'}

        if props.lightmap_mode == 'REPLACE':
            for obj in selected_objects:
                obj.data.materials.clear()

        generated_types = []
        for template_type in selected_template_types:
            template_material = self._create_template_material(template_type, mat_prefix)
            for obj in selected_objects:
                obj.data.materials.append(template_material)
            generated_types.append(template_material.name)

        self.report({'INFO'}, f"已为 {len(selected_objects)} 个物体生成共享材质: {', '.join(generated_types)}")
        return {'FINISHED'}

    def _create_template_material(self, template_type, obj_name):
        mat_name = f"{template_type}_{obj_name}"
        existing = bpy.data.materials.get(mat_name)
        if existing:
            return existing

        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True

        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()

        rgb_node = nodes.new('ShaderNodeRGB')
        rgb_node.label = template_type
        rgb_node.location = (-280, 0)
        rgb_node.outputs['Color'].default_value = (1.0, 1.0, 1.0, 1.0)

        diffuse_node = nodes.new('ShaderNodeBsdfDiffuse')
        diffuse_node.location = (0, 0)

        output_node = nodes.new('ShaderNodeOutputMaterial')
        output_node.location = (260, 0)

        links.new(rgb_node.outputs['Color'], diffuse_node.inputs['Color'])
        links.new(diffuse_node.outputs['BSDF'], output_node.inputs['Surface'])
        return mat


tt_lightmap_list = (
    TT_OT_generate_lightmap_template,
)
