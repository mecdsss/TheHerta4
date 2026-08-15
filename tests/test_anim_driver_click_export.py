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


PKG = "_anim_driver_click_export_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []


class _NodeGroups(list):
    def get(self, name, default=None):
        return next((group for group in self if getattr(group, "name", "") == name), default)


_bpy_props = _install_module(
    "bpy.props",
    IntProperty=lambda **_kwargs: None,
    StringProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
)
_bpy_types = _install_module(
    "bpy.types",
    PropertyGroup=object,
    UIList=object,
    Operator=object,
)
_fake_bpy = _install_module(
    "bpy",
    props=_bpy_props,
    types=_bpy_types,
    data=types.SimpleNamespace(node_groups=_NodeGroups()),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)


class _FakeAnimBase:
    bl_idname = "SSMTNode_AnimDriver_Base"


_install_module(
    f"{PKG}.blueprint.anim_driver_base",
    ANIM_DRIVER_INPUT_SOCKET_NAME="链输入",
    ANIM_DRIVER_OUTPUT_SOCKET_NAME="链输出",
    SSMTNode_AnimDriver_Base=_FakeAnimBase,
    SSMTSocketAnimDriver=object,
)
_install_module(
    f"{PKG}.blueprint.node_postprocess_draginteraction",
    DEFAULT_MOD_NAMESPACE="A",
    MAX_ZONES=256,
    is_postprocess_node_on_export_chain=lambda _tree, node: getattr(
        node, "on_export_chain", True
    ),
)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    normalize_variable_name=lambda value: str(value or "").strip().lstrip("$"),
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "anim_driver_click_export.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.anim_driver_click_export", module_path)
click_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = click_module
spec.loader.exec_module(click_module)

collector_path = Path(__file__).resolve().parents[1] / "blueprint" / "anim_driver_collector.py"
collector_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.anim_driver_collector", collector_path
)
collector_module = importlib.util.module_from_spec(collector_spec)
sys.modules[collector_spec.name] = collector_module
collector_spec.loader.exec_module(collector_module)


class _FakeTree:
    def __init__(self, name, nodes=(), *, animation_driver=False):
        self.name = name
        self.bl_idname = "SSMTBlueprintTreeType"
        self.nodes = list(nodes)
        self.links = []
        self._properties = {"is_animation_driver": animation_driver}

    def get(self, key, default=None):
        return self._properties.get(key, default)


def _drag_node(namespace="A"):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_DragInteraction",
        mute=False,
        enable_shapekey_drive=True,
        _resolve_namespace=lambda _ini_path: namespace,
    )


def _owner_tree(anim_name, drag_nodes):
    postprocess = types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_AnimDriver",
        mute=False,
        on_export_chain=True,
        blueprint_name=anim_name,
    )
    return _FakeTree("Owner", [postprocess, *drag_nodes])


def _click_node(targets=("$Swap",), zone=2, *, owners=1, namespace="A"):
    anim_tree = _FakeTree("AnimTree", animation_driver=True)
    owners_list = [_owner_tree(anim_tree.name, [_drag_node(namespace)]) for _ in range(owners)]
    _fake_bpy.data.node_groups = _NodeGroups([anim_tree, *owners_list])

    node = click_module.SSMTNode_AnimDriver_ClickExport.__new__(
        click_module.SSMTNode_AnimDriver_ClickExport
    )
    node.id_data = anim_tree
    node.name = "Click Export"
    node.mute = False
    node.click_zone_id = zone
    node.cycle_length = 0
    node.click_target_list = [types.SimpleNamespace(variable_name=value) for value in targets]
    return node


