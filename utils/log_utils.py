from .format_utils import Fatal
import builtins
import io
import os
import sys
import traceback


UTF8_ENCODING = "utf-8"
_ORIGINAL_PRINT = builtins.print


def _force_windows_console_utf8():
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def _reconfigure_stdio_utf8():
    _force_windows_console_utf8()
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding=UTF8_ENCODING, errors="replace")
        except Exception:
            pass


def _join_print_values(*values, sep=" ", end=""):
    parts = ["" if value is None else str(value) for value in values]
    return str(sep).join(parts) + str(end)


_reconfigure_stdio_utf8()


class LOG:
    _original_stdout = None
    _original_stderr = None
    _log_capture = None
    _is_collecting = False
    _print_hook_installed = False
    _installed_print = None
    _print_passthrough_depth = 0

    @classmethod
    def install_print_hook(cls):
        if cls._print_hook_installed:
            return

        def _hooked_print(*values, sep=" ", end="\n", file=None, flush=False):
            target = file if file is not None else sys.stdout
            text = _join_print_values(*values, sep=sep, end=end)

            if cls._print_passthrough_depth > 0:
                _ORIGINAL_PRINT(*values, sep=sep, end=end, file=target, flush=flush)
                return

            if target in (sys.stdout, cls._original_stdout, None):
                cls._write_stdout_text(text, flush=flush)
                return

            if target in (sys.stderr, cls._original_stderr):
                cls._write_stderr_text(text, flush=flush)
                return

            _ORIGINAL_PRINT(*values, sep=sep, end=end, file=target, flush=flush)

        builtins.print = _hooked_print
        cls._installed_print = _hooked_print
        cls._print_hook_installed = True

    @classmethod
    def uninstall_print_hook(cls):
        if not cls._print_hook_installed:
            return
        if builtins.print is cls._installed_print:
            builtins.print = _ORIGINAL_PRINT
        cls._installed_print = None
        cls._print_hook_installed = False

    @classmethod
    def _passthrough_print(cls, text, *, stream=None, flush=False):
        cls._print_passthrough_depth += 1
        try:
            _ORIGINAL_PRINT(text, sep="", end="", file=stream, flush=flush)
        finally:
            cls._print_passthrough_depth -= 1

    @classmethod
    def _write_stdout_text(cls, text, *, flush=False):
        target = sys.stdout
        cls._passthrough_print(text, stream=target, flush=flush)
        if cls._is_collecting and cls._log_capture is not None:
            cls._log_capture.write(text)

    @classmethod
    def _write_stderr_text(cls, text, *, flush=False):
        target = sys.stderr
        cls._passthrough_print(text, stream=target, flush=flush)
        if cls._is_collecting and cls._log_capture is not None:
            cls._log_capture.write(text)

    @classmethod
    def start_collecting(cls):
        """开始收集日志输出"""
        _reconfigure_stdio_utf8()
        cls.install_print_hook()
        cls._log_capture = io.StringIO()
        cls._original_stdout = sys.stdout
        cls._original_stderr = sys.stderr
        cls._is_collecting = True

    @classmethod
    def stop_collecting(cls):
        cls._is_collecting = False
        cls._original_stdout = None
        cls._original_stderr = None

    @classmethod
    def get_log_content(cls, strip_ansi: bool = False) -> str:
        if cls._log_capture:
            content = cls._log_capture.getvalue()
            return cls._strip_ansi_codes(content) if strip_ansi else content
        return ""

    @classmethod
    def clear_log(cls):
        """清空已收集的日志内容"""
        if cls._log_capture:
            cls._log_capture = io.StringIO()

    @classmethod
    def _normalize_log_text(cls, text) -> str:
        if text is None:
            return ""
        return str(text)

    @classmethod
    def _print_line(cls, text):
        print(cls._normalize_log_text(text))

    @classmethod
    def info(cls, input):
        if type(input) == list:
            for something in input:
                cls._print_line(something)
        else:
            cls._print_line(input)

    @classmethod
    def error(cls, input: str):
        raise Fatal(input)

    @classmethod
    def warning(cls, input: str):
        """输出警告日志（黄色文本）"""
        cls._print_line("\033[33m" + "Warning: " + cls._normalize_log_text(input) + "\033[0m")
        cls.newline()

    @classmethod
    def debug(cls, input: str):
        """输出调试日志（青色文本）"""
        cls._print_line("\033[36m" + "Debug: " + cls._normalize_log_text(input) + "\033[0m")

    @classmethod
    def exception(cls, exc: BaseException | None = None):
        """输出异常堆栈，并纳入现有日志收集链。"""
        if exc is None:
            formatted = traceback.format_exc()
        else:
            formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if not formatted or formatted == "NoneType: None\n":
            return
        cls._write_stderr_text(formatted)

    @classmethod
    def newline(cls):
        """输出分隔线（绿色）"""
        cls._print_line("\033[32m" + "-" * 110 + "\033[0m")

    @classmethod
    def save_to_text_editor(cls, text_name: str = "导出流程日志"):
        import bpy

        log_content = cls.get_log_content(strip_ansi=True)
        if not log_content:
            return

        if text_name in bpy.data.texts:
            text_block = bpy.data.texts[text_name]
            text_block.clear()
        else:
            text_block = bpy.data.texts.new(text_name)

        text_block.write(log_content)

        return text_name

    @classmethod
    def _strip_ansi_codes(cls, text: str) -> str:
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)


LOG.install_print_hook()
