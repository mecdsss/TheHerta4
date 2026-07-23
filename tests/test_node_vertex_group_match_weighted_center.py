import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
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
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.utils"):
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
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
    ),
)


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


class _OffsetMatrix:
    def __init__(self, offset):
        self.offset = np.asarray(offset, dtype=np.float32)

    def __matmul__(self, value):
        return np.asarray(value, dtype=np.float32) + self.offset


class _EvaluatedObject:
    def __init__(self):
        self.matrix_world = _OffsetMatrix((10.0, 20.0, 30.0))
        self.mesh = types.SimpleNamespace(
            vertices=[types.SimpleNamespace(co=np.array((1.0, 2.0, 3.0), dtype=np.float32))]
        )
        self.cleared = False

    def to_mesh(self, **_kwargs):
        return self.mesh

    def to_mesh_clear(self):
        self.cleared = True


class VertexGroupMatchWeightedCenterTests(unittest.TestCase):
    """测试顶点组匹配的加权质心计算：权重大的顶点对质心影响更大"""

    def test_point_cloud_center_biases_toward_stronger_weight(self):
        """测试点云质心偏向权重更大的顶点"""
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

    def test_deformed_positions_use_evaluated_object_world_matrix(self):
        matcher = module.VertexGroupMatcherOptimized()
        evaluated = _EvaluatedObject()
        obj = types.SimpleNamespace(
            matrix_world=_OffsetMatrix((100.0, 100.0, 100.0)),
            evaluated_get=lambda _depsgraph: evaluated,
        )
        context = types.SimpleNamespace(evaluated_depsgraph_get=lambda: object())

        positions = matcher.get_vertex_positions(obj, use_shape_key=True, context=context)

        np.testing.assert_allclose(positions, [[11.0, 22.0, 33.0]])
        self.assertTrue(evaluated.cleared)


if __name__ == "__main__":
    unittest.main()
