import unittest

from common.text_width_utils import (
    DEFAULT_MIN_NODE_WIDTH,
    DEFAULT_TEXT_WIDTH_SAFETY_FACTOR,
    estimate_text_width,
    get_effective_min_width,
    is_wide_character,
)


class TextWidthUtilsTests(unittest.TestCase):
    """测试文字宽度计算工具：字符宽度估算和最小宽度计算"""

    def test_get_effective_min_width_respects_node_minimum(self):
        """测试 get_effective_min_width 尊重节点最小宽度"""
        self.assertEqual(get_effective_min_width(300), 300.0)
        self.assertEqual(get_effective_min_width(120), DEFAULT_MIN_NODE_WIDTH)

    def test_wide_characters_count_wider_than_ascii(self):
        """测试宽字符（如中文）宽度大于 ASCII 字符"""
        self.assertTrue(is_wide_character("测"))
        self.assertFalse(is_wide_character("A"))
        self.assertGreater(
            estimate_text_width("测试", min_width=0, padding=0),
            estimate_text_width("AB", min_width=0, padding=0),
        )

    def test_multiline_uses_widest_line(self):
        """测试多行文本使用最宽行的宽度"""
        single_line_width = estimate_text_width("very_long_identifier", min_width=0, padding=0)
        multi_line_width = estimate_text_width("short\nvery_long_identifier", min_width=0, padding=0)
        self.assertEqual(single_line_width, multi_line_width)

    def test_empty_text_falls_back_to_min_width(self):
        """测试空文本回退到最小宽度"""
        self.assertEqual(estimate_text_width("", min_width=320), 320.0)

    def test_safety_factor_expands_estimated_width(self):
        """测试安全系数扩展估算宽度"""
        self.assertEqual(
            estimate_text_width("AB", min_width=0, padding=0),
            16.0 * DEFAULT_TEXT_WIDTH_SAFETY_FACTOR,
        )


if __name__ == "__main__":
    unittest.main()
