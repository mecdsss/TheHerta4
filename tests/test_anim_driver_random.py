import importlib.util
import re
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


PKG = "_anim_driver_random_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


_bpy_props = _install_module(
    "bpy.props",
    FloatProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
    StringProperty=lambda **_kwargs: None,
    EnumProperty=lambda **_kwargs: None,
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
    data=types.SimpleNamespace(objects={}, node_groups=[]),
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
random_module = _load_blueprint_module("anim_driver_random")
toggle_module = _load_blueprint_module("anim_driver_toggle")
global_config = sys.modules[f"{PKG}.common.global_config"]


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


def _simulate_lcg(seed, steps):
    """按生成代码的精确整数语义模拟 LCG，用于验证随机序列。"""
    modulus = 16777216
    multiplier = 4001
    increment = 12345
    q = 4193
    r = 1023
    m_minus_c = modulus - increment
    x = seed
    values = []
    for _ in range(steps):
        low = x % q
        high = (x - low) // q
        x = multiplier * low - r * high
        if x < 0:
            x += modulus
        if x < m_minus_c:
            x += increment
        else:
            x -= m_minus_c
        values.append(x)
    return values


class _FakeItem:
    def __init__(self):
        self.variable_name = ""


class _FakeCollection(list):
    def add(self):
        item = _FakeItem()
        self.append(item)
        return item

    def remove(self, index):
        del self[index]


class _FakeSocketList:
    def __init__(self):
        self.created = []

    def new(self, socket_type, name):
        self.created.append((socket_type, name))


def _make_node(targets=("$shape_up", "$shape_down", "$shape_left", "$shape_right"),
               seed=13579, min_value=0.0, max_value=1.0,
               default_paused=True, custom_paused_var=""):
    node = random_module.SSMTNode_AnimDriver_Random()
    node.name = "Random"
    node.auto_index = 1
    node.id_data = None
    node.seed = seed
    node.min_value = min_value
    node.max_value = max_value
    node.default_paused = default_paused
    node.custom_paused_var = custom_paused_var
    node.driven_variable_list = [types.SimpleNamespace(variable_name=name) for name in targets]
    node.driven_variable_list_active = 0
    return node


_MAPPING_RE = re.compile(r"^\s+(\S+) = ([0-9.-]+) \+ \(\$random_seed\d+ / 16777216\.0\) \* ([0-9.-]+)$")


def _extract_mappings(ini_text):
    mappings = []
    for line in ini_text.splitlines():
        match = _MAPPING_RE.match(line)
        if match:
            mappings.append((match.group(1), float(match.group(2)), float(match.group(3))))
    return mappings


class RandomDriverNodeTests(unittest.TestCase):
    def test_default_init_creates_four_default_axis_variables(self):
        node = random_module.SSMTNode_AnimDriver_Random()
        node.id_data = None
        node.inputs = _FakeSocketList()
        node.outputs = _FakeSocketList()
        node.driven_variable_list = _FakeCollection()
        node.driven_variable_list_active = 0

        node.init(context=None)

        self.assertEqual(node.auto_index, 1)
        self.assertEqual(node.custom_paused_var, "$random_paused1")
        self.assertEqual(
            [item.variable_name for item in node.driven_variable_list],
            ["$shape_up", "$shape_down", "$shape_left", "$shape_right"],
        )
        self.assertEqual(node.inputs.created, [("SSMTSocketAnimDriver", "链输入")])
        self.assertEqual(node.outputs.created, [("SSMTSocketAnimDriver", "链输出")])

    def test_generate_returns_empty_when_no_targets(self):
        node = _make_node(targets=[])
        self.assertEqual(node.generate_ini_segment(), "")

        node = _make_node(targets=["", "  "])
        self.assertEqual(node.generate_ini_segment(), "")

    def test_generate_structure_and_balanced_conditionals(self):
        node = _make_node()
        ini = node.generate_ini_segment()

        self.assertIn("[Constants]", ini)
        self.assertIn("[Present]", ini)
        self.assertIn("global persist $random_seed1 = 13579", ini)
        self.assertIn("global $random_high1 = 0", ini)
        self.assertIn("global $random_low1 = 0", ini)
        self.assertIn("global persist $random_paused1 = 1", ini)
        self.assertIn("if $random_paused1 == 1", ini)
        self.assertTrue(ini.endswith("endif"))
        self.assertIn("$random_seed1 = (4001 * $random_low1) - (1023 * $random_high1)", ini)
        self.assertIn("$random_seed1 % 4193", ini)
        self.assertIn("$random_high1 = ($random_seed1 - $random_low1) / 4193", ini)
        self.assertIn("if $random_seed1 < 16764871", ini)
        self.assertIn("        $random_seed1 = $random_seed1 - 16764871", ini)
        _assert_balanced_conditionals(self, ini)

        # 暂停时目标变量复位为 0
        ini_lines = ini.splitlines()
        self.assertIn("else", ini_lines)
        for target in ("$shape_up", "$shape_down", "$shape_left", "$shape_right"):
            self.assertIn(f"    {target} = 0", ini_lines)

        # 每个目标变量获得独立的 LCG 推进块（4 个目标 → 4 块）
        self.assertEqual(ini.count(f"$random_seed1 = (4001 * $random_low1)"), 4)
        self.assertEqual(ini.count("% 4193"), 4)
        self.assertNotIn("// 4193", ini)
        self.assertNotIn("$active0", ini)

    def test_generate_maps_custom_range(self):
        node = _make_node(min_value=0.25, max_value=1.0)
        ini = node.generate_ini_segment()

        self.assertIn("$shape_up = 0.25 + ($random_seed1 / 16777216.0) * 0.75", ini)
        self.assertIn("$shape_left = 0.25 + ($random_seed1 / 16777216.0) * 0.75", ini)

    def test_generate_swaps_reversed_range(self):
        node = _make_node(min_value=1.0, max_value=0.2)
        ini = node.generate_ini_segment()

        self.assertIn("$shape_up = 0.2 + ($random_seed1 / 16777216.0) * 0.8", ini)
        self.assertNotIn("1 + (", ini)

    def test_generate_dedupes_and_normalizes_target_names(self):
        node = _make_node(targets=["shape_up", "$shape_up", "", "shape_down"])
        ini = node.generate_ini_segment()

        self.assertEqual(ini.count("$shape_up = 0 + ($random_seed1 / 16777216.0) * 1"), 1)
        self.assertEqual(ini.count("$shape_down = 0 + ($random_seed1 / 16777216.0) * 1"), 1)
        ini_lines = ini.splitlines()
        self.assertEqual(ini_lines.count("    $shape_up = 0"), 1)
        self.assertEqual(ini_lines.count("    $shape_down = 0"), 1)
        self.assertIsNone(re.search(r"(?m)^\s+shape_up = ", ini))
        self.assertIsNone(re.search(r"(?m)^\s+shape_down = ", ini))

    def test_generate_clamps_seed(self):
        node = _make_node(seed=0)
        self.assertIn("global persist $random_seed1 = 0", node.generate_ini_segment())

        node = _make_node(seed=999999999)
        self.assertIn("global persist $random_seed1 = 16777215", node.generate_ini_segment())

    def test_sequence_differs_per_target_and_across_frames(self):
        node = _make_node()
        ini = node.generate_ini_segment()
        mappings = _extract_mappings(ini)
        self.assertEqual(len(mappings), 4)

        # 第一帧：每个目标消耗一步序列，取值互不相同
        frame1_seeds = _simulate_lcg(13579, 4)
        self.assertEqual(len(set(frame1_seeds)), 4)
        for (target, min_value, span), seed in zip(mappings, frame1_seeds):
            self.assertGreaterEqual(seed, 0)
            self.assertLess(seed, 16777216)
            value = min_value + (seed / 16777216.0) * span
            self.assertGreaterEqual(value, min_value)
            self.assertLessEqual(value, min_value + span)
            self.assertNotEqual(value, min_value)

        # 第二帧：状态继续推进，取值与第一帧不同
        frame2_seeds = _simulate_lcg(13579, 8)[4:]
        self.assertEqual(len(set(frame1_seeds) & set(frame2_seeds)), 0)

    def test_generate_uses_default_paused_state_for_gate(self):
        node = _make_node(default_paused=False)
        ini = node.generate_ini_segment()

        self.assertIn("global persist $random_paused1 = 0", ini)
        self.assertIn("if $random_paused1 == 1", ini)

    def test_generate_uses_custom_paused_var(self):
        node = _make_node(custom_paused_var="my_paused")
        ini = node.generate_ini_segment()

        self.assertIn("global persist $my_paused = 1", ini)
        self.assertIn("if $my_paused == 1", ini)
        self.assertNotIn("$random_paused1", ini)

    def test_toggle_node_collects_random_paused_var(self):
        toggle = toggle_module.SSMTNode_AnimDriver_Toggle()
        toggle.name = "Toggle"
        toggle.auto_index = 2
        toggle.id_data = None

        random_node = _make_node(custom_paused_var="$random_paused1")

        link = types.SimpleNamespace(
            from_node=toggle,
            from_socket=types.SimpleNamespace(bl_idname="SSMTSocketAnimDriver"),
            to_node=random_node,
        )
        tree = types.SimpleNamespace(nodes=[toggle, random_node], links=[link])
        toggle.id_data = tree
        random_node.id_data = tree

        self.assertEqual(toggle._collect_downstream_pause_vars(), ["$random_paused1"])

        # 开关生成的热键段落按暂停变量循环切换随机驱动的启停
        toggle.id_data = None
        toggle.key_binding = "no_modifiers k"
        toggle.switch_type = "cycle"
        toggle.toggle_values = "0,1"
        toggle.comment = ""
        toggle.pause_target_list = [types.SimpleNamespace(variable_name="$random_paused1")]

        segment = toggle.generate_ini_segment()
        self.assertIn("[KeyToggle_Anim2]", segment)
        self.assertIn("$random_paused1 = 0,1", segment)

    def test_format_number(self):
        self.assertEqual(random_module._format_number(0.0), "0")
        self.assertEqual(random_module._format_number(-0.0), "0")
        self.assertEqual(random_module._format_number(1.0), "1")
        self.assertEqual(random_module._format_number(0.2), "0.2")
        self.assertEqual(random_module._format_number(-0.5), "-0.5")
        self.assertEqual(random_module._format_number(1.0 / 3.0), "0.3333333333")

    def test_operator_adds_and_removes_targets(self):
        node = random_module.SSMTNode_AnimDriver_Random()
        node.driven_variable_list = _FakeCollection()
        node.driven_variable_list_active = 0

        add = random_module.SSMT_OT_RandomDriverTargetAdd()
        add.node_name = ""
        remove = random_module.SSMT_OT_RandomDriverTargetRemove()
        remove.node_name = ""

        class _FakeNodes:
            active = node

        class _FakeTree:
            nodes = _FakeNodes()

        class _FakeSpaceData:
            edit_tree = _FakeTree()

        context = types.SimpleNamespace(space_data=_FakeSpaceData())

        self.assertEqual(add.execute(context), {'FINISHED'})
        self.assertEqual(len(node.driven_variable_list), 1)
        self.assertEqual(node.driven_variable_list_active, 0)

        self.assertEqual(remove.execute(context), {'FINISHED'})
        self.assertEqual(len(node.driven_variable_list), 0)
        self.assertEqual(node.driven_variable_list_active, -1)


if __name__ == "__main__":
    unittest.main()
