import importlib.util
import sys
import types
import unittest
from pathlib import Path
import tempfile

import numpy as np
from unittest import mock


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_channel_composite_pipeline_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object),
    props=types.SimpleNamespace(IntProperty=lambda **_kwargs: None),
    path=types.SimpleNamespace(abspath=lambda value: value),
)
_install_module("bpy", **_fake_bpy.__dict__)

module_path = Path(__file__).resolve().parents[1] / "toolkit" / "tt_normal_map.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.tt_normal_map", module_path)
tt_normal_map = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tt_normal_map
spec.loader.exec_module(tt_normal_map)


class ChannelCompositePipelineTests(unittest.TestCase):
    """测试通道合成管线：规则间的输出可被后续规则引用"""

    def test_previous_output_can_feed_next_rule(self):
        """测试前一条规则的输出可以正确作为下一条规则的输入源"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        processor = tt_normal_map.ChannelProcessor()

        base_pixels = np.zeros((2, 2, 4), dtype=np.float32)
        base_pixels[:, :, 0] = 0.2
        base_pixels[:, :, 1] = 0.4
        base_pixels[:, :, 2] = 0.6
        base_pixels[:, :, 3] = 1.0

        rule_a = types.SimpleNamespace(
            rule_name="HeightStep",
            input_source_mode="BASE_COLOR",
            input_rule_name="",
            output_channels=[
                types.SimpleNamespace(source_type="GENERATED_HEIGHT", source_channel="R", constant_value=0.0, invert=False),
                types.SimpleNamespace(source_type="GENERATED_HEIGHT", source_channel="R", constant_value=0.0, invert=False),
                types.SimpleNamespace(source_type="GENERATED_HEIGHT", source_channel="R", constant_value=0.0, invert=False),
                types.SimpleNamespace(source_type="CONSTANT", source_channel="A", constant_value=1.0, invert=False),
            ],
            normal_strength=5.0,
            normal_blur_radius=1.0,
            normal_invert_height=False,
        )

        pixels_a = operator._compose_rule_pixels(rule_a, base_pixels, 2, 2, processor)
        outputs = {"HeightStep": (pixels_a, 2, 2)}

        resolved = operator._resolve_rule_input_pixels(
            types.SimpleNamespace(input_source_mode="NAMED_OUTPUT", input_rule_name="HeightStep"),
            outputs,
            base_pixels,
            2,
            2,
            base_pixels,
            2,
            2,
        )
        self.assertIsNotNone(resolved)
        resolved_pixels, width, height = resolved
        self.assertEqual((width, height), (2, 2))
        self.assertAlmostEqual(float(resolved_pixels[0, 0, 0]), float(pixels_a[0, 0, 0]), places=6)

    def test_execute_keeps_only_final_composite_result(self):
        """测试执行后仅保留最终合成结果，中间规则输出文件被清理"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()

        base_pixels = np.zeros((2, 2, 4), dtype=np.float32)
        base_pixels[:, :, 0] = 0.25
        base_pixels[:, :, 1] = 0.5
        base_pixels[:, :, 2] = 0.75
        base_pixels[:, :, 3] = 1.0

        rule_a = types.SimpleNamespace(
            rule_name="StepA",
            input_source_mode="BASE_COLOR",
            input_rule_name="",
            output_name_prefix="StepA_",
            output_channels=[
                types.SimpleNamespace(source_type="CONSTANT", source_channel="R", constant_value=0.1, invert=False),
                types.SimpleNamespace(source_type="CONSTANT", source_channel="G", constant_value=0.2, invert=False),
                types.SimpleNamespace(source_type="CONSTANT", source_channel="B", constant_value=0.3, invert=False),
                types.SimpleNamespace(source_type="CONSTANT", source_channel="A", constant_value=1.0, invert=False),
            ],
            normal_strength=5.0,
            normal_blur_radius=1.0,
            normal_invert_height=False,
            enabled=True,
        )
        rule_b = types.SimpleNamespace(
            rule_name="StepB",
            input_source_mode="PREVIOUS_OUTPUT",
            input_rule_name="",
            output_name_prefix="StepB_",
            output_channels=[
                types.SimpleNamespace(source_type="IMAGE_CHANNEL", source_channel="R", constant_value=0.0, invert=False),
                types.SimpleNamespace(source_type="IMAGE_CHANNEL", source_channel="G", constant_value=0.0, invert=False),
                types.SimpleNamespace(source_type="IMAGE_CHANNEL", source_channel="B", constant_value=0.0, invert=False),
                types.SimpleNamespace(source_type="CONSTANT", source_channel="A", constant_value=1.0, invert=False),
            ],
            normal_strength=5.0,
            normal_blur_radius=1.0,
            normal_invert_height=False,
            enabled=True,
        )

        class _HashableMaterial:
            def __init__(self, name):
                self.name = name
                self.use_nodes = False
            def __hash__(self):
                return hash(self.name)
            def __eq__(self, other):
                return isinstance(other, _HashableMaterial) and self.name == other.name

        material = _HashableMaterial("Mat One")
        obj = types.SimpleNamespace(
            type="MESH",
            material_slots=[types.SimpleNamespace(material=material)],
            data=types.SimpleNamespace(materials=[]),
        )
        props = types.SimpleNamespace(
            output_dir="",
            composite_rules=[rule_a, rule_b],
            normal_map_create_materials=False,
        )
        context = types.SimpleNamespace(
            selected_objects=[obj],
            scene=types.SimpleNamespace(texture_tools_props=props),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            props.output_dir = temp_dir
            calls = []
            operator.report = lambda *_args, **_kwargs: None

            def fake_apply(self, rule, material, base_pixels, width, height, processor, output_dir):
                calls.append((rule.rule_name, base_pixels.copy()))
                result_pixels = np.full((height, width, 4), 0.1 if rule.rule_name == "StepA" else 0.9, dtype=np.float32)
                safe_mat_name = "".join(c for c in material.name if c.isalnum() or c in ("-", "_", ".")).rstrip()
                output_path = output_dir / f"{rule.output_name_prefix}{safe_mat_name}.png"
                output_path.write_text(rule.rule_name, encoding="utf-8")
                return str(output_path), result_pixels

            with mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite, "_find_base_color_texture", return_value=object()), \
                 mock.patch.object(tt_normal_map.ChannelProcessor, "load_image_pixels", return_value=(base_pixels, 2, 2)), \
                 mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite, "_apply_composite_rule", new=fake_apply):
                result = operator.execute(context)

            self.assertEqual(result, {"FINISHED"})
            self.assertEqual([call[0] for call in calls], ["StepA", "StepB"])
            self.assertTrue(np.allclose(calls[1][1], np.full((2, 2, 4), 0.1, dtype=np.float32)))

            remaining_files = sorted(Path(temp_dir).glob("*.png"))
            self.assertEqual(len(remaining_files), 1)
            self.assertTrue(remaining_files[0].name.startswith("StepB_"))
            self.assertEqual(remaining_files[0].read_text(encoding="utf-8"), "StepB")

    def test_execute_keeps_all_generated_outputs_when_materials_are_created(self):
        """测试创建材质模式时保留所有规则的输出文件"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()

        base_pixels = np.zeros((2, 2, 4), dtype=np.float32)
        base_pixels[:, :, 3] = 1.0

        rule_a = types.SimpleNamespace(
            rule_name="StepA",
            input_source_mode="BASE_COLOR",
            input_rule_name="",
            output_name_prefix="StepA_",
            output_channels=[
                types.SimpleNamespace(source_type="CONSTANT", source_channel="R", constant_value=0.1, invert=False),
                types.SimpleNamespace(source_type="CONSTANT", source_channel="G", constant_value=0.2, invert=False),
                types.SimpleNamespace(source_type="CONSTANT", source_channel="B", constant_value=0.3, invert=False),
                types.SimpleNamespace(source_type="CONSTANT", source_channel="A", constant_value=1.0, invert=False),
            ],
            normal_strength=5.0,
            normal_blur_radius=1.0,
            normal_invert_height=False,
            enabled=True,
        )
        rule_b = types.SimpleNamespace(
            rule_name="StepB",
            input_source_mode="PREVIOUS_OUTPUT",
            input_rule_name="",
            output_name_prefix="StepB_",
            output_channels=[
                types.SimpleNamespace(source_type="IMAGE_CHANNEL", source_channel="R", constant_value=0.0, invert=False),
                types.SimpleNamespace(source_type="IMAGE_CHANNEL", source_channel="G", constant_value=0.0, invert=False),
                types.SimpleNamespace(source_type="IMAGE_CHANNEL", source_channel="B", constant_value=0.0, invert=False),
                types.SimpleNamespace(source_type="CONSTANT", source_channel="A", constant_value=1.0, invert=False),
            ],
            normal_strength=5.0,
            normal_blur_radius=1.0,
            normal_invert_height=False,
            enabled=True,
        )

        class _HashableMaterial:
            def __init__(self, name):
                self.name = name
                self.use_nodes = False
            def __hash__(self):
                return hash(self.name)
            def __eq__(self, other):
                return isinstance(other, _HashableMaterial) and self.name == other.name

        material = _HashableMaterial("Mat One")
        obj = types.SimpleNamespace(
            type="MESH",
            material_slots=[types.SimpleNamespace(material=material)],
            data=types.SimpleNamespace(materials=[]),
        )
        props = types.SimpleNamespace(
            output_dir="",
            composite_rules=[rule_a, rule_b],
            normal_map_create_materials=True,
        )
        context = types.SimpleNamespace(
            selected_objects=[obj],
            scene=types.SimpleNamespace(texture_tools_props=props),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            props.output_dir = temp_dir
            created_materials = []
            operator.report = lambda *_args, **_kwargs: None

            def fake_apply(self, rule, material, base_pixels, width, height, processor, output_dir):
                result_pixels = np.full((height, width, 4), 0.1 if rule.rule_name == "StepA" else 0.9, dtype=np.float32)
                safe_mat_name = "".join(c for c in material.name if c.isalnum() or c in ("-", "_", ".")).rstrip()
                output_path = output_dir / f"{rule.output_name_prefix}{safe_mat_name}.png"
                output_path.write_text(rule.rule_name, encoding="utf-8")
                return str(output_path), result_pixels

            def fake_create_material(self, new_mat_name, composite_image_path):
                created_materials.append((new_mat_name, Path(composite_image_path)))
                return types.SimpleNamespace(name=new_mat_name), True

            with mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite, "_find_base_color_texture", return_value=object()), \
                 mock.patch.object(tt_normal_map.ChannelProcessor, "load_image_pixels", return_value=(base_pixels, 2, 2)), \
                 mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite, "_apply_composite_rule", new=fake_apply), \
                 mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite, "_create_composite_material", new=fake_create_material):
                result = operator.execute(context)

            self.assertEqual(result, {"FINISHED"})
            self.assertEqual([name for name, _path in created_materials], ["StepA_Mat One", "StepB_Mat One"])
            for _name, path in created_materials:
                self.assertTrue(path.exists(), path)

            remaining_files = sorted(Path(temp_dir).glob("*.png"))
            self.assertEqual([path.name for path in remaining_files], ["StepA_MatOne.png", "StepB_MatOne.png"])


class GeneratedMapDependencyTests(unittest.TestCase):
    """测试派生贴图依赖图：颜色 → 置换 → 法线 → (置换+法线) → AO，每种贴图只生成一次"""

    @staticmethod
    def _channel(source_type, source_channel="R", constant_value=0.0, invert=False):
        return types.SimpleNamespace(
            source_type=source_type,
            source_channel=source_channel,
            constant_value=constant_value,
            invert=invert,
        )

    @staticmethod
    def _rule(channels, **overrides):
        params = dict(
            rule_name="DepRule",
            input_source_mode="BASE_COLOR",
            input_rule_name="",
            normal_strength=5.0,
            normal_blur_radius=0.0,
            normal_invert_height=False,
            ao_radius=6,
            ao_height_scale=16.0,
            ao_power=1.0,
        )
        params.update(overrides)
        return types.SimpleNamespace(output_channels=channels, **params)

    @staticmethod
    def _pit_pixels(size=32, background=0.8, pit=0.2):
        """构造一张中央带凹陷的颜色图（凹陷 = 暗色区域）"""
        pixels = np.ones((size, size, 4), dtype=np.float32) * background
        pixels[12:20, 12:20, 0:3] = pit
        pixels[:, :, 3] = 1.0
        return pixels

    def test_normal_generated_once_when_multiple_channels_use_it(self):
        """R/G/B 同时引用法线时，法线贴图只计算一次"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        processor = tt_normal_map.ChannelProcessor()

        rule = self._rule([
            self._channel("GENERATED_NORMAL", "R"),
            self._channel("GENERATED_NORMAL", "G"),
            self._channel("GENERATED_NORMAL", "B"),
            self._channel("CONSTANT", "A", constant_value=1.0),
        ])
        pixels = self._pit_pixels()

        with mock.patch.object(
            tt_normal_map.ChannelProcessor, "sobel_xy", wraps=tt_normal_map.ChannelProcessor.sobel_xy
        ) as sobel_spy:
            operator._compose_rule_pixels(rule, pixels, 32, 32, processor)

        self.assertEqual(sobel_spy.call_count, 1)

    def test_ao_darkens_cavities_from_geometry(self):
        """AO 由置换+法线推导：凹陷处遮蔽变暗，平坦处接近 1"""
        processor = tt_normal_map.ChannelProcessor()

        size = 32
        height = np.ones((size, size), dtype=np.float32) * 0.8
        height[12:20, 12:20] = 0.2
        normal = (
            np.full((size, size), 0.5, dtype=np.float32),
            np.full((size, size), 0.5, dtype=np.float32),
            np.ones((size, size), dtype=np.float32),
        )

        ao = processor.generate_ao_from_geometry(height, normal, radius=6, height_scale=16.0, power=1.0)

        self.assertGreater(float(ao[2, 2]), 0.99)
        self.assertLess(float(ao[16, 16]), float(ao[2, 2]))
        self.assertLess(float(ao[16, 16]), 1.0)

    def test_compose_normal_xy_plus_ao_example(self):
        """端到端：R=法线X、G=法线Y、B=AO —— 法线与 AO 沿依赖链各算一次再取通道"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        processor = tt_normal_map.ChannelProcessor()

        rule = self._rule([
            self._channel("GENERATED_NORMAL", "R"),
            self._channel("GENERATED_NORMAL", "G"),
            self._channel("GENERATED_AO"),
            self._channel("CONSTANT", "A", constant_value=1.0),
        ])
        pixels = self._pit_pixels()

        output = operator._compose_rule_pixels(rule, pixels, 32, 32, processor)

        reference = tt_normal_map.RuleMapGenerator(processor, pixels, 32, 32, rule)
        normal_x, normal_y, _normal_z = reference.get("NORMAL")
        expected_ao = reference.get("AO")

        self.assertTrue(np.allclose(output[:, :, 0], normal_x))
        self.assertTrue(np.allclose(output[:, :, 1], normal_y))
        self.assertTrue(np.allclose(output[:, :, 2], expected_ao))
        self.assertTrue(np.allclose(output[:, :, 3], 1.0))

        # 语义校验：合成结果里凹陷区域的 AO 通道确实更暗
        self.assertLess(float(output[16, 16, 2]), float(output[2, 2, 2]))

    def test_height_channel_is_processed_displacement(self):
        """置换通道输出的是含反转/模糊处理后的高度（法线的上游产物）"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        processor = tt_normal_map.ChannelProcessor()

        pixels = np.zeros((4, 4, 4), dtype=np.float32)
        pixels[:, :, 0] = 0.25
        pixels[:, :, 1] = 0.25
        pixels[:, :, 2] = 0.25
        pixels[:, :, 3] = 1.0

        rule = self._rule(
            [self._channel("GENERATED_HEIGHT")] * 3 + [self._channel("CONSTANT", "A", constant_value=1.0)],
            normal_invert_height=True,
        )
        output = operator._compose_rule_pixels(rule, pixels, 4, 4, processor)

        luminance = 0.299 * 0.25 + 0.587 * 0.25 + 0.114 * 0.25
        self.assertTrue(np.allclose(output[:, :, 0], 1.0 - luminance, atol=1e-6))


if __name__ == "__main__":
    unittest.main()
