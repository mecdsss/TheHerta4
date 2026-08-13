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
    def test_alpha_prefix_for_semitransparency_uses_ttlmap(self):
        self.assertEqual(tt_alpha_extract.alpha_prefix_for_semitransparency(True), "TTLMap_")

    def test_alpha_prefix_for_semitransparency_uses_fxmap(self):
        self.assertEqual(tt_alpha_extract.alpha_prefix_for_semitransparency(False), "FXMap_")

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

    def _build_execute_harness(self, allow_semitransparency, extra_material_slots=None, threshold=0.1):
        class _FakePixels:
            def __init__(self, values):
                self._values = values

            def foreach_get(self, target):
                target[:] = self._values

            def foreach_set(self, values):
                self._values = np.asarray(values, dtype=np.float32).copy()

        class _FakeTexture:
            def __init__(self):
                self.name = "BaseTex.png"
                self.size = (2, 2)
                if allow_semitransparency:
                    alpha_values = [0.8, 1.0, 1.0, 1.0]
                else:
                    alpha_values = [0.0, 1.0, 1.0, 1.0]
                self.pixels = _FakePixels(np.array([
                    0.1, 0.2, 0.3, alpha_values[0],
                    0.1, 0.2, 0.3, alpha_values[1],
                    0.1, 0.2, 0.3, alpha_values[2],
                    0.1, 0.2, 0.3, alpha_values[3],
                ], dtype=np.float32))

        class _FakeSavedImage:
            def __init__(self, name):
                self.name = name
                self.pixels = _FakePixels(np.zeros(16, dtype=np.float32))
                self.filepath_raw = ""
                self.file_format = ""

            def save(self):
                if self.filepath_raw:
                    with open(self.filepath_raw, "wb") as file_obj:
                        file_obj.write(b"png")

        class _FakeMaterials(list):
            pass

        class _HashableMaterial:
            def __init__(self, name):
                self.name = name
                self.use_nodes = True

            def __hash__(self):
                return hash(self.name)

            def __eq__(self, other):
                return isinstance(other, _HashableMaterial) and self.name == other.name

        class _FakeObject:
            def __init__(self, material, extra_materials):
                self.type = "MESH"
                slots = [types.SimpleNamespace(material=material)]
                slots.extend(types.SimpleNamespace(material=extra) for extra in extra_materials)
                self.material_slots = slots
                self.data = types.SimpleNamespace(materials=_FakeMaterials())

        material = _HashableMaterial("Mat")
        extra_materials = []
        if extra_material_slots:
            extra_materials = [_HashableMaterial(name) for name in extra_material_slots]
        obj = _FakeObject(material, extra_materials)

        created_names = []
        operator = tt_alpha_extract.TT_OT_extract_alpha_channel()
        operator.report = lambda _level, _message: None
        operator._find_base_color_texture = lambda _material: _FakeTexture()
        operator._write_alpha_to_vertex_colors = lambda *_args, **_kwargs: True
        operator._create_alpha_material = lambda name, _path: (
            created_names.append(name) or types.SimpleNamespace(name=name),
            True,
        )

        tt_alpha_extract.bpy.data = types.SimpleNamespace(
            images=types.SimpleNamespace(
                new=lambda name, **_kwargs: _FakeSavedImage(name),
                remove=lambda *_args, **_kwargs: None,
                load=lambda *_args, **_kwargs: None,
            ),
            materials=types.SimpleNamespace(
                get=lambda _name: None,
                new=lambda name: types.SimpleNamespace(name=name),
            ),
        )

        return operator, material, obj, created_names

    def test_execute_uses_ttlmap_prefix_when_semitransparency_allowed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            operator, _material, obj, created_names = self._build_execute_harness(True)
            props = types.SimpleNamespace(
                output_dir=temp_dir,
                alpha_extract_allow_semitransparency=True,
                alpha_extract_threshold=0.1,
                alpha_extract_create_materials=True,
            )
            context = types.SimpleNamespace(
                selected_objects=[obj],
                scene=types.SimpleNamespace(texture_tools_props=props),
            )

            result = operator.execute(context)

            self.assertEqual(result, {'FINISHED'})
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "TTLMap_BaseTex.png")))
            self.assertFalse(os.path.exists(os.path.join(temp_dir, "FXMap_BaseTex.png")))
            self.assertEqual(created_names, ["TTLMap_Mat"])
            self.assertEqual([m.name for m in obj.data.materials], ["TTLMap_Mat"])

    def test_execute_uses_fxmap_prefix_when_semitransparency_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            operator, _material, obj, created_names = self._build_execute_harness(False)
            props = types.SimpleNamespace(
                output_dir=temp_dir,
                alpha_extract_allow_semitransparency=False,
                alpha_extract_threshold=0.1,
                alpha_extract_create_materials=True,
            )
            context = types.SimpleNamespace(
                selected_objects=[obj],
                scene=types.SimpleNamespace(texture_tools_props=props),
            )

            result = operator.execute(context)

            self.assertEqual(result, {'FINISHED'})
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "FXMap_BaseTex.png")))
            self.assertFalse(os.path.exists(os.path.join(temp_dir, "TTLMap_BaseTex.png")))
            self.assertEqual(created_names, ["FXMap_Mat"])
            self.assertEqual([m.name for m in obj.data.materials], ["FXMap_Mat"])

    def test_execute_skips_ttl_when_object_already_has_fxmap_material(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            operator, _material, obj, created_names = self._build_execute_harness(
                True, extra_material_slots=["FXMap_Existing"],
            )
            props = types.SimpleNamespace(
                output_dir=temp_dir,
                alpha_extract_allow_semitransparency=True,
                alpha_extract_threshold=0.1,
                alpha_extract_create_materials=True,
            )
            context = types.SimpleNamespace(
                selected_objects=[obj],
                scene=types.SimpleNamespace(texture_tools_props=props),
            )

            result = operator.execute(context)

            self.assertEqual(result, {'FINISHED'})
            self.assertEqual(created_names, [])
            self.assertEqual([m.name for m in obj.data.materials], [])

    def test_execute_skips_fx_when_object_already_has_ttlmap_material(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            operator, _material, obj, created_names = self._build_execute_harness(
                False, extra_material_slots=["TTLMap_Existing"],
            )
            props = types.SimpleNamespace(
                output_dir=temp_dir,
                alpha_extract_allow_semitransparency=False,
                alpha_extract_threshold=0.1,
                alpha_extract_create_materials=True,
            )
            context = types.SimpleNamespace(
                selected_objects=[obj],
                scene=types.SimpleNamespace(texture_tools_props=props),
            )

            result = operator.execute(context)

            self.assertEqual(result, {'FINISHED'})
            self.assertEqual(created_names, [])
            self.assertEqual([m.name for m in obj.data.materials], [])


if __name__ == "__main__":
    unittest.main()
