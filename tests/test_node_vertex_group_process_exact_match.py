import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_node_vg_process_exact_match_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Node=object, Object=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(texts={}),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace(
    extract_prefix_info=lambda name: (name.split(".", 1)[0], ".") if "." in str(name) else None,
    normalize_prefix=lambda value: str(value or "").strip(),
    split_lod_prefix=lambda name: (name.split(".", 1)[0], name.split(".", 1)[1]) if "." in str(name) else ("", str(name or "").strip()),
    parse_prefix_parts=lambda prefix: {"bare_unique_str": str(prefix or "").strip().split(".", 1)[-1]},
))
_install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_vertex_group_process.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_vertex_group_process", module_path)
node_vertex_group_process = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.node_vertex_group_process"] = node_vertex_group_process
spec.loader.exec_module(node_vertex_group_process)


class _FakeMatchNode:
    def __init__(self, name, mapping, target_hash, exact_hash_match):
        self.name = name
        self.mapping_text_name = ""
        self.target_object = ""
        self.target_hash = target_hash
        self.exact_hash_match = exact_hash_match
        self._mapping = dict(mapping)

    def get_mapping_dict(self):
        return dict(self._mapping)


class ExactMatchPriorityTests(unittest.TestCase):
    """测试精确匹配顶点组处理的优先级逻辑"""

    def test_target_hash_with_dot_suffix_is_not_collapsed_into_parent_hash(self):
        """测试带点后缀的目标哈希不会被折叠为父级哈希"""
        process_node = node_vertex_group_process.SSMTNode_VertexGroupProcess()

        self.assertTrue(
            process_node._matches_target_hash(
                "LOD0.ae1ab184-71202-29187.00裙子_copy",
                "LOD0.ae1ab184-71202-29187.00",
            )
        )
        self.assertFalse(
            process_node._matches_target_hash(
                "LOD0.ae1ab184-71202-29187.00裙子_copy",
                "LOD0.ae1ab184-71202-29187",
            )
        )
        self.assertTrue(
            process_node._matches_target_hash(
                "LOD0.ae1ab184-71202-29187.袜子_copy",
                "LOD0.ae1ab184-71202-29187",
            )
        )

    def test_exact_match_stops_further_merging(self):
        """测试精确匹配节点阻止后续映射的合并"""
        process_node = node_vertex_group_process.SSMTNode_VertexGroupProcess()
        exact_node = _FakeMatchNode(
            "Exact",
            {"DEF-A": "1", "DEF-B": "2"},
            "LOD0.ae1ab184-71202-29187",
            True,
        )
        later_exact_node = _FakeMatchNode(
            "ExactLater",
            {"DEF-A": "9"},
            "LOD0.ae1ab184-71202-29187",
            True,
        )
        normal_node = _FakeMatchNode(
            "Normal",
            {"DEF-C": "3"},
            "LOD0.ae1ab184-71202-29187",
            False,
        )
        mapping_nodes = [
            {"node": exact_node, "target_hash": exact_node.target_hash, "index": 1, "type": "input"},
            {"node": later_exact_node, "target_hash": later_exact_node.target_hash, "index": 2, "type": "input"},
            {"node": normal_node, "target_hash": normal_node.target_hash, "index": 3, "type": "input"},
        ]

        merged = process_node.get_merged_mapping_for_object(
            "LOD0.ae1ab184-71202-29187.袜子_copy",
            mapping_nodes,
        )

        self.assertEqual(merged, {"DEF-A": "1", "DEF-B": "2"})

    def test_threadsafe_exact_match_stops_further_merging(self):
        """测试线程安全的精确匹配也阻止后续映射合并"""
        process_node = node_vertex_group_process.SSMTNode_VertexGroupProcess()
        prepared_data = {
            "nodes": [
                {
                    "target_hash": "LOD0.ae1ab184-71202-29187",
                    "index": 1,
                    "type": "input",
                    "exact_match": True,
                    "mapping": {"DEF-A": "1"},
                },
                {
                    "target_hash": "LOD0.ae1ab184-71202-29187",
                    "index": 2,
                    "type": "input",
                    "exact_match": False,
                    "mapping": {"DEF-B": "2"},
                },
            ],
            "global_mappings": {},
        }

        merged = process_node.compute_mapping_for_object_threadsafe(
            "LOD0.ae1ab184-71202-29187.袜子_copy",
            prepared_data,
            text_cache={},
        )

        self.assertEqual(merged, {"DEF-A": "1"})

    def test_exact_hash_match_can_isolate_more_specific_hash(self):
        """测试精确哈希匹配能隔离更具体的哈希"""
        process_node = node_vertex_group_process.SSMTNode_VertexGroupProcess()
        specific_node = _FakeMatchNode(
            "Specific",
            {"DEF-Skirt": "75"},
            "LOD0.ae1ab184-71202-29187.00",
            True,
        )
        generic_node = _FakeMatchNode(
            "Generic",
            {"DEF-Skirt": "146"},
            "LOD0.ae1ab184-71202-29187",
            False,
        )
        mapping_nodes = [
            {"node": generic_node, "target_hash": generic_node.target_hash, "index": 1, "type": "input"},
            {"node": specific_node, "target_hash": specific_node.target_hash, "index": 2, "type": "input"},
        ]

        merged = process_node.get_merged_mapping_for_object(
            "LOD0.ae1ab184-71202-29187.00裙子_copy",
            mapping_nodes,
        )

        self.assertEqual(merged, {"DEF-Skirt": "75"})


if __name__ == "__main__":
    unittest.main()
