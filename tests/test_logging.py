# -*- coding: utf-8 -*-
"""日志模块测试：级别、落盘位置、中文、异常兜底与 Qt 回调异常的可见性。

运行： python -m unittest discover -s tests
"""

import io
import logging
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # Qt 回调可见性用例需离屏

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import APP_NAME  # noqa: E402
from utils import log_setup  # noqa: E402


class TestLogSetup(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="benecan_log_")
        self._previous_hook = sys.excepthook
        self._previous_flag = log_setup._configured
        self._previous_env = os.environ.pop(log_setup.LOG_LEVEL_ENV, None)
        self._root = logging.getLogger()
        self._previous_handlers = list(self._root.handlers)
        self._previous_level = self._root.level
        self._root.handlers = []
        log_setup._configured = False

    def tearDown(self):
        for handler in list(self._root.handlers):
            handler.close()
        self._root.handlers = self._previous_handlers
        self._root.setLevel(self._previous_level)
        log_setup._configured = self._previous_flag
        sys.excepthook = self._previous_hook
        if self._previous_env is not None:
            os.environ[log_setup.LOG_LEVEL_ENV] = self._previous_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_log(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    # ---------------------------------------------------------------- 级别
    def test_default_level_is_debug(self):
        self.assertEqual(log_setup.DEFAULT_LEVEL, logging.DEBUG)
        self.assertEqual(log_setup.resolve_level(), logging.DEBUG)

    def test_env_var_overrides_level(self):
        os.environ[log_setup.LOG_LEVEL_ENV] = "INFO"
        self.assertEqual(log_setup.resolve_level(), logging.INFO)
        os.environ[log_setup.LOG_LEVEL_ENV] = "胡说八道"          # 非法值 → 回退
        self.assertEqual(log_setup.resolve_level(), logging.DEBUG)

    # ---------------------------------------------------------------- 落盘
    def test_writes_debug_records_to_file(self):
        log_dir = os.path.join(self.tmpdir, "logs")
        path = log_setup.setup_logging(log_dir=log_dir, console=False)
        self.assertTrue(path.startswith(log_dir), path)
        self.assertTrue(os.path.basename(path).startswith(APP_NAME + "_"))
        self.assertTrue(path.endswith(".log"))

        logger = logging.getLogger("benecan.test")
        logger.debug("调试信息：中文与数字 %d", 42)
        logger.warning("告警信息")
        for handler in logging.getLogger().handlers:
            handler.flush()

        content = self._read_log(path)
        self.assertIn("DEBUG", content)
        self.assertIn("调试信息：中文与数字 42", content)          # UTF-8 未乱码
        self.assertIn("告警信息", content)
        self.assertIn("benecan.test", content)
        self.assertEqual(log_setup.log_path(), path)

    def test_setup_is_idempotent(self):
        first = log_setup.setup_logging(log_dir=os.path.join(self.tmpdir, "a"),
                                        console=False)
        second = log_setup.setup_logging(log_dir=os.path.join(self.tmpdir, "b"),
                                         console=False)
        self.assertEqual(first, second)                          # 只配置一次

    def test_third_party_loggers_are_quiet(self):
        log_setup.setup_logging(log_dir=os.path.join(self.tmpdir, "logs"), console=False)
        for name in log_setup.NOISY_LOGGERS:
            self.assertEqual(logging.getLogger(name).level, logging.WARNING, name)

    def test_falls_back_when_dir_not_writable(self):
        """日志目录不可写时退到临时目录，不能影响主流程。"""
        blocked = os.path.join(self.tmpdir, "blocked")
        with open(blocked, "w", encoding="utf-8") as handle:      # 同名文件占位
            handle.write("x")
        path = log_setup.setup_logging(log_dir=blocked, console=False)
        self.assertTrue(os.path.exists(path))
        self.assertNotIn(blocked + os.sep, path)
        for handler in logging.getLogger().handlers:
            handler.flush()
        self.assertIn("启动", self._read_log(path))

    # ---------------------------------------------------------------- 兜底
    def test_unhandled_exception_is_logged(self):
        path = log_setup.setup_logging(log_dir=os.path.join(self.tmpdir, "logs"),
                                       console=False)
        saved_stderr, sys.stderr = sys.stderr, io.StringIO()   # 默认钩子会往 stderr 打印，测试里静音
        try:
            try:
                raise RuntimeError("测试用异常")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        finally:
            sys.stderr = saved_stderr
        for handler in logging.getLogger().handlers:
            handler.flush()
        content = self._read_log(path)
        self.assertIn("未捕获异常", content)
        self.assertIn("测试用异常", content)

    # ------------------------------------------------- Qt 回调异常的可见性
    def test_qt_slot_exception_is_logged_as_unhandled(self):
        """PySide6 6.x 会把 Python 回调里抛的异常交给 sys.excepthook，必须落进日志。

        这是"界面点了没反应"的**唯一自动线索**：一旦这条链路断了（或被误判成"槽异常
        会被静默吞掉"），排查就只能靠源码运行的终端输出，打包版则完全无从下手。
        """
        try:
            from PySide6.QtCore import QObject, QTimer, Signal
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("未安装 PySide6")

        path = log_setup.setup_logging(log_dir=os.path.join(self.tmpdir, "logs"),
                                       console=False)
        app = QApplication.instance() or QApplication([])

        class Emitter(QObject):
            ping = Signal()

        class Receiver(QObject):
            def on_ping(self):
                raise TypeError("槽里抛出的异常")

        emitter, receiver = Emitter(), Receiver()
        emitter.ping.connect(receiver.on_ping)
        QTimer.singleShot(0, emitter.ping)
        QTimer.singleShot(50, app.quit)
        saved_stderr, sys.stderr = sys.stderr, io.StringIO()   # 默认钩子会打印回溯，测试里静音
        try:
            app.exec()
        finally:
            sys.stderr = saved_stderr
        for handler in logging.getLogger().handlers:
            handler.flush()

        content = self._read_log(path)
        self.assertIn("benecan.unhandled", content)
        self.assertIn("未捕获异常", content)
        self.assertIn("槽里抛出的异常", content)          # 回溯里的异常文本也在

    # ---------------------------------------------------------------- 小工具
    def test_format_helpers(self):
        self.assertEqual(log_setup.fmt_seconds(0.25), "250 ms")
        self.assertEqual(log_setup.fmt_seconds(2.5), "2.50 s")
        self.assertEqual(log_setup.fmt_bytes(512), "512.0 B")
        self.assertEqual(log_setup.fmt_bytes(1536), "1.5 KB")
        self.assertEqual(log_setup.fmt_bytes(3 * 1048576), "3.0 MB")


if __name__ == "__main__":
    unittest.main()
