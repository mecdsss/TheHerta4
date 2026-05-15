import unittest

import numpy as np

from utils.format_utils import FormatUtils


class FormatUtilsTests(unittest.TestCase):
    def test_fit_component_width_truncates_extra_columns(self):
        source = np.arange(12, dtype=np.uint8).reshape(3, 4)

        fitted = FormatUtils.fit_component_width(source, 2)

        expected = np.array(
            [
                [0, 1],
                [4, 5],
                [8, 9],
            ],
            dtype=np.uint8,
        )
        np.testing.assert_array_equal(fitted, expected)

    def test_fit_component_width_pads_missing_columns(self):
        source = np.array(
            [
                [10, 11],
                [20, 21],
            ],
            dtype=np.uint16,
        )

        fitted = FormatUtils.fit_component_width(source, 4)

        expected = np.array(
            [
                [10, 11, 0, 0],
                [20, 21, 0, 0],
            ],
            dtype=np.uint16,
        )
        np.testing.assert_array_equal(fitted, expected)

    def test_fit_component_width_prevents_structured_assignment_broadcast_error(self):
        source = np.arange(72, dtype=np.uint8).reshape(3, 24)
        structured = np.zeros(3, dtype=np.dtype([("BLENDINDICES", (np.uint8, 8))]))

        structured["BLENDINDICES"] = FormatUtils.fit_component_width(source, 8)

        self.assertEqual(structured["BLENDINDICES"].shape, (3, 8))
        np.testing.assert_array_equal(structured["BLENDINDICES"], source[:, :8])


if __name__ == "__main__":
    unittest.main()
