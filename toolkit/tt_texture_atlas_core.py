import io
import math
import os
from collections import OrderedDict, defaultdict

import bpy
from mathutils import Vector

from .tt_texture_atlas_vendor import RectPack2D

try:
    from PIL import Image, ImageChops, ImageFile, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = None
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        RESAMPLING = Image.Resampling.LANCZOS
    except AttributeError:
        RESAMPLING = Image.LANCZOS
except ImportError:
    Image = None
    ImageChops = None
    ImageFile = None
    UnidentifiedImageError = OSError
    RESAMPLING = None


ATLAS_TEXTURE_PREFIX = "texture_atlas_"
ATLAS_MATERIAL_PREFIX = "material_atlas_"
EXTRA_TEXTURE_INPUTS = {
    "metallic": "Metallic",
    "roughness": "Roughness",
    "normal_map": "Normal",
    "emission": "Emission Color",
}
NON_COLOR_TEXTURES = {"metallic", "roughness", "normal_map"}


class AtlasError(RuntimeError):
    pass


def is_pillow_available():
    return Image is not None


def align_uv(face_uv):
    min_x = min((uv.x for uv in face_uv if not math.isnan(uv.x)), default=0.0)
    min_y = min((uv.y for uv in face_uv if not math.isnan(uv.y)), default=0.0)
    shift_x = math.floor(min_x)
    shift_y = math.floor(min_y)
    if shift_x != 0 or shift_y != 0:
        for uv in face_uv:
            uv.x -= shift_x
            uv.y -= shift_y
    return face_uv


def get_image_source(image):
    if not image:
        return None, None
    filepath = bpy.path.abspath(image.filepath) if image.filepath else ""
    source_ext = os.path.splitext(filepath)[1].lower()
    if source_ext == ".dds":
        raise AtlasError("DDS 贴图已跳过，因为 Pillow 无法稳定读取该格式")

    if image.packed_file:
        return image.packed_file.data, source_ext

    absolute_path = os.path.abspath(filepath) if filepath else ""
    if not absolute_path or not os.path.isfile(absolute_path):
        raise AtlasError("贴图文件在磁盘上不存在")
    if absolute_path.lower().endswith(".dds"):
        raise AtlasError("DDS 贴图已跳过，因为 Pillow 无法稳定读取该格式")
    return absolute_path, source_ext


def open_pillow_image(image):
    source, _ = get_image_source(image)
    if isinstance(source, bytes):
        loader = io.BytesIO(source)
    else:
        loader = source
    try:
        return Image.open(loader).convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AtlasError(f"Pillow 无法读取贴图 '{image.name}'：{exc}") from exc


def find_output_node(node_tree):
    if not node_tree:
        return None
    for node in node_tree.nodes:
        if node.bl_idname == "ShaderNodeOutputMaterial":
            return node
    return None


def trace_connected_nodes(node, visited=None):
    if visited is None:
        visited = set()
    if node in visited:
        return visited
    visited.add(node)
    for socket in getattr(node, "inputs", []):
        for link in socket.links:
            trace_connected_nodes(link.from_node, visited)
    return visited


def find_principled_node(material):
    if not material.use_nodes or not material.node_tree:
        return None
    output = find_output_node(material.node_tree)
    if not output:
        return None
    connected = trace_connected_nodes(output)
    for node in connected:
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            return node
    return None


def find_image_node_from_socket(socket, visited=None):
    if visited is None:
        visited = set()
    for link in socket.links:
        node = link.from_node
        if node in visited:
            continue
        visited.add(node)
        if node.bl_idname == "ShaderNodeTexImage" and getattr(node, "image", None):
            return node
        for nested_socket in getattr(node, "inputs", []):
            found = find_image_node_from_socket(nested_socket, visited)
            if found:
                return found
    return None


def get_material_color(material):
    if not material.use_nodes or not material.node_tree:
        color = getattr(material, "diffuse_color", (1.0, 1.0, 1.0, 1.0))
    else:
        principled = find_principled_node(material)
        color = principled.inputs["Base Color"].default_value if principled else (1.0, 1.0, 1.0, 1.0)
    return tuple(max(0, min(255, int(round(channel * 255)))) for channel in color[:4])


def get_material_albedo_image(material):
    if not material.use_nodes or not material.node_tree:
        return None
    principled = find_principled_node(material)
    if principled and "Base Color" in principled.inputs:
        image_node = find_image_node_from_socket(principled.inputs["Base Color"])
        if image_node:
            return image_node.image

    output = find_output_node(material.node_tree)
    if output:
        connected = trace_connected_nodes(output)
        for node in connected:
            if node.bl_idname == "ShaderNodeTexImage" and getattr(node, "image", None):
                return node.image
    return None


