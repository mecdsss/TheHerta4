# -*- coding: utf-8 -*-
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


PKG = "_node_obj_view_layer_filter_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    app=types.SimpleNamespace(version=(5, 0, 0)),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
    ),
    types=types.SimpleNamespace(
        Operator=object,
        NodeTree=object,
        Node=object,
        NodeSocket=object,
        VIEW3D_HT_header=types.SimpleNamespace(append=lambda _fn: None, remove=lambda _fn: None),
    ),
    data=types.SimpleNamespace(node_groups={}, objects={}),
    context=types.SimpleNamespace(selected_objects=[]),
    ops=types.SimpleNamespace(
        object=types.SimpleNamespace(mode_set=lambda **_kwargs: None),
        view3d=types.SimpleNamespace(localview=lambda: None, view_axis=lambda **_kwargs: None, view_selected=lambda: None),
    ),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    "bpy.types",
    NodeTree=object,
    Node=object,
    NodeSocket=object,
    Operator=object,
    VIEW3D_HT_header=_fake_bpy.types.VIEW3D_HT_header,
)
_install_module(
    "bpy.props",
    StringProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
)
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace())
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace())
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace())
_install_module(f"{PKG}.blueprint.node_base", SSMTBlueprintTree=object, SSMTNodeBase=object)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_obj.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_obj", module_path)
node_obj = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = node_obj
spec.loader.exec_module(node_obj)


class NodeObjViewLayerFilterTests(unittest.TestCase):
    def test_view_group_objects_skips_objects_not_in_current_view_layer(self):
        class _FakeObject:
            def __init__(self, name):
                self.name = name

            def select_set(self, _value):
                return None

            def __hash__(self):
                return hash(self.name)

        in_layer = _FakeObject("VisibleObj")
        out_of_layer = _FakeObject("WidgetEyeRightRigify")

        tree = types.SimpleNamespace(nodes={})
        node = types.SimpleNamespace(name="GroupNode")
        tree.nodes["GroupNode"] = node

        operator = node_obj.SSMT_OT_View_Group_Objects()
        operator.node_name = "GroupNode"
        reports = []
        operator.report = lambda level, message: reports.append((level, message))
        operator._collect_group_preview_objects = lambda *_args, **_kwargs: _args[1].update({in_layer, out_of_layer})

        context = types.SimpleNamespace(
            space_data=types.SimpleNamespace(edit_tree=tree, node_tree=tree),
            window_manager=types.SimpleNamespace(
                windows=[
                    types.SimpleNamespace(
                        screen=types.SimpleNamespace(
                            areas=[
                                types.SimpleNamespace(
                                    type="VIEW_3D",
                                    spaces=[types.SimpleNamespace(type="VIEW_3D", local_view=False)],
                                    regions=[types.SimpleNamespace(type="WINDOW")],
                                )
                            ]
                        )
                    )
                ]
            ),
            view_layer=types.SimpleNamespace(objects=[in_layer], active=None),
            mode="OBJECT",
            selected_objects=[],
            temp_override=lambda **_kwargs: types.SimpleNamespace(__enter__=lambda self: None, __exit__=lambda self, exc_type, exc, tb: False),
        )

        class _Override:
            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, tb):
                return False

        context.temp_override = lambda **_kwargs: _Override()

        result = operator.execute(context)

        self.assertEqual(result, {'FINISHED'})
        self.assertTrue(any("Showing 1 objects" in str(message) for _level, message in reports))


if __name__ == "__main__":
    unittest.main()
