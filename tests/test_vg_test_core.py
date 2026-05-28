import importlib.util
import sys
import types
import unittest
from pathlib import Path


if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace(data=types.SimpleNamespace(objects={}))

sys.modules.setdefault("TheHerta4", types.ModuleType("TheHerta4"))
sys.modules.setdefault("TheHerta4.common", types.ModuleType("TheHerta4.common"))

module_path = Path(__file__).resolve().parents[1] / "common" / "vg_test_core.py"
spec = importlib.util.spec_from_file_location("TheHerta4.common.vg_test_core", module_path)
vg_test_core = importlib.util.module_from_spec(spec)
sys.modules["TheHerta4.common.vg_test_core"] = vg_test_core
spec.loader.exec_module(vg_test_core)


class VGTestCoreTests(unittest.TestCase):
    def test_build_mapping_document_uses_selection_order(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[0, 2, 5]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[0, 1]),
            ],
            mapping_id="demo",
        )

        self.assertEqual(document.prefix_order, ["PrefixA", "PrefixB"])
        self.assertEqual(document.prefixes["PrefixA"].local_to_global, {0: 0, 2: 1, 5: 2})
        self.assertEqual(document.prefixes["PrefixB"].local_to_global, {0: 3, 1: 4})

    def test_duplicate_prefixes_are_rejected(self):
        with self.assertRaises(vg_test_core.VGTestError):
            vg_test_core.build_mapping_document(
                [
                    vg_test_core.VGTestObjectInfo(name="A", prefix="Same", numeric_groups=[0]),
                    vg_test_core.VGTestObjectInfo(name="B", prefix="Same", numeric_groups=[1]),
                ]
            )

    def test_mapping_document_round_trip(self):
        original = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[0, 1]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[7, 9]),
            ],
            mapping_id="roundtrip",
        )

        serialized = vg_test_core.serialize_mapping_document(original)
        parsed = vg_test_core.parse_mapping_document(serialized)

        self.assertEqual(parsed.mapping_id, "roundtrip")
        self.assertEqual(parsed.prefix_order, ["PrefixA", "PrefixB"])
        self.assertEqual(parsed.prefixes["PrefixB"].local_to_global, {7: 2, 9: 3})

    def test_classify_face_prefixes_duplicates_overlap(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[0]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[0]),
            ],
            mapping_id="faces",
        )
        face_prefixes = vg_test_core.classify_face_prefixes(
            [
                {0},
                {1},
                {0, 1},
            ],
            document,
        )

        self.assertEqual(face_prefixes[0], {"PrefixA"})
        self.assertEqual(face_prefixes[1], {"PrefixB"})
        self.assertEqual(face_prefixes[2], {"PrefixA", "PrefixB"})

    def test_find_mixed_prefix_vertex_indices_detects_cross_range_vertices(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[7, 10]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[19, 22, 35]),
            ],
            mapping_id="verts",
        )

        mixed = vg_test_core.find_mixed_prefix_vertex_indices(
            [
                {0, 1},
                {2, 3, 4},
                {0, 2},
                set(),
            ],
            document,
        )

        self.assertEqual(mixed, [2])

    def test_resolve_unique_shared_local_group_detects_common_local_group(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
            ],
            mapping_id="shared",
        )

        shared = vg_test_core.resolve_unique_shared_local_group({0, 1, 2, 3}, document)

        self.assertEqual(shared, 2)

    def test_build_target_prefix_vertex_weights_collapses_to_shared_local_group(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
            ],
            mapping_id="collapse",
        )

        collapsed_a = vg_test_core.build_target_prefix_vertex_weights(
            {
                document.prefixes["PrefixA"].local_to_global[1]: 0.2,
                document.prefixes["PrefixA"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[3]: 0.3,
            },
            "PrefixA",
            document,
        )
        collapsed_b = vg_test_core.build_target_prefix_vertex_weights(
            {
                document.prefixes["PrefixA"].local_to_global[1]: 0.2,
                document.prefixes["PrefixA"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[3]: 0.3,
            },
            "PrefixB",
            document,
        )

        self.assertEqual(
            collapsed_a,
            {document.prefixes["PrefixA"].local_to_global[2]: 1.0},
        )
        self.assertEqual(
            collapsed_b,
            {document.prefixes["PrefixB"].local_to_global[2]: 1.0},
        )

    def test_resolve_vertex_split_profile_assigns_shared_local_group_vertex_to_single_owner(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
            ],
            mapping_id="split_profile_boundary",
        )

        profile = vg_test_core.resolve_vertex_split_profile(
            {
                document.prefixes["PrefixA"].local_to_global[1]: 0.2,
                document.prefixes["PrefixA"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[3]: 0.3,
            },
            document,
        )

        self.assertEqual(profile.owner_prefix, "PrefixB")
        self.assertEqual(profile.compatible_prefixes, {"PrefixB"})
        self.assertFalse(profile.is_boundary)

    def test_resolve_vertex_split_profile_assigns_conflict_vertex_to_dominant_prefix(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[7, 10]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[19, 22, 35]),
            ],
            mapping_id="split_profile_dominant",
        )

        profile = vg_test_core.resolve_vertex_split_profile(
            {
                document.prefixes["PrefixA"].local_to_global[7]: 0.3,
                document.prefixes["PrefixB"].local_to_global[19]: 0.7,
            },
            document,
        )

        self.assertEqual(profile.owner_prefix, "PrefixB")
        self.assertEqual(profile.compatible_prefixes, {"PrefixB"})
        self.assertFalse(profile.is_boundary)

    def test_build_target_prefix_vertex_weights_distributes_across_multiple_shared_local_groups(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2, 4]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3, 4]),
            ],
            mapping_id="collapse_multi_shared",
        )

        collapsed_a = vg_test_core.build_target_prefix_vertex_weights(
            {
                document.prefixes["PrefixA"].local_to_global[1]: 0.2,
                document.prefixes["PrefixA"].local_to_global[2]: 0.3,
                document.prefixes["PrefixA"].local_to_global[4]: 0.1,
                document.prefixes["PrefixB"].local_to_global[2]: 0.1,
                document.prefixes["PrefixB"].local_to_global[3]: 0.2,
                document.prefixes["PrefixB"].local_to_global[4]: 0.1,
            },
            "PrefixA",
            document,
        )

        self.assertAlmostEqual(sum(collapsed_a.values()), 1.0)
        self.assertEqual(set(collapsed_a.keys()), {
            document.prefixes["PrefixA"].local_to_global[2],
            document.prefixes["PrefixA"].local_to_global[4],
        })

    def test_filter_vertex_weights_for_prefix_does_not_require_shared_local_group(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[7, 10]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[19, 22, 35]),
            ],
            mapping_id="owner_filter",
        )

        filtered = vg_test_core.filter_vertex_weights_for_prefix(
            {
                document.prefixes["PrefixA"].local_to_global[7]: 0.3,
                document.prefixes["PrefixB"].local_to_global[19]: 0.7,
            },
            "PrefixB",
            document,
        )

        self.assertEqual(
            filtered,
            {document.prefixes["PrefixB"].local_to_global[19]: 0.7},
        )

    def test_get_vertex_compatible_prefixes_from_weights_returns_single_owner_prefix(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
            ],
            mapping_id="boundary_compatible",
        )

        compatible_prefixes = vg_test_core.get_vertex_compatible_prefixes_from_weights(
            {
                document.prefixes["PrefixA"].local_to_global[1]: 0.2,
                document.prefixes["PrefixA"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[2]: 0.5,
                document.prefixes["PrefixB"].local_to_global[3]: 0.3,
            },
            document,
        )

        self.assertEqual(compatible_prefixes, {"PrefixB"})

    def test_classify_faces_by_vertex_compatibility_assigns_face_to_both_compatible_prefixes(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
            ],
            mapping_id="face_compat",
        )

        face_prefixes = vg_test_core.classify_faces_by_vertex_compatibility(
            [
                [0, 1, 2],
            ],
            [
                {document.prefixes["PrefixA"].local_to_global[1], document.prefixes["PrefixA"].local_to_global[2], document.prefixes["PrefixB"].local_to_global[2], document.prefixes["PrefixB"].local_to_global[3]},
                {document.prefixes["PrefixA"].local_to_global[2], document.prefixes["PrefixB"].local_to_global[2]},
                {document.prefixes["PrefixA"].local_to_global[1], document.prefixes["PrefixA"].local_to_global[2], document.prefixes["PrefixB"].local_to_global[2], document.prefixes["PrefixB"].local_to_global[3]},
            ],
            document,
        )

        self.assertEqual(face_prefixes, [{"PrefixA", "PrefixB"}])

    def test_classify_faces_by_vertex_compatibility_raises_detailed_chinese_error(self):
        document = vg_test_core.build_mapping_document(
            [
                vg_test_core.VGTestObjectInfo(name="A", prefix="PrefixA", numeric_groups=[1, 2]),
                vg_test_core.VGTestObjectInfo(name="B", prefix="PrefixB", numeric_groups=[2, 3]),
                vg_test_core.VGTestObjectInfo(name="C", prefix="PrefixC", numeric_groups=[4, 5]),
            ],
            mapping_id="face_error",
        )

        with self.assertRaises(vg_test_core.VGTestError) as error:
            vg_test_core.classify_faces_by_vertex_compatibility(
                [
                    [118, 117, 103],
                ],
                ([set() for _ in range(103)]
                 + [{document.prefixes["PrefixA"].local_to_global[1], document.prefixes["PrefixA"].local_to_global[2]}]
                 + [set() for _ in range(13)]
                 + [{document.prefixes["PrefixB"].local_to_global[2], document.prefixes["PrefixB"].local_to_global[3]}]
                 + [{document.prefixes["PrefixC"].local_to_global[4], document.prefixes["PrefixC"].local_to_global[5]}]),
                document,
            )

        message = str(error.exception)
        self.assertIn("VG Test 切割失败", message)
        self.assertIn("面索引：0", message)
        self.assertIn("118, 117, 103", message)
        self.assertIn("顶点 118", message)

    def test_build_runtime_vgtest_copy_name_inserts_before_copy(self):
        self.assertEqual(vg_test_core.build_runtime_vgtest_copy_name("Body_copy"), "Body_vgtest_copy")
        self.assertEqual(vg_test_core.build_runtime_vgtest_copy_name("Body_chain1_copy"), "Body_chain1_vgtest_copy")

    def test_strip_runtime_vgtest_suffix_handles_unassigned_copy(self):
        self.assertEqual(
            vg_test_core.strip_runtime_vgtest_suffix("Body_vgtest_unassigned_copy"),
            "Body",
        )
        self.assertEqual(
            vg_test_core.strip_runtime_vgtest_suffix("Body_chain1_vgtest_unassigned_copy"),
            "Body",
        )

    def test_replace_runtime_object_prefix_replaces_existing_prefix_once(self):
        replaced = vg_test_core.replace_runtime_object_prefix(
            "LOD0.fbb18630-24567-0_copy",
            "LOD0.fbb18630-24567-0",
            "LOD0.aaaaaaaa-11111-0",
        )

        self.assertEqual(replaced, "LOD0.aaaaaaaa-11111-0_copy")


if __name__ == "__main__":
    unittest.main()
