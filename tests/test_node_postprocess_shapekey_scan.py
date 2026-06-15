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


PKG = "_node_postprocess_shapekey_scan_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeShapeKeyBlock:
    def __init__(self, name):
        self.name = name


class _FakeShapeKeyData:
    def __init__(self, *names):
        self.key_blocks = [_FakeShapeKeyBlock("Basis"), *[_FakeShapeKeyBlock(name) for name in names]]


class _FakeObject:
    def __init__(self, name, *shape_key_names):
        self.name = name
        self.type = "MESH"
        self.data = types.SimpleNamespace(shape_keys=_FakeShapeKeyData(*shape_key_names))


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(objects={}),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=object)
_install_module(f"{PKG}.blueprint.direct_export", sync_shapekey_direct_mode=lambda *_args, **_kwargs: None)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    allocate_shape_key_variable_name=lambda shape_key_name, **_kwargs: f"Freq_{shape_key_name}",
    mark_variable_name_used=lambda *_args, **_kwargs: None,
    normalize_variable_name=lambda value: str(value or "").strip(),
)
_install_module(
    f"{PKG}.common.mod_path_compat",
    collect_base_position_resource_map=lambda *_args, **_kwargs: {},
    derive_shapekey_base_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_freq_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_merged_data_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_merged_map_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_slot_map_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_slot_resource_name=lambda *args, **_kwargs: "",
    ensure_resource_alias_section=lambda *_args, **_kwargs: None,
    resolve_hash_buffer_candidate=lambda *_args, **_kwargs: "",
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(resolve_source_object_name=lambda name: name),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None),
)
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        is_basis_shape_key_name=lambda name: str(name or "").strip().lower() == "basis",
    ),
)

