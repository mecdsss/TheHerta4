import numpy as np


def rebase_shape_key_coordinates(
    coordinates_by_name,
    basis_name,
    new_basis_name,
    remove_new_basis_key=True,
):
    """Rebase all shape key coordinates onto a new basis shape."""
    if basis_name == new_basis_name:
        raise ValueError("new basis shape key must be different from the current basis")
    if basis_name not in coordinates_by_name:
        raise KeyError(f"missing basis shape key: {basis_name}")
    if new_basis_name not in coordinates_by_name:
        raise KeyError(f"missing source shape key: {new_basis_name}")

    old_basis_coords = np.asarray(coordinates_by_name[basis_name], dtype=np.float32)
    new_basis_coords = np.asarray(coordinates_by_name[new_basis_name], dtype=np.float32)
    if old_basis_coords.shape != new_basis_coords.shape:
        raise ValueError("basis and source shape key coordinates must use the same shape")

    basis_delta = new_basis_coords - old_basis_coords
    rebased_coordinates = {
        basis_name: np.array(new_basis_coords, copy=True),
    }

    for key_name, coords in coordinates_by_name.items():
        if key_name == basis_name:
            continue

        key_coords = np.asarray(coords, dtype=np.float32)
        if key_coords.shape != old_basis_coords.shape:
            raise ValueError(f"shape key '{key_name}' has inconsistent coordinate shape")

        if key_name == new_basis_name:
            if not remove_new_basis_key:
                rebased_coordinates[key_name] = np.array(new_basis_coords, copy=True)
            continue

        rebased_coordinates[key_name] = key_coords + basis_delta

    return rebased_coordinates
