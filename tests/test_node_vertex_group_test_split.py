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


PKG = "_node_vg_test_split_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Node=object),
    props=types.SimpleNamespace(StringProperty=lambda **_kwargs: None),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
    data=types.SimpleNamespace(objects={}),
)
_install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object)
_install_module(
    f"{PKG}.blueprint.vg_test_runtime",
    VGTestRuntime=types.SimpleNamespace(expand_chain_object_for_export=lambda **_kwargs: []),
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_vertex_group_test_split.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_vertex_group_test_split", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.node_vertex_group_test_split"] = module
spec.loader.exec_module(module)


class VGTestSplitNodeTests(unittest.TestCase):
    def test_node_contract_does_not_expose_runtime_suffix_override(self):
        self.assertFalse(hasattr(module.SSMTNode_VertexGroupTestSplit, "runtime_suffix"))

    def test_validate_chain_position_allows_downstream_mutating_nodes(self):
        split_node = types.SimpleNamespace(bl_idname="SSMTNode_VertexGroupTestSplit", name="Split")
        rename_node = types.SimpleNamespace(bl_idname="SSMTNode_Object_Rename", name="Rename")
        chain = types.SimpleNamespace(node_path=[split_node, rename_node])

        errors = module.SSMTNode_VertexGroupTestSplit.validate_chain_position(chain)

        self.assertEqual(errors, [])

    def test_validate_chain_position_accepts_terminal_split(self):
        split_node = types.SimpleNamespace(bl_idname="SSMTNode_VertexGroupTestSplit", name="Split")
        output_node = types.SimpleNamespace(bl_idname="SSMTNode_Result_Output", name="Output")
        chain = types.SimpleNamespace(node_path=[split_node, output_node])

        self.assertEqual(module.SSMTNode_VertexGroupTestSplit.validate_chain_position(chain), [])

    def test_validate_chain_position_rejects_multiple_split_nodes(self):
        split_node_a = types.SimpleNamespace(bl_idname="SSMTNode_VertexGroupTestSplit", name="SplitA")
        split_node_b = types.SimpleNamespace(bl_idname="SSMTNode_VertexGroupTestSplit", name="SplitB")
        chain = types.SimpleNamespace(node_path=[split_node_a, split_node_b])

        errors = module.SSMTNode_VertexGroupTestSplit.validate_chain_position(chain)

        self.assertTrue(errors)
        self.assertIn("Only one VG Test Split node", errors[0])


if __name__ == "__main__":
    unittest.main()
