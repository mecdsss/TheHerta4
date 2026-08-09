import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []
    module.__package__ = name
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_sk_dragdrive_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    _install_module(package_name)

_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object, Node=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kw: None,
        BoolProperty=lambda **_kw: None,
        IntProperty=lambda **_kw: None,
        FloatProperty=lambda **_kw: None,
        EnumProperty=lambda **_kw: None,
        CollectionProperty=lambda **_kw: None,
        PointerProperty=lambda **_kw: None,
    ),
    data=types.SimpleNamespace(objects={}, texts=[], node_groups=types.SimpleNamespace(nodes=[])),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("bpy.props", **_fake_bpy.props.__dict__)
_install_module(
    "bpy.types",
    PropertyGroup=object,
    Operator=object,
    UIList=object,
    Node=object,
    NodeSocket=object,
)
_install_module("bpy.data", **_fake_bpy.data.__dict__)

_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(lambda content: (content, "")),
            "_create_cumulative_backup": lambda self, ini_file_path, mod_export_path: None,
        },
    ),
)
_install_module(f"{PKG}.blueprint.direct_export", sync_shapekey_direct_mode=lambda self, ctx: None)
_install_module(f"{PKG}.blueprint.deform_chain")
_install_module(
    f"{PKG}.blueprint.variable_registry",
    allocate_shape_key_variable_name=lambda name, **_kw: f"Freq_{name}",
    mark_variable_name_used=lambda *_a, **_kw: None,
    normalize_variable_name=lambda value: str(value or "").strip(),
)
_install_module(
    f"{PKG}.common.mod_path_compat",
    collect_base_position_resource_map=lambda *_a, **_kw: {},
    derive_shapekey_base_resource_name=lambda *a: "",
    derive_shapekey_freq_resource_name=lambda *a: "",
    derive_shapekey_merged_data_resource_name=lambda *a: "",
    derive_shapekey_merged_map_resource_name=lambda *a: "",
    derive_shapekey_slot_map_resource_name=lambda *a: "",
    derive_shapekey_slot_resource_name=lambda *a: "",
    ensure_resource_alias_section=lambda *a, **_kw: "",
    resolve_hash_buffer_candidate=lambda *a, **_kw: None,
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(resolve_source_object_name=lambda name: name),
)

_MODULE_PATH = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_shapekey", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

_TEMPLATES = {
    "merged_delta": "shapekey_anim_packed_delta_v5_merged.hlsl",
    "merged_full": "shapekey_anim_packed_v5_merged.hlsl",
    "opt_delta": "shapekey_anim_packed_delta_v4_optimized.hlsl",
    "standard": "shapekey_anim_standard.hlsl",
}


def _make_node(zone_map, stage_map=None, dir_map=None):
    node = _module.SSMTNode_PostProcess_ShapeKey.__new__(_module.SSMTNode_PostProcess_ShapeKey)
    node.name = "SKNode"
    node.inputs = [types.SimpleNamespace(is_linked=False, links=[])]
    node.outputs = [types.SimpleNamespace(is_linked=False, links=[])]
    node.shapekey_variable_items = []
    node.shapekey_variable_index = 0
    node.id_data = types.SimpleNamespace(nodes=[])
    for name, zone in zone_map.items():
        item = _module.ShapeKeyVariableItem()
        item.shape_key_name = name
        item.assigned_variable_name = f"Freq_{name}"
        item.custom_variable_name = ""
        item.drag_zone_id = zone
        item.drag_click_stage = (stage_map or {}).get(name, 1)
        item.drag_dir_id = str((dir_map or {}).get(name, 0))
        node.shapekey_variable_items.append(item)
    return node


