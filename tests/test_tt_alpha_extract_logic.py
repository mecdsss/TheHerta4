# -*- coding: utf-8 -*-
import importlib.util
import os
import sys
import tempfile
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


PKG = "_tt_alpha_extract_logic_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object),
    path=types.SimpleNamespace(abspath=lambda value: value),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.utils.color_attribute_utils", write_color_attribute_data=lambda *_args, **_kwargs: None)


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "tt_alpha_extract.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.tt_alpha_extract", module_path)
tt_alpha_extract = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tt_alpha_extract
spec.loader.exec_module(tt_alpha_extract)


class AlphaExtractLogicTests(unittest.TestCase):
    def test_effectively_opaque_accepts_exact_full_white(self):
        alpha = np.ones((4, 4), dtype=np.float32)
        self.assertTrue(tt_alpha_extract._alpha_channel_is_effectively_opaque(alpha))

    def test_effectively_opaque_accepts_float_noise_near_full_white(self):
        alpha = np.full((4, 4), 0.99999994, dtype=np.float32)
        self.assertTrue(tt_alpha_extract._alpha_channel_is_effectively_opaque(alpha))

    def test_effectively_opaque_accepts_values_above_252_over_255_threshold(self):
        alpha = np.full((4, 4), 252.0 / 255.0, dtype=np.float32)
        self.assertTrue(tt_alpha_extract._alpha_channel_is_effectively_opaque(alpha))

    def test_effectively_opaque_rejects_values_below_252_over_255_threshold(self):
        alpha = np.full((4, 4), 251.0 / 255.0, dtype=np.float32)
        self.assertFalse(tt_alpha_extract._alpha_channel_is_effectively_opaque(alpha))

    def test_effectively_opaque_rejects_real_transparency(self):
        alpha = np.array([[1.0, 1.0], [1.0, 0.8]], dtype=np.float32)
        self.assertFalse(tt_alpha_extract._alpha_channel_is_effectively_opaque(alpha))

    def test_execute_skips_output_for_full_white_alpha_even_when_allowing_semitransparency(self):
        class _FakePixels:
            def __init__(self, values):
                self._values = values

            def foreach_get(self, target):
                target[:] = self._values

        class _FakeTexture:
            def __init__(self):
                self.name = "BaseTex.png"
                self.size = (2, 2)
                self.pixels = _FakePixels(np.array([
                    0.1, 0.2, 0.3, 1.0,
                    0.1, 0.2, 0.3, 1.0,
                    0.1, 0.2, 0.3, 1.0,
                    0.1, 0.2, 0.3, 1.0,
                ], dtype=np.float32))

        class _FakeMaterials(list):
            def append(self, material):
                super().append(material)

        class _FakeObject:
            def __init__(self, material):
                self.type = "MESH"
                self.material_slots = [types.SimpleNamespace(material=material)]
                self.data = types.SimpleNamespace(materials=_FakeMaterials())

        reports = []
        operator = tt_alpha_extract.TT_OT_extract_alpha_channel()
        operator.report = lambda level, message: reports.append((level, message))
        operator._find_base_color_texture = lambda _material: _FakeTexture()
        operator._write_alpha_to_vertex_colors = lambda *_args, **_kwargs: True
        operator._create_alpha_material = lambda *_args, **_kwargs: (object(), True)

        class _HashableMaterial:
            def __init__(self, name):
                self.name = name
                self.use_nodes = True

            def __hash__(self):
                return hash(self.name)

            def __eq__(self, other):
                return isinstance(other, _HashableMaterial) and self.name == other.name

        material = _HashableMaterial("Mat")
        obj = _FakeObject(material)

        with tempfile.TemporaryDirectory() as temp_dir:
            props = types.SimpleNamespace(
                output_dir=temp_dir,
                alpha_extract_allow_semitransparency=True,
                alpha_extract_threshold=0.1,
                alpha_extract_create_materials=True,
                alpha_extract_material_prefix="FXMap_",
            )
            context = types.SimpleNamespace(
                selected_objects=[obj],
                scene=types.SimpleNamespace(texture_tools_props=props),
            )

            _fake_bpy.data = types.SimpleNamespace(
                images=types.SimpleNamespace(
                    new=lambda **_kwargs: self.fail("should not create alpha image for opaque texture"),
                    remove=lambda *_args, **_kwargs: None,
                    load=lambda *_args, **_kwargs: None,
                ),
                materials=types.SimpleNamespace(get=lambda _name: None, new=lambda name: types.SimpleNamespace(name=name)),
            )

            result = operator.execute(context)

            self.assertEqual(result, {'FINISHED'})
            self.assertFalse(any(name.endswith(".png") for name in os.listdir(temp_dir)))
            self.assertTrue(any("全白" in str(message) for _level, message in reports))


if __name__ == "__main__":
    unittest.main()
