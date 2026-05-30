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
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object),
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
    def setUp(self):
        _fake_bpy.data.objects.clear()
        _helper_state["collect_connected_start_nodes"] = lambda _tree: []
        _helper_state["blueprint_model"] = None

    def test_collect_blueprint_shape_key_names_uses_processing_chain_aliases(self):
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

if __name__ == "__main__":
    unittest.main()
