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


PKG = "_direct_shapekey_activation_guard_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module(
    f"{PKG}.common.mod_path_compat",
    **{
        name: (lambda *_args, **_kwargs: None)
        for name in (
            "collect_base_position_resource_map",
            "derive_shapekey_base_resource_name",
            "derive_shapekey_freq_resource_name",
            "derive_shapekey_merged_data_resource_name",
            "derive_shapekey_merged_map_resource_name",
            "derive_shapekey_slot_map_resource_name",
            "derive_shapekey_slot_resource_name",
            "ensure_resource_alias_section",
        )
    },
)
_install_module(f"{PKG}.utils.log_utils", LOG=types.SimpleNamespace())
_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    apply_position_override_in_place=lambda *_args, **_kwargs: None,
    extract_position_bytes_by_indices=lambda *_args, **_kwargs: b"",
    iter_drawib_models=lambda *_args, **_kwargs: [],
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=RuntimeError,
    _buffer_to_bytes=lambda value: value,
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "direct_export_shapekey_output_mixin.py"
spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.direct_export_shapekey_output_mixin",
    module_path,
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class _Harness(module.DirectShapeKeyOutputMixin):
    def __init__(self, key_map):
        self.blueprint_model = types.SimpleNamespace(keyname_mkey_dict=key_map)


class DirectShapeKeyActivationGuardTests(unittest.TestCase):
    def test_without_object_switch_keys_runs_unconditionally(self):
        lines = _Harness({})._build_present_run_block(["abc"])

        self.assertIn("run = CustomShader_abc_Anim", lines)
        self.assertNotIn("if $active0 == 1", lines)

    def test_with_object_switch_keys_uses_active_guard(self):
        lines = _Harness({"$swap": object()})._build_present_run_block(["abc"])

        self.assertIn("if $active0 == 1", lines)
        self.assertIn("    run = CustomShader_abc_Anim", lines)
        self.assertIn("endif", lines)


if __name__ == "__main__":
    unittest.main()
