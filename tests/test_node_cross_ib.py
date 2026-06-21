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


PKG = "_node_cross_ib_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


def _prop_stub(**kwargs):
    return kwargs.get("default")


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object, PropertyGroup=object),
    props=types.SimpleNamespace(
        StringProperty=_prop_stub,
        CollectionProperty=lambda **kwargs: [],
        BoolProperty=_prop_stub,
        EnumProperty=_prop_stub,
        IntProperty=_prop_stub,
    ),
)
_install_module("bpy.types", Node=object, PropertyGroup=object)
_install_module(
    "bpy.props",
    StringProperty=_prop_stub,
    CollectionProperty=lambda **kwargs: [],
    BoolProperty=_prop_stub,
    EnumProperty=_prop_stub,
    IntProperty=_prop_stub,
)
_install_module(
    f"{PKG}.blueprint.node_base",
    SSMTNodeBase=type("SSMTNodeBase", (), {}),
    SSMTSocketObject=object,
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name="GIMI"),
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_cross_ib.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_cross_ib", module_path)
node_cross_ib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = node_cross_ib
spec.loader.exec_module(node_cross_ib)


class _SocketList(list):
    def new(self, *_args, **_kwargs):
        self.append(types.SimpleNamespace())


class CrossIBNodeTests(unittest.TestCase):
    def test_update_cross_ib_method_sets_unsupported_reason_without_raising(self):
        node = object.__new__(node_cross_ib.SSMTNode_CrossIB)
        node.cross_ib_method = node_cross_ib.CrossIBMethodEnum.END_FIELD
        node.match_mode = node_cross_ib.CrossIBMatchMode.INDEX_COUNT
        node.unsupported_reason = ""

        node._update_cross_ib_method()

        self.assertTrue(node.unsupported_reason)

    def test_init_does_not_raise_for_unsupported_logic_name(self):
        node = object.__new__(node_cross_ib.SSMTNode_CrossIB)
        node.inputs = _SocketList()
        node.outputs = _SocketList()
        node.cross_ib_method = node_cross_ib.CrossIBMethodEnum.END_FIELD
        node.match_mode = node_cross_ib.CrossIBMatchMode.INDEX_COUNT
        node.unsupported_reason = ""

        node.init(context=None)

        self.assertEqual(len(node.inputs), 1)
        self.assertEqual(len(node.outputs), 1)
        self.assertTrue(node.unsupported_reason)

    def test_zzmi_available_methods_include_new_cross_ib_options(self):
        methods = node_cross_ib.CrossIBMethodEnum.get_available_methods("ZZMI")
        self.assertEqual(
            methods,
            [
                node_cross_ib.CrossIBMethodEnum.VB_COPY,
                node_cross_ib.CrossIBMethodEnum.VB_COPY_CB1,
                node_cross_ib.CrossIBMethodEnum.VB_REF_SO0,
            ],
        )

    def test_cross_ib_set_method_operator_is_registered_in_module_classes(self):
        self.assertIn(node_cross_ib.SSMT_OT_CrossIB_SetMethod, node_cross_ib.classes)


if __name__ == "__main__":
    unittest.main()
