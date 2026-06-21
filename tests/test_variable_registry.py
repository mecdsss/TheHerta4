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


PKG = "_variable_registry_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeNodeGroups(list):
    pass


class _FakeGlobalProperties(dict):
    def __getattr__(self, name):
        if name in self:
            return self[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


_fake_global_properties = _FakeGlobalProperties()
_fake_bpy = types.SimpleNamespace(
    context=types.SimpleNamespace(
        scene=types.SimpleNamespace(global_properties=_fake_global_properties),
    ),
    data=types.SimpleNamespace(node_groups=_FakeNodeGroups()),
)
_install_module("bpy", **_fake_bpy.__dict__)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "variable_registry.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.variable_registry", module_path)
variable_registry = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.variable_registry"] = variable_registry
spec.loader.exec_module(variable_registry)


def _make_swap_node(var_name: str):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_ObjectSwap",
        custom_var_name=var_name,
        assigned_variable_name=var_name,
    )


def _make_shapekey_item(var_name: str):
    return types.SimpleNamespace(
        custom_variable_name=var_name,
        assigned_variable_name=var_name,
    )


def _make_shapekey_node(*var_names: str):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_ShapeKey",
        shapekey_variable_items=[_make_shapekey_item(name) for name in var_names],
    )


def _make_anim_driver_node(assigned_name: str = "", custom_name: str = ""):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_AnimDriver_ForwardPlay",
        custom_paused_var="",
        driven_variable="",
        assigned_continuous_index_variable_name=assigned_name,
        custom_continuous_index_variable_name=custom_name,
    )


def _make_anim_driver_runtime_node(paused_name: str = "", driven_name: str = ""):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_AnimDriver_ShapeKeySequence",
        custom_paused_var=paused_name,
        driven_variable=driven_name,
        driven_variable_list=[],
        assigned_continuous_index_variable_name="",
        custom_continuous_index_variable_name="",
    )


def _make_anim_driver_list_item(variable_name: str):
    return types.SimpleNamespace(variable_name=variable_name)


def _make_tree(*nodes):
    return types.SimpleNamespace(
        bl_idname="SSMTBlueprintTreeType",
        nodes=list(nodes),
    )


class VariableRegistryTests(unittest.TestCase):
    def setUp(self):
        _fake_global_properties.clear()
        _fake_bpy.data.node_groups[:] = []
        variable_registry.bpy = _fake_bpy

    def test_object_swap_reuses_first_free_index(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_swap_node("swapkey0"),
                _make_swap_node("swapkey2"),
            )
        ]
        new_node = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            custom_var_name="",
            assigned_variable_name="",
        )

        allocated = variable_registry.ensure_object_swap_variable_name(new_node)

        self.assertEqual(allocated, "swapkey1")
        self.assertEqual(new_node.assigned_variable_name, "swapkey1")
        self.assertEqual(_fake_global_properties.object_swap_variable_counter, 3)

    def test_shapekey_variable_reuses_first_free_suffix(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_shapekey_node("Freq_Smile", "Freq_Smile_2"),
            )
        ]

        allocated = variable_registry.allocate_shape_key_variable_name("Smile")

        self.assertEqual(allocated, "Freq_Smile_1")

    def test_shapekey_owned_name_is_not_treated_as_conflict(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_shapekey_node("Freq_Blink"),
            )
        ]

        allocated = variable_registry.allocate_shape_key_variable_name(
            "Blink",
            preferred="Freq_Blink",
            owned_names=("Freq_Blink", "Freq_Blink"),
        )

        self.assertEqual(allocated, "Freq_Blink")

    def test_continuous_anim_driver_variable_reuses_first_free_suffix(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_anim_driver_node("continuous_shapekey_frame1"),
                _make_anim_driver_node("continuous_shapekey_frame3"),
            )
        ]

        allocated = variable_registry.allocate_continuous_shapekey_index_variable_name()

        self.assertEqual(allocated, "continuous_shapekey_frame2")

    def test_used_variable_name_counts_include_anim_driver_paused_and_sequence_variables(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_anim_driver_runtime_node("$animation_paused1", "$shapekey_seq2"),
            )
        ]

        used_names = variable_registry.get_used_variable_names()

        self.assertIn("animation_paused1", used_names)
        self.assertIn("shapekey_seq2", used_names)

    def test_used_variable_name_counts_include_anim_driver_driven_variable_list(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                types.SimpleNamespace(
                    bl_idname="SSMTNode_AnimDriver_ForwardPlay",
                    custom_paused_var="",
                    driven_variable="",
                    driven_variable_list=[
                        _make_anim_driver_list_item("$driven_a"),
                        _make_anim_driver_list_item("driven_b"),
                    ],
                    assigned_continuous_index_variable_name="",
                    custom_continuous_index_variable_name="",
                )
            )
        ]

        used_names = variable_registry.get_used_variable_names()

        self.assertIn("driven_a", used_names)
        self.assertIn("driven_b", used_names)


if __name__ == "__main__":
    unittest.main()
