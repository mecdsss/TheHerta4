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


PKG = "_blueprint_model_vgtest_exec_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeObjects(dict):
    def get(self, name, default=None):
        return super().get(name, default)


class _FakeLog:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def debug(self, message):
        self.messages.append(("debug", message))

    def warning(self, message):
        self.messages.append(("warning", message))


class _FakeDrawCallModel:
    def __init__(self, obj_name):
        self.obj_name = obj_name
        self.source_obj_name = ""
        self.work_key_list = []


class _FakeBlueprintExportHelper:
    @staticmethod
    def get_current_blueprint_tree(context=None):
        return None

    @staticmethod
    def get_node_from_bl_idname(tree, bl_idname):
        return None

    @staticmethod
    def is_result_output_node(node):
        return False


_fake_log = _FakeLog()
_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(objects=_FakeObjects()),
    types=types.SimpleNamespace(Node=object, NodeTree=object, Object=object),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.utils.log_utils", LOG=_fake_log)
_install_module(f"{PKG}.common.m_key", M_Key=type("M_Key", (), {}))
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=_FakeDrawCallModel)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        build_virtual_object_name_for_node=lambda node, strict=True: getattr(node, "object_name", ""),
        extract_prefix_info=lambda _name: None,
        parse_prefix_parts=lambda _prefix: {"draw_ib": "", "index_count": "", "first_index": "", "unique_str": "", "bare_unique_str": ""},
        split_name_and_prefix=lambda object_name, prefix, separator: (prefix, separator, object_name),
    ),
)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=_FakeBlueprintExportHelper)
_install_module(
    f"{PKG}.blueprint.node_rename",
    SSMTNode_Object_Rename=types.SimpleNamespace(
        apply_to_object_name=lambda object_name, node=None: (object_name, False, [], "Rename[]"),
        log_rename_summary=lambda _chains: None,
    ),
)
_install_module(
    f"{PKG}.blueprint.node_vertex_group_test_split",
    SSMTNode_VertexGroupTestSplit=types.SimpleNamespace(validate_chain_position=lambda chain: []),
)


def _expand_chain_object_for_export(source_object_name, original_object_name=""):
    suffix = "_vgtest"
    return [
        {
            "object_name": f"LOD0.prefixA-1-0.Mesh{suffix}_copy",
            "original_object_name": original_object_name or source_object_name,
            "export_name": f"LOD0.prefixA-1-0.Mesh{suffix}_copy",
            "prefix": "LOD0.prefixA-1-0",
        },
        {
            "object_name": f"LOD0.prefixB-1-0.Mesh{suffix}_copy",
            "original_object_name": original_object_name or source_object_name,
            "export_name": f"LOD0.prefixB-1-0.Mesh{suffix}_copy",
            "prefix": "LOD0.prefixB-1-0",
        },
    ]


_install_module(
    f"{PKG}.blueprint.vg_test_runtime",
    VGTestRuntime=types.SimpleNamespace(expand_chain_object_for_export=_expand_chain_object_for_export),
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "model.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.model", module_path)
model_module = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.model"] = model_module
spec.loader.exec_module(model_module)


class BluePrintModelVGTestExecutionTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.objects.clear()
        _fake_log.messages.clear()
        model_module.BluePrintModel.clear_object_name_mapping()

    def test_blueprint_model_initializes_vgtest_split_node_registry(self):
        tree = types.SimpleNamespace(name="Default", nodes=[], bl_idname="SSMTBlueprintTreeType")
        output_node = types.SimpleNamespace(
            bl_idname="SSMTNode_Result_Output",
            outputs=[],
            inputs=[],
        )
        export_helper = sys.modules[f"{PKG}.blueprint.export_helper"].BlueprintExportHelper
        export_helper.get_node_from_bl_idname = staticmethod(lambda _tree, _bl_idname: output_node)
        original_forward_parse = model_module.BluePrintModel._forward_parse_blueprint
        model_module.BluePrintModel._forward_parse_blueprint = lambda self, _tree, _output_node: None

        try:
            blueprint_model = model_module.BluePrintModel(tree=tree, context=None)
        finally:
            model_module.BluePrintModel._forward_parse_blueprint = original_forward_parse

        self.assertEqual(blueprint_model.vertex_group_test_split_nodes, [])

    def test_split_node_expands_one_chain_into_multiple_export_chains(self):
        source_obj = types.SimpleNamespace(name="LOD0.source-1-0.Mesh_copy", type="MESH")
        split_a_obj = types.SimpleNamespace(name="LOD0.prefixA-1-0.Mesh_vgtest_copy", type="MESH")
        split_b_obj = types.SimpleNamespace(name="LOD0.prefixB-1-0.Mesh_vgtest_copy", type="MESH")
        _fake_bpy.data.objects[source_obj.name] = source_obj
        _fake_bpy.data.objects[split_a_obj.name] = split_a_obj
        _fake_bpy.data.objects[split_b_obj.name] = split_b_obj

        split_node = types.SimpleNamespace(
            bl_idname="SSMTNode_VertexGroupTestSplit",
            name="VG Test Split",
            id_data=types.SimpleNamespace(name="Default"),
        )

        chain = model_module.ProcessingChain(
            object_name=source_obj.name,
            original_object_name=source_obj.name,
            node_path=[split_node],
            node_param_signatures=["VertexGroupTestSplit[]"],
            vertex_group_test_split_nodes=[split_node],
            reached_output=True,
            is_valid=True,
        )

        blueprint_model = model_module.BluePrintModel.__new__(model_module.BluePrintModel)
        blueprint_model.processing_chains = [chain]
        blueprint_model.chain_groups = []
        blueprint_model.ordered_draw_obj_data_model_list = []
        blueprint_model.vertex_group_process_nodes = []
        blueprint_model.vertex_group_test_split_nodes = [split_node]
        blueprint_model.multi_file_export_nodes = []
        blueprint_model.cross_ib_nodes = []
        blueprint_model.postprocess_nodes = []
        blueprint_model.nested_blueprint_trees = []
        blueprint_model.keyname_mkey_dict = {}

        blueprint_model._execute_chain_nodes_sequentially()
        blueprint_model._build_draw_call_models_from_chains()

        self.assertEqual(len(blueprint_model.processing_chains), 2)
        self.assertEqual(
            sorted(chain.object_name for chain in blueprint_model.processing_chains),
            sorted([split_a_obj.name, split_b_obj.name]),
        )
        self.assertEqual(
            sorted(chain.get_export_object_name() for chain in blueprint_model.processing_chains),
            sorted([split_a_obj.name, split_b_obj.name]),
        )
        self.assertEqual(
            sorted(model.obj_name for model in blueprint_model.ordered_draw_obj_data_model_list),
            sorted([split_a_obj.name, split_b_obj.name]),
        )
        self.assertTrue(
            any("[VGTEST-SPLIT] Sequential chain execution complete" in message for _level, message in _fake_log.messages)
        )

    def test_split_outputs_continue_through_downstream_rename_node(self):
        source_obj = types.SimpleNamespace(name="LOD0.source-1-0.Mesh_copy", type="MESH")
        split_a_obj = types.SimpleNamespace(name="LOD0.prefixA-1-0.Mesh_vgtest_copy", type="MESH")
        split_b_obj = types.SimpleNamespace(name="LOD0.prefixB-1-0.Mesh_vgtest_copy", type="MESH")
        _fake_bpy.data.objects[source_obj.name] = source_obj
        _fake_bpy.data.objects[split_a_obj.name] = split_a_obj
        _fake_bpy.data.objects[split_b_obj.name] = split_b_obj

        def _rename_apply(object_name, node=None):
            new_name = object_name.replace("_vgtest_copy", "_vgtest_renamed_copy")
            was_modified = new_name != object_name
            history = []
            if was_modified:
                history.append(
                    {
                        "old_name": object_name,
                        "new_name": new_name,
                        "search": "_vgtest_copy",
                        "replace": "_vgtest_renamed_copy",
                        "is_reversed": False,
                    }
                )
            return (new_name, was_modified, history, "Rename[]")

        sys.modules[f"{PKG}.blueprint.node_rename"].SSMTNode_Object_Rename.apply_to_object_name = _rename_apply

        split_node = types.SimpleNamespace(
            bl_idname="SSMTNode_VertexGroupTestSplit",
            name="VG Test Split",
            id_data=types.SimpleNamespace(name="Default"),
        )
        rename_node = types.SimpleNamespace(
            bl_idname="SSMTNode_Object_Rename",
            name="Rename After Split",
            defer_until_after_vertex_group_process=False,
            id_data=types.SimpleNamespace(name="Default"),
        )

        chain = model_module.ProcessingChain(
            object_name=source_obj.name,
            original_object_name=source_obj.name,
            node_path=[split_node, rename_node],
            node_param_signatures=["VertexGroupTestSplit[]", "Rename[]"],
            vertex_group_test_split_nodes=[split_node],
            reached_output=True,
            is_valid=True,
        )

        blueprint_model = model_module.BluePrintModel.__new__(model_module.BluePrintModel)
        blueprint_model.processing_chains = [chain]
        blueprint_model.chain_groups = []
        blueprint_model.ordered_draw_obj_data_model_list = []
        blueprint_model.vertex_group_process_nodes = []
        blueprint_model.vertex_group_test_split_nodes = [split_node]
        blueprint_model.multi_file_export_nodes = []
        blueprint_model.cross_ib_nodes = []
        blueprint_model.postprocess_nodes = []
        blueprint_model.nested_blueprint_trees = []
        blueprint_model.keyname_mkey_dict = {}

        blueprint_model._execute_chain_nodes_sequentially()

        self.assertEqual(
            sorted(chain.object_name for chain in blueprint_model.processing_chains),
            sorted([
                "LOD0.prefixA-1-0.Mesh_vgtest_renamed_copy",
                "LOD0.prefixB-1-0.Mesh_vgtest_renamed_copy",
            ]),
        )
        self.assertEqual(
            sorted(chain.get_export_object_name() for chain in blueprint_model.processing_chains),
            sorted([
                "LOD0.prefixA-1-0.Mesh_vgtest_renamed_copy",
                "LOD0.prefixB-1-0.Mesh_vgtest_renamed_copy",
            ]),
        )

    def test_split_node_always_uses_vgtest_runtime_suffix(self):
        source_obj = types.SimpleNamespace(name="LOD0.source-1-0.Mesh_copy", type="MESH")
        split_a_obj = types.SimpleNamespace(name="LOD0.prefixA-1-0.Mesh_vgtest_copy", type="MESH")
        split_b_obj = types.SimpleNamespace(name="LOD0.prefixB-1-0.Mesh_vgtest_copy", type="MESH")
        _fake_bpy.data.objects[source_obj.name] = source_obj
        _fake_bpy.data.objects[split_a_obj.name] = split_a_obj
        _fake_bpy.data.objects[split_b_obj.name] = split_b_obj

        split_node = types.SimpleNamespace(
            bl_idname="SSMTNode_VertexGroupTestSplit",
            name="VG Test Split",
            id_data=types.SimpleNamespace(name="Default"),
        )

        chain = model_module.ProcessingChain(
            object_name=source_obj.name,
            original_object_name=source_obj.name,
            node_path=[split_node],
            node_param_signatures=["VertexGroupTestSplit[suffix=_vgtest]"],
            vertex_group_test_split_nodes=[split_node],
            reached_output=True,
            is_valid=True,
        )

        blueprint_model = model_module.BluePrintModel.__new__(model_module.BluePrintModel)
        blueprint_model.processing_chains = [chain]
        blueprint_model.chain_groups = []
        blueprint_model.ordered_draw_obj_data_model_list = []
        blueprint_model.vertex_group_process_nodes = []
        blueprint_model.vertex_group_test_split_nodes = [split_node]
        blueprint_model.multi_file_export_nodes = []
        blueprint_model.cross_ib_nodes = []
        blueprint_model.postprocess_nodes = []
        blueprint_model.nested_blueprint_trees = []
        blueprint_model.keyname_mkey_dict = {}

        blueprint_model._execute_chain_nodes_sequentially()

        self.assertEqual(
            sorted(processing_chain.object_name for processing_chain in blueprint_model.processing_chains),
            sorted([split_a_obj.name, split_b_obj.name]),
        )


if __name__ == "__main__":
    unittest.main()
