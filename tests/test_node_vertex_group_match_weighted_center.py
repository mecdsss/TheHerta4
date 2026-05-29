import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _FakeKDTree:
    def __init__(self, _size):
        self.items = []

    def insert(self, centroid, index):
        self.items.append((centroid, index))

    def balance(self):
        return None

    def find_range(self, _center, _radius):
        return []


class _FakeVector(tuple):
    def __new__(cls, value):
        return super().__new__(cls, tuple(value))


PKG = "_node_vg_match_weighted_center_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Node=object, Operator=object, PropertyGroup=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        FloatProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy.types", PropertyGroup=object)
_install_module("bmesh")
_install_module(
    "mathutils",
    Vector=_FakeVector,
    kdtree=types.SimpleNamespace(KDTree=_FakeKDTree),
)
_install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_vertex_group_match.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_vertex_group_match", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.node_vertex_group_match"] = module
spec.loader.exec_module(module)


class _FakeGroupElem:
    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _FakeVertex:
    def __init__(self, groups):
        self.groups = groups


class _FakeVertexGroup:
    def __init__(self, name):
        self.name = name


class _FakeObject:
    def __init__(self):
        self.vertex_groups = [_FakeVertexGroup("Bone")]
        self.data = types.SimpleNamespace(
            vertices=[
                _FakeVertex([_FakeGroupElem(0, 0.1)]),
                _FakeVertex([_FakeGroupElem(0, 0.9)]),
            ]
        )


class VertexGroupMatchWeightedCenterTests(unittest.TestCase):
    def test_point_cloud_center_biases_toward_stronger_weight(self):
        matcher = module.VertexGroupMatcherOptimized()
        obj = _FakeObject()
        positions = np.array(
            [
                [0.0, 0.0, 0.0],
                [10.0, 0.0, 0.0],
            ],
            dtype=np.float32,
        )

        cloud = matcher.get_vg_point_clouds(obj, positions)["Bone"]

        self.assertAlmostEqual(float(cloud["weighted_centroid"][0]), 9.0)
        self.assertAlmostEqual(float(cloud["centroid"][0]), 9.0)
        self.assertGreater(float(cloud["weighted_centroid"][0]), 5.0)


if __name__ == "__main__":
    unittest.main()
