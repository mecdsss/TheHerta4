import io
import math
import os
from collections import OrderedDict, defaultdict

import bpy

from .tt_texture_atlas_vendor import RectPack2D

try:
    from PIL import Image, ImageFile, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = None
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        RESAMPLING_LANCZOS = Image.Resampling.LANCZOS
        RESAMPLING_NEAREST = Image.Resampling.NEAREST
    except AttributeError:
        RESAMPLING_LANCZOS = Image.LANCZOS
        RESAMPLING_NEAREST = Image.NEAREST
except ImportError:
    Image = None
    ImageFile = None
    UnidentifiedImageError = OSError
    RESAMPLING_LANCZOS = None
    RESAMPLING_NEAREST = None


ATLAS_TEXTURE_PREFIX = "texture_atlas_"
ATLAS_MATERIAL_PREFIX = "material_atlas_"
EXTRA_TEXTURE_INPUTS = {
    "metallic": "Metallic",
    "roughness": "Roughness",
    "normal_map": "Normal",
    "specular": "Specular IOR Level",
    "emission": "Emission Color",
}
NON_COLOR_TEXTURES = {"metallic", "roughness", "normal_map", "specular"}


class AtlasError(RuntimeError):
    pass


def is_pillow_available():
    return Image is not None


def get_resampling_filter(props):
    if getattr(props, "atlas_pixel_art_scale", False):
        return RESAMPLING_NEAREST
    return RESAMPLING_LANCZOS


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
    loader = io.BytesIO(source) if isinstance(source, bytes) else source
    try:
        return Image.open(loader).convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AtlasError(f"Pillow 无法读取贴图 '{image.name}'：{exc}") from exc


def crop_image_transparent_border(image):
    if image is None:
        return None, None
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        return image.copy(), None
    return image.crop(bbox), bbox


def find_output_node(node_tree):
    if not node_tree:
        return None
    nodes = list(getattr(node_tree, "nodes", []) or [])
    connected_outputs = []
    active_outputs = []
    fallback_outputs = []
    for node in nodes:
        if node.bl_idname != "ShaderNodeOutputMaterial":
            continue
        fallback_outputs.append(node)
        if getattr(node, "is_active_output", False):
            active_outputs.append(node)
        if any(link.is_valid for socket in getattr(node, "inputs", []) for link in socket.links):
            connected_outputs.append(node)

    for node in active_outputs:
        if node in connected_outputs:
            return node
    if connected_outputs:
        return connected_outputs[0]
    if active_outputs:
        return active_outputs[0]
    return fallback_outputs[0] if fallback_outputs else None


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
    for node in trace_connected_nodes(output):
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
        for node in trace_connected_nodes(output):
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
    count_x = max(1, math.ceil(uv_size[0]))
    count_y = max(1, math.ceil(uv_size[1]))
    output_height = output_size[1]
    for row in range(count_y):
        y = output_height - tile_height - row * tile_height
        for column in range(count_x):
            tiled.paste(image, (column * tile_width, y))
    return tiled


def _normalize_source_image(image, props):
    crop_box = None
    if image and getattr(props, "atlas_crop_transparent", False):
        image, crop_box = crop_image_transparent_border(image)
    return image, crop_box


def _calculate_item_size(base_image_size, uv_size, padding):
    return (
        int(base_image_size[0] * max(1, math.ceil(uv_size[0])) + padding),
        int(base_image_size[1] * max(1, math.ceil(uv_size[1])) + padding),
    )


def build_material_records(context, props):
    structure = OrderedDict()
    warnings = []
    uniform_width = 1
    uniform_height = 1

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
            continue

        albedo_image = get_material_albedo_image(material)
        try:
            albedo_pil = open_pillow_image(albedo_image) if albedo_image else None
        except AtlasError as exc:
            warnings.append(f"{material.name}: {exc}")
            continue

        albedo_pil, crop_box = _normalize_source_image(albedo_pil, props)
        uv_size = get_max_uv_coordinates(uv_loops)
        base_image_size = albedo_pil.size if albedo_pil else (props.atlas_color_size, props.atlas_color_size)

        uniform_width = max(uniform_width, int(base_image_size[0]))
        uniform_height = max(uniform_height, int(base_image_size[1]))

        extra_images = {}
        if props.atlas_include_extra_textures:
            for texture_type, image in get_material_extra_images(material).items():
                try:
                    extra_image = open_pillow_image(image)
                    if crop_box:
                        extra_image = extra_image.crop(crop_box)
                    extra_images[texture_type] = extra_image
                except AtlasError as exc:
                    warnings.append(f"{material.name}: skipped {texture_type} texture ({exc})")

        structure[material] = {
            "material": material,
            "objects": source_objects,
            "uv_loops": uv_loops,
            "gfx": {
                "size": _calculate_item_size(base_image_size, uv_size, props.atlas_padding),
                "uv_size": uv_size,
                "fit": None,
                "albedo": albedo_pil,
                "color": get_material_color(material),
                "extras": extra_images,
                "base_image_size": base_image_size,
            },
        }

    if getattr(props, "atlas_force_uniform_size", False):
        for item in structure.values():
            item["gfx"]["base_image_size"] = (uniform_width, uniform_height)
            item["gfx"]["size"] = _calculate_item_size(
                (uniform_width, uniform_height),
                item["gfx"]["uv_size"],
                props.atlas_padding,
            )

    return structure, warnings


