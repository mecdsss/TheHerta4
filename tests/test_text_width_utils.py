import unittest

from common.text_width_utils import (
    DEFAULT_MIN_NODE_WIDTH,
    DEFAULT_TEXT_WIDTH_SAFETY_FACTOR,
    estimate_text_width,
    get_effective_min_width,
    is_wide_character,
)


class TextWidthUtilsTests(unittest.TestCase):
    def test_get_effective_min_width_respects_node_minimum(self):
        self.assertEqual(get_effective_min_width(300), 300.0)
        self.assertEqual(get_effective_min_width(120), DEFAULT_MIN_NODE_WIDTH)

    def test_wide_characters_count_wider_than_ascii(self):
        self.assertTrue(is_wide_character("测"))
        self.assertFalse(is_wide_character("A"))
        self.assertGreater(
            estimate_text_width("测试", min_width=0, padding=0),
            estimate_text_width("AB", min_width=0, padding=0),
        )

    def test_multiline_uses_widest_line(self):
        single_line_width = estimate_text_width("very_long_identifier", min_width=0, padding=0)
        multi_line_width = estimate_text_width("short\nvery_long_identifier", min_width=0, padding=0)
        self.assertEqual(single_line_width, multi_line_width)

    def test_empty_text_falls_back_to_min_width(self):
        self.assertEqual(estimate_text_width("", min_width=320), 320.0)

    def test_safety_factor_expands_estimated_width(self):
        self.assertEqual(
            estimate_text_width("AB", min_width=0, padding=0),
            16.0 * DEFAULT_TEXT_WIDTH_SAFETY_FACTOR,
        )


if __name__ == "__main__":
    unittest.main()
