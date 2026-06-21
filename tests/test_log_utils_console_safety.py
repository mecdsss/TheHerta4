# -*- coding: utf-8 -*-
import importlib.util
import contextlib
import io
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_log_utils_console_safety_test_pkg"
for package_name in (PKG, f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _Fatal(Exception):
    pass


_install_module(f"{PKG}.utils.format_utils", Fatal=_Fatal)


module_path = Path(__file__).resolve().parents[1] / "utils" / "log_utils.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.utils.log_utils", module_path)
log_utils = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = log_utils
spec.loader.exec_module(log_utils)


class _FakeStream:
    def __init__(self, encoding="gbk"):
        self.encoding = encoding
        self.reconfigure_calls = []
        self.buffer = io.StringIO()

    def reconfigure(self, **kwargs):
        self.reconfigure_calls.append(kwargs)
        if "encoding" in kwargs:
            self.encoding = kwargs["encoding"]

    def write(self, text):
        self.buffer.write(text)

    def flush(self):
        pass


class LogUtilsConsoleSafetyTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(log_utils.LOG.uninstall_print_hook)
        log_utils.LOG.install_print_hook()

    def test_reconfigure_stdio_forces_utf8_for_stdout_and_stderr(self):
        stdout = _FakeStream(encoding="gbk")
        stderr = _FakeStream(encoding="cp936")
        original_stdout = log_utils.sys.stdout
        original_stderr = log_utils.sys.stderr
        original_force = log_utils._force_windows_console_utf8
        try:
            log_utils.sys.stdout = stdout
            log_utils.sys.stderr = stderr
            log_utils._force_windows_console_utf8 = lambda: None

            log_utils._reconfigure_stdio_utf8()
        finally:
            log_utils.sys.stdout = original_stdout
            log_utils.sys.stderr = original_stderr
            log_utils._force_windows_console_utf8 = original_force

        self.assertEqual(stdout.reconfigure_calls[-1]["encoding"], "utf-8")
        self.assertEqual(stderr.reconfigure_calls[-1]["encoding"], "utf-8")

    def test_normalize_log_text_preserves_original_unicode(self):
        self.assertEqual(log_utils.LOG._normalize_log_text("中文→测试🚀"), "中文→测试🚀")

    def test_print_hook_keeps_raw_utf8_text_for_console_and_log_capture(self):
        console = _FakeStream(encoding="utf-8")
        original_stdout = log_utils.sys.stdout
        original_collecting = log_utils.LOG._is_collecting
        original_capture = log_utils.LOG._log_capture
        original_stream = log_utils.LOG._original_stdout
        try:
            log_utils.sys.stdout = console
            log_utils.LOG._original_stdout = console
            log_utils.LOG._log_capture = io.StringIO()
            log_utils.LOG._is_collecting = True
            print("中文→测试🚀", end="")
            captured = log_utils.LOG.get_log_content()
        finally:
            log_utils.sys.stdout = original_stdout
            log_utils.LOG._is_collecting = original_collecting
            log_utils.LOG._log_capture = original_capture
            log_utils.LOG._original_stdout = original_stream

        self.assertEqual(console.buffer.getvalue(), "中文→测试🚀")
        self.assertEqual(captured, "中文→测试🚀")

    def test_print_hook_respects_redirect_stdout_while_collecting(self):
        redirected = io.StringIO()
        original_stdout = log_utils.sys.stdout
        original_stderr = log_utils.sys.stderr
        try:
            log_utils.LOG.start_collecting()
            with contextlib.redirect_stdout(redirected):
                print("中文→重定向")
        finally:
            log_utils.LOG.stop_collecting()
            log_utils.sys.stdout = original_stdout
            log_utils.sys.stderr = original_stderr

        self.assertEqual(redirected.getvalue(), "中文→重定向\n")
        self.assertEqual(log_utils.LOG.get_log_content(), "中文→重定向\n")

    def test_exception_writes_traceback_to_stderr_and_log_capture(self):
        redirected = io.StringIO()
        original_stdout = log_utils.sys.stdout
        original_stderr = log_utils.sys.stderr
        try:
            log_utils.LOG.start_collecting()
            with contextlib.redirect_stderr(redirected):
                try:
                    raise ValueError("中文异常")
                except ValueError as exc:
                    log_utils.LOG.exception(exc)
        finally:
            log_utils.LOG.stop_collecting()
            log_utils.sys.stdout = original_stdout
            log_utils.sys.stderr = original_stderr

        redirected_value = redirected.getvalue()
        captured = log_utils.LOG.get_log_content()
        self.assertIn("ValueError: 中文异常", redirected_value)
        self.assertIn("ValueError: 中文异常", captured)
        self.assertIn("Traceback", redirected_value)
        self.assertIn("Traceback", captured)

    def test_uninstall_print_hook_restores_original_print(self):
        hooked_print = log_utils.builtins.print

        log_utils.LOG.uninstall_print_hook()

        self.assertIsNot(log_utils.builtins.print, hooked_print)
        self.assertIs(log_utils.builtins.print, log_utils._ORIGINAL_PRINT)

    def test_get_log_content_can_strip_ansi_without_losing_unicode(self):
        original_capture = log_utils.LOG._log_capture
        log_utils.LOG._log_capture = io.StringIO()
        try:
            log_utils.LOG._log_capture.write("\033[33mWarning: 中文→测试\033[0m")
            self.assertEqual(log_utils.LOG.get_log_content(), "\033[33mWarning: 中文→测试\033[0m")
            self.assertEqual(log_utils.LOG.get_log_content(strip_ansi=True), "Warning: 中文→测试")
        finally:
            log_utils.LOG._log_capture = original_capture


if __name__ == "__main__":
    unittest.main()
