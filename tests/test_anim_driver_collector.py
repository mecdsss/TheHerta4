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


PKG = "_anim_driver_collector_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeBase:
    bl_idname = "SSMTNode_AnimDriver_Base"


_install_module(
    f"{PKG}.blueprint.anim_driver_base",
    SSMTNode_AnimDriver_Base=_FakeBase,
    SSMTSocketAnimDriver=object,
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "anim_driver_collector.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.anim_driver_collector", module_path)
collector_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = collector_module
spec.loader.exec_module(collector_module)

AnimationDriverCollector = collector_module.AnimationDriverCollector


class _FakeNode:
    def __init__(self, name, bl_idname, segment, fps=None, playback_rate=None):
        self.name = name
        self.bl_idname = bl_idname
        self._segment = segment
        if fps is not None:
            self.fps = fps
        if playback_rate is not None:
            self.playback_rate = playback_rate

    def generate_ini_segment(self, connected_nodes=None):
        return self._segment


class AnimationDriverCollectorTests(unittest.TestCase):
    def test_merge_paragraph_sections_tolerates_leading_comment_before_first_section(self):
        collector = AnimationDriverCollector(types.SimpleNamespace(nodes=[], links=[]))

        merged = collector._merge_paragraph_sections([
            "; comment before section\n[Constants]\nvalue = 1\n[Present]\nrun = yes"
        ])

        self.assertIn("[Constants]", merged)
        self.assertIn("value = 1", merged)
        self.assertIn("[Present]", merged)

    def test_collect_keeps_runtime_segments_with_same_fps_but_different_playback_rate(self):
        runtime_a = _FakeNode(
            name="RuntimeA",
            bl_idname="SSMTNode_AnimDriver_Runtime",
            segment="[Constants]\nglobal $speed_auto1 = 1",
            fps=30,
            playback_rate=1,
        )
        runtime_b = _FakeNode(
            name="RuntimeB",
            bl_idname="SSMTNode_AnimDriver_Runtime",
            segment="[Constants]\nglobal $speed_auto2 = 2",
            fps=30,
            playback_rate=2,
        )
        group = types.SimpleNamespace(nodes=[runtime_a, runtime_b], links=[])

        result = AnimationDriverCollector(group).collect()

        self.assertEqual(len(result), 2)
        merged_text = "\n".join(paragraph["ini_content"] for paragraph in result)
        self.assertIn("global $speed_auto1 = 1", merged_text)
        self.assertIn("global $speed_auto2 = 2", merged_text)

    def test_branched_topological_order_is_stable(self):
        root = _FakeNode("Root", "Driver", "[Present]\nroot = 1")
        branch_b = _FakeNode("BranchB", "Driver", "[Present]\nb = 1")
        branch_a = _FakeNode("BranchA", "Driver", "[Present]\na = 1")
        end = _FakeNode("End", "Driver", "[Present]\nend = 1")

        socket = types.SimpleNamespace(bl_idname="SSMTSocketAnimDriver")

        def link(from_node, to_node):
            return types.SimpleNamespace(
                from_node=from_node,
                to_node=to_node,
                from_socket=socket,
                to_socket=socket,
            )

        links = [
            link(root, branch_b),
            link(root, branch_a),
            link(branch_b, end),
            link(branch_a, end),
        ]
        expected = ["Root", "BranchA", "BranchB", "End"]
        for nodes in (
            [branch_b, end, root, branch_a],
            [end, branch_a, branch_b, root],
        ):
            collector = AnimationDriverCollector(types.SimpleNamespace(nodes=nodes, links=links))
            graph, node_set = collector._build_graph(nodes)
            paragraph = collector._divide_into_paragraphs(graph, node_set)[0]
            ordered = collector._topological_sort(paragraph, graph)
            self.assertEqual([node.name for node in ordered], expected)

    def test_collect_orders_disconnected_paragraphs_by_node_name(self):
        node_b = _FakeNode("NodeB", "Driver", "[Present]\nb = 1")
        node_a = _FakeNode("NodeA", "Driver", "[Present]\na = 1")

        result = AnimationDriverCollector(
            types.SimpleNamespace(nodes=[node_b, node_a], links=[])
        ).collect()

        self.assertEqual(
            [paragraph["node_names"] for paragraph in result],
            [["NodeA"], ["NodeB"]],
        )


if __name__ == "__main__":
    unittest.main()
