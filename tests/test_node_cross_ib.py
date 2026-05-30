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
_install_module(
    "bpy.types",
    Node=object,
    PropertyGroup=object,
)
_install_module(
    "bpy.props",
    StringProperty=_prop_stub,
    CollectionProperty=lambda **kwargs: [],
    BoolProperty=_prop_stub,
    EnumProperty=_prop_stub,
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
    """测试 CrossIB 节点的初始化与不支持原因检测"""

    def test_update_cross_ib_method_sets_unsupported_reason_without_raising(self):
        """测试 update_cross_ib_method 能设置不支持原因而不抛出异常"""
        node = object.__new__(node_cross_ib.SSMTNode_CrossIB)
        node.cross_ib_method = node_cross_ib.CrossIBMethodEnum.END_FIELD
        node.match_mode = node_cross_ib.CrossIBMatchMode.INDEX_COUNT
        node.unsupported_reason = ""

        node._update_cross_ib_method()

        self.assertTrue(node.unsupported_reason)

    def test_init_does_not_raise_for_unsupported_logic_name(self):
        """测试 init 在遇到不支持的逻辑名称时不抛出异常"""
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


if __name__ == "__main__":
    unittest.main()
