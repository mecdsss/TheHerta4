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


PKG = "_dds_conversion_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object, Panel=object),
    props=types.SimpleNamespace(
        BoolProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
        FloatProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        StringProperty=lambda **_kwargs: None,
    ),
    context=types.SimpleNamespace(scene=types.SimpleNamespace(texture_tools_props=types.SimpleNamespace())),
    data=types.SimpleNamespace(images=[]),
)
_install_module("bpy", **_fake_bpy.__dict__)

module_path = Path(__file__).resolve().parents[1] / "toolkit" / "tt_dds_conversion.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.tt_dds_conversion", module_path)
dds_conversion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dds_conversion
spec.loader.exec_module(dds_conversion)


class DDSConversionTests(unittest.TestCase):
    def test_color_texture_uses_srgb_input_flag(self):
        self.assertIn("--srgb-in", dds_conversion._texconv_colorspace_flags("DiffuseMap"))
        self.assertIn("--ignore-srgb", dds_conversion._texconv_colorspace_flags("NormalMap"))

    def test_custom_rule_keeps_real_texture_type_for_srgb_decision(self):
        props = types.SimpleNamespace(
            dds_use_custom_rules=True,
            dds_rules=[
                types.SimpleNamespace(
                    enabled=True,
                    pattern=r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)",
                    format="bc7_unorm_srgb",
                )
            ],
        )
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target("DiffuseMap_Body.png", props)
        self.assertEqual(texture_type, "DiffuseMap")
        self.assertEqual(dds_format, "bc7_unorm_srgb")
        self.assertIn("--srgb-in", dds_conversion._texconv_colorspace_flags(texture_type))


if __name__ == "__main__":
    unittest.main()
