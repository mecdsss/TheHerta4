from unicodedata import east_asian_width


DEFAULT_MIN_NODE_WIDTH = 200.0
DEFAULT_NODE_PADDING = 56.0
DEFAULT_NARROW_GLYPH_WIDTH = 8.0
DEFAULT_WIDE_GLYPH_WIDTH = 16.0
DEFAULT_TEXT_WIDTH_SAFETY_FACTOR = 1.5


def get_effective_min_width(node_min_width=None, fallback=DEFAULT_MIN_NODE_WIDTH):
    """获取有效的最小宽度值，确保不小于默认值"""
    try:
        numeric_min_width = float(node_min_width)
    except (TypeError, ValueError):
        numeric_min_width = fallback
    return max(float(fallback), numeric_min_width)


def _iter_display_lines(text):
    normalized_text = str(text or "").expandtabs(4)
    lines = normalized_text.splitlines()
    return lines if lines else [normalized_text]


def is_wide_character(char):
    """判断字符是否为宽字符（中文、日文等全角字符）"""
    return east_asian_width(char) in {"W", "F"}


def estimate_line_width(
    text,
    narrow_glyph_width=DEFAULT_NARROW_GLYPH_WIDTH,
    wide_glyph_width=DEFAULT_WIDE_GLYPH_WIDTH,
):
    """估算单行文本的显示宽度"""
    width = 0.0
    for char in str(text or ""):
        width += wide_glyph_width if is_wide_character(char) else narrow_glyph_width
    return width


def estimate_text_width(
    text,
    padding=DEFAULT_NODE_PADDING,
    min_width=DEFAULT_MIN_NODE_WIDTH,
    narrow_glyph_width=DEFAULT_NARROW_GLYPH_WIDTH,
    wide_glyph_width=DEFAULT_WIDE_GLYPH_WIDTH,
    safety_factor=DEFAULT_TEXT_WIDTH_SAFETY_FACTOR,
):
    """估算完整文本块的显示宽度，自动处理多行并应用安全系数"""
    try:
        effective_min_width = max(0.0, float(min_width))
    except (TypeError, ValueError):
        effective_min_width = DEFAULT_MIN_NODE_WIDTH
    widest_line = 0.0

    for line in _iter_display_lines(text):
        widest_line = max(
            widest_line,
            estimate_line_width(
                line,
                narrow_glyph_width=narrow_glyph_width,
                wide_glyph_width=wide_glyph_width,
            ),
        )

    adjusted_width = widest_line * float(safety_factor)
    return max(effective_min_width, adjusted_width + float(padding))