_helper_state = {"collect_connected_start_nodes": lambda _tree: [], "blueprint_model": None}
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        collect_connected_start_nodes=lambda tree: _helper_state["collect_connected_start_nodes"](tree),
        get_current_blueprint_model=lambda: _helper_state["blueprint_model"],
    ),
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_shapekey", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class NodePostprocessShapeKeyScanTests(unittest.TestCase):
    """测试形态键后处理扫描节点：收集和变量映射管理"""

    def setUp(self):
        """每个测试前清空伪装数据"""
        _fake_bpy.data.objects.clear()
        _helper_state["collect_connected_start_nodes"] = lambda _tree: []
        _helper_state["blueprint_model"] = None

    def test_collect_blueprint_shape_key_names_uses_processing_chain_aliases(self):
        """测试 collect_blueprint_shape_key_names 使用处理链别名收集形态键名"""
        _fake_bpy.data.objects["Body"] = _FakeObject("Body", "Smile", "Blink")
        _helper_state["blueprint_model"] = types.SimpleNamespace(
            processing_chains=[
                types.SimpleNamespace(
                    is_valid=True,
                    reached_output=True,
                    object_name="LOD0.hash-0.Body_chain1_copy",
                    original_object_name="Body",
                    virtual_object_name="LOD0.hash-0.Body_chain1_copy",
                    export_object_name_override="",
                    rename_history=[],
                    get_export_object_name=lambda: "LOD0.hash-0.Body_chain1_copy",
                )
            ]
        )

        node = module.SSMTNode_PostProcess_ShapeKey()
        node.id_data = object()

        result = node.collect_blueprint_shape_key_names()

        self.assertEqual(result, ["Blink", "Smile"])

    def test_ensure_shape_key_variable_map_rebuilds_items_from_current_scan(self):
        """测试 ensure_shape_key_variable_map 从当前扫描重建变量映射条目"""
        class _FakeItem:
            def __init__(self, shape_key_name="", assigned_variable_name="", custom_variable_name=""):
                self.shape_key_name = shape_key_name
                self.assigned_variable_name = assigned_variable_name
                self.custom_variable_name = custom_variable_name

        class _FakeCollection(list):
            def add(self):
                item = _FakeItem()
                self.append(item)
                return item

            def remove(self, index):
                del self[index]

        node = module.SSMTNode_PostProcess_ShapeKey()
        node.shapekey_variable_items = _FakeCollection([
            _FakeItem("A", "Freq_A", "Freq_A"),
            _FakeItem("B", "Freq_B", "Manual_B"),
            _FakeItem("C", "Freq_C", "Freq_C"),
            _FakeItem("D", "Freq_D", "Freq_D"),
            _FakeItem("E", "Freq_E", "Freq_E"),
        ])

        created_count, backfilled_count = node.ensure_shape_key_variable_map(["A", "B", "C"])

        self.assertEqual(created_count, 0)
        self.assertEqual(backfilled_count, 0)
        self.assertEqual(
            [item.shape_key_name for item in node.shapekey_variable_items],
            ["A", "B", "C"],
        )
        self.assertEqual(node.shapekey_variable_items[1].custom_variable_name, "Manual_B")

    def test_ensure_shape_key_variable_map_adds_new_items_after_pruning_stale_ones(self):
        """测试 ensure_shape_key_variable_map 在裁剪过期条目后添加新条目"""
        class _FakeItem:
            def __init__(self, shape_key_name="", assigned_variable_name="", custom_variable_name=""):
                self.shape_key_name = shape_key_name
                self.assigned_variable_name = assigned_variable_name
                self.custom_variable_name = custom_variable_name

        class _FakeCollection(list):
            def add(self):
                item = _FakeItem()
                self.append(item)
                return item

            def remove(self, index):
                del self[index]

        node = module.SSMTNode_PostProcess_ShapeKey()
        node.shapekey_variable_items = _FakeCollection([
            _FakeItem("A", "Freq_A", "Freq_A"),
            _FakeItem("B", "Freq_B", "Freq_B"),
            _FakeItem("C", "Freq_C", "Freq_C"),
            _FakeItem("D", "Freq_D", "Freq_D"),
            _FakeItem("E", "Freq_E", "Freq_E"),
        ])

        node.ensure_shape_key_variable_map(["A", "B", "C"])
        created_count, _backfilled_count = node.ensure_shape_key_variable_map(["A", "B", "F"])

        self.assertEqual(created_count, 1)
        self.assertEqual(
            [item.shape_key_name for item in node.shapekey_variable_items],
            ["A", "B", "F"],
        )

    def test_compute_dispatch_group_count_rounds_up_by_thread_group(self):
        node = module.SSMTNode_PostProcess_ShapeKey()

        self.assertEqual(node._compute_dispatch_group_count(0, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(1, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(16, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(17, threads_per_group=16), 2)
        self.assertEqual(node._compute_dispatch_group_count(128, threads_per_group=64), 2)

    def test_update_shader_file_optimized_mode_skips_vertex_range_definitions(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        node.INTENSITY_START_INDEX = 100
        node.VERTEX_RANGE_START_INDEX = 200
        node._get_vertex_struct_definition = lambda: (
            "struct VertexAttributes {\n"
            "    float3 position;\n"
            "    float3 normal;\n"
            "    float4 tangent;\n"
            "};"
        )

        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            shader_path = Path(temp_dir) / "shader.hlsl"
            shader_path.write_text(
                "// --- [PYTHON-MANAGED BLOCK START] ---\n"
                "// --- [PYTHON-MANAGED BLOCK END] ---\n"
                "// --- [PYTHON-MANAGED LOGIC START] ---\n"
                "// --- [PYTHON-MANAGED LOGIC END] ---\n",
                encoding="utf-8",
            )

            success = node._update_shader_file(
                str(shader_path),
                hash_slot_data={1: {"Smile": ["ObjA"]}},
                use_packed=True,
                use_delta=True,
                unique_names=["Smile"],
                unique_objects=["ObjA"],
                use_optimized=True,
                merge_slot_files=False,
            )

            self.assertTrue(success)
            shader_source = shader_path.read_text(encoding="utf-8")
            self.assertIn("FREQ1", shader_source)
            self.assertNotIn("START1", shader_source)
            self.assertNotIn("END1", shader_source)

    def test_draw_buttons_renders_shape_key_variable_mappings_as_template_list(self):
        class _FakeItem:
            def __init__(self, shape_key_name="", assigned_variable_name="", custom_variable_name=""):
                self.shape_key_name = shape_key_name
                self.assigned_variable_name = assigned_variable_name
                self.custom_variable_name = custom_variable_name

        node = module.SSMTNode_PostProcess_ShapeKey()
        node.name = "ShapeKey"
        node.shapekey_variable_items = [
            _FakeItem("A", "Freq_A", "Freq_A"),
            _FakeItem("B", "Freq_B", "Manual_B"),
        ]
        node.shapekey_variable_index = 0

        calls = []

        class _FakeOperator:
            node_name = ""

        class _FakeBox:
            def label(self, *args, **kwargs):
                calls.append(("label", args, kwargs))

            def template_list(self, *args, **kwargs):
                calls.append(("template_list", args, kwargs))

        class _FakeLayout:
            def operator(self, *args, **kwargs):
                calls.append(("operator", args, kwargs))
                return _FakeOperator()

            def box(self):
                calls.append(("box", (), {}))
                return _FakeBox()

            def prop(self, *args, **kwargs):
                calls.append(("prop", args, kwargs))

            def label(self, *args, **kwargs):
                calls.append(("label", args, kwargs))

        node.draw_buttons(context=None, layout=_FakeLayout())

        self.assertTrue(any(call[0] == "template_list" for call in calls))

    def test_shape_key_variable_mapping_ui_list_is_registered(self):
        self.assertIn(module.SSMT_UL_ShapeKeyVariableMappings, module.classes)

if __name__ == "__main__":
    unittest.main()
