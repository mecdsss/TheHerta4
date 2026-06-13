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


PKG = "_anim_driver_base_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
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
_install_module(
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
)
_install_module(
    f"{PKG}.blueprint.node_base",
    SSMTBlueprintTree=object,
    SSMTNodeBase=object,
    refresh_blueprint_node_colors=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(workspacename="Test", read_from_main_json_ssmt4=lambda: None),
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        extract_prefix_info=lambda _name: None,
        parse_prefix_parts=lambda _prefix: {},
    ),
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "anim_driver_base.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.anim_driver_base", module_path)
anim_driver_base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = anim_driver_base
spec.loader.exec_module(anim_driver_base)


def _load_blueprint_module(module_name):
    module_path = Path(__file__).resolve().parents[1] / "blueprint" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


forward_play_module = _load_blueprint_module("anim_driver_forward_play")
pingpong_module = _load_blueprint_module("anim_driver_pingpong")
runtime_module = _load_blueprint_module("anim_driver_runtime")
toggle_module = _load_blueprint_module("anim_driver_toggle")
node_menu_module = _load_blueprint_module("node_menu")


def _assert_balanced_conditionals(test_case, ini_text):
    stack = []
    for lineno, raw_line in enumerate(ini_text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(";") or (line.startswith("[") and line.endswith("]")):
            continue
        if line.startswith("if "):
            stack.append((lineno, line))
        elif line == "else":
            test_case.assertTrue(stack, f"Line {lineno} has else without matching if")
        elif line == "endif":
            test_case.assertTrue(stack, f"Line {lineno} has endif without matching if")
            stack.pop()

    test_case.assertEqual(stack, [], f"Unclosed if blocks: {stack}")


class AnimDriverBaseTests(unittest.TestCase):
    def test_read_safe_index_uses_sorted_position_without_mutating_invalid_indices(self):
        base_cls = anim_driver_base.SSMTNode_AnimDriver_Base

        node_b = base_cls()
        node_b.name = "B"
        node_b.auto_index = 2

        node_a = base_cls()
        node_a.name = "A"
        node_a.auto_index = 2

        node_c = base_cls()
        node_c.name = "C"
        node_c.auto_index = 0

        tree = types.SimpleNamespace(nodes=[node_b, node_a, node_c])
        for node in (node_a, node_b, node_c):
            node.id_data = tree

        safe_index = node_b._read_safe_index()

        self.assertEqual(safe_index, 2)
        self.assertEqual(node_a.auto_index, 2)
        self.assertEqual(node_b.auto_index, 2)
        self.assertEqual(node_c.auto_index, 0)

    def test_forward_play_clamps_all_driven_vars_to_last_frame_after_overshoot(self):
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
        node.driven_variable_list = [
            types.SimpleNamespace(variable_name="$varA"),
            types.SimpleNamespace(variable_name="$varB"),
        ]
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("if $varA > $frameEnd1", ini)
        self.assertIn("$varA = $frameEnd1", ini)
        self.assertIn("$varB = $frameEnd1", ini)
        self.assertIn("global $paused = 1", ini)

    def test_pingpong_clamps_all_driven_vars_on_both_bounds_after_overshoot(self):
        node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        node.name = "PingPong"
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
        node.driven_variable_list = [
            types.SimpleNamespace(variable_name="$varA"),
            types.SimpleNamespace(variable_name="$varB"),
        ]
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("if $varA > $frameEnd1", ini)
        self.assertIn("$varA = $frameEnd1", ini)
        self.assertIn("$varB = $frameEnd1", ini)
        self.assertIn("if $varA < $frameStart1", ini)
        self.assertIn("$varA = $frameStart1", ini)
        self.assertIn("$varB = $frameStart1", ini)
        self.assertIn("global $paused = 1", ini)

    def test_pingpong_generated_ini_has_balanced_conditionals(self):
        node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        node.name = "PingPong"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 0.0
        node.frame_end = 3.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = True
        node.loop_playback = True
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

        _assert_balanced_conditionals(self, ini)

    def test_runtime_and_pingpong_segments_merge_with_balanced_conditionals(self):
        runtime_node = runtime_module.SSMTNode_AnimDriver_Runtime()
        runtime_node.name = "Runtime"
        runtime_node.auto_index = 1
        runtime_node.fps = 30
        runtime_node.playback_rate = 1

        pingpong_node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        pingpong_node.name = "PingPong"
        pingpong_node.auto_index = 2
        pingpong_node.frame_start = 0.0
        pingpong_node.frame_end = 3.0
        pingpong_node.play_total_duration = 0.1
        pingpong_node.use_float_interval = True
        pingpong_node.default_paused = True
        pingpong_node.custom_paused_var = "$paused"
        pingpong_node.reverse_playback = False
        pingpong_node.loop_playback = False
        pingpong_node.driven_variable_list = [types.SimpleNamespace(variable_name="$varA")]
        pingpong_node.driven_variable = ""

        class _FakeSocket:
            def __init__(self, name):
                self.bl_idname = "SSMTSocketAnimDriver"
                self.name = name

        class _FakeLink:
            def __init__(self, from_node, to_node, from_name, to_name):
                self.from_node = from_node
                self.to_node = to_node
                self.from_socket = _FakeSocket(from_name)
                self.to_socket = _FakeSocket(to_name)

        node_group = types.SimpleNamespace(
            nodes=[runtime_node, pingpong_node],
            links=[
                _FakeLink(runtime_node, pingpong_node, "链输出", "链输入"),
                _FakeLink(runtime_node, pingpong_node, "链输出", "时间输入"),
            ],
        )
        runtime_node.id_data = node_group
        pingpong_node.id_data = node_group

        collector_module = _load_blueprint_module("anim_driver_collector")
        merged = collector_module.AnimationDriverCollector(node_group).collect()[0]["ini_content"]

        _assert_balanced_conditionals(self, merged)

    def test_toggle_comment_is_emitted_into_ini(self):
        node = toggle_module.SSMTNode_AnimDriver_Toggle()
        node.name = "Toggle"
        node.auto_index = 2
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.key_binding = "no_modifiers k"
        node.toggle_values = "0,1"
        node.comment = "切换测试"
        node.pause_target_list = [types.SimpleNamespace(variable_name="$animation_paused2")]

        ini = node.generate_ini_segment()

        self.assertIn("; 切换测试", ini)
        self.assertIn("[KeyToggle_Anim2]", ini)
        self.assertIn("$animation_paused2 = 0,1", ini)

    def test_draw_node_add_menu_uses_layout_for_animation_driver_tree(self):
        calls = []

        class _FakeOperator:
            def __init__(self):
                self.type = None

        class _FakeLayout:
            def operator(self, op_idname, text="", icon=""):
                calls.append((op_idname, text, icon))
                return _FakeOperator()

            def menu(self, *args, **kwargs):
                calls.append(("menu", args, kwargs))

            def separator(self):
                calls.append(("separator",))

        fake_context = types.SimpleNamespace(
            space_data=types.SimpleNamespace(
                edit_tree=types.SimpleNamespace(
                    bl_idname='SSMTBlueprintTreeType',
                    get=lambda key, default=None: True if key == "is_animation_driver" else default,
                ),
                node_tree=None,
            )
        )
        fake_self = types.SimpleNamespace(layout=_FakeLayout())

        node_menu_module.draw_node_add_menu(fake_self, fake_context)

        self.assertTrue(any(call[1] == "运行时间" for call in calls if call[0] == "node.add_node"))
        self.assertTrue(any(call[1] == "动画驱动开关" for call in calls if call[0] == "node.add_node"))


if __name__ == "__main__":
    unittest.main()