def get_material_extra_images(material):
    principled = find_principled_node(material)
    if not principled:
        return {}
    results = {}
    for texture_type, input_name in EXTRA_TEXTURE_INPUTS.items():
        if input_name not in principled.inputs:
            continue
        socket = principled.inputs[input_name]
        if texture_type == "normal_map":
            for link in socket.links:
                normal_node = link.from_node
                if normal_node.bl_idname == "ShaderNodeNormalMap" and "Color" in normal_node.inputs:
                    image_node = find_image_node_from_socket(normal_node.inputs["Color"])
                    if image_node:
                        results[texture_type] = image_node.image
                        break
        else:
            image_node = find_image_node_from_socket(socket)
            if image_node:
                results[texture_type] = image_node.image
    return results


def iter_selected_materials(context):
    materials = OrderedDict()
    for obj in context.selected_objects:
        if obj.type != "MESH" or not obj.data.materials or not obj.data.uv_layers.active:
            continue
        for slot in obj.material_slots:
            material = slot.material
            if not material:
                continue
            materials.setdefault(material, []).append(obj)
    return materials


def build_refresh_entries(context, props):
    previous = {
        item.material.name: bool(item.enabled)
        for item in props.atlas_materials
        if item.material
    }
    material_map = iter_selected_materials(context)
    entries = []
    warnings = []

    for material, objects in sorted(material_map.items(), key=lambda pair: pair[0].name):
        skip_reason = ""
        albedo_image = get_material_albedo_image(material)
        if albedo_image:
            try:
                get_image_source(albedo_image)
            except AtlasError as exc:
                skip_reason = str(exc)
                warnings.append(f"{material.name}: {skip_reason}")

        entries.append(
            {
                "material": material,
                "source_objects": "|".join(obj.name for obj in objects),
                "enabled": previous.get(material.name, not skip_reason),
                "skip_reason": skip_reason,
            }
        )
    return entries, warnings


def collect_material_uvs(obj, material):
    loops = []
    uv_data = obj.data.uv_layers.active.data
    for poly in obj.data.polygons:
        if poly.material_index >= len(obj.data.materials):
            continue
        if obj.data.materials[poly.material_index] != material:
            continue
        face_uv = [uv_data[index].uv for index in poly.loop_indices]
        loops.extend(align_uv(face_uv))
    return loops


def get_max_uv_coordinates(uv_loops):
    max_x = 1
    max_y = 1
    for uv in uv_loops:
        if not math.isnan(uv.x):
            max_x = max(max_x, uv.x)
        if not math.isnan(uv.y):
            max_y = max(max_y, uv.y)
    return max_x, max_y


def calculate_adjusted_size(mode, base_size, custom_size):
    if mode == "PO2":
        return tuple(1 << int(value - 1).bit_length() for value in base_size)
    if mode == "QUAD":
        edge = int(max(base_size))
        return edge, edge
    if mode == "CUSTOM":
        return custom_size
    return base_size


def get_scale_factors(atlas_size, raw_size):
    scaled = tuple(raw / atlas for raw, atlas in zip(raw_size, atlas_size))
    if all(value <= 1 for value in scaled):
        return scaled
    atlas_width, atlas_height = atlas_size
    raw_width, raw_height = raw_size
    aspect_ratio = (raw_width * atlas_height) / max(raw_height * atlas_width, 1)
    return (1, 1 / aspect_ratio) if aspect_ratio > 1 else (aspect_ratio, 1)


def tile_image(image, uv_size, output_size):
    tiled = Image.new("RGBA", output_size)
    tile_width, tile_height = image.size
    count_x = math.ceil(uv_size[0])
    count_y = math.ceil(uv_size[1])
    output_height = output_size[1]
    for row in range(count_y):
        y = output_height - tile_height - row * tile_height
        for column in range(count_x):
            tiled.paste(image, (column * tile_width, y))
    return tiled


