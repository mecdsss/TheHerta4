import unittest

import numpy as np

from utils.color_attribute_utils import (
    get_color_storage_field,
    read_color_attribute_data,
    sample_color_attribute_to_loops,
    write_color_attribute_data,
)


class DummyAttributeData:
    def __init__(self, records, expected_field):
        self.records = np.asarray(records, dtype=np.float32).reshape(-1, 4)
        self.expected_field = expected_field
        self.last_field = None

    def __len__(self):
        return len(self.records)

    def foreach_get(self, field_name, output):
        self.last_field = field_name
        if field_name != self.expected_field:
            raise AssertionError(f"unexpected foreach_get field: {field_name}")
        output[:] = self.records.reshape(-1)

    def foreach_set(self, field_name, values):
        self.last_field = field_name
        if field_name != self.expected_field:
            raise AssertionError(f"unexpected foreach_set field: {field_name}")
        self.records = np.asarray(values, dtype=np.float32).reshape(-1, 4)


class DummyColorAttribute:
    def __init__(self, data_type, domain, records):
        self.data_type = data_type
        self.domain = domain
        self.data = DummyAttributeData(
            records=records,
            expected_field="color" if data_type == "FLOAT_COLOR" else "color_srgb",
        )


class DummyLegacyVertexColorLayer:
    def __init__(self, records):
        self.data = DummyAttributeData(records=records, expected_field="color")


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
    def __init__(self, vertex_indices):
        self.loops = DummyLoops(vertex_indices)


class ColorAttributeUtilsTests(unittest.TestCase):
    def test_byte_color_uses_srgb_storage_field(self):
        attr = DummyColorAttribute(
            data_type="BYTE_COLOR",
            domain="CORNER",
            records=[(0.1, 0.2, 0.3, 1.0)],
        )

        data = read_color_attribute_data(attr)

        self.assertEqual(get_color_storage_field(attr), "color_srgb")
        self.assertEqual(attr.data.last_field, "color_srgb")
        np.testing.assert_allclose(data, np.array([[0.1, 0.2, 0.3, 1.0]], dtype=np.float32))

    def test_float_color_uses_linear_storage_field(self):
        attr = DummyColorAttribute(
            data_type="FLOAT_COLOR",
            domain="CORNER",
            records=[(0.25, 0.5, 0.75, 1.0)],
        )

        data = read_color_attribute_data(attr)

        self.assertEqual(get_color_storage_field(attr), "color")
        self.assertEqual(attr.data.last_field, "color")
        np.testing.assert_allclose(data, np.array([[0.25, 0.5, 0.75, 1.0]], dtype=np.float32))

    def test_write_color_attribute_data_uses_matching_storage_field(self):
        attr = DummyColorAttribute(
            data_type="BYTE_COLOR",
            domain="CORNER",
            records=[(0.0, 0.0, 0.0, 0.0)],
        )

        write_color_attribute_data(attr, [(1.0, 0.5, 0.25, 1.0)])

        self.assertEqual(attr.data.last_field, "color_srgb")
        np.testing.assert_allclose(attr.data.records, np.array([[1.0, 0.5, 0.25, 1.0]], dtype=np.float32))

    def test_legacy_vertex_color_layer_uses_color_field(self):
        attr = DummyLegacyVertexColorLayer(records=[(0.2, 0.4, 0.6, 1.0)])

        data = read_color_attribute_data(attr)

        self.assertEqual(get_color_storage_field(attr), "color")
        self.assertEqual(attr.data.last_field, "color")
        np.testing.assert_allclose(data, np.array([[0.2, 0.4, 0.6, 1.0]], dtype=np.float32))

    def test_point_domain_is_expanded_to_loop_order(self):
        mesh = DummyMesh(vertex_indices=[2, 0, 1, 2])
        attr = DummyColorAttribute(
            data_type="BYTE_COLOR",
            domain="POINT",
            records=[
                (0.1, 0.1, 0.1, 1.0),
                (0.2, 0.2, 0.2, 1.0),
                (0.3, 0.3, 0.3, 1.0),
            ],
        )

        data = sample_color_attribute_to_loops(mesh, attr)

        expected = np.array(
            [
                (0.3, 0.3, 0.3, 1.0),
                (0.1, 0.1, 0.1, 1.0),
                (0.2, 0.2, 0.2, 1.0),
                (0.3, 0.3, 0.3, 1.0),
            ],
            dtype=np.float32,
        )
        np.testing.assert_allclose(data, expected)


if __name__ == "__main__":
    unittest.main()
