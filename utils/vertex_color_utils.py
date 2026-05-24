import math

import numpy as np


def srgb_to_linear(srgb_value):
    """Convert an sRGB channel to a linear-space channel."""
    if srgb_value <= 0.04045:
        return srgb_value / 12.92
    return math.pow((srgb_value + 0.055) / 1.055, 2.4)


def convert_color_srgb_to_linear(color_rgba):
    """Convert an RGBA color from gamma-corrected UI space to linear space."""
    color_array = np.clip(np.asarray(color_rgba, dtype=np.float32), 0.0, 1.0)
    linear_color = np.empty(4, dtype=np.float32)
    linear_color[:3] = [srgb_to_linear(float(channel)) for channel in color_array[:3]]
    linear_color[3] = color_array[3]
    return linear_color


def build_vertex_color_payload(num_loops, color_rgba_srgb, vc_mode, existing_colors=None):
    """Build the normalized float payload expected by Blender color attributes."""
    if num_loops < 0:
        raise ValueError("num_loops must be non-negative")

    color_rgba_linear = convert_color_srgb_to_linear(color_rgba_srgb)
    expected_size = num_loops * 4

    if vc_mode == "FULL_COLOR":
        return np.tile(color_rgba_linear, num_loops).astype(np.float32)

    if vc_mode != "ALPHA_ONLY":
        raise ValueError(f"Unsupported vertex color mode: {vc_mode}")

    if existing_colors is None:
        raise ValueError("existing_colors is required for ALPHA_ONLY mode")

    color_data = np.asarray(existing_colors, dtype=np.float32).copy()
    if color_data.size != expected_size:
        raise ValueError(
            f"existing_colors has {color_data.size} values, expected {expected_size}"
        )

    color_data[3::4] = color_rgba_linear[3]
    return color_data


def ensure_color_attribute(color_attributes, attr_name, attr_domain, attr_data_type):
    """Get or recreate a Blender color attribute with the requested shape."""
    color_attr = color_attributes.get(attr_name)
    if color_attr is not None:
        if color_attr.domain != attr_domain or color_attr.data_type != attr_data_type:
            color_attributes.remove(color_attr)
            color_attr = None

    if color_attr is None:
        color_attr = color_attributes.new(
            name=attr_name,
            type=attr_data_type,
            domain=attr_domain,
        )

    return color_attr
