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
    """测试 DDS 转换工具的格式解析与值保留转换策略"""

    def test_flags_always_ignore_srgb(self):
        """所有贴图一律按原始数值读取（--ignore-srgb），不做色彩空间变换"""
        self.assertEqual(["--ignore-srgb"], dds_conversion._texconv_colorspace_flags())

    def test_custom_rule_format_is_authoritative(self):
        """自定义规则命中：格式完全由规则决定，不再按文件名推断类型"""
        props = types.SimpleNamespace(
            dds_use_custom_rules=True,
            dds_rules=[
                types.SimpleNamespace(
                    enabled=True,
                    pattern=r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)",
                    format="r8g8b8a8_unorm",
                )
            ],
        )
        texture_type, dds_format, matched_by = dds_conversion.resolve_dds_target("DiffuseMap_Body.png", props)
        self.assertEqual(texture_type, "custom")
        self.assertEqual(dds_format, "r8g8b8a8_unorm")
        self.assertEqual(matched_by, r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)")

    def test_custom_rule_ignores_filename_keywords(self):
        """文件名含多个已知类型关键词时，不影响自定义规则的判定结果"""
        props = types.SimpleNamespace(
            dds_use_custom_rules=True,
            dds_rules=[
                types.SimpleNamespace(
                    enabled=True,
                    pattern=r"(?i)(?:^|[_\-. ])DiffuseMap_high(?:[_\-. ]|$)",
                    format="bc7_unorm_srgb",
                ),
                types.SimpleNamespace(
                    enabled=True,
                    pattern=r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)",
                    format="bc7_unorm_srgb",
                ),
                types.SimpleNamespace(
                    enabled=True,
                    pattern=r"(?i)(?:^|[_\-. ])NormalMap(?:[_\-. ]|$)",
                    format="r8g8b8a8_unorm",
                ),
            ],
        )
        texture_type, dds_format, matched_by = dds_conversion.resolve_dds_target(
            "NormalMap_DiffuseMap_high.png", props
        )
        self.assertEqual(texture_type, "custom")
        self.assertEqual(dds_format, "bc7_unorm_srgb")
        self.assertEqual(matched_by, r"(?i)(?:^|[_\-. ])DiffuseMap_high(?:[_\-. ]|$)")

    def test_default_rules_pick_first_matching_keyword(self):
        """测试文件名同时包含 NormalMap 和 DiffuseMap 时，取最先出现的匹配规则"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target(
            "NormalMap_DiffuseMap_high_丝袜.png", props
        )
        self.assertEqual(texture_type, "NormalMap")
        self.assertEqual(dds_format, "r8g8b8a8_unorm")

    def test_default_rules_single_keyword_normalmap(self):
        """测试仅包含 NormalMap 时正常匹配"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target("cloth_NormalMap.png", props)
        self.assertEqual(texture_type, "NormalMap")
        self.assertEqual(dds_format, "r8g8b8a8_unorm")

    def test_default_rules_recognize_ttlmap(self):
        """测试 TTLMap 前缀与 FXMap 一样按遮罩格式识别（bc7_unorm）"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target("TTLMap_BaseTex.png", props)
        self.assertEqual(texture_type, "TTLMap")
        self.assertEqual(dds_format, "bc7_unorm")
        self.assertIn("--ignore-srgb", dds_conversion._texconv_colorspace_flags())

    def test_unmatched_falls_back_to_default_format(self):
        """未命中任何规则时走默认 bc7_unorm"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, matched_by = dds_conversion.resolve_dds_target("UnknownMask.png", props)
        self.assertEqual(texture_type, "default")
        self.assertEqual(dds_format, "bc7_unorm")
        self.assertEqual(matched_by, "Default")


if __name__ == "__main__":
    unittest.main()
