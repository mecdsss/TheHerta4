import numpy as np


def get_color_storage_field(color_attribute) -> str:
    """Return the Blender RNA field used to access this color attribute."""
    if not hasattr(color_attribute, "domain"):
        # Legacy mesh.vertex_colors layers expose MeshLoopColor.color only.
        return "color"
    data_type = str(getattr(color_attribute, "data_type", "") or "")
    if data_type == "FLOAT_COLOR":
        return "color"
    return "color_srgb"


def read_color_attribute_data(color_attribute, count: int | None = None) -> np.ndarray:
    """Read a Blender color attribute into a float32 RGBA array."""
    if color_attribute is None:
        return np.zeros((0, 4), dtype=np.float32)

    data = getattr(color_attribute, "data", None)
    if data is None:
        return np.zeros((0, 4), dtype=np.float32)

    if count is None:
        count = len(data)

    if count <= 0:
        return np.zeros((0, 4), dtype=np.float32)

    result = np.empty(count * 4, dtype=np.float32)
    data.foreach_get(get_color_storage_field(color_attribute), result)
    return result.reshape(-1, 4)


def write_color_attribute_data(color_attribute, rgba_data) -> None:
    """Write normalized float RGBA data into a Blender color attribute."""
    if color_attribute is None:
        return

    array = np.asarray(rgba_data, dtype=np.float32).reshape(-1, 4)
    color_attribute.data.foreach_set(
        get_color_storage_field(color_attribute),
        array.ravel(),
    )


def sample_color_attribute_to_loops(mesh, color_attribute) -> np.ndarray:
    """Return per-loop RGBA values for either CORNER or POINT color attributes."""
    attr_values = read_color_attribute_data(color_attribute)
    if attr_values.size == 0:
        return attr_values

    domain = str(getattr(color_attribute, "domain", "") or "")
    if domain != "POINT":
        return attr_values

    num_loops = len(getattr(mesh, "loops", ()))
    if num_loops == 0:
        return np.zeros((0, 4), dtype=np.float32)

    loop_vertex_indices = np.empty(num_loops, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", loop_vertex_indices)
    return attr_values[loop_vertex_indices]