class ClickExportTests(unittest.TestCase):
    def test_collector_exports_click_driver_as_present_only_reference_segment(self):
        node = _click_node(targets=("$Swap", "$Other"), zone=6)
        node.id_data.nodes.append(node)

        paragraphs = collector_module.AnimationDriverCollector(node.id_data).collect()

        self.assertEqual(len(paragraphs), 1)
        content = paragraphs[0]["ini_content"]
        self.assertEqual(content.count("[Present]"), 1)
        # 值仲裁、变量为主：受控变量本身不重新声明，只声明每绑定的 ckprev 辅助变量
        self.assertEqual(content.count("[Constants]"), 1)
        self.assertNotIn("global $Swap", content)
        self.assertIn("global $ssmtdrag_ckprev_A_Swap = 0", content)
        self.assertIn("global $ssmtdrag_ckprev_A_Other = 0", content)
        self.assertIn("store = $Swap, ResourceDragShapeKeyClickCountF_A, 6", content)
        self.assertIn("store = $Other, ResourceDragShapeKeyClickCountF_A, 6", content)
        self.assertIn("$ssmtdrag_seed_pending_A = 1", content)

    def test_generate_ini_segment_references_owned_variables_without_redeclaring_them(self):
        node = _click_node(targets=("$Swap", "Swap", "", "$Other"), zone=7)

        content = node.generate_ini_segment()

        self.assertNotIn("global $Swap", content)
        self.assertNotIn("global $Other", content)
        self.assertEqual(content.count("store = $Swap,"), 1)
        self.assertIn("if $ssmtdrag_booted_A == 1 && $ssmtdrag_seed_pending_A == 0", content)
        self.assertIn("store = $Swap, ResourceDragShapeKeyClickCountF_A, 7", content)
        self.assertIn("store = $Other, ResourceDragShapeKeyClickCountF_A, 7", content)

    def test_generate_ini_segment_arbitrates_variable_first(self):
        """回归：点击计数导出必须做值仲裁——变量变化(热键)时置 seed_pending
        触发播种推回缓冲且不回读；变量未变才拉取缓冲。旧实现每帧无条件
        store 会把快捷键切换下一瞬间顶掉。"""
        node = _click_node(targets=("$Swap",), zone=7)

        content = node.generate_ini_segment()

        self.assertIn("if $Swap != $ssmtdrag_ckprev_A_Swap", content)
        self.assertIn("$ssmtdrag_ckprev_A_Swap = $Swap", content)
        self.assertIn("$ssmtdrag_seed_pending_A = 1", content)
        self.assertIn("else", content)
        self.assertIn("\t\tstore = $Swap, ResourceDragShapeKeyClickCountF_A, 7", content)
        # store 只在变量未变分支里出现
        self.assertEqual(content.count("store = $Swap,"), 1)

    def test_generate_ini_segment_fails_closed_when_animation_tree_has_multiple_drag_owners(self):
        node = _click_node(owners=2)

        self.assertEqual(len(node._find_drag_drive_nodes()), 2)
        self.assertIsNone(node._find_drag_drive_node())
        self.assertEqual(node.generate_ini_segment(), "")

    def test_muted_postprocess_owner_does_not_activate_click_export(self):
        node = _click_node()
        owner = next(tree for tree in _fake_bpy.data.node_groups if tree.name == "Owner")
        owner.nodes[0].mute = True

        self.assertEqual(node._find_anim_owner_trees(), [])
        self.assertEqual(node.generate_ini_segment(), "")

    def test_disconnected_postprocess_owner_does_not_activate_click_export(self):
        node = _click_node()
        owner = next(tree for tree in _fake_bpy.data.node_groups if tree.name == "Owner")
        owner.nodes[0].on_export_chain = False

        self.assertEqual(node._find_anim_owner_trees(), [])
        self.assertEqual(node.generate_ini_segment(), "")

    def test_muted_drag_node_does_not_activate_click_export(self):
        node = _click_node()
        owner = next(tree for tree in _fake_bpy.data.node_groups if tree.name == "Owner")
        owner.nodes[1].mute = True

        self.assertEqual(node._find_drag_drive_nodes(), [])
        self.assertEqual(node.generate_ini_segment(), "")

    def test_disconnected_drag_node_does_not_create_a_second_owner(self):
        node = _click_node()
        owner = next(tree for tree in _fake_bpy.data.node_groups if tree.name == "Owner")
        disconnected_drag = _drag_node("Unused")
        disconnected_drag.on_export_chain = False
        owner.nodes.append(disconnected_drag)

        self.assertEqual(node._find_drag_drive_nodes(), [owner.nodes[1]])
        self.assertIn("ResourceDragShapeKeyClickCountF_A", node.generate_ini_segment())

    def test_generate_ini_segment_rejects_runtime_zone_outside_stable_capacity(self):
        node = _click_node(zone=256)

        self.assertEqual(node.generate_ini_segment(), "")

    def test_generate_ini_segment_accepts_last_stable_zone(self):
        node = _click_node(zone=255)

        content = node.generate_ini_segment()

        self.assertIn("ResourceDragShapeKeyClickCountF_A, 255", content)

    def test_compute_cycle_from_swaps_uses_maximum_matching_active_option_count(self):
        node = _click_node(targets=("$Swap",))
        active = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            mute=False,
            custom_var_name="",
            assigned_variable_name="$Swap",
            input_slot_count=5,
        )
        muted = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            mute=True,
            custom_var_name="$Swap",
            assigned_variable_name="",
            input_slot_count=99,
        )
        _fake_bpy.data.node_groups.append(_FakeTree("SwapTree", [active, muted]))

        self.assertEqual(node._compute_cycle_from_swaps(), (5, 1))

    def test_compute_cycle_from_multiple_targets_uses_global_maximum_by_design(self):
        node = _click_node(targets=("$Hair", "$Outfit"))
        hair = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap", mute=False,
            custom_var_name="$Hair", assigned_variable_name="", input_slot_count=3,
        )
        outfit = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap", mute=False,
            custom_var_name="", assigned_variable_name="$Outfit", input_slot_count=7,
        )
        _fake_bpy.data.node_groups.extend([
            _FakeTree("HairBlueprint", [hair]),
            _FakeTree("OutfitBlueprint", [outfit]),
        ])

        self.assertEqual(node._compute_cycle_from_swaps(), (7, 2))


if __name__ == "__main__":
    unittest.main()
