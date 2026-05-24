import unittest

import numpy as np

from utils.vertex_color_utils import (
    build_vertex_color_payload,
    ensure_color_attribute,
    srgb_to_linear,
)


class DummyColorAttribute:
    def __init__(self, name, domain, data_type):
        self.name = name
        self.domain = domain
        self.data_type = data_type


class DummyColorAttributes:
    def __init__(self, existing=None):
        self._items = {attr.name: attr for attr in (existing or [])}
        self.removed = []
        self.created = []

    def get(self, name):
        return self._items.get(name)

    def remove(self, attr):
        self.removed.append(attr)
        self._items.pop(attr.name, None)

    def new(self, name, type, domain):
        attr = DummyColorAttribute(name=name, domain=domain, data_type=type)
        self._items[name] = attr
        self.created.append(attr)
        return attr


class VertexColorUtilsTests(unittest.TestCase):
    def test_full_color_builds_linear_float_payload(self):
        payload = build_vertex_color_payload(
            num_loops=2,
            color_rgba_srgb=(0.5, 1.0, 0.0, 1.0),
            vc_mode="FULL_COLOR",
        )

        expected_color = np.array(
            [srgb_to_linear(0.5), 1.0, 0.0, 1.0],
            dtype=np.float32,
        )
        expected_payload = np.tile(expected_color, 2)

        self.assertEqual(payload.dtype, np.float32)
        np.testing.assert_allclose(payload, expected_payload, rtol=1e-6, atol=1e-6)
        self.assertAlmostEqual(float(payload[0]), float(srgb_to_linear(0.5)), places=6)

    def test_alpha_only_preserves_rgb_channels(self):
        existing_colors = np.array(
            [0.1, 0.2, 0.3, 0.9, 0.4, 0.5, 0.6, 0.8],
            dtype=np.float32,
        )

        payload = build_vertex_color_payload(
            num_loops=2,
            color_rgba_srgb=(0.8, 0.1, 0.2, 0.25),
            vc_mode="ALPHA_ONLY",
            existing_colors=existing_colors,
        )

        expected_payload = existing_colors.copy()
        expected_payload[3::4] = 0.25

        self.assertEqual(payload.dtype, np.float32)
        np.testing.assert_allclose(payload, expected_payload, rtol=1e-6, atol=1e-6)

    def test_alpha_only_requires_existing_color_buffer(self):
        with self.assertRaises(ValueError):
            build_vertex_color_payload(
                num_loops=1,
                color_rgba_srgb=(1.0, 1.0, 1.0, 1.0),
                vc_mode="ALPHA_ONLY",
            )

    def test_ensure_color_attribute_reuses_matching_attribute(self):
        existing = DummyColorAttribute("COLOR", "CORNER", "BYTE_COLOR")
        collection = DummyColorAttributes(existing=[existing])

        attr = ensure_color_attribute(collection, "COLOR", "CORNER", "BYTE_COLOR")

        self.assertIs(attr, existing)
        self.assertEqual(collection.removed, [])
        self.assertEqual(collection.created, [])

    def test_ensure_color_attribute_recreates_mismatched_attribute(self):
        existing = DummyColorAttribute("COLOR", "POINT", "FLOAT_COLOR")
        collection = DummyColorAttributes(existing=[existing])

        attr = ensure_color_attribute(collection, "COLOR", "CORNER", "BYTE_COLOR")

        self.assertIsNot(attr, existing)
        self.assertEqual(collection.removed, [existing])
        self.assertEqual(len(collection.created), 1)
        self.assertEqual(attr.domain, "CORNER")
        self.assertEqual(attr.data_type, "BYTE_COLOR")


if __name__ == "__main__":
    unittest.main()
