import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_obj_buffer_helper_color_test_pkg"
for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module("bpy", types=types.SimpleNamespace(Object=object, Mesh=object))
_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(WWMI="WWMI", NTEMI="NTEMI", EFMI="EFMI", YYSLS="YYSLS", SnowBreak="SnowBreak"),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="GIMI"))
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        recalculate_color=lambda: False,
        recalculate_tangent=lambda: False,
        import_merged_vgmap=lambda: False,
    ),
)
_install_module(
    f"{PKG}.utils.ssmt_error_utils",
    SSMTErrorUtils=types.SimpleNamespace(raise_fatal=lambda message: (_ for _ in ()).throw(RuntimeError(message))),
    Fatal=RuntimeError,
)
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace(Start=lambda *_: None, End=lambda *_: None))
_install_module(f"{PKG}.utils.tbn_codec", TBNCodec=types.SimpleNamespace())


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


format_utils_module = _load_module("utils.format_utils", "utils/format_utils.py")
_load_module("utils.color_attribute_utils", "utils/color_attribute_utils.py")
obj_buffer_helper_module = _load_module("common.obj_buffer_helper", "common/obj_buffer_helper.py")

ObjBufferHelper = obj_buffer_helper_module.ObjBufferHelper
FormatUtils = format_utils_module.FormatUtils


class DummyColorAttributeData:
    def __init__(self, records, field_name):
        self.records = np.asarray(records, dtype=np.float32).reshape(-1, 4)
        self.field_name = field_name

    def __len__(self):
        return len(self.records)

    def foreach_get(self, field_name, output):
        if field_name != self.field_name:
            raise AssertionError(f"unexpected field {field_name}")
        output[:] = self.records.reshape(-1)


class DummyColorAttribute:
    def __init__(self, name, domain, data_type, records):
        self.name = name
        self.domain = domain
        self.data_type = data_type
        self.data = DummyColorAttributeData(
            records=records,
            field_name="color" if data_type == "FLOAT_COLOR" else "color_srgb",
        )


class DummyColorAttributes(dict):
    def __contains__(self, item):
        return dict.__contains__(self, item)


class DummyVertexColors(dict):
    def __contains__(self, item):
        return dict.__contains__(self, item)


class DummyLoops:
    def __init__(self, vertex_indices):
        self.vertex_indices = np.asarray(vertex_indices, dtype=np.int32)

    def __len__(self):
        return len(self.vertex_indices)

    def foreach_get(self, field_name, output):
        if field_name != "vertex_index":
            raise AssertionError(f"unexpected loop field: {field_name}")
        output[:] = self.vertex_indices


class DummyMesh:
    def __init__(self, vertex_indices, color_attr):
        self.loops = DummyLoops(vertex_indices)
        self.color_attributes = DummyColorAttributes({color_attr.name: color_attr})
        self.vertex_colors = DummyVertexColors()


class ObjBufferHelperColorTests(unittest.TestCase):
    def test_parse_color_expands_point_domain_to_loop_order(self):
        mesh = DummyMesh(
            vertex_indices=[2, 0, 1, 2],
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="POINT",
                data_type="BYTE_COLOR",
                records=[
                    (0.1, 0.1, 0.1, 1.0),
                    (0.2, 0.2, 0.2, 1.0),
                    (0.3, 0.3, 0.3, 1.0),
                ],
            ),
        )
        element = types.SimpleNamespace(Format="R8G8B8A8_UNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        expected = FormatUtils.convert_4x_float32_to_r8g8b8a8_unorm(
            np.array(
                [
                    (0.3, 0.3, 0.3, 1.0),
                    (0.1, 0.1, 0.1, 1.0),
                    (0.2, 0.2, 0.2, 1.0),
                    (0.3, 0.3, 0.3, 1.0),
                ],
                dtype=np.float32,
            )
        )
        np.testing.assert_array_equal(result, expected)

    def test_parse_color_supports_snorm_output(self):
        mesh = DummyMesh(
            vertex_indices=[0, 1],
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="CORNER",
                data_type="FLOAT_COLOR",
                records=[
                    (-1.0, 0.0, 1.0, 1.0),
                    (0.5, -0.5, 0.25, -0.25),
                ],
            ),
        )
        element = types.SimpleNamespace(Format="R8G8B8A8_SNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        expected = FormatUtils.convert_4x_float32_to_r8g8b8a8_snorm(
            np.array(
                [
                    (-1.0, 0.0, 1.0, 1.0),
                    (0.5, -0.5, 0.25, -0.25),
                ],
                dtype=np.float32,
            )
        )
        np.testing.assert_array_equal(result, expected)

    def test_parse_color_supports_r16g16b16a16_unorm(self):
        mesh = DummyMesh(
            vertex_indices=[0],
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="CORNER",
                data_type="BYTE_COLOR",
                records=[(0.25, 0.5, 0.75, 1.0)],
            ),
        )
        element = types.SimpleNamespace(Format="R16G16B16A16_UNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        expected = FormatUtils.convert_4x_float32_to_r16g16b16a16_unorm(
            np.array([(0.25, 0.5, 0.75, 1.0)], dtype=np.float32)
        )
        np.testing.assert_array_equal(result, expected)


if __name__ == "__main__":
    unittest.main()
