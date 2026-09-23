# -*- coding: utf-8 -*-
"""日志：文件 + 控制台，测试调试阶段默认全量 DEBUG。

用法（只在程序入口调用一次，之后各模块直接用标准 logging）：
    from utils.log_setup import setup_logging
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.debug("...")

设计要点
--------
1. **默认 DEBUG**：V0.1 处于测试调试阶段，默认把所有 DEBUG 记录落盘；
   交付前把 `DEFAULT_LEVEL` 改成 INFO 即可，也可用环境变量 `BENECAN_LOG_LEVEL` 临时覆盖
   （如 `set BENECAN_LOG_LEVEL=INFO`）。
2. **日志位置**：程序同级 `logs/BeneCAN_<日期>_<时间>.log`
   （源码运行 = 项目根；打包后 = exe 同级），单文件 5 MB 滚动、保留 5 份。
   目录不可写时自动退到临时目录 —— 日志出问题绝不能影响主流程。
3. **中文用 UTF-8**：Windows 默认 GBK，中文日志会乱码甚至抛异常，必须显式指定编码。
4. **没有控制台也不报错**：打包版是窗口程序（console=False），`sys.stderr` 可能是 None。
5. **兜底**：未捕获异常与 Qt 的 qWarning/qFatal 都写进同一份日志，便于事后定位崩溃。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import tempfile
import time

from config import APP_NAME, LOG_DIR_NAME, LOG_LEVEL_ENV, TOOL_VERSION

DEFAULT_LEVEL = logging.DEBUG
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
FILE_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-5s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 第三方库在 DEBUG 下话很多（逐 row group、逐文件句柄），与排查业务问题无关，
# 统一压到 WARNING，日志才看得清。
NOISY_LOGGERS = ("pyarrow", "cantools", "PIL", "urllib3", "matplotlib")

_configured = False
_log_path = ""


def app_dir() -> str:
    """程序目录：源码运行时是项目根；打包成 exe 后是 exe 所在目录（与 main.app_dir 一致）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_log_dir() -> str:
    return os.path.join(app_dir(), LOG_DIR_NAME)


def resolve_level(name: str = None) -> int:
    """日志级别：参数 > 环境变量 BENECAN_LOG_LEVEL > DEFAULT_LEVEL（DEBUG）。"""
    text = (name or os.environ.get(LOG_LEVEL_ENV) or "").strip()
    if not text:
        return DEFAULT_LEVEL
    value = logging.getLevelName(text.upper())
    return value if isinstance(value, int) else DEFAULT_LEVEL


def setup_logging(level=None, log_dir: str = None, console: bool = True) -> str:
    """配置根日志器（幂等），返回日志文件路径。"""
    global _configured, _log_path
    if _configured:
        return _log_path

    directory = _writable_dir(log_dir or default_log_dir())
    path = os.path.join(directory, "%s_%s.log" % (
        APP_NAME, time.strftime("%Y%m%d_%H%M%S")))
    formatter = logging.Formatter(FILE_FORMAT, DATE_FORMAT)
    level_value = resolve_level(level)

    handlers = []
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as exc:                       # 磁盘满/权限问题：退到控制台
        print("日志文件不可写：%s" % exc, file=sys.stderr or sys.stdout)

    if console and sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    root = logging.getLogger()
    root.setLevel(level_value)
    for handler in handlers:
        handler.setLevel(level_value)
        root.addHandler(handler)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
    _log_path = path if os.path.exists(path) else ""

    logger = logging.getLogger(__name__)
    logger.info("%s %s 启动 | 日志级别 %s | 日志文件 %s",
                APP_NAME, TOOL_VERSION, logging.getLevelName(level_value),
                _log_path or "（不可写）")
    logger.debug("运行环境：Python %s | %s | 打包运行=%s | CWD=%s",
                 platform.python_version(), platform.platform(),
                 bool(getattr(sys, "frozen", False)), os.getcwd())
    logger.debug("程序目录=%s | 日志目录=%s", app_dir(), directory)
    install_excepthook()
    install_qt_message_handler()
    return _log_path


def log_path() -> str:
    return _log_path


def _writable_dir(preferred: str) -> str:
    """确保目录存在且可写；不可写则退到临时目录。"""
    for candidate in (preferred,
                      os.path.join(tempfile.gettempdir(), APP_NAME, LOG_DIR_NAME)):
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write_test")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            return candidate
        except OSError:
            continue
    return tempfile.gettempdir()


def install_excepthook() -> None:
    """未捕获异常写进日志（否则窗口程序崩溃时什么都不剩）。"""
    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_traceback):
        logging.getLogger("benecan.unhandled").critical(
            "未捕获异常", exc_info=(exc_type, exc_value, exc_traceback))
        if hasattr(sys, "stderr") and sys.stderr is not None:
            # 打包版是窗口程序，stderr 可能为 None，此时别再抛第二个异常
            previous(exc_type, exc_value, exc_traceback)

    sys.excepthook = hook


def install_qt_message_handler() -> None:
    """把 Qt 自己的 qDebug/qWarning/qFatal 接进同一份日志。"""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:                          # 无 Qt（CLI 工具）时不接管
        return

    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message):
        logger = logging.getLogger("qt")
        logger.log(levels.get(mode, logging.DEBUG), "%s", message)

    qInstallMessageHandler(handler)


def fmt_seconds(seconds: float) -> str:
    """耗时统一格式（日志里到处要用）。"""
    if seconds < 1:
        return "%.0f ms" % (seconds * 1000.0)
    return "%.2f s" % seconds


def fmt_bytes(size) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f GB" % value