def build_material_records(context, props):
    structure = OrderedDict()
    warnings = []
    for item in props.atlas_materials:
        material = item.material
        if not material or not item.enabled or item.skip_reason:
            continue

        source_object_names = [name for name in item.source_objects.split("|") if name in bpy.data.objects]
        uv_loops = []
        source_objects = []
        for name in source_object_names:
            obj = bpy.data.objects[name]
            if obj.type != "MESH" or not obj.data.uv_layers.active:
                continue
            loops = collect_material_uvs(obj, material)
            if loops:
                uv_loops.extend(loops)
                source_objects.append(obj)

        if not uv_loops:
            warnings.append(f"{material.name}: skipped because no UVs were found on current source objects")
            warnings[-1] = f"{material.name}: 已跳过，当前来源物体上没有找到可用 UV"
            continue

        albedo_image = get_material_albedo_image(material)
        try:
            albedo_pil = open_pillow_image(albedo_image) if albedo_image else None
        except AtlasError as exc:
            warnings.append(f"{material.name}: {exc}")
            continue

        uv_size = get_max_uv_coordinates(uv_loops)
        if albedo_pil:
            base_image_size = albedo_pil.size
        else:
            base_image_size = (props.atlas_color_size, props.atlas_color_size)
        size = (
            int(base_image_size[0] * math.ceil(uv_size[0]) + props.atlas_padding),
            int(base_image_size[1] * math.ceil(uv_size[1]) + props.atlas_padding),
        )

        extra_images = {}
        if props.atlas_include_extra_textures:
            for texture_type, image in get_material_extra_images(material).items():
                try:
                    extra_images[texture_type] = open_pillow_image(image)
                except AtlasError as exc:
                    warnings.append(f"{material.name}: 已跳过 {texture_type} 贴图（{exc}）")

        structure[material] = {
            "material": material,
            "objects": source_objects,
            "uv_loops": uv_loops,
            "gfx": {
                "size": size,
                "uv_size": uv_size,
                "fit": None,
                "albedo": albedo_pil,
                "color": get_material_color(material),
                "extras": extra_images,
            },
        }
    return structure, warnings


def get_raw_atlas_size(structure):
    max_x = 1
    max_y = 1
    for item in structure.values():
        fit = item["gfx"]["fit"]
        max_x = max(max_x, fit["x"] + item["gfx"]["size"][0])
        max_y = max(max_y, fit["y"] + item["gfx"]["size"][1])
    return int(max_x), int(max_y)


def generate_atlas_images(structure, atlas_size, props):
    half_padding = int(props.atlas_padding / 2)
    atlases = {"albedo": Image.new("RGBA", atlas_size, (0, 0, 0, 0))}
    if props.atlas_include_extra_textures:
        for texture_type in EXTRA_TEXTURE_INPUTS:
            atlases[texture_type] = Image.new("RGBA", atlas_size, (0, 0, 0, 0))

    for item in structure.values():
        gfx = item["gfx"]
        fit = gfx["fit"]
        inner_size = (
            max(1, int(gfx["size"][0] - props.atlas_padding)),
            max(1, int(gfx["size"][1] - props.atlas_padding)),
        )
        position = (int(fit["x"] + half_padding), int(fit["y"] + half_padding))

        if gfx["albedo"]:
            albedo = gfx["albedo"].copy()
            if albedo.size != inner_size:
                albedo = albedo.resize(inner_size, RESAMPLING)
            if max(gfx["uv_size"]) > 1:
                albedo = tile_image(albedo, gfx["uv_size"], inner_size)
        else:
            albedo = Image.new("RGBA", inner_size, gfx["color"])
        atlases["albedo"].paste(albedo, position)

        if not props.atlas_include_extra_textures:
            continue

        for texture_type in EXTRA_TEXTURE_INPUTS:
            texture_image = gfx["extras"].get(texture_type)
            if not texture_image:
                continue
            current = texture_image.copy()
            if current.size != inner_size:
                current = current.resize(inner_size, RESAMPLING)
            if max(gfx["uv_size"]) > 1:
                current = tile_image(current, gfx["uv_size"], inner_size)
            atlases[texture_type].paste(current, position)
    return atlases


def align_uvs(structure, atlas_size, raw_size, props):
    raw_width, raw_height = raw_size
    scaled_width, scaled_height = get_scale_factors(atlas_size, raw_size)
    margin = props.atlas_padding + 2
    border_margin = int(props.atlas_padding / 2) + 1

    for item in structure.values():
        gfx = item["gfx"]
        uv_width, uv_height = gfx["uv_size"]
        gfx_width, gfx_height = gfx["size"]
        width_margin = gfx_width - margin
        height_margin = gfx_height - margin
        x_offset = gfx["fit"]["x"] + border_margin
        y_offset = gfx["fit"]["y"] - border_margin

        for uv in item["uv_loops"]:
            reset_x = uv.x / uv_width * width_margin
            reset_y = uv.y / uv_height * height_margin - gfx_height
            uv.x = ((reset_x + x_offset) / raw_width) * scaled_width
            uv.y = ((reset_y - y_offset) / raw_height) * scaled_height + 1