def _apply_grid_pack(structure):
    if not structure:
        return
    max_width = max(item["gfx"]["size"][0] for item in structure.values())
    max_height = max(item["gfx"]["size"][1] for item in structure.values())
    columns = max(1, int(math.ceil(math.sqrt(len(structure)))))
    for index, item in enumerate(structure.values()):
        column = index % columns
        row = index // columns
        item["gfx"]["fit"] = {
            "x": column * max_width,
            "y": row * max_height,
        }


def pack_structure(structure, props):
    packer_type = getattr(props, "atlas_packer_type", "RECTPACK")
    if packer_type == "GRID":
        _apply_grid_pack(structure)
    else:
        RectPack2D().pack(structure)


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
    resize_filter = get_resampling_filter(props)
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
                albedo = albedo.resize(inner_size, resize_filter)
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
                current = current.resize(inner_size, resize_filter)
            if max(gfx["uv_size"]) > 1:
                current = tile_image(current, gfx["uv_size"], inner_size)
            atlases[texture_type].paste(current, position)

    return atlases


def align_uvs(structure, atlas_size, raw_size, props):
    raw_width, raw_height = raw_size
    scaled_width, scaled_height = get_scale_factors(atlas_size, raw_size)
    margin = props.atlas_padding + (0 if getattr(props, "atlas_pixel_art_scale", False) else 2)
    border_margin = int(props.atlas_padding / 2) + (0 if getattr(props, "atlas_pixel_art_scale", False) else 1)

    for item in structure.values():
        gfx = item["gfx"]
        uv_width, uv_height = gfx["uv_size"]
        gfx_width, gfx_height = gfx["size"]
        width_margin = gfx_width - margin
        height_margin = gfx_height - margin
        x_offset = gfx["fit"]["x"] + border_margin
        y_offset = gfx["fit"]["y"] - border_margin

        for uv in item["uv_loops"]:
            reset_x = uv.x / max(uv_width, 1e-6) * width_margin
            reset_y = uv.y / max(uv_height, 1e-6) * height_margin - gfx_height
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
    image_format = atlases.get("_meta", {}).get("image_format", "PNG")
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

    output_node = node_tree.nodes.new(type="ShaderNodeOutputMaterial")
    output_node.location = (500, 0)
    principled_node = node_tree.nodes.new(type="ShaderNodeBsdfPrincipled")
    principled_node.location = (180, 0)
    node_tree.links.new(principled_node.outputs["BSDF"], output_node.inputs["Surface"])

    albedo_node = create_texture_node(node_tree, saved_paths["albedo"], "Atlas Albedo", (-600, 200))
    node_tree.links.new(albedo_node.outputs["Color"], principled_node.inputs["Base Color"])
    if "Alpha" in principled_node.inputs:
        node_tree.links.new(albedo_node.outputs["Alpha"], principled_node.inputs["Alpha"])

    if "normal_map" in saved_paths and "Normal" in principled_node.inputs:
        normal_tex = create_texture_node(node_tree, saved_paths["normal_map"], "Atlas Normal", (-600, -80), non_color=True)
        normal_map = node_tree.nodes.new(type="ShaderNodeNormalMap")
        normal_map.location = (-250, -80)
        node_tree.links.new(normal_tex.outputs["Color"], normal_map.inputs["Color"])
        node_tree.links.new(normal_map.outputs["Normal"], principled_node.inputs["Normal"])

    scalar_inputs = {
        "metallic": "Metallic",
        "roughness": "Roughness",
        "specular": "Specular IOR Level",
        "emission": "Emission Color",
    }
    y_positions = {
        "metallic": -220,
        "roughness": -360,
        "specular": -500,
        "emission": -640,
    }
    for texture_type, input_name in scalar_inputs.items():
        if texture_type not in saved_paths or input_name not in principled_node.inputs:
            continue
        texture_node = create_texture_node(
            node_tree,
            saved_paths[texture_type],
            f"Atlas {texture_type.title()}",
            (-600, y_positions[texture_type]),
            non_color=texture_type in NON_COLOR_TEXTURES,
        )
        node_tree.links.new(texture_node.outputs["Color"], principled_node.inputs[input_name])

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

    pack_structure(structure, props)
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
    atlas_name = props.atlas_output_name.strip() or "TextureAtlas"
    saved_paths = save_atlases(output_dir, atlas_name, atlases)
    atlas_material = create_atlas_material(saved_paths, atlas_name)
    assign_atlas_material(structure, atlas_material)

    return {
        "atlas_size": atlas_size,
        "saved_paths": saved_paths,
        "material": atlas_material,
        "warnings": warnings,
        "material_count": len(structure),
    }
