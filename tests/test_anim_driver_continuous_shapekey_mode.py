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


PKG = "_anim_driver_continuous_shapekey_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []

for package_name in (f"{PKG}.common",):
    package = _install_module(package_name)
    package.__path__ = []


_bpy_props = _install_module(
    "bpy.props",
    FloatProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
    StringProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
)
_bpy_types = _install_module(
    "bpy.types",
    Menu=object,
    NodeTree=object,
    NodeSocket=object,
    Node=object,
    SpaceNodeEditor=object,
    PropertyGroup=object,
    UIList=object,
    Operator=object,
)
_fake_bpy = _install_module(
    "bpy",
    props=_bpy_props,
    types=_bpy_types,
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
    app=types.SimpleNamespace(
        handlers=types.SimpleNamespace(
            persistent=lambda func: func,
            load_post=[],
        )
    ),
    data=types.SimpleNamespace(node_groups=[], objects={}),
)
_install_module(
    f"{PKG}.blueprint.node_base",
    SSMTBlueprintTree=object,
    SSMTNodeBase=object,
    refresh_blueprint_node_colors=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name=""),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(NTEMI="NTEMI"),
)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    allocate_continuous_shapekey_index_variable_name=lambda **_kwargs: "continuous_shapekey_frame1",
    mark_variable_name_used=lambda *_args, **_kwargs: None,
    normalize_variable_name=lambda value: str(value or "").strip().lstrip("$"),
)


def _load_blueprint_module(module_name):
    module_path = Path(__file__).resolve().parents[1] / "blueprint" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


anim_driver_base = _load_blueprint_module("anim_driver_base")
forward_play_module = _load_blueprint_module("anim_driver_forward_play")
pingpong_module = _load_blueprint_module("anim_driver_pingpong")


class _FakeShapeKeyBlock:
    def __init__(self, name):
        self.name = name


class _FakeShapeKeyBlocks(list):
    pass


class _FakeContinuousCollection(list):
    def add(self):
        item = types.SimpleNamespace(shape_key_name="", variable_name="")
        self.append(item)
        return item

    def remove(self, index):
        del self[index]


class _FakeDrivenItem:
    def __init__(self, variable_name):
        self.variable_name = variable_name


class _FakeTree:
    def __init__(self, name, is_animation_driver=False, nodes=None):
        self.name = name
        self.bl_idname = "SSMTBlueprintTreeType"
        self.nodes = list(nodes or [])
        self.links = []
        self._props = {"is_animation_driver": is_animation_driver}

    def get(self, key, default=None):
        return self._props.get(key, default)


class _FakeAnimDriverRefNode:
    bl_idname = "SSMTNode_PostProcess_AnimDriver"

    def __init__(self, blueprint_name):
        self.blueprint_name = blueprint_name


class _FakeShapeKeyVarItem:
    def __init__(self, shape_key_name, custom_variable_name="", assigned_variable_name=""):
        self.shape_key_name = shape_key_name
        self.custom_variable_name = custom_variable_name
        self.assigned_variable_name = assigned_variable_name


class _FakeShapeKeyConfigNode:
    bl_idname = "SSMTNode_PostProcess_ShapeKey"

    def __init__(self, items):
        self.shapekey_variable_items = items


class AnimDriverContinuousShapeKeyTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.node_groups = []
        _fake_bpy.data.objects = {}

    def test_refresh_collects_shape_keys_in_object_order_and_matches_parent_variables(self):
        anim_tree = _FakeTree("动画驱动蓝图", is_animation_driver=True)
        source_tree = _FakeTree(
            "主蓝图",
            nodes=[
                _FakeAnimDriverRefNode("动画驱动蓝图"),
                _FakeShapeKeyConfigNode([
                    _FakeShapeKeyVarItem("Frame_001", custom_variable_name="Freq_Frame_001"),
                    _FakeShapeKeyVarItem("Frame_002", assigned_variable_name="Freq_Frame_002"),
                ]),
            ],
        )
        _fake_bpy.data.node_groups = [anim_tree, source_tree]
        _fake_bpy.data.objects["Target"] = types.SimpleNamespace(
            name="Target",
            type="MESH",
            data=types.SimpleNamespace(
                shape_keys=types.SimpleNamespace(
                    key_blocks=_FakeShapeKeyBlocks([
                        _FakeShapeKeyBlock("Basis"),
                        _FakeShapeKeyBlock("Frame_001"),
                        _FakeShapeKeyBlock("Frame_002"),
                    ])
                )
            ),
        )

        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Forward"
        node.id_data = anim_tree
        node.continuous_target_object = "Target"
        node.continuous_shape_key_items = _FakeContinuousCollection()

        count, missing = anim_driver_base.SSMTNode_AnimDriver_Base._rebuild_continuous_shape_key_items(
            node,
            _fake_bpy.data.objects["Target"].data.shape_keys.key_blocks,
            anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map(anim_tree),
        )

        self.assertEqual(count, 2)
        self.assertEqual(missing, 0)
        self.assertEqual(
            [(item.shape_key_name, item.variable_name) for item in node.continuous_shape_key_items],
            [("Frame_001", "$Freq_Frame_001"), ("Frame_002", "$Freq_Frame_002")],
        )

    def test_refresh_applies_prefix_filter_before_building_continuous_list(self):
        anim_tree = _FakeTree("动画驱动蓝图", is_animation_driver=True)
        source_tree = _FakeTree(
            "主蓝图",
            nodes=[
                _FakeAnimDriverRefNode("动画驱动蓝图"),
                _FakeShapeKeyConfigNode([
                    _FakeShapeKeyVarItem("Idle_001", assigned_variable_name="Freq_Idle_001"),
                    _FakeShapeKeyVarItem("Idle_002", assigned_variable_name="Freq_Idle_002"),
                    _FakeShapeKeyVarItem("Talk_001", assigned_variable_name="Freq_Talk_001"),
                ]),
            ],
        )
        _fake_bpy.data.node_groups = [anim_tree, source_tree]
        key_blocks = _FakeShapeKeyBlocks([
            _FakeShapeKeyBlock("Basis"),
            _FakeShapeKeyBlock("Idle_001"),
            _FakeShapeKeyBlock("Idle_002"),
            _FakeShapeKeyBlock("Talk_001"),
        ])

        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Forward"
        node.id_data = anim_tree
        node.continuous_shape_key_prefix_filter = "Idle_"
        node.continuous_shape_key_items = _FakeContinuousCollection()

        count, missing = anim_driver_base.SSMTNode_AnimDriver_Base._rebuild_continuous_shape_key_items(
            node,
            key_blocks,
            anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map(anim_tree),
        )

        self.assertEqual(count, 2)
        self.assertEqual(missing, 0)
        self.assertEqual(
            [(item.shape_key_name, item.variable_name) for item in node.continuous_shape_key_items],
            [("Idle_001", "$Freq_Idle_001"), ("Idle_002", "$Freq_Idle_002")],
        )

    def test_forward_play_continuous_mode_emits_mapping_lines(self):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Forward"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 0.0
        node.frame_end = 3.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = False
        node.loop_playback = False
        node.use_continuous_shapekey_mode = True
        node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Frame_001", variable_name="$Freq_Frame_001"),
            types.SimpleNamespace(shape_key_name="Frame_002", variable_name="$Freq_Frame_002"),
        ]
        node.driven_variable_list = []
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("global persist $continuous_shapekey_frame1", ini)
        self.assertIn("$Freq_Frame_001 = $continuous_shapekey_frame1 - 0.0", ini)
        self.assertIn("$Freq_Frame_002 = $continuous_shapekey_frame1 - 1.0", ini)
        self.assertIn("if $Freq_Frame_001 > 1", ini)
        self.assertIn("if $Freq_Frame_002 < 0", ini)

    def test_forward_play_continuous_mode_uses_customizable_primary_variable(self):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Forward"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 0.0
        node.frame_end = 3.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = False
        node.loop_playback = False
        node.use_continuous_shapekey_mode = True
        node.assigned_continuous_index_variable_name = "continuous_shapekey_frame1"
        node.custom_continuous_index_variable_name = "my_continuous_index"
        node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Frame_001", variable_name="$Freq_Frame_001"),
        ]
        node.driven_variable_list = []
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("global persist $my_continuous_index = 0.0", ini)
        self.assertIn("$Freq_Frame_001 = $my_continuous_index - 0.0", ini)
        self.assertNotIn("$continuous_shapekey_frame1 = 0.0", ini)

    def test_forward_play_continuous_mode_preserves_shape_key_offsets_when_middle_variable_is_missing(self):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Forward"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 10.0
        node.frame_end = 13.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = False
        node.loop_playback = False
        node.use_continuous_shapekey_mode = True
        node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Frame_001", variable_name="$Freq_Frame_001"),
            types.SimpleNamespace(shape_key_name="Frame_002", variable_name=""),
            types.SimpleNamespace(shape_key_name="Frame_003", variable_name="$Freq_Frame_003"),
        ]
        node.driven_variable_list = []
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("$Freq_Frame_001 = $continuous_shapekey_frame1 - 10.0", ini)
        self.assertIn("$Freq_Frame_003 = $continuous_shapekey_frame1 - 12.0", ini)

    def test_forward_play_continuous_mode_reverse_initializes_from_frame_end(self):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Forward"
        node.auto_index = 3
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 0.0
        node.frame_end = 5.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = True
        node.loop_playback = False
        node.use_continuous_shapekey_mode = True
        node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Frame_001", variable_name="$Freq_Frame_001"),
        ]
        node.driven_variable_list = []
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("global persist $continuous_shapekey_frame1 = 5.0", ini)

    def test_forward_play_continuous_mode_uses_remaining_items_after_manual_prune(self):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Forward"
        node.auto_index = 5
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 20.0
        node.frame_end = 30.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = False
        node.loop_playback = False
        node.use_continuous_shapekey_mode = True
        node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Talk_001", variable_name="$Freq_Talk_001"),
            types.SimpleNamespace(shape_key_name="Talk_002", variable_name="$Freq_Talk_002"),
        ]
        node.driven_variable_list = []
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("$Freq_Talk_001 = $continuous_shapekey_frame1 - 20.0", ini)
        self.assertIn("$Freq_Talk_002 = $continuous_shapekey_frame1 - 21.0", ini)

    def test_pingpong_continuous_mode_emits_mapping_lines(self):
        node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        node.name = "PingPong"
        node.auto_index = 2
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 0.0
        node.frame_end = 2.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = False
        node.loop_playback = False
        node.use_continuous_shapekey_mode = True
        node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Frame_001", variable_name="$Freq_Frame_001"),
        ]
        node.driven_variable_list = []
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("global persist $continuous_shapekey_frame1", ini)
        self.assertIn("$Freq_Frame_001 = $continuous_shapekey_frame1 - 0.0", ini)
        self.assertIn("if $Freq_Frame_001 > 1", ini)

    def test_pingpong_continuous_mode_falls_back_to_assigned_primary_variable(self):
        node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        node.name = "PingPong"
        node.auto_index = 2
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 0.0
        node.frame_end = 2.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = False
        node.loop_playback = False
        node.use_continuous_shapekey_mode = True
        node.assigned_continuous_index_variable_name = "continuous_shapekey_frame_custom"
        node.custom_continuous_index_variable_name = ""
        node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Frame_001", variable_name="$Freq_Frame_001"),
        ]
        node.driven_variable_list = []
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("global persist $continuous_shapekey_frame_custom = 0.0", ini)
        self.assertIn("$Freq_Frame_001 = $continuous_shapekey_frame_custom - 0.0", ini)

    def test_pingpong_continuous_mode_reverse_initializes_from_frame_end(self):
        node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        node.name = "PingPong"
        node.auto_index = 4
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 1.0
        node.frame_end = 6.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = True
        node.loop_playback = False
        node.use_continuous_shapekey_mode = True
        node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Frame_001", variable_name="$Freq_Frame_001"),
        ]
        node.driven_variable_list = []
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("global persist $continuous_shapekey_frame1 = 6.0", ini)

    def test_remove_operator_deletes_active_continuous_shape_key_item(self):
        node = types.SimpleNamespace(
            continuous_shape_key_items=_FakeContinuousCollection(),
            continuous_shape_key_items_active=1,
        )
        node.continuous_shape_key_items.add().shape_key_name = "Idle_001"
        node.continuous_shape_key_items.add().shape_key_name = "Idle_002"
        tree = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda _name: node, active=node))
        context = types.SimpleNamespace(space_data=types.SimpleNamespace(edit_tree=tree))

        operator = anim_driver_base.SSMT_OT_ContinuousShapeKeyRemove()
        operator.node_name = "Forward"
        operator.report = lambda *_args, **_kwargs: None

        result = operator.execute(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([item.shape_key_name for item in node.continuous_shape_key_items], ["Idle_001"])


if __name__ == "__main__":
    unittest.main()
