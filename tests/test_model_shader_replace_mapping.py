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


PKG = "_model_shader_replace_mapping_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module(
    "bpy",
    types=types.SimpleNamespace(Node=object, NodeTree=object),
    data=types.SimpleNamespace(objects={}),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
    ),
)
_install_module(f"{PKG}.common.m_key", M_Key=object)
class _FakeDrawCallModel:
    def __init__(self, obj_name):
        self.obj_name = obj_name
        self.work_key_list = []
        self.shader_replace_info_list = []


_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=_FakeDrawCallModel)
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace())
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=types.SimpleNamespace())

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "model.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.model", module_path)
model_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model_module
spec.loader.exec_module(model_module)


class ModelShaderReplaceMappingTests(unittest.TestCase):
    def test_nested_trees_with_same_node_name_keep_distinct_shader_info(self):
        tree_a = types.SimpleNamespace(name="NestedA")
        tree_b = types.SimpleNamespace(name="NestedB")
        info_a = {"name_prefix": "RainA", "shaders": []}
        info_b = {"name_prefix": "RainB", "shaders": []}
        node_a = types.SimpleNamespace(
            name="Shader Replace",
            id_data=tree_a,
            bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_a,
        )
        node_b = types.SimpleNamespace(
            name="Shader Replace",
            id_data=tree_b,
            bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_b,
        )
        chain_a = types.SimpleNamespace(
            is_valid=True,
            reached_output=True,
            node_path=[node_a],
            object_name="ObjectA",
            get_export_object_name=lambda: "ExportA",
        )
        chain_b = types.SimpleNamespace(
            is_valid=True,
            reached_output=True,
            node_path=[node_b],
            object_name="ObjectB",
            get_export_object_name=lambda: "ExportB",
        )

        model = model_module.BluePrintModel.__new__(model_module.BluePrintModel)
        model.shader_replace_nodes = [node_a, node_b]
        model.shader_replace_info_list = []
        model.shader_replace_object_names = set()
        model.shader_replace_object_info_map = {}
        model.has_shader_replace = False
        model.processing_chains = [chain_a, chain_b]

        model._process_shader_replace_nodes()

        self.assertEqual(model.shader_replace_object_info_map["ExportA"], [info_a])
        self.assertEqual(model.shader_replace_object_info_map["ExportB"], [info_b])

    def test_same_export_name_keeps_shader_info_on_each_processing_chain(self):
        tree = types.SimpleNamespace(name="Main")
        info_a = {"name_prefix": "RainA", "shaders": []}
        info_b = {"name_prefix": "RainB", "shaders": []}
        node_a = types.SimpleNamespace(
            name="Shader A",
            id_data=tree,
            bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_a,
        )
        node_b = types.SimpleNamespace(
            name="Shader B",
            id_data=tree,
            bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_b,
        )
        chain_a = model_module.ProcessingChain(object_name="SharedExport", node_path=[node_a])
        chain_a.reached_output = True
        chain_b = model_module.ProcessingChain(object_name="SharedExport", node_path=[node_b])
        chain_b.reached_output = True

        model = model_module.BluePrintModel.__new__(model_module.BluePrintModel)
        model.shader_replace_nodes = [node_a, node_b]
        model.shader_replace_info_list = []
        model.shader_replace_object_names = set()
        model.shader_replace_object_info_map = {}
        model.has_shader_replace = False
        model.processing_chains = [chain_a, chain_b]

        model._process_shader_replace_nodes()

        draw_a = chain_a.to_draw_call_model()
        draw_b = chain_b.to_draw_call_model()
        self.assertEqual(draw_a.shader_replace_info_list, [info_a])
        self.assertEqual(draw_b.shader_replace_info_list, [info_b])
        self.assertTrue(draw_a.shader_replace_info_resolved)
        self.assertTrue(draw_b.shader_replace_info_resolved)
        self.assertEqual(model.shader_replace_object_info_map["SharedExport"], [info_a, info_b])

    def test_unreferenced_shader_node_is_excluded_from_export_configuration(self):
        tree = types.SimpleNamespace(name="Main")
        used_info = {"name_prefix": "Used", "shaders": []}
        unused_info = {"name_prefix": "Unused", "shaders": []}
        used_node = types.SimpleNamespace(
            name="Used Shader",
            id_data=tree,
            bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: used_info,
        )
        unused_node = types.SimpleNamespace(
            name="Unused Shader",
            id_data=tree,
            bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: unused_info,
        )
        chain = model_module.ProcessingChain(object_name="Mesh", node_path=[used_node])
        chain.reached_output = True

        model = model_module.BluePrintModel.__new__(model_module.BluePrintModel)
        model.shader_replace_nodes = [used_node, unused_node]
        model.shader_replace_info_list = []
        model.shader_replace_object_names = set()
        model.shader_replace_object_info_map = {}
        model.has_shader_replace = False
        model.processing_chains = [chain]

        model._process_shader_replace_nodes()

        self.assertEqual(model.shader_replace_info_list, [used_info])
        self.assertEqual(chain.shader_replace_info_list, [used_info])


    def test_identical_shader_nodes_across_chains_are_merged(self):
        tree = types.SimpleNamespace(name="Main")
        shaders = [
            {"variant_name": "World", "shader_file_path": "C:/shaders/world.txt", "shader_hash": "ABCDEF"},
            {"variant_name": "NonWorld", "shader_file_path": "C:/shaders/nonworld.txt", "shader_hash": "123456"},
        ]
        info_a = {"name_prefix": "Rain", "toggle_key": "F1", "component_index": 0, "shaders": shaders}
        info_b = {"name_prefix": "Rain", "toggle_key": "F1", "component_index": 0, "shaders": shaders}
        node_a = types.SimpleNamespace(
            name="Shader A",
            id_data=tree,
            bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_a,
        )
        node_b = types.SimpleNamespace(
            name="Shader B",
            id_data=tree,
            bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_b,
        )
        chain_a = types.SimpleNamespace(
            is_valid=True,
            reached_output=True,
            node_path=[node_a],
            object_name="ObjectA",
            get_export_object_name=lambda: "ExportA",
        )
        chain_b = types.SimpleNamespace(
            is_valid=True,
            reached_output=True,
            node_path=[node_b],
            object_name="ObjectB",
            get_export_object_name=lambda: "ExportB",
        )

        model = model_module.BluePrintModel.__new__(model_module.BluePrintModel)
        model.shader_replace_nodes = [node_a, node_b]
        model.shader_replace_info_list = []
        model.shader_replace_object_names = set()
        model.shader_replace_object_info_map = {}
        model.has_shader_replace = False
        model.processing_chains = [chain_a, chain_b]

        model._process_shader_replace_nodes()

        # 相同配置只保留一份，两个链路的物体共享同一份配置
        self.assertEqual(model.shader_replace_info_list, [info_a])
        self.assertEqual(model.shader_replace_object_info_map["ExportA"], [info_a])
        self.assertEqual(model.shader_replace_object_info_map["ExportB"], [info_a])
        self.assertEqual(chain_a.shader_replace_info_list, [info_a])
        self.assertEqual(chain_b.shader_replace_info_list, [info_a])

    def test_same_prefix_with_different_shaders_is_not_merged(self):
        tree = types.SimpleNamespace(name="Main")
        info_a = {
            "name_prefix": "Rain", "toggle_key": "F1", "component_index": 0,
            "shaders": [{"variant_name": "World", "shader_file_path": "C:/a.txt", "shader_hash": "1111"}],
        }
        info_b = {
            "name_prefix": "Rain", "toggle_key": "F1", "component_index": 0,
            "shaders": [{"variant_name": "World", "shader_file_path": "C:/b.txt", "shader_hash": "2222"}],
        }
        node_a = types.SimpleNamespace(
            name="Shader A", id_data=tree, bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_a,
        )
        node_b = types.SimpleNamespace(
            name="Shader B", id_data=tree, bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_b,
        )
        chain_a = types.SimpleNamespace(
            is_valid=True, reached_output=True, node_path=[node_a],
            object_name="ObjectA", get_export_object_name=lambda: "ExportA",
        )
        chain_b = types.SimpleNamespace(
            is_valid=True, reached_output=True, node_path=[node_b],
            object_name="ObjectB", get_export_object_name=lambda: "ExportB",
        )

        model = model_module.BluePrintModel.__new__(model_module.BluePrintModel)
        model.shader_replace_nodes = [node_a, node_b]
        model.shader_replace_info_list = []
        model.shader_replace_object_names = set()
        model.shader_replace_object_info_map = {}
        model.has_shader_replace = False
        model.processing_chains = [chain_a, chain_b]

        model._process_shader_replace_nodes()

        # 同前缀但着色器不同的配置保持独立，由后续唯一前缀校验拦截
        self.assertEqual(model.shader_replace_info_list, [info_a, info_b])
        self.assertEqual(model.shader_replace_object_info_map["ExportA"], [info_a])
        self.assertEqual(model.shader_replace_object_info_map["ExportB"], [info_b])

    def test_same_content_with_different_toggle_key_is_not_merged(self):
        tree = types.SimpleNamespace(name="Main")
        info_a = {
            "name_prefix": "Rain", "toggle_key": "F1", "component_index": 0,
            "shaders": [{"variant_name": "World", "shader_file_path": "C:/a.txt", "shader_hash": "1111"}],
        }
        info_b = {
            "name_prefix": "Rain", "toggle_key": "F2", "component_index": 0,
            "shaders": [{"variant_name": "World", "shader_file_path": "C:/a.txt", "shader_hash": "1111"}],
        }
        node_a = types.SimpleNamespace(
            name="Shader A", id_data=tree, bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_a,
        )
        node_b = types.SimpleNamespace(
            name="Shader B", id_data=tree, bl_idname="SSMTNode_ShaderReplace",
            get_shader_replace_info=lambda: info_b,
        )
        chain_a = types.SimpleNamespace(
            is_valid=True, reached_output=True, node_path=[node_a],
            object_name="ObjectA", get_export_object_name=lambda: "ExportA",
        )
        chain_b = types.SimpleNamespace(
            is_valid=True, reached_output=True, node_path=[node_b],
            object_name="ObjectB", get_export_object_name=lambda: "ExportB",
        )

        model = model_module.BluePrintModel.__new__(model_module.BluePrintModel)
        model.shader_replace_nodes = [node_a, node_b]
        model.shader_replace_info_list = []
        model.shader_replace_object_names = set()
        model.shader_replace_object_info_map = {}
        model.has_shader_replace = False
        model.processing_chains = [chain_a, chain_b]

        model._process_shader_replace_nodes()

        # 快捷键不同 = 独立切换，不合并
        self.assertEqual(model.shader_replace_info_list, [info_a, info_b])
        self.assertEqual(model.shader_replace_object_info_map["ExportA"], [info_a])
        self.assertEqual(model.shader_replace_object_info_map["ExportB"], [info_b])


if __name__ == "__main__":
    unittest.main()
