# -*- coding: utf-8 -*-
"""CANoe Graphic 风格图形窗口：多行堆叠、共享 X 轴、缩放平移。

设计要点
--------
1. **一行 = 一个信号**（SignalSpec）：纵轴独立、横轴共享（pyqtgraph XLink），
   缩放/平移任一行，其余行同步 —— 与 CANoe Graphic 一致。
2. **按需取数 + 降采样**：每行只取自己那一个报文的序列（13~26 ms），
   pyqtgraph 用 clipToView + peak 降采样，缩小时不会渲染全部百万点。
3. **增删行只重建图元**：已取到内存的序列被缓存复用，因此反复增删行几乎零成本。
4. **X 轴三种模式**：原样时间 / 空档压缩 / 全局帧序号（seq），
   帧序号模式对形态 B（源文件时间戳乱序）是最稳的轴。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QLabel, QMenu, QVBoxLayout, QWidget

from core.query import MODE_INDEX, MODE_SESSION
from signals import model as sigmodel
from signals.stats import format_number, nearest_sample, stats_of

logger = logging.getLogger(__name__)

AXIS_WIDTH = 170
MIN_ROW_HEIGHT = 54
CURVE_WIDTH = 1.2
CURSOR_COLORS = ("#d62728", "#1f77b4")
CURSOR_NAMES = ("T1", "T2")
CURSOR_BAR_HEIGHT = 22
CURSOR_HANDLE_W = 9
CURSOR_HANDLE_H = 9
CURSOR_SNAP_PX = 26.0            # 按下点离手柄超过该像素数时，手柄先吸附到点击处
EXTREME_COLOR = "#9aa0a6"        # 极值标记线（淡灰，不抢曲线）
READOUT_BG = "#ffffffd8"
SYMBOL = "o"                     # 采样点形状
SYMBOL_SIZE = 5                  # 采样点直径（px）
SYMBOL_MAX_POINTS = 3000         # 单行**可见**点数超过它就不画采样点（否则糊成色块且拖慢渲染）
AXIS_LABEL_MAX_CHARS = 18        # Y 轴标题（竖排）超过该长度就退化成单位/不显示，避免被裁
MAX_TICK_LABELS = 14             # 枚举信号最多在 Y 轴上标几个文本刻度（太多会挤成一团）

MODE_LABELS = {
    "time": "时间 (s)",
    "session": "时间 (空档压缩, s)",
    "index": "全局帧序号",
}


class CursorHandleBar(QWidget):
    """光标手柄条：位于图形区上方，整条都可点击拖动，解决"必须精确点中线"的问题。

    - 条上的手柄与实际光标线严格对齐（用绘图区场景矩形换算左右边界）
    - 按在任意位置：离最近手柄近则直接拖它；离得远则把最近手柄吸附到点击处再拖
    - 两光标之间显示 ΔT
    """

    dragged = Signal(int, float)          # (光标序号, 视图 X 值)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(CURSOR_BAR_HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._enabled = False
        self._x_lo = 0.0
        self._x_hi = 1.0
        self._left_ratio = 0.0            # 绘图区左右边界（相对本控件宽度 0..1）
        self._right_ratio = 1.0
        self._values = (float("nan"), float("nan"))
        self._drag_index = -1

    # -------------------------------------------------- 状态输入
    def set_enabled_state(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.update()

    def set_x_range(self, x_lo: float, x_hi: float) -> None:
        if x_hi > x_lo:
            self._x_lo, self._x_hi = float(x_lo), float(x_hi)
            self.update()

    def set_plot_ratio(self, left_ratio: float, right_ratio: float) -> None:
        """绘图区左右边界占本控件宽度的比例（避免依赖尚未布局完成时的像素几何）。"""
        self._left_ratio = float(left_ratio)
        self._right_ratio = float(right_ratio)
        self.update()

    def set_cursor_values(self, x1: float, x2: float) -> None:
        self._values = (float(x1), float(x2))
        self.update()

    # -------------------------------------------------- 坐标换算（纯数学，可单测）
    @property
    def px_left(self) -> float:
        return self._left_ratio * self.width()

    @property
    def px_right(self) -> float:
        return self._right_ratio * self.width()

    @property
    def span_ready(self) -> bool:
        return (self._right_ratio > self._left_ratio and self.width() > 0
                and self._x_hi > self._x_lo)

    def x_to_px(self, x: float) -> float:
        ratio = (x - self._x_lo) / (self._x_hi - self._x_lo)
        return self.px_left + ratio * (self.px_right - self.px_left)

    def px_to_x(self, px: float) -> float:
        ratio = (px - self.px_left) / (self.px_right - self.px_left)
        return self._x_lo + ratio * (self._x_hi - self._x_lo)

    def _nearest_index(self, x: float) -> int:
        v1, v2 = self._values
        if np.isnan(v1):
            return 0
        if np.isnan(v2):
            return 1
        return 0 if abs(x - v1) <= abs(x - v2) else 1

    def press_target(self, px: float) -> tuple:
        """按下位置 -> (要移动的光标序号, 需要吸附到的 x 值或 None)。

        离最近手柄超过 CURSOR_SNAP_PX 像素时返回吸附目标（把手柄直接拉到点击处），
        否则返回 None（视为直接抓住该手柄拖动）。
        """
        x = self.px_to_x(px)
        index = self._nearest_index(x)
        handle_px = self.x_to_px(self._values[index]) if not np.isnan(
            self._values[index]) else None
        if handle_px is None or abs(handle_px - px) > CURSOR_SNAP_PX:
            return index, x
        return index, None

    # -------------------------------------------------- 鼠标交互
    def mousePressEvent(self, event) -> None:
        if not self._enabled or not self.span_ready:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index, snap_to = self.press_target(float(event.position().x()))
        if snap_to is not None:
            self.dragged.emit(index, snap_to)     # 离手柄较远：先吸附到点击处
        self._drag_index = index
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_index < 0 or not self.span_ready:
            return
        self.dragged.emit(self._drag_index, self.px_to_x(float(event.position().x())))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_index = -1
        event.accept()

    # -------------------------------------------------- 绘制
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f4f4f4"))
        painter.setPen(QPen(QColor("#c8c8c8"), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        if not self._enabled or not self.span_ready:
            painter.setPen(QPen(QColor("#999999"), 1))
            painter.drawText(self.rect().adjusted(int(self.px_left) if self.px_left else 8,
                                                  0, -8, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             "双光标未启用（工具栏或右键开启）" if not self._enabled
                             else "光标手柄")
            return

        bottom = self.height() - 1
        positions = []
        for index, value in enumerate(self._values):
            if np.isnan(value):
                continue
            px = self.x_to_px(value)
            positions.append(px)
            color = QColor(CURSOR_COLORS[index])
            # 手柄：倒三角 + 竖线，命中区域由整个控件承担，故此处只需画得清楚
            triangle = QPolygonF([
                QPointF(px - CURSOR_HANDLE_W / 2.0, 0.0),
                QPointF(px + CURSOR_HANDLE_W / 2.0, 0.0),
                QPointF(px, CURSOR_HANDLE_H),
            ])
            painter.setBrush(color)
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.drawPolygon(triangle)
            painter.setPen(QPen(color, 2))
            painter.drawLine(QPointF(px, CURSOR_HANDLE_H - 1), QPointF(px, bottom))

        if len(positions) == 2:
            left, right = sorted(positions)
            # 两光标之间高亮
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e8eef7"))
            painter.drawRect(int(left), 0, max(1, int(right - left)),
                             self.height() - 2)
            for index, px in enumerate(positions):       # 高亮后补画手柄
                color = QColor(CURSOR_COLORS[index])
                painter.setBrush(color)
                painter.setPen(QPen(QColor("#ffffff"), 1))
                painter.drawPolygon(QPolygonF([
                    QPointF(px - CURSOR_HANDLE_W / 2.0, 0.0),
                    QPointF(px + CURSOR_HANDLE_W / 2.0, 0.0),
                    QPointF(px, CURSOR_HANDLE_H),
                ]))
            delta = abs(self._values[1] - self._values[0])
            painter.setPen(QPen(QColor("#33475b"), 1))
            painter.drawText(int((left + right) / 2) - 60, 2, 120, CURSOR_HANDLE_H + 4,
                             Qt.AlignmentFlag.AlignCenter, "ΔT = %.6f s" % delta)
        painter.end()


@dataclass
class GraphicRow:
    """一行 = 一个信号（含已取到的数据，便于增删行时复用）。"""
    spec: object
    series: object
    info: object
    color: str
    plot: object = None
    curve: object = None
    caption: object = None
    readout: object = None               # 右上角"鼠标读值"文本
    extreme_lines: list = field(default_factory=list)   # 极值标记线（最多 2 条）
    stats: dict = None                   # signals/stats.stats_of 的结果
    marker_on: bool = False              # 当前是否画了采样点
    visible_points: int = 0              # 当前视窗内可见点数（决定要不要画采样点）
    values: np.ndarray = field(default=None)
    x: np.ndarray = field(default=None)

    @property
    def name(self) -> str:
        return self.spec.display_name()

    @property
    def label_text(self) -> str:
        """行内横排说明：信号名（单位放在 Y 轴标题上，不重复）。"""
        return self.spec.name or self.spec.default_name()

    @property
    def unit_text(self) -> str:
        """DBC 里规定的单位（没有单位时为空）。"""
        return "[%s]" % self.spec.unit if self.spec.unit else ""

    @property
    def axis_text(self) -> str:
        """Y 轴标题：优先 DBC 单位（短、信息量大），没有单位时退回短信号名。

        轴标题是竖排的，能显示的字符数受**行高**限制（几十像素 ≈ 7~12 个字符），
        所以长名字要截断 —— 完整名字仍横排显示在行内左上角。
        """
        if self.spec.unit:
            return "[%s]" % self.spec.unit
        name = self.spec.name or self.spec.default_name()
        short = name.split(".")[-1]              # DBC 名字形如「报文.信号」→ 取信号段
        if len(short) > AXIS_LABEL_MAX_CHARS:
            return short[:AXIS_LABEL_MAX_CHARS - 1] + "…"
        return short

    @property
    def min_text(self) -> str:
        return format_number((self.stats or {}).get("min"))

    @property
    def max_text(self) -> str:
        return format_number((self.stats or {}).get("max"))

    @property
    def mean_text(self) -> str:
        return format_number((self.stats or {}).get("mean"))

    def value_text_at(self, x) -> tuple:
        """取"该时刻的值"（保持前一值语义）；返回 (数值或 None, 显示文本)。"""
        if self.x is None or len(self.x) == 0 or x is None or np.isnan(x):
            return None, "—"
        index = int(np.searchsorted(self.x, x, side="right")) - 1
        if index < 0:
            return None, "—"
        value = float(self.values[index])
        if self.spec.labels:
            return value, self.spec.labels.get(int(value), str(int(value)))
        if value.is_integer():
            return value, "%d" % int(value)
        return value, "%.4f" % value


class GraphicWindow(QWidget):
    """多行堆叠图形窗口。"""

    rowsChanged = Signal()
    statusMessage = Signal(str)
    cursorChanged = Signal(float, float)      # T1 / T2 的 x 位置（未启用时为 nan）

    def __init__(self, parent=None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=False, background="#ffffff", foreground="#333333")

        self._reader = None
        self._time_mode = "time"
        self._origin = 0.0
        self._overlay = False                # True = 单坐标轴叠加（全部行共用 Y 轴）
        self._rows = []
        self._color_index = 0
        self._cursors_enabled = False
        self._cursor_values = [float("nan"), float("nan")]
        self._cursor_lines = []              # [(row_index, cursor_index, InfiniteLine)]
        self._syncing = False
        self._range_sources = []             # 各坐标系 ViewBox（监听缩放/平移）
        self._readout_enabled = True         # 鼠标跟随读值（CANoe 风格）
        self._readout_x = None               # 当前读值位置（None = 未指向绘图区）
        self._snap = True                    # 读值/光标吸附到真实采样点
        self._show_extremes = True           # 行内极值标记线（全局默认，可被单行覆盖）
        self._show_symbols = True            # 采样点标记（放大到一定密度自动出现）
        self._marker_rows_shown = None       # 上次"画了采样点"的行数（仅用于日志）

        self.header = QLabel(self._header_text())
        self.header.setStyleSheet("color:#555; padding:4px 6px;")
        self.cursor_bar = CursorHandleBar(self)
        self.cursor_bar.setVisible(False)
        self.cursor_bar.dragged.connect(self._on_bar_dragged)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground("#ffffff")
        self.glw.ci.setContentsMargins(0, 0, 0, 0)
        self.glw.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.glw.customContextMenuRequested.connect(self._on_context_menu)
        # 鼠标跟随读值：scene 是持久的（glw.clear() 只清图元），代理只连一次
        self._mouse_proxy = pg.SignalProxy(self.glw.scene().sigMouseMoved,
                                           rateLimit=60,
                                           slot=self._on_scene_mouse_moved)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.header)
        layout.addWidget(self.cursor_bar)
        layout.addWidget(self.glw, 1)

    # ---------------------------------------------------------------- 数据源
    def set_reader(self, reader):
        self._reader = reader
        self._rows.clear()
        self._origin = float(reader.metadata.get("ts_first", 0.0)) if reader else 0.0
        self._color_index = 0
        self._rebuild()
        if reader is not None:
            logger.debug("图形区绑定数据源：%s（%s 帧，时间起点 %.6f）",
                         reader.source_path, reader.frames, self._origin)

    @property
    def reader(self):
        return self._reader

    @property
    def time_mode(self) -> str:
        return self._time_mode

    def set_time_mode(self, mode: str):
        if mode == self._time_mode:
            return
        logger.debug("图形区切换时间轴模式：%s → %s", self._time_mode, mode)
        self._time_mode = mode
        self._reload_rows()

    @property
    def overlay(self) -> bool:
        """True = 单坐标轴叠加模式（所有信号共用一个 Y 轴）。"""
        return self._overlay

    def set_overlay(self, enabled: bool) -> None:
        """切换 多轴堆叠 / 单坐标轴叠加。"""
        enabled = bool(enabled)
        if enabled == self._overlay:
            return
        self._overlay = enabled
        logger.debug("坐标轴布局切换为：%s", "单坐标轴叠加" if enabled else "多轴堆叠")
        self._rebuild()

    # ------------------------------------------------- 鼠标读值 / 吸附 / 极值标记
    @property
    def cursor_snap(self) -> bool:
        """光标与读值是否吸附到真实采样点。"""
        return self._snap

    def set_cursor_snap(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._snap:
            return
        self._snap = enabled
        logger.debug("采样点吸附：%s", "开启" if enabled else "关闭")
        self.set_readout_x(self._readout_x)          # 立刻按新策略重算读值
        self._sync_cursor_lines()

    @property
    def show_extremes(self) -> bool:
        return self._show_extremes

    def set_show_extremes(self, enabled: bool) -> None:
        """全局显示/隐藏极值标记线（只影响"跟随全局"的行，单行覆盖不受影响）。"""
        enabled = bool(enabled)
        if enabled == self._show_extremes:
            return
        self._show_extremes = enabled
        logger.debug("极值标记（全局）：%s", "显示" if enabled else "隐藏")
        for row in self._rows:
            self._update_extremes(row)
        self.header.setText(self._header_text())

    # ---------------------------------------------- 行级极值开关（覆盖全局）
    def row_extremes(self, index: int) -> bool:
        """该行**当前有效**的极值显示状态。"""
        if not 0 <= index < len(self._rows):
            return False
        spec = self._rows[index].spec
        return self._show_extremes if spec.extreme_marks is None else bool(spec.extreme_marks)

    def set_row_extremes(self, index: int, enabled) -> None:
        """设置单行极值开关：True/False = 本行强制；None = 清除设置、恢复跟随全局。"""
        if not 0 <= index < len(self._rows):
            return
        row = self._rows[index]
        row.spec.extreme_marks = None if enabled is None else bool(enabled)
        logger.debug("行级极值标记：%s → %s", row.name,
                     "跟随全局" if enabled is None else ("显示" if enabled else "隐藏"))
        self._update_extremes(row)
        self.header.setText(self._header_text())
        self.rowsChanged.emit()

    @property
    def show_symbols(self) -> bool:
        """采样点标记总开关（默认开）。"""
        return self._show_symbols

    def set_show_symbols(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._show_symbols:
            return
        self._show_symbols = enabled
        logger.debug("采样点标记：%s", "显示" if enabled else "隐藏")
        self._update_point_markers(force=True)

    def _apply_markers(self, row, show: bool) -> None:
        """给某行开/关采样点标记（画在**真实采样点**上，不开降采样以免标记错位）。"""
        if row.curve is None:
            return
        if show:
            row.curve.setSymbol(SYMBOL)
            row.curve.setSymbolSize(SYMBOL_SIZE)
            row.curve.setSymbolBrush(pg.mkBrush(row.color))
            row.curve.setSymbolPen(None)
            row.curve.setDownsampling(auto=False, ds=1)
        else:
            row.curve.setSymbol(None)
            row.curve.setDownsampling(auto=True, method="peak")
        row.marker_on = show

    def _update_point_markers(self, force: bool = False) -> None:
        """按"视窗内可见点数"决定是否画采样点：放大才出现，缩小时不糊成一片。

        逐行独立判断：稀疏行（如 1 Hz 的报文）在很宽的视图里也能看到点。
        """
        if not self._rows:
            return
        x_range = self.x_range()
        if x_range is None:
            return
        lo, hi = x_range
        for row in self._rows:
            if row.curve is None:
                continue
            if row.x is None or len(row.x) == 0:
                row.visible_points = 0
            else:
                first = int(np.searchsorted(row.x, lo, side="left"))
                last = int(np.searchsorted(row.x, hi, side="right"))
                row.visible_points = max(0, last - first)
            show = self._show_symbols and 0 < row.visible_points <= SYMBOL_MAX_POINTS
            if force or row.marker_on != show:
                self._apply_markers(row, show)
        shown = sum(1 for row in self._rows if row.marker_on)
        if shown != self._marker_rows_shown:
            self._marker_rows_shown = shown
            logger.debug("采样点：%d/%d 行显示（阈值 %d 点/行）",
                         shown, len(self._rows), SYMBOL_MAX_POINTS)

    @property
    def readout_enabled(self) -> bool:
        return self._readout_enabled

    def set_readout_enabled(self, enabled: bool) -> None:
        self._readout_enabled = bool(enabled)
        if not self._readout_enabled:
            self.set_readout_x(None)

    @property
    def readout_x(self):
        """当前鼠标读值位置（未指向绘图区时为 None）。"""
        return self._readout_x

    def snap_x(self, x, row=None) -> float:
        """把 x 吸附到采样点（吸附关闭或无数据时原样返回）。"""
        if not self._snap or x is None:
            return x
        base = row if (row is not None and row.x is not None and len(row.x)) else (
            self._rows[0] if self._rows else None)
        if base is None or base.x is None or len(base.x) == 0:
            return x
        _index, sample = nearest_sample(base.x, x)
        return float(x) if sample is None else sample

    def set_readout_x(self, x) -> None:
        """设定鼠标读值位置（None = 清除）。吸附策略在这里生效。"""
        if (not self._readout_enabled or x is None
                or (isinstance(x, float) and np.isnan(x))):
            self._readout_x = None
        else:
            self._readout_x = float(self.snap_x(x))
        self._update_readouts()
        self.header.setText(self._header_text())

    def readouts(self) -> list:
        """每行当前的读值文本（供值表/测试使用）。"""
        return [self._row_readout_text(row) for row in self._rows]

    def _row_readout_text(self, row) -> str:
        if self._readout_x is None or row.x is None or len(row.x) == 0:
            return ""
        _value, text = row.value_text_at(self._readout_x)
        if text == "—":
            return "—"
        return "%s %s" % (text, row.spec.unit) if row.spec.unit else text

    def _update_readouts(self) -> None:
        """刷新读值文本：堆叠模式每行一处；叠加模式合并成多行放在共用坐标系上。"""
        if self._overlay:
            lines = []
            for row in self._rows:
                text = self._row_readout_text(row)
                if text:
                    lines.append("%s = %s" % (row.label_text, text))
            merged = "\n".join(lines)
            for row in self._rows:
                if row.readout is not None:          # 叠加模式只有一个共用读值
                    row.readout.setText(merged)
            return
        for row in self._rows:
            if row.readout is not None:
                row.readout.setText(self._row_readout_text(row))

    def _readout_base_row(self, scene_pos):
        """吸附基准行：鼠标所在的那一行（不在任何行内时用第一行）。"""
        for row in self._rows:
            if row.plot is None:
                continue
            if row.plot.getViewBox().sceneBoundingRect().contains(scene_pos):
                return row
        return self._rows[0] if self._rows else None

    def _on_scene_mouse_moved(self, event) -> None:
        """鼠标在图形区移动 → 刷新每行右上角的读值（CANoe 的鼠标跟随读值）。

        注意：pyqtgraph 的 SignalProxy 会把信号参数**打包成 tuple** 再转发
        （实测 `QRectF.contains(tuple)` 会抛 TypeError，异常被 Qt 吞掉 → 表现为"鼠标没反应"）。
        这里统一归一化成 QPointF，别让参数形状变化再次打断功能。
        """
        if not self._readout_enabled or not self._rows:
            return
        scene_pos = event[0] if isinstance(event, (tuple, list)) else event
        if not isinstance(scene_pos, QPointF):
            scene_pos = QPointF(scene_pos)           # 兼容 QPoint 等可转换类型
        plot = next((row.plot for row in self._rows if row.plot is not None), None)
        if plot is None:
            return
        viewbox = plot.getViewBox()
        if not viewbox.sceneBoundingRect().contains(scene_pos):
            self.set_readout_x(None)                 # 移出绘图区 → 收掉读值
            return
        x = viewbox.mapSceneToView(scene_pos).x()
        self.set_readout_x(self.snap_x(x, self._readout_base_row(scene_pos)))

    def leaveEvent(self, event) -> None:
        self.set_readout_x(None)
        super().leaveEvent(event)

    def _row_extremes_enabled(self, row) -> bool:
        """该行是否要画极值线：单行设置优先，否则跟随全局开关。"""
        override = row.spec.extreme_marks
        return self._show_extremes if override is None else bool(override)

    def _update_extremes(self, row) -> None:
        """按当前开关与统计结果重建该行的极值标记线。"""
        for line in row.extreme_lines:
            row.plot.removeItem(line)
        row.extreme_lines = []
        stats = row.stats or {}
        if (not self._row_extremes_enabled(row) or row.plot is None
                or stats.get("min") is None):
            return
        if stats["min"] == stats["max"]:             # 常量行不需要两条重合的线
            return
        for value, name in ((stats["max"], "max"), (stats["min"], "min")):
            line = pg.InfiniteLine(
                pos=float(value), angle=0, movable=False,
                pen=pg.mkPen(EXTREME_COLOR, width=1, style=Qt.PenStyle.DashLine),
                label="%s %s" % (name, format_number(value)),
                labelOpts={"position": 0.02, "color": "#666666",
                           "fill": READOUT_BG, "movable": False})
            line.setZValue(20)
            row.plot.addItem(line, ignoreBounds=True)
            row.extreme_lines.append(line)

    def _refresh_row_stats(self, row) -> None:
        """重算一行统计（加行/换数据源后调用），并同步极值标记。"""
        row.stats = stats_of(row.values)
        self._update_extremes(row)

    # ---------------------------------------------------------------- 增删行
    def add_message(self, info, max_rows: int = 6) -> int:
        """按报文自动挑选信号加入图形，返回新增行数。"""
        if self._reader is None:
            return 0
        profile = self._reader.byte_profile(info.kind, info.channel, info.can_id)
        specs = sigmodel.auto_specs(profile, info.key, max_rows=max_rows)
        for spec in specs:
            spec.name = "%s %s" % (info.id_text, spec.field_text)
        return self.add_specs(specs)

    def add_specs(self, specs) -> int:
        """批量加入信号行（specs 可为单个 SignalSpec 或列表）。"""
        if self._reader is None:
            return 0
        if not isinstance(specs, (list, tuple)):
            specs = [specs]
        added = 0
        for spec in specs:
            if self._index_of(spec) >= 0:
                logger.debug("该行已存在，跳过：%s", spec.display_name())
                continue
            try:
                series = spec.extract(self._reader)
            except (KeyError, ValueError) as exc:
                logger.warning("取数失败 %s：%s", spec.field_text, exc)
                self.statusMessage.emit("取数失败 %s：%s" % (spec.field_text, exc))
                continue
            if len(series) == 0:
                logger.debug("该行没有数据，跳过：%s", spec.display_name())
                continue
            info = series.message
            if not spec.color:
                spec.color = sigmodel.DEFAULT_COLORS[
                    self._color_index % len(sigmodel.DEFAULT_COLORS)]
                self._color_index += 1
            self._rows.append(GraphicRow(spec=spec, series=series, info=info,
                                         color=spec.color))
            logger.debug("加入图形行：%s | %s 点 | 来源 %s",
                         spec.display_name(), len(series), spec.source)
            added += 1
        if added:
            self._rebuild()
            self.statusMessage.emit(
                "已加入 %d 行（共 %d 行，%s）" % (added, len(self._rows),
                                              MODE_LABELS.get(self._time_mode, "")))
        return added

    def remove_row(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            logger.debug("删除图形行：%s", self._rows[index].name)
            self._rows.pop(index)
            self._rebuild()

    def remove_message_rows(self, info) -> int:
        before = len(self._rows)
        self._rows = [row for row in self._rows
                      if (row.info.kind, row.info.channel, row.info.can_id)
                      != (info.kind, info.channel, info.can_id)]
        removed = before - len(self._rows)
        if removed:
            self._rebuild()
        return removed

    def move_row(self, index: int, delta: int) -> int:
        """把某行上移/下移（delta = -1 / +1）或置顶（delta = -index）。

        Returns:
            移动后的行号（越界时原样返回）。
        """
        if not 0 <= index < len(self._rows):
            return index
        target = index + delta
        if not 0 <= target < len(self._rows) or target == index:
            return index
        self._rows.insert(target, self._rows.pop(index))
        self._rebuild()
        return target

    def clear(self) -> None:
        logger.debug("清空图形区（原有 %d 行）", len(self._rows))
        self._rows.clear()
        self._rebuild()

    def rows(self) -> list:
        return list(self._rows)

    def row_count(self) -> int:
        return len(self._rows)

    def specs(self) -> list:
        return [row.spec for row in self._rows]

    def set_specs(self, specs, time_mode: str = None) -> int:
        """用给定的行定义重建图形区（加载布局用）。"""
        self.clear()
        if time_mode:
            self._time_mode = time_mode
        return self.add_specs(list(specs))

    # ---------------------------------------------------------------- 双光标测量
    @property
    def cursors_enabled(self) -> bool:
        return self._cursors_enabled

    def set_cursors_enabled(self, enabled: bool) -> None:
        if enabled == self._cursors_enabled:
            return
        self._cursors_enabled = enabled
        self.cursor_bar.setVisible(enabled)
        if enabled:
            self.reset_cursors()
        else:
            self._cursor_values = [float("nan"), float("nan")]
        logger.debug("双光标 %s（T1=%s, T2=%s）", "开启" if enabled else "关闭",
                     self._cursor_values[0], self._cursor_values[1])
        self._rebuild()
        self._emit_cursors()

    def reset_cursors(self) -> None:
        """把两条光标放在当前视图的 1/3 与 2/3 处。"""
        x_range = self.x_range()
        if x_range is None:
            self._cursor_values = [float("nan"), float("nan")]
            return
        lo, hi = x_range
        self._cursor_values = [lo + (hi - lo) / 3.0, lo + (hi - lo) * 2.0 / 3.0]

    def set_cursor_positions(self, x1: float, x2: float) -> None:
        self._cursor_values = [float(x1), float(x2)]
        self._sync_cursor_lines()
        self._emit_cursors()

    def cursor_positions(self) -> tuple:
        if not self._cursors_enabled:
            return (float("nan"), float("nan"))
        return (self._cursor_values[0], self._cursor_values[1])

    def delta_t(self):
        """两光标之间的间隔（未启用或未定位时为 None）。"""
        if not self._cursors_enabled:
            return None
        x1, x2 = self._cursor_values
        if np.isnan(x1) or np.isnan(x2):
            return None
        return abs(x2 - x1)

    def measurements(self) -> list:
        """每行的 T1/T2 取值与差值，外加**行统计**（最小/最大/均值）与极值开关状态。

        未启用双光标时 T1/T2 为 "—"，但行统计照常给出 —— 值表因此始终有内容。
        """
        x1, x2 = self._cursor_values
        out = []
        for index, row in enumerate(self._rows):
            v1, t1 = row.value_text_at(x1)
            v2, t2 = row.value_text_at(x2)
            delta_text = "—"
            if v1 is not None and v2 is not None:
                diff = v2 - v1
                delta_text = "%d" % int(diff) if float(diff).is_integer() else "%.4f" % diff
            out.append({"name": row.name, "v1": v1, "v2": v2,
                        "text1": t1, "text2": t2, "text_delta": delta_text,
                        "min_text": row.min_text, "max_text": row.max_text,
                        "mean_text": row.mean_text, "stats": row.stats,
                        "row_index": index,
                        "extreme_on": self._row_extremes_enabled(row),
                        "extreme_follow": row.spec.extreme_marks is None})
        return out

    def _emit_cursors(self) -> None:
        x1, x2 = self.cursor_positions()
        self.cursorChanged.emit(x1, x2)
        self.header.setText(self._header_text())

    def _sync_cursor_lines(self) -> None:
        """把两条光标线同步到当前位置（拖动其中一条时其余镜像线跟随）。"""
        if self._syncing:
            return
        self._syncing = True
        try:
            for _, cursor_index, line in self._cursor_lines:
                value = self._cursor_values[cursor_index]
                if not np.isnan(value):
                    line.setValue(value)
        finally:
            self._syncing = False
        self.cursor_bar.set_cursor_values(*self._cursor_values)

    # -------------------------------------------------- 手柄条联动
    def _on_bar_dragged(self, cursor_index: int, x_value: float) -> None:
        """手柄条上按下/拖动：直接设定该光标位置（自动同步光标线与值表）。

        开启吸附时会把位置对齐到采样点，避免"看起来 40、其实 39"的读数错觉。
        """
        if not self._cursors_enabled:
            return
        x_lo, x_hi = self._bar_range()
        clamped = float(min(max(x_value, x_lo), x_hi))
        self._cursor_values[cursor_index] = float(self.snap_x(clamped))
        self._sync_cursor_lines()
        self._emit_cursors()

    def _bar_range(self) -> tuple:
        x_range = self.x_range()
        return x_range if x_range else (0.0, 1.0)

    def _sync_cursor_bar(self) -> None:
        """同步手柄条的可见性、绘图区比例边界、X 范围与光标位置。"""
        bar = self.cursor_bar
        bar.set_enabled_state(self._cursors_enabled)
        if not self._cursors_enabled or not self._rows or self._rows[0].plot is None:
            return
        viewbox = self._rows[0].plot.getViewBox()
        rect = viewbox.sceneBoundingRect()
        viewport_width = max(self.glw.viewport().width(), 1)
        # 手柄条与图形区同宽同左边界，因此用"占视口宽度的比例"表达绘图区范围最稳
        left = self.glw.mapFromScene(rect.topLeft()).x() / float(viewport_width)
        right = self.glw.mapFromScene(rect.bottomRight()).x() / float(viewport_width)
        bar.set_plot_ratio(left, right)
        bar.set_x_range(*self._bar_range())
        bar.set_cursor_values(*self._cursor_values)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_cursor_bar()

    def _on_cursor_dragged(self, cursor_index: int, line) -> None:
        if self._syncing:
            return
        self._cursor_values[cursor_index] = float(line.value())
        self._sync_cursor_lines()
        self._emit_cursors()

    def _build_cursor_lines(self, row_index: int, plot) -> None:
        for cursor_index in range(2):
            value = self._cursor_values[cursor_index]
            position = 0.0 if np.isnan(value) else float(value)
            line = pg.InfiniteLine(
                pos=position,
                angle=90, movable=True,
                pen=pg.mkPen(CURSOR_COLORS[cursor_index], width=1.4),
                label="%s={value:.6f}" % CURSOR_NAMES[cursor_index],
                labelOpts={"position": 0.08, "color": CURSOR_COLORS[cursor_index],
                           "fill": "#ffffffcc", "movable": False})
            line.setZValue(50)
            line.sigPositionChanged.connect(
                lambda _line, ci=cursor_index: self._on_cursor_dragged(ci, _line))
            plot.addItem(line)
            self._cursor_lines.append((row_index, cursor_index, line))

    # ---------------------------------------------------------------- 视图范围
    def x_range(self):
        if not self._rows or self._rows[0].plot is None:
            return None
        view_range = self._rows[0].plot.getViewBox().viewRange()[0]
        return (float(view_range[0]), float(view_range[1]))

    def set_x_range(self, x0: float, x1: float) -> None:
        self._set_x_range(float(x0), float(x1))

    # ---------------------------------------------------------------- 内部
    def _index_of(self, spec) -> int:
        for index, row in enumerate(self._rows):
            if (row.spec.key == spec.key
                    and row.spec.byte_offset == spec.byte_offset
                    and row.spec.size == spec.size
                    and row.spec.dtype == spec.dtype
                    and row.spec.byte_order == spec.byte_order
                    and row.spec.bit_index == spec.bit_index
                    and row.spec.bit_length == spec.bit_length):
                return index
        return -1

    def _row_x(self, row) -> np.ndarray:
        """按当前时间轴模式生成该行的 X 轴。"""
        if self._time_mode == MODE_INDEX:
            return row.series.seq.astype(np.float64)
        return row.series.x - self._origin

    def _reload_rows(self) -> None:
        if self._reader is None:
            return
        for row in self._rows:
            try:
                row.series = row.spec.extract(self._reader)
            except (KeyError, ValueError):
                continue
        self._rebuild()

    def _rebuild(self) -> None:
        """清空图元后按行序重建（行的数据已缓存，重建成本很低）。"""
        started = time.time()
        self.glw.clear()
        self._cursor_lines = []
        for row in self._rows:               # glw 已清空，旧图元引用全部作废
            row.plot, row.curve, row.caption = None, None, None
            row.readout, row.extreme_lines = None, []
            row.marker_on = False
        if self._overlay:
            self._rebuild_overlay()
        else:
            self._rebuild_stacked()

        for row in self._rows:               # 统计与极值标记依赖 plot，故放在重建之后
            if row.plot is not None:
                self._refresh_row_stats(row)

        if self._rows and self._rows[0].x is not None and len(self._rows[0].x):
            self._set_x_range(float(self._rows[0].x[0]), float(self._rows[0].x[-1]))
        self._connect_range_changes()
        self._place_overlays()
        self._update_readouts()
        self._update_point_markers()
        self._sync_cursor_bar()
        self.header.setText(self._header_text())
        self.rowsChanged.emit()
        points = sum(len(row.values) for row in self._rows if row.values is not None)
        logger.debug("重建图形：%d 行 / %d 点 | %s | 时间轴 %s | 双光标 %s | 极值标记 %s | 耗时 %.0f ms",
                     len(self._rows), points,
                     "单坐标轴叠加" if self._overlay else "多轴堆叠",
                     self._time_mode, self._cursors_enabled, self._show_extremes,
                     (time.time() - started) * 1000.0)

    def _prepare_row(self, row) -> bool:
        """把已缓存的序列整理成绘图用的 X / Y（行数据为空时返回 False）。"""
        if row.series is None:
            return False
        row.values = row.series.y.astype(np.float64, copy=False)
        row.x = self._row_x(row)
        return True

    def _style_plot(self, plot) -> None:
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.setMenuEnabled(False)
        plot.getAxis("left").setWidth(AXIS_WIDTH)
        plot.getAxis("left").setStyle(tickTextOffset=6)

    def _plot_curve(self, plot, row):
        row.curve = plot.plot(row.x, row.values,
                              pen=pg.mkPen(row.color, width=CURVE_WIDTH),
                              stepMode="left" if row.spec.step else False,
                              name=row.name if self._overlay else None)
        row.curve.setClipToView(True)
        row.curve.setDownsampling(auto=True, method="peak")

    def _rebuild_stacked(self) -> None:
        """多轴堆叠：一行一个坐标系，X 轴共享、Y 轴独立。"""
        for index, row in enumerate(self._rows):
            if not self._prepare_row(row):
                continue
            plot = self.glw.addPlot(row=index, col=0)
            self._style_plot(plot)
            plot.setMinimumHeight(MIN_ROW_HEIGHT)
            self._apply_axis_labels(plot, row)     # Y 轴标题 + 枚举文本刻度（来自 DBC）
            self._plot_curve(plot, row)
            row.plot = plot
            row.caption = self._add_caption(plot, row)
            row.readout = self._add_readout(plot, row)
            self.glw.ci.layout.setRowStretchFactor(index, 1)
            if self._cursors_enabled:
                self._build_cursor_lines(index, plot)

        # 非最后一行隐藏 X 轴刻度，最后一行显示轴名
        for index, row in enumerate(self._rows):
            if row.plot is None:
                continue
            if index < len(self._rows) - 1:
                row.plot.getAxis("bottom").setStyle(showValues=False)
            else:
                row.plot.setLabel("bottom", MODE_LABELS.get(self._time_mode, ""))

        # 共享 X 轴：缩放/平移任一行，其余同步
        for row in self._rows[1:]:
            if row.plot is not None and self._rows[0].plot is not None:
                row.plot.setXLink(self._rows[0].plot)

    def _rebuild_overlay(self) -> None:
        """单坐标轴：所有信号叠加进同一个坐标系，共用一个 Y 轴。"""
        plot = self.glw.addPlot(row=0, col=0)
        self._style_plot(plot)
        plot.addLegend(offset=(8, 8))
        first = True
        for row in self._rows:
            if not self._prepare_row(row):
                continue
            row.plot = plot
            self._plot_curve(plot, row)
            if first:                          # 叠加模式所有行共用左上角一处读值
                row.readout = self._add_readout(plot, row)
                first = False
        plot.setLabel("left", self._overlay_axis_text())
        plot.setLabel("bottom", MODE_LABELS.get(self._time_mode, ""))
        if self._cursors_enabled:
            self._build_cursor_lines(0, plot)

    def _overlay_axis_text(self) -> str:
        """叠加模式的共用 Y 轴标题：单位一致就写出来，否则说明是多信号叠加。"""
        units = sorted({row.spec.unit for row in self._rows if row.spec.unit})
        if len(units) == 1:
            return "[%s]" % units[0]
        if len(self._rows) > 1:
            return "多信号叠加"
        return self._rows[0].axis_text if self._rows else ""

    def _apply_axis_labels(self, plot, row) -> None:
        """Y 轴标题 + 枚举信号文本刻度。

        - 标题：DBC 信号名 [单位]（太长时退化成 [单位]），让用户一眼知道这条轴来自 DBC 哪个信号
        - 刻度：信号带 VAL_ 枚举时直接用文本（如 Off/On/Ready），而不是 0/1/2 数字
        """
        text = row.axis_text
        if text:
            plot.setLabel("left", text)
        axis = plot.getAxis("left")
        labels = row.spec.labels or {}
        if not labels or len(labels) > MAX_TICK_LABELS:
            axis.setTicks(None)
            return
        axis.setTicks([[(float(value), str(name)) for value, name in sorted(labels.items())]])

    def _add_caption(self, plot, row):
        """绘图区左上角的横排信号名。

        旋转的轴标题在矮行里会被截断（用户就看不到名字了），所以名称横排放在行内；
        单位则挂在 Y 轴标题上。anchor=(0, 0) 表示把文字左上角钉在给定坐标上（朝右下展开），
        否则文字会跑到绘图区外面被裁掉。
        """
        item = pg.TextItem(row.label_text, color=row.color, anchor=(0, 0),
                           fill=pg.mkBrush(255, 255, 255, 210))
        item.setZValue(40)
        plot.addItem(item, ignoreBounds=True)
        return item

    def _add_readout(self, plot, row):
        """绘图区右上角的"鼠标读值"（跟随鼠标，anchor=(1, 0) 表示钉住文字右上角）。"""
        item = pg.TextItem("", color=row.color, anchor=(1, 0),
                           fill=pg.mkBrush(READOUT_BG))
        item.setZValue(45)
        plot.addItem(item, ignoreBounds=True)
        return item

    def _place_overlays(self) -> None:
        """把行内说明/读值钉在各自绘图区的角上（缩放/平移后仍随视图走）。"""
        for row in self._rows:
            if row.plot is None:
                continue
            (x_lo, x_hi), (_y_lo, y_hi) = row.plot.getViewBox().viewRange()
            if row.caption is not None:
                row.caption.setPos(x_lo + (x_hi - x_lo) * 0.004, y_hi)
            if row.readout is not None:
                row.readout.setPos(x_hi - (x_hi - x_lo) * 0.004, y_hi)

    def _connect_range_changes(self) -> None:
        """范围变化（缩放/平移/自动缩放）时同步手柄条与行内说明。

        监听**每个**坐标系：堆叠模式下各行的 Y 轴互相独立，
        只监听第一行会漏掉其他行的自动缩放事件（行内说明会跑到可视区外）。
        """
        for viewbox in self._range_sources:
            try:
                viewbox.sigRangeChanged.disconnect(self._on_view_range_changed)
            except (RuntimeError, TypeError):
                pass
        self._range_sources = []
        for row in self._rows:
            if row.plot is None:
                continue
            viewbox = row.plot.getViewBox()
            viewbox.sigRangeChanged.connect(self._on_view_range_changed)
            self._range_sources.append(viewbox)

    def _on_view_range_changed(self, *_args) -> None:
        self._place_overlays()
        self._update_point_markers()
        self._sync_cursor_bar()

    def _set_x_range(self, x0: float, x1: float) -> None:
        """所有行共享 X 轴，所以设置第一行即可带动全部。"""
        for row in self._rows:
            if row.plot is not None:
                row.plot.setXRange(x0, x1, padding=0.01)
                break

    def _header_text(self) -> str:
        if not self._rows:
            return "图形区：在左侧树里双击报文（或展开后双击某个字节）即可加入行"
        total_points = sum(len(row.values) for row in self._rows if row.values is not None)
        text = "共 %d 行 / %s 点    %s    X 轴：%s    滚轮缩放 · 拖动平移 · 右键菜单" % (
            len(self._rows), _format_count(total_points),
            "单坐标轴叠加" if self._overlay else "多轴堆叠",
            MODE_LABELS.get(self._time_mode, self._time_mode))
        if self._readout_x is not None:
            text += "    | 鼠标 t = %.6f（%s）" % (
                self._readout_x, "已吸附采样点" if self._snap else "自由位置")
        if self._cursors_enabled:
            delta = self.delta_t()
            if delta is None:
                text += "    | T1/T2 未定位（拖动红线/蓝线）"
            else:
                text += "    | ΔT = %.6f s (%.3f ms)" % (delta, delta * 1000.0)
        return text

    # ---------------------------------------------------------------- 右键菜单
    def _on_context_menu(self, pos) -> None:
        menu = QMenu(self)
        scene_pos = self.glw.mapToScene(pos)
        index = -1
        for candidate, row in enumerate(self._rows):
            if row.plot is not None and row.plot.vb.sceneBoundingRect().contains(scene_pos):
                index = candidate
                break
        if index >= 0:
            menu.addAction("删除该行：%s" % self._rows[index].name,
                           lambda: self.remove_row(index))
            menu.addAction("上移该行", lambda: self.move_row(index, -1))
            menu.addAction("下移该行", lambda: self.move_row(index, 1))
            menu.addAction("该行置顶", lambda: self.move_row(index, -index))
            menu.addSeparator()
            row_extreme = menu.addAction("本行显示极值标记（min/max）")
            row_extreme.setCheckable(True)
            row_extreme.setChecked(self.row_extremes(index))
            row_extreme.toggled.connect(
                lambda checked, i=index: self.set_row_extremes(i, checked))
            if self._rows[index].spec.extreme_marks is not None:
                menu.addAction("本行恢复跟随全局开关",
                               lambda i=index: self.set_row_extremes(i, None))
        menu.addSeparator()
        snap_action = menu.addAction("光标/读值吸附到采样点")
        snap_action.setCheckable(True)
        snap_action.setChecked(self._snap)
        snap_action.toggled.connect(self.set_cursor_snap)
        readout_action = menu.addAction("鼠标跟随读值")
        readout_action.setCheckable(True)
        readout_action.setChecked(self._readout_enabled)
        readout_action.toggled.connect(self.set_readout_enabled)
        symbols_action = menu.addAction("显示采样点（放大后自动出现）")
        symbols_action.setCheckable(True)
        symbols_action.setChecked(self._show_symbols)
        symbols_action.toggled.connect(self.set_show_symbols)
        extremes_action = menu.addAction("显示极值标记（全部行）")
        extremes_action.setCheckable(True)
        extremes_action.setChecked(self._show_extremes)
        extremes_action.toggled.connect(self.set_show_extremes)
        menu.addSeparator()
        overlay_action = menu.addAction("单坐标轴叠加（全部信号共用一个 Y 轴）")
        overlay_action.setCheckable(True)
        overlay_action.setChecked(self._overlay)
        overlay_action.toggled.connect(self.set_overlay)
        menu.addSeparator()
        menu.addAction("隐藏双光标" if self._cursors_enabled else "显示双光标测量",
                       lambda: self.set_cursors_enabled(not self._cursors_enabled))
        if self._cursors_enabled:
            menu.addAction("重置光标到视图 1/3 与 2/3", self.reset_cursors)
        menu.addSeparator()
        menu.addAction("全部自动缩放", self._auto_range)
        menu.addAction("清空所有行", self.clear)
        menu.exec(self.glw.mapToGlobal(pos))

    def _auto_range(self) -> None:
        for row in self._rows:
            if row.plot is not None:
                row.plot.enableAutoRange()
        if self._rows and self._rows[0].x is not None and len(self._rows[0].x):
            self._set_x_range(float(self._rows[0].x[0]), float(self._rows[0].x[-1]))

    # ---------------------------------------------------------------- 渲染/导出
    def render_png(self, path: str) -> str:
        """把整个图形区（含标题信息、光标手柄条、各行曲线）渲染成 PNG。"""
        started = time.time()
        self.grab().save(path)
        logger.info("图形区渲染 PNG：%s（%.1f KB，耗时 %.0f ms）", path,
                    os.path.getsize(path) / 1024.0, (time.time() - started) * 1000.0)
        return path

    def point_counts(self) -> list:
        """每行已加载的点数（实际渲染点数由 pyqtgraph 降采样决定）。"""
        return [len(row.values) for row in self._rows if row.values is not None]


def _format_count(value: int) -> str:
    if value >= 1_000_000:
        return "%.1fM" % (value / 1_000_000.0)
    if value >= 1000:
        return "%.1fk" % (value / 1000.0)
    return str(value)
