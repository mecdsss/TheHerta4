import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_vg_test_runtime_vertex_split_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    data=types.SimpleNamespace(objects={}, texts=[], collections=types.SimpleNamespace(get=lambda _name: None)),
    types=types.SimpleNamespace(Object=object),
)
_install_module("bmesh", new=lambda: None, ops=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(extract_prefix_info=lambda name: (name.split(".")[0],)),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda _message: None, warning=lambda _message: None, debug=lambda _message: None),
)

root = Path(__file__).resolve().parents[1]
core_path = root / "common" / "vg_test_core.py"
core_spec = importlib.util.spec_from_file_location(f"{PKG}.common.vg_test_core", core_path)
vg_test_core = importlib.util.module_from_spec(core_spec)
sys.modules[f"{PKG}.common.vg_test_core"] = vg_test_core
core_spec.loader.exec_module(vg_test_core)

runtime_path = root / "blueprint" / "vg_test_runtime.py"
runtime_spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.vg_test_runtime", runtime_path)
vg_test_runtime = importlib.util.module_from_spec(runtime_spec)
sys.modules[f"{PKG}.blueprint.vg_test_runtime"] = vg_test_runtime
runtime_spec.loader.exec_module(vg_test_runtime)


class VGTestRuntimeVertexSplitTests(unittest.TestCase):
    def test_global_group_sign_uses_the_specific_group_weight(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[7, 10]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[19, 22, 35]),
            ],
            mapping_id="runtime_owner_filter",
        )

        plans = vg_test_runtime._build_vertex_plans(
            [
                {
                    document.prefixes["PrefixA"].local_to_global[7]: 0.3,
                    document.prefixes["PrefixB"].local_to_global[19]: 0.7,
                }
            ],
            document,
        )

        self.assertEqual(
            vg_test_runtime._classify_sign_for_global_group(
                plans[0],
                document.prefixes["PrefixA"].local_to_global[7],
            ),
            1,
        )
        self.assertEqual(
            vg_test_runtime._classify_sign_for_global_group(
                plans[0],
                document.prefixes["PrefixA"].local_to_global[10],
            ),
            -1,
        )
        self.assertEqual(
            vg_test_runtime._classify_sign_for_global_group(
                plans[0],
                document.prefixes["PrefixB"].local_to_global[19],
            ),
            1,
        )

    def test_shared_local_group_vertex_is_still_owned_by_one_prefix_before_geometry_clipping(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
            ],
            mapping_id="runtime_shared_owner",
        )

        plans = vg_test_runtime._build_vertex_plans(
            [
                {
                    document.prefixes["PrefixA"].local_to_global[1]: 0.2,
                    document.prefixes["PrefixA"].local_to_global[2]: 0.5,
                    document.prefixes["PrefixB"].local_to_global[2]: 0.5,
                    document.prefixes["PrefixB"].local_to_global[3]: 0.3,
                }
            ],
            document,
        )

        self.assertEqual(
            vg_test_runtime._classify_sign_for_global_group(
                plans[0],
                document.prefixes["PrefixA"].local_to_global[1],
            ),
            1,
        )
        self.assertEqual(
            vg_test_runtime._classify_sign_for_global_group(
                plans[0],
                document.prefixes["PrefixB"].local_to_global[3],
            ),
            1,
        )

    def test_mixed_prefix_triangle_is_dropped_without_clipping_or_diagnostic_face(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[7, 10]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[19, 22]),
            ],
            mapping_id="runtime_unassigned_boundary",
        )

        original_collect = vg_test_runtime._collect_mesh_loop_payload
        try:
            vg_test_runtime._collect_mesh_loop_payload = lambda _obj: {
                "positions": [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                ],
                "polygons": [
                    {
                        "vertices": [0, 1, 2],
                        "loop_indices": [0, 1, 2],
                        "material_index": 0,
                        "use_smooth": False,
                    }
                ],
                "uv_layers": {},
                "color_layers": {},
                "loop_normals": [(0.0, 0.0, 1.0)] * 3,
            }

            result = vg_test_runtime._build_prefix_triangle_soups(
                types.SimpleNamespace(name="RuntimeObject"),
                document,
                [
                    {document.prefixes["PrefixA"].local_to_global[7]: 1.0},
                    {document.prefixes["PrefixB"].local_to_global[19]: 1.0},
                    {document.prefixes["PrefixB"].local_to_global[22]: 1.0},
                ],
            )
        finally:
            vg_test_runtime._collect_mesh_loop_payload = original_collect

        self.assertEqual(result.prefix_soups["PrefixA"].faces, [])
        self.assertEqual(result.prefix_soups["PrefixB"].faces, [])
        self.assertEqual(result.unassigned_soup.faces, [])

    def test_complete_prefix_triangle_restores_output_to_local_names_without_new_vertices(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
            ],
            mapping_id="runtime_boundary_local_restore",
        )

        original_collect = vg_test_runtime._collect_mesh_loop_payload
        try:
            vg_test_runtime._collect_mesh_loop_payload = lambda _obj: {
                "positions": [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                ],
                "polygons": [
                    {
                        "vertices": [0, 1, 2],
                        "loop_indices": [0, 1, 2],
                        "material_index": 0,
                        "use_smooth": False,
                    }
                ],
                "uv_layers": {},
                "color_layers": {},
                "loop_normals": [(0.0, 0.0, 1.0)] * 3,
            }

            result = vg_test_runtime._build_prefix_triangle_soups(
                types.SimpleNamespace(name="RuntimeObject"),
                document,
                [
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 0.8,
                        document.prefixes["PrefixA"].local_to_global[2]: 0.2,
                    },
                    {
                        document.prefixes["PrefixA"].local_to_global[2]: 0.7,
                    },
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 0.4,
                        document.prefixes["PrefixA"].local_to_global[2]: 0.6,
                    },
                ],
            )
        finally:
            vg_test_runtime._collect_mesh_loop_payload = original_collect

        self.assertEqual(result.unassigned_soup.faces, [])
        self.assertEqual(result.prefix_soups["PrefixB"].faces, [])
        soup = result.prefix_soups["PrefixA"]
        self.assertEqual(soup.faces, [(0, 1, 2)])
        self.assertEqual(
            soup.vertices,
            [
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            ],
        )
        for weight_map in soup.vertex_weights:
            self.assertLessEqual(set(weight_map.keys()), {1, 2})
        self.assertTrue(any(2 in weight_map for weight_map in soup.vertex_weights))

    def test_cross_prefix_boundary_vertex_weights_collapse_to_shared_local_group(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
            ],
            mapping_id="runtime_boundary_shared_local_weight",
        )

        original_collect = vg_test_runtime._collect_mesh_loop_payload
        try:
            vg_test_runtime._collect_mesh_loop_payload = lambda _obj: {
                "positions": [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                ],
                "polygons": [
                    {
                        "vertices": [0, 1, 2],
                        "loop_indices": [0, 1, 2],
                        "material_index": 0,
                        "use_smooth": False,
                    }
                ],
                "uv_layers": {},
                "color_layers": {},
                "loop_normals": [(0.0, 0.0, 1.0)] * 3,
            }

            mixed_weights = {
                document.prefixes["PrefixA"].local_to_global[1]: 0.2,
                document.prefixes["PrefixA"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[3]: 0.3,
            }
            result = vg_test_runtime._build_prefix_triangle_soups(
                types.SimpleNamespace(name="RuntimeObject"),
                document,
                [dict(mixed_weights), dict(mixed_weights), dict(mixed_weights)],
            )
        finally:
            vg_test_runtime._collect_mesh_loop_payload = original_collect

        self.assertEqual(result.prefix_soups["PrefixA"].faces, [(0, 1, 2)])
        self.assertEqual(result.prefix_soups["PrefixA"].vertex_weights, [{2: 1.0}, {2: 1.0}, {2: 1.0}])
        self.assertEqual(result.prefix_soups["PrefixB"].faces, [])

    def test_mixed_vertices_are_assigned_to_one_owner_prefix_not_both_outputs(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
            ],
            mapping_id="runtime_unique_owner_prefix",
        )

        original_collect = vg_test_runtime._collect_mesh_loop_payload
        try:
            vg_test_runtime._collect_mesh_loop_payload = lambda _obj: {
                "positions": [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                ],
                "polygons": [
                    {
                        "vertices": [0, 1, 2],
                        "loop_indices": [0, 1, 2],
                        "material_index": 0,
                        "use_smooth": False,
                    }
                ],
                "uv_layers": {},
                "color_layers": {},
                "loop_normals": [(0.0, 0.0, 1.0)] * 3,
            }

            mixed_weights = {
                document.prefixes["PrefixA"].local_to_global[1]: 0.2,
                document.prefixes["PrefixA"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[3]: 0.3,
            }
            result = vg_test_runtime._build_prefix_triangle_soups(
                types.SimpleNamespace(name="RuntimeObject"),
                document,
                [dict(mixed_weights), dict(mixed_weights), dict(mixed_weights)],
            )
        finally:
            vg_test_runtime._collect_mesh_loop_payload = original_collect

        self.assertEqual(result.prefix_soups["PrefixA"].faces, [(0, 1, 2)])
        self.assertEqual(result.prefix_soups["PrefixB"].faces, [])
        self.assertEqual(result.prefix_soups["PrefixA"].vertex_weights, [{2: 1.0}, {2: 1.0}, {2: 1.0}])

    def test_same_prefix_group_cuts_merge_overlapping_vertices_and_weights(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
            ],
            mapping_id="runtime_merge_same_prefix_vertices",
        )

        original_collect = vg_test_runtime._collect_mesh_loop_payload
        try:
            vg_test_runtime._collect_mesh_loop_payload = lambda _obj: {
                "positions": [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                ],
                "polygons": [
                    {
                        "vertices": [0, 1, 2],
                        "loop_indices": [0, 1, 2],
                        "material_index": 0,
                        "use_smooth": False,
                    }
                ],
                "uv_layers": {},
                "color_layers": {},
                "loop_normals": [(0.0, 0.0, 1.0)] * 3,
            }

            result = vg_test_runtime._build_prefix_triangle_soups(
                types.SimpleNamespace(name="RuntimeObject"),
                document,
                [
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 0.25,
                        document.prefixes["PrefixA"].local_to_global[2]: 0.75,
                    },
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 0.4,
                        document.prefixes["PrefixA"].local_to_global[2]: 0.6,
                    },
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 1.0,
                        document.prefixes["PrefixA"].local_to_global[2]: 0.5,
                    },
                ],
            )
        finally:
            vg_test_runtime._collect_mesh_loop_payload = original_collect

        soup = result.prefix_soups["PrefixA"]
        self.assertEqual(soup.faces, [(0, 1, 2)])
        self.assertEqual(len(soup.vertices), 3)
        self.assertEqual(
            soup.vertex_weights,
            [
                {1: 0.25, 2: 0.75},
                {1: 0.4, 2: 0.6},
                {1: 1.0, 2: 0.5},
            ],
        )

    def test_same_prefix_reversed_duplicate_faces_are_removed(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1]),
            ],
            mapping_id="runtime_remove_reversed_duplicate_faces",
        )

        original_collect = vg_test_runtime._collect_mesh_loop_payload
        try:
            vg_test_runtime._collect_mesh_loop_payload = lambda _obj: {
                "positions": [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                ],
                "polygons": [
                    {
                        "vertices": [0, 1, 2],
                        "loop_indices": [0, 1, 2],
                        "material_index": 0,
                        "use_smooth": False,
                    },
                    {
                        "vertices": [0, 2, 1],
                        "loop_indices": [3, 4, 5],
                        "material_index": 0,
                        "use_smooth": False,
                    },
                ],
                "uv_layers": {},
                "color_layers": {},
                "loop_normals": [(0.0, 0.0, 1.0)] * 6,
            }

            result = vg_test_runtime._build_prefix_triangle_soups(
                types.SimpleNamespace(name="RuntimeObject"),
                document,
                [
                    {document.prefixes["PrefixA"].local_to_global[1]: 1.0},
                    {document.prefixes["PrefixA"].local_to_global[1]: 0.5},
                    {document.prefixes["PrefixA"].local_to_global[1]: 0.75},
                ],
            )
        finally:
            vg_test_runtime._collect_mesh_loop_payload = original_collect

        soup = result.prefix_soups["PrefixA"]
        self.assertEqual(soup.faces, [(0, 1, 2)])
        self.assertEqual(len(soup.vertices), 3)
        self.assertEqual(soup.vertex_weights, [{1: 1.0}, {1: 0.5}, {1: 0.75}])

    def test_overlapping_vertices_merge_weights_from_removed_duplicate_faces(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
            ],
            mapping_id="runtime_merge_duplicate_face_weights",
        )

        original_collect = vg_test_runtime._collect_mesh_loop_payload
        try:
            vg_test_runtime._collect_mesh_loop_payload = lambda _obj: {
                "positions": [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                    (0.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                    (1.0, 0.0, 0.0),
                ],
                "polygons": [
                    {
                        "vertices": [0, 1, 2],
                        "loop_indices": [0, 1, 2],
                        "material_index": 0,
                        "use_smooth": False,
                    },
                    {
                        "vertices": [3, 4, 5],
                        "loop_indices": [3, 4, 5],
                        "material_index": 0,
                        "use_smooth": False,
                    },
                ],
                "uv_layers": {},
                "color_layers": {},
                "loop_normals": [(0.0, 0.0, 1.0)] * 6,
            }

            result = vg_test_runtime._build_prefix_triangle_soups(
                types.SimpleNamespace(name="RuntimeObject"),
                document,
                [
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 0.2,
                        document.prefixes["PrefixA"].local_to_global[2]: 0.4,
                    },
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 0.3,
                    },
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 0.5,
                    },
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 0.8,
                        document.prefixes["PrefixA"].local_to_global[2]: 0.6,
                    },
                    {
                        document.prefixes["PrefixA"].local_to_global[2]: 0.9,
                        document.prefixes["PrefixA"].local_to_global[1]: 0.1,
                    },
                    {
                        document.prefixes["PrefixA"].local_to_global[1]: 0.7,
                        document.prefixes["PrefixA"].local_to_global[2]: 0.1,
                    },
                ],
            )
        finally:
            vg_test_runtime._collect_mesh_loop_payload = original_collect

        soup = result.prefix_soups["PrefixA"]
        self.assertEqual(soup.faces, [(0, 1, 2)])
        self.assertEqual(len(soup.vertices), 3)
        self.assertEqual(
            soup.vertex_weights,
            [
                {1: 0.8, 2: 0.6},
                {1: 0.7, 2: 0.1},
                {1: 0.5, 2: 0.9},
            ],
        )


if __name__ == "__main__":
    unittest.main()
