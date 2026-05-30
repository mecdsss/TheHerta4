from .format_utils import Fatal


class SSMTErrorUtils:
    """SSMT 错误工具类"""

    @staticmethod
    def raise_fatal(error_message: str):
        """抛出致命错误异常"""
        raise Fatal(error_message)