def save_atlases(output_dir, atlas_name, atlases):
    os.makedirs(output_dir, exist_ok=True)
    saved_paths = {}
    extension_map = {
        "PNG": "png",
        "TGA": "tga",
        "TIFF": "tif",
        "BMP": "bmp",
    }
    image_format = getattr(atlases.get("_meta", {}), "get", lambda _k, _d=None: None)("image_format", "PNG")
    extension = extension_map.get(image_format, "png")
    for texture_type, atlas in atlases.items():
        if texture_type == "_meta":
            continue
        if texture_type != "albedo" and atlas.getbbox() is None:
            continue
        suffix = "" if texture_type == "albedo" else f"_{texture_type.title()}"
        filename = f"{atlas_name}{suffix}.{extension}"
        path = os.path.join(output_dir, filename)
        atlas.save(path, format=image_format)
        saved_paths[texture_type] = path
    return saved_paths


def create_texture_node(node_tree, image_path, label, location, non_color=False):
    node = node_tree.nodes.new(type="ShaderNodeTexImage")
    node.image = bpy.data.images.load(image_path, check_existing=True)
    node.label = label
    node.location = location
    if non_color:
        node.image.colorspace_settings.name = "Non-Color"
    else:
        node.image.colorspace_settings.name = "sRGB"
        node.image.alpha_mode = "CHANNEL_PACKED"
    return node


def create_atlas_material(saved_paths, atlas_name):
    material = bpy.data.materials.new(name=f"{ATLAS_MATERIAL_PREFIX}{atlas_name}")
    material.use_nodes = True
    material.blend_method = "BLEND"
    if hasattr(material, "use_transparency_overlap"):
        material.use_transparency_overlap = False
    elif hasattr(material, "show_transparent_back"):
        material.show_transparent_back = False
    material.use_backface_culling = True
    node_tree = material.node_tree
    node_tree.nodes.clear()

    transparent_node = node_tree.nodes.new(type="ShaderNodeBsdfTransparent")
    transparent_node.location = (-250, 120)

    diffuse_node = node_tree.nodes.new(type="ShaderNodeBsdfDiffuse")
    diffuse_node.location = (-250, -80)

    mix_shader = node_tree.nodes.new(type="ShaderNodeMixShader")
    mix_shader.location = (0, 0)

    output_node = node_tree.nodes.new(type="ShaderNodeOutputMaterial")
    output_node.location = (240, 0)

    albedo_node = create_texture_node(node_tree, saved_paths["albedo"], "Atlas Albedo", (-600, 300))
    node_tree.links.new(albedo_node.outputs["Color"], diffuse_node.inputs["Color"])
    node_tree.links.new(albedo_node.outputs["Alpha"], mix_shader.inputs["Fac"])
    node_tree.links.new(transparent_node.outputs["BSDF"], mix_shader.inputs[1])
    node_tree.links.new(diffuse_node.outputs["BSDF"], mix_shader.inputs[2])
    node_tree.links.new(mix_shader.outputs["Shader"], output_node.inputs["Surface"])

    return material


def assign_atlas_material(structure, atlas_material):
    object_materials = defaultdict(set)
    for material, item in structure.items():
        for obj in item["objects"]:
            object_materials[obj].add(material)

    for obj, materials in object_materials.items():
        if atlas_material.name not in obj.data.materials:
            obj.data.materials.append(atlas_material)
        atlas_index = obj.data.materials.find(atlas_material.name)
        for poly in obj.data.polygons:
            if poly.material_index >= len(obj.data.materials):
                continue
            if obj.data.materials[poly.material_index] in materials:
                poly.material_index = atlas_index


def generate_texture_atlas(context, props):
    if not is_pillow_available():
        raise AtlasError("Pillow 尚未安装")

    structure, warnings = build_material_records(context, props)
    if len(structure) < 2:
        raise AtlasError("至少需要两个可读取的材质才能生成图集")

    RectPack2D().pack(structure)
    raw_size = get_raw_atlas_size(structure)
    atlas_size = calculate_adjusted_size(
        props.atlas_size_mode,
        raw_size,
        (props.atlas_custom_width, props.atlas_custom_height),
    )
    if max(atlas_size) > props.atlas_max_size:
        raise AtlasError(f"图集尺寸 {atlas_size[0]}x{atlas_size[1]} 超过上限 {props.atlas_max_size}")

    atlases = generate_atlas_images(structure, atlas_size, props)
    atlases["_meta"] = {"image_format": props.atlas_image_format}
    align_uvs(structure, atlas_size, raw_size, props)
    output_dir = bpy.path.abspath(props.output_dir)
    saved_paths = save_atlases(output_dir, props.atlas_output_name.strip() or "TextureAtlas", atlases)
    atlas_material = create_atlas_material(saved_paths, props.atlas_output_name.strip() or "TextureAtlas")
    assign_atlas_material(structure, atlas_material)

    return {
        "atlas_size": atlas_size,
        "saved_paths": saved_paths,
        "material": atlas_material,
        "warnings": warnings,
        "material_count": len(structure),
    }