class ShapeKeyDragDriveTests(unittest.TestCase):
    ZONE_MAP = {"Breast_L": 2, "Breast_R": 3, "Hip": -1}

    def setUp(self):
        self.node = _make_node(self.ZONE_MAP)
        self.out_dir = tempfile.mkdtemp(prefix="sk_dragdrive_test_")

    def tearDown(self):
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def _generate(self, template_name):
        src = os.path.abspath(os.path.join("Toolset", template_name))
        if not os.path.exists(src):
            self.skipTest(f"template missing: {src}")
        dest = os.path.join(self.out_dir, template_name)
        shutil.copy2(src, dest)
        self.node._update_shader_file(
            dest,
            {1: {"Breast_L": ["obj1"], "Breast_R": ["obj2"]}, 2: {"Hip": ["obj3"]}},
            True,
            True,
            ["Breast_L", "Breast_R", "Hip"],
            ["obj1", "obj2", "obj3"],
            use_optimized=True,
            merge_slot_files=(template_name in ("shapekey_anim_packed_delta_v5_merged.hlsl", "shapekey_anim_packed_v5_merged.hlsl")),
            drag_drive_enabled=True,
            drag_zone_ids=self.node._drag_drive_zone_ids(["Breast_L", "Breast_R", "Hip"]),
        )
        with open(dest, encoding="utf-8") as f:
            return f.read()

    def test_zone_ids_alignment(self):
        self.assertEqual(self.node._drag_drive_zone_ids(["Breast_L", "Breast_R", "Hip"]), [2, 3, -1])

    def test_stage_and_dir_helpers(self):
        node = _make_node(
            {"A": 0, "B": 0, "C": 1},
            stage_map={"A": 1, "B": 2, "C": 1},
            dir_map={"A": 0, "B": 2, "C": 3},
        )
        self.assertEqual(node._drag_drive_click_stages(["A", "B", "C"]), [1, 2, 1])
        self.assertEqual(node._drag_drive_dirs(["A", "B", "C"]), [0, 2, 3])

    def test_multi_stage_and_dir_generate_3d_index(self):
        node = _make_node(
            {"A": 0, "B": 0, "C": 1},
            stage_map={"A": 1, "B": 2, "C": 1},
            dir_map={"A": 0, "B": 2, "C": 3},
        )
        # 模拟同树拖拽节点开启 2 档
        drag_node = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_DragInteraction",
            enable_shapekey_drive=True,
            _drag_drive_stage_count=lambda: 2,
            _resolve_namespace=lambda ini: "ns",
        )
        node.id_data = types.SimpleNamespace(nodes=[drag_node])
        src = os.path.abspath(os.path.join("Toolset", "shapekey_anim_packed_delta_v5_merged.hlsl"))
        if not os.path.exists(src):
            self.skipTest("template missing")
        dest = os.path.join(self.out_dir, "multi.hlsl")
        shutil.copy2(src, dest)
        node._update_shader_file(
            dest,
            {1: {"A": ["obj1"], "B": ["obj1"]}, 2: {"C": ["obj2"]}},
            True,
            True,
            ["A", "B", "C"],
            ["obj1", "obj2"],
            use_optimized=True,
            merge_slot_files=True,
            drag_drive_enabled=True,
            drag_zone_ids=node._drag_drive_zone_ids(["A", "B", "C"]),
            drag_click_stages=node._drag_drive_click_stages(["A", "B", "C"]),
            drag_stage_count=node._drag_drive_stage_count(),
            drag_dirs=node._drag_drive_dirs(["A", "B", "C"]),
        )
        with open(dest, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("static const uint SHAPEKEY_STAGE_COUNT = 2u;", content)
        self.assertIn("static const uint SHAPEKEY_DIR_COUNT = 4u;", content)
        self.assertIn("static const uint SHAPEKEY_STAGE_IDS[3] = { 1, 2, 1 };", content)
        self.assertIn("static const uint SHAPEKEY_DIR_IDS[3] = { 0, 2, 3 };", content)
        # B 的档位 2、方向 2：B 由点击第 2 次 + 向下驱动
        self.assertIn("// B (zone 0, click 2, dir 2)", content)

    def test_merged_optimized_generates_drive_read(self):
        content = self._generate("shapekey_anim_packed_delta_v5_merged.hlsl")
        self.assertIn("Buffer<float> ShapeKeyDrive : register(t100);", content)
        self.assertIn("Buffer<uint> ShapeKeyClickCount : register(t101);", content)
        self.assertIn("SHAPEKEY_ZONE_IDS", content)
        self.assertIn("SHAPEKEY_DIR_COUNT", content)
        self.assertIn("ShapeKeyClickCount[sk_zone_slot0] == sk_stage_slot0", content)
        self.assertIn("ShapeKeyDrive[sk_zone_slot0 * SHAPEKEY_STAGE_COUNT * SHAPEKEY_DIR_COUNT", content)
        self.assertIn("#define FREQ1 (SHAPEKEY_ZONE_IDS[0] != 0xFFFFFFFFu", content)
        # 未绑定形态键保持变量回退
        self.assertIn("#define FREQ3 IniParams[102].x", content)
        # 未绑定守卫：不会访问 ShapeKeyDrive[-1]
        self.assertIn("0xFFFFFFFFu", content)

    def test_optimized_non_merged_generates_drive_read(self):
        content = self._generate("shapekey_anim_packed_delta_v4_optimized.hlsl")
        self.assertIn("register(t100)", content)
        self.assertIn("ShapeKeyClickCount[sk_zone_slot0] == sk_stage_slot0", content)
        self.assertIn("ShapeKeyDrive[sk_zone_slot0 * SHAPEKEY_STAGE_COUNT * SHAPEKEY_DIR_COUNT", content)

    def test_all_templates_generate_drive_binding(self):
        for template_name in _TEMPLATES.values():
            content = self._generate(template_name)
            self.assertIn("register(t100)", content, template_name)
            self.assertIn("SHAPEKEY_DIR_COUNT", content, template_name)
            self.assertIn("ShapeKeyDrive[SHAPEKEY_ZONE_IDS[0] * SHAPEKEY_STAGE_COUNT * SHAPEKEY_DIR_COUNT", content, template_name)

    def test_disabled_keeps_original_ini_params_weight(self):
        src = os.path.abspath(os.path.join("Toolset", "shapekey_anim_packed_delta_v5_merged.hlsl"))
        if not os.path.exists(src):
            self.skipTest("template missing")
        dest = os.path.join(self.out_dir, "disabled.hlsl")
        shutil.copy2(src, dest)
        self.node._update_shader_file(
            dest,
            {1: {"Breast_L": ["obj1"]}},
            True,
            True,
            ["Breast_L"],
            ["obj1"],
            use_optimized=True,
            merge_slot_files=True,
            drag_drive_enabled=False,
        )
        with open(dest, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("ShapeKeyDrive", content)
        self.assertIn("IniParams[100 + freq_idx_slot0].x", content)

    def test_drag_drive_fields_hidden_when_toggle_off(self):
        node = _make_node({"A": 2})
        calls = []

        class _FakeRow:
            def row(self, align=False):
                return self

            def column(self, align=False):
                return self

            def label(self, text="", icon=""):
                pass

            def prop(self, data, prop, **kwargs):
                calls.append((prop, kwargs.get("text", "")))

        fake_row = _FakeRow()

        class _FakeLayout:
            def row(self, align=False):
                return fake_row

            def column(self, align=False):
                return self

            def label(self, text="", icon=""):
                pass

        ulist = _module.SSMT_UL_ShapeKeyVariableMappings()
        ulist.layout_type = "DEFAULT"

        # 开关关闭：不绘制区域/档位/方向
        node.drag_drive_enabled = False
        ulist.draw_item(
            None, _FakeLayout(), node, node.shapekey_variable_items[0],
            "", None, "", 0,
        )
        self.assertNotIn(("drag_zone_id", "区域"), calls)
        self.assertNotIn(("drag_click_stage", "档位"), calls)
        self.assertNotIn(("drag_dir_id", "方向"), calls)

        # 开关打开：绘制区域/档位/方向
        calls.clear()
        node.drag_drive_enabled = True
        ulist.draw_item(
            None, _FakeLayout(), node, node.shapekey_variable_items[0],
            "", None, "", 0,
        )
        self.assertIn(("drag_zone_id", "区域"), calls)
        self.assertIn(("drag_click_stage", "档位"), calls)
        self.assertIn(("drag_dir_id", "方向"), calls)


if __name__ == "__main__":
    unittest.main()
