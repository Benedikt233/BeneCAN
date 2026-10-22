# -*- coding: utf-8 -*-
"""BeneCAN 入口：主窗口（报文树 + Graphic 图形区 + 建索引进度）。

用法：
    python main.py [某.asc]        # 可选：启动后自动打开该 ASC
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QComboBox, QDockWidget, QFileDialog,
                               QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QProgressDialog, QSplitter,
                               QTableWidget, QTableWidgetItem, QToolBar,
                               QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import APP_NAME, TOOL_VERSION  # noqa: E402
from core.indexer import build_index, default_index_path, index_is_fresh  # noqa: E402
from core.query import MODE_INDEX, MODE_SESSION, MODE_TIME, IndexReader  # noqa: E402
from signals.dbc_loader import KINDS, DbcLibrary  # noqa: E402
from ui.dbc_mapping_dialog import DbcMappingDialog  # noqa: E402
from ui.graphic_window import GraphicWindow  # noqa: E402
from ui.msg_tree import MessageTree  # noqa: E402
from utils import export as exporter  # noqa: E402
from utils.log_setup import fmt_bytes, fmt_seconds, setup_logging  # noqa: E402

logger = logging.getLogger(__name__)

TIME_MODES = [("原样时间", MODE_TIME), ("空档压缩", MODE_SESSION), ("全局帧序号", MODE_INDEX)]
MEASURE_COLUMNS = ["信号", "T1 值", "T2 值", "差值", "最小", "最大", "均值", "极值"]
EXTREME_COLUMN = len(MEASURE_COLUMNS) - 1        # 最后一列：勾选 = 该行显示 min/max 虚线


def app_dir() -> str:
    """程序目录：源码运行时是项目根；打包成 exe 后是 exe 所在目录。

    dbc/ 目录与映射配置都放在这里，用户可以直接把 DBC 拷进来。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()
DBC_DIR = os.path.join(APP_DIR, "dbc")
MB = 1048576.0


def ensure_dbc_dir() -> str:
    """确保程序同级存在 dbc/ 目录；打包版把包内示例 DBC 释放出来。

    单文件 exe 的 datas 会解压到临时目录（sys._MEIPASS），用户看不到也用不上，
    所以首次运行时把示例 DBC 复制到 exe 同级，方便直接试跑。
    """
    try:
        os.makedirs(DBC_DIR, exist_ok=True)
        if getattr(sys, "frozen", False):
            bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "dbc")
            has_dbc = any(name.lower().endswith(".dbc") for name in os.listdir(DBC_DIR))
            if os.path.isdir(bundled) and not has_dbc:
                for name in os.listdir(bundled):
                    if name.lower().endswith(".dbc"):
                        shutil.copy2(os.path.join(bundled, name),
                                     os.path.join(DBC_DIR, name))
    except OSError:
        pass                     # 只读目录（如 Program Files）下静默跳过
    return DBC_DIR


class IndexWorker(QThread):
    """后台建索引（首次打开 500 MB 级 ASC 需要几十秒，不能卡 UI）。"""

    progressed = Signal(int, int)          # done_bytes, total_bytes
    finishedOk = Signal(dict)
    failed = Signal(str)

    def __init__(self, asc_path: str, parent=None):
        super().__init__(parent)
        self._asc_path = asc_path

    def run(self) -> None:
        started = time.time()
        size = os.path.getsize(self._asc_path) if os.path.exists(self._asc_path) else 0
        logger.info("开始建索引（后台线程）：%s（%s）", self._asc_path, fmt_bytes(size))
        try:
            stats = build_index(
                self._asc_path,
                index_path=default_index_path(self._asc_path),
                progress=lambda lines, done, total: self.progressed.emit(done, total),
            )
            logger.info("建索引完成：%s 帧，耗时 %s，输出 %s",
                        stats.get("frames"), fmt_seconds(time.time() - started),
                        fmt_bytes(stats.get("index_bytes")))
            self.finishedOk.emit(stats)
        except Exception as exc:                     # noqa: BLE001
            logger.error("建索引失败：%s: %s", type(exc).__name__, exc, exc_info=True)
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("%s — ASC Graphic Viewer %s" % (APP_NAME, TOOL_VERSION))
        self.resize(1560, 940)

        self._reader = None
        self._worker = None
        self._dialog = None
        self._asc_path = ""
        ensure_dbc_dir()
        self.dbc = DbcLibrary.open(DBC_DIR)      # dbc/ 目录 + channel_map.json

        self.tree = MessageTree()
        self.graphic = GraphicWindow()
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("过滤报文 ID / 通道（如 17A、ch5）…")
        self.filter_box.textChanged.connect(self.tree.apply_filter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(self.filter_box)
        left_layout.addWidget(self.tree, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.graphic)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 1040])
        self.setCentralWidget(splitter)

        self._build_actions()
        self._build_toolbar()
        self.status_label = QLabel("就绪：请打开一个 .asc 文件（Ctrl+O）")
        self.statusBar().addWidget(self.status_label)

        self.tree.addMessageRequested.connect(self.graphic.add_message)
        self.tree.addSpecRequested.connect(self.graphic.add_specs)
        self.tree.removeMessageRequested.connect(self._on_remove_message)
        self.tree.clearRequested.connect(self.graphic.clear)
        self.graphic.statusMessage.connect(self.status_label.setText)
        self.graphic.rowsChanged.connect(self._update_status)
        self.graphic.rowsChanged.connect(self._refresh_measure_table)
        self.graphic.cursorChanged.connect(self._on_cursor_changed)

        self._build_measure_dock()

    # ---------------------------------------------------------------- 值表（测量）
    def _build_measure_dock(self) -> None:
        self.delta_label = QLabel("加入图形行后，这里显示每行的最小/最大/均值；"
                                  "启用双光标还能测 T1/T2 与 ΔT")
        self.delta_label.setStyleSheet("padding:4px 6px; font-weight:bold;")
        self.measure_table = QTableWidget(0, len(MEASURE_COLUMNS))
        self.measure_table.setHorizontalHeaderLabels(MEASURE_COLUMNS)
        self.measure_table.verticalHeader().setVisible(False)
        self.measure_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.measure_table.itemChanged.connect(self._on_measure_item_changed)
        self._refreshing_measure = False
        header = self.measure_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(MEASURE_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.delta_label)
        layout.addWidget(self.measure_table, 1)

        self.measure_dock = QDockWidget("测量值表（双光标）", self)
        self.measure_dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.measure_dock)
        self.measure_dock.setMinimumHeight(150)

    def _on_cursor_changed(self, x1: float, x2: float) -> None:
        self._refresh_measure_table()

    def _on_measure_item_changed(self, item) -> None:
        """值表里勾/取消"极值"列 → 切换该行的极值标记（单行覆盖，不影响其他行）。"""
        if self._refreshing_measure or item.column() != EXTREME_COLUMN:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        self.graphic.set_row_extremes(item.row(), enabled)

    def _refresh_measure_table(self) -> None:
        rows = self.graphic.measurements()
        self._refreshing_measure = True              # 防：填表触发 itemChanged 再次刷新
        try:
            if not rows:
                self.measure_table.setRowCount(0)
                self.delta_label.setText("图形区还没有行：加入报文后这里会显示每行的最小/最大/均值"
                                         "（启用双光标还会显示 T1/T2 取值与 ΔT）")
                return
            stats_note = "行统计 %d 行：最小 / 最大 / 均值；最后一列可单独关掉某行的极值线" % len(rows)
            if not self.graphic.cursors_enabled:
                self.delta_label.setText("%s    |    未启用双光标（Ctrl+M 或图形区右键）："
                                         "启用后可测 T1/T2 与 ΔT" % stats_note)
            else:
                delta = self.graphic.delta_t()
                x1, x2 = self.graphic.cursor_positions()
                if delta is None:
                    self.delta_label.setText("%s    |    双光标未定位（拖动红线 T1 / 蓝线 T2）"
                                             % stats_note)
                else:
                    self.delta_label.setText(
                        "ΔT = %.6f s (%.3f ms)    T1 = %.6f    T2 = %.6f    %s"
                        % (delta, delta * 1000.0, x1, x2, stats_note))
            self.measure_table.setRowCount(len(rows))
            for index, item in enumerate(rows):
                values = [item["name"], item["text1"], item["text2"], item["text_delta"],
                          item["min_text"], item["max_text"], item["mean_text"], ""]
                for column, text in enumerate(values):
                    cell = self.measure_table.item(index, column)
                    if cell is None:
                        cell = QTableWidgetItem()
                        self.measure_table.setItem(index, column, cell)
                    cell.setText(str(text))
                extreme = self.measure_table.item(index, EXTREME_COLUMN)
                extreme.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                                 | Qt.ItemFlag.ItemIsUserCheckable)
                extreme.setCheckState(Qt.CheckState.Checked if item["extreme_on"]
                                      else Qt.CheckState.Unchecked)
                extreme.setToolTip(
                    "勾选 = 本行显示 min/max 虚线%s"
                    % ("（当前跟随全局开关）" if item["extreme_follow"] else "（已单独设置）"))
        finally:
            self._refreshing_measure = False

    # ---------------------------------------------------------------- 工具栏/菜单
    def _build_actions(self) -> None:
        self.open_action = QAction("打开 ASC…", self)
        self.open_action.setShortcut("Ctrl+O")
        self.open_action.triggered.connect(self.choose_asc)

        self.rebuild_action = QAction("重建索引", self)
        self.rebuild_action.triggered.connect(self._on_rebuild_index)

        self.cursor_action = QAction("双光标测量", self)
        self.cursor_action.setCheckable(True)
        self.cursor_action.setShortcut("Ctrl+M")
        self.cursor_action.toggled.connect(self._on_toggle_cursors)

        self.overlay_action = QAction("单坐标轴叠加", self)
        self.overlay_action.setCheckable(True)
        self.overlay_action.setShortcut("Ctrl+G")
        self.overlay_action.setToolTip("所有信号叠加到同一坐标系，共用一个 Y 轴")
        self.overlay_action.toggled.connect(self.graphic.set_overlay)

        self.snap_action = QAction("吸附采样点", self)
        self.snap_action.setCheckable(True)
        self.snap_action.setChecked(True)
        self.snap_action.setShortcut("Ctrl+Shift+L")
        self.snap_action.setToolTip("鼠标读值与双光标吸附到真实帧时刻（避免“看起来 40、其实 39”）")
        self.snap_action.toggled.connect(self.graphic.set_cursor_snap)

        self.extremes_action = QAction("显示极值标记", self)
        self.extremes_action.setCheckable(True)
        self.extremes_action.setChecked(True)
        self.extremes_action.setToolTip("在每行画 min/max 水平虚线（单行可在值表最后一列或右键单独关掉）")
        self.extremes_action.toggled.connect(self.graphic.set_show_extremes)

        self.symbols_action = QAction("显示采样点", self)
        self.symbols_action.setCheckable(True)
        self.symbols_action.setChecked(True)
        self.symbols_action.setToolTip("在真实采样点处画圆点（每行可见点数过多时自动隐藏，放大后出现）")
        self.symbols_action.toggled.connect(self.graphic.set_show_symbols)

        self.readout_action = QAction("鼠标跟随读值", self)
        self.readout_action.setCheckable(True)
        self.readout_action.setChecked(True)
        self.readout_action.setToolTip("鼠标移到曲线上时，在每行右上角显示该时刻的值")
        self.readout_action.toggled.connect(self.graphic.set_readout_enabled)

        self.reset_cursor_action = QAction("重置光标位置", self)
        self.reset_cursor_action.triggered.connect(self._on_reset_cursors)

        self.png_action = QAction("导出 PNG…", self)
        self.png_action.setShortcut("Ctrl+Shift+P")
        self.png_action.triggered.connect(self.export_png)

        self.csv_action = QAction("导出 CSV…", self)
        self.csv_action.setShortcut("Ctrl+Shift+C")
        self.csv_action.triggered.connect(self.export_csv)

        self.save_layout_action = QAction("保存布局…", self)
        self.save_layout_action.triggered.connect(self.save_layout)
        self.load_layout_action = QAction("加载布局…", self)
        self.load_layout_action.triggered.connect(self.load_layout)

        self.measure_dock_action = QAction("显示测量值表", self)
        self.measure_dock_action.setCheckable(True)
        self.measure_dock_action.setChecked(True)
        self.measure_dock_action.toggled.connect(
            lambda shown: self.measure_dock.setVisible(shown))

        self.load_dbc_action = QAction("DBC 映射设置…", self)
        self.load_dbc_action.triggered.connect(self.load_dbc)
        self.reload_dbc_action = QAction("重新加载 dbc/ 目录", self)
        self.reload_dbc_action.triggered.connect(self.reload_dbc)
        self.clear_dbc_action = QAction("清除全部 DBC 映射", self)
        self.clear_dbc_action.triggered.connect(self.clear_dbc)

    def _build_toolbar(self) -> None:
        bar = QToolBar("主工具栏")
        bar.setMovable(False)
        self.addToolBar(bar)

        bar.addAction(self.open_action)
        bar.addSeparator()
        bar.addWidget(QLabel(" 时间轴："))
        self.mode_box = QComboBox()
        for label, mode in TIME_MODES:
            self.mode_box.addItem(label, mode)
        self.mode_box.currentIndexChanged.connect(self._on_mode_changed)
        bar.addWidget(self.mode_box)
        bar.addSeparator()
        bar.addAction(self.cursor_action)
        bar.addAction(self.overlay_action)
        bar.addAction(self.snap_action)
        bar.addAction(self.reset_cursor_action)
        bar.addSeparator()
        bar.addAction(self.png_action)
        bar.addAction(self.csv_action)
        bar.addSeparator()
        bar.addAction(self.save_layout_action)
        bar.addAction(self.load_layout_action)
        bar.addSeparator()
        clear_action = QAction("清空图形", self)
        clear_action.triggered.connect(self.graphic.clear)
        bar.addAction(clear_action)
        bar.addAction(self.rebuild_action)

        menu_file = self.menuBar().addMenu("文件(&F)")
        menu_file.addAction(self.open_action)
        menu_file.addAction(self.rebuild_action)
        menu_file.addSeparator()
        menu_file.addAction(self.png_action)
        menu_file.addAction(self.csv_action)
        menu_file.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        menu_file.addAction(quit_action)

        menu_view = self.menuBar().addMenu("视图(&V)")
        menu_view.addAction(self.cursor_action)
        menu_view.addAction(self.overlay_action)
        menu_view.addAction(self.snap_action)
        menu_view.addAction(self.readout_action)
        menu_view.addAction(self.symbols_action)
        menu_view.addAction(self.extremes_action)
        menu_view.addAction(self.reset_cursor_action)
        menu_view.addAction(self.measure_dock_action)

        menu_layout = self.menuBar().addMenu("布局(&L)")
        menu_layout.addAction(self.save_layout_action)
        menu_layout.addAction(self.load_layout_action)

        menu_dbc = self.menuBar().addMenu("DBC(&D)")
        menu_dbc.addAction(self.load_dbc_action)
        menu_dbc.addAction(self.reload_dbc_action)
        menu_dbc.addAction(self.clear_dbc_action)

    # ---------------------------------------------------------------- DBC（可选）
    def _channel_choices(self) -> list:
        """当前 ASC 的 (帧类型, 通道, 帧数) 列表，供映射对话框选择。"""
        if self._reader is None:
            return []
        return [(channel.kind, channel.channel, channel.frames)
                for channel in self._reader.channels()]

    def load_dbc(self) -> None:
        """打开 DBC 映射工作台：拖拽/改名/备注 → 应用后刷新报文树。

        没有打开 ASC 也可以先维护通道模板与 DBC 映射；
        帧类型不用用户选，DBC 自己带着 VFrameFormat，加载时自动识别。
        """
        dialog = DbcMappingDialog(DBC_DIR, self._channel_choices(), self,
                                  library=self.dbc)
        if dialog.exec() != DbcMappingDialog.DialogCode.Accepted:
            return

        self.dbc.load_all()
        self._apply_dbc()
        stats = self.dbc.stats()
        summary = dialog.applied_summary or {}
        logger.info("DBC 映射已应用：%s", summary or stats)
        parts = ["已保存 DBC 映射",
                 "%d 通道 / %d 已映射 / %d 条映射"
                 % (stats["channels"], stats["mapped_channels"], stats["mappings"])]
        if summary.get("multi_dbc_channels"):
            parts.append("%d 个通道含多个 DBC（同 ID 以拖入顺序为准）"
                         % summary["multi_dbc_channels"])
        if summary.get("conflicts"):
            parts.append("%d 个同 ID 冲突（以第一个为准）" % summary["conflicts"])
        if stats["errors"]:
            parts.append("%d 个 DBC 解析失败" % stats["errors"])
        self.status_label.setText("    |    ".join(parts))

    def reload_dbc(self) -> None:
        self.dbc = DbcLibrary.open(DBC_DIR)
        self._apply_dbc()
        stats = self.dbc.stats()
        logger.info("重新加载 dbc/ 目录：%s", stats)
        self.status_label.setText(
            "已重新加载 dbc/：%d 个 DBC / %d 条映射 / %d 报文 / %d 信号%s"
            % (stats["dbc_files"], stats["mappings"], stats["messages"],
               stats["signals"],
               "（%d 个文件解析失败）" % stats["errors"] if stats["errors"] else ""))

    def clear_dbc(self) -> None:
        self.dbc.clear()
        self.dbc.save_config()
        self._apply_dbc()
        logger.info("清除全部 DBC 映射")
        self.status_label.setText("已清除全部 DBC 映射（dbc/channel_map.json 已更新）")

    def _apply_dbc(self) -> None:
        """把 DBC 库挂到报文树上并刷新状态栏。"""
        self.tree.set_dbc_library(self.dbc)
        stats = self.dbc.stats()
        logger.debug("应用 DBC 库：%s", stats)
        if self.dbc.errors:
            logger.warning("有 DBC 解析失败：%s", self.dbc.errors)
        if stats["dbc_files"] or stats["mappings"]:
            self.status_label.setText(
                "DBC：%d 个文件 / %d 条通道映射 / %d 报文 / %d 信号"
                % (stats["dbc_files"], stats["mappings"], stats["messages"],
                   stats["signals"]))

    # ---------------------------------------------------------------- M4：测量与导出
    def _on_toggle_cursors(self, checked: bool) -> None:
        logger.debug("双光标测量：%s", "开启" if checked else "关闭")
        self.graphic.set_cursors_enabled(bool(checked))
        self._refresh_measure_table()

    def _on_reset_cursors(self) -> None:
        if not self.graphic.cursors_enabled:
            self.cursor_action.setChecked(True)
        self.graphic.reset_cursors()
        self.graphic._rebuild()
        logger.debug("重置光标位置：%s", self.graphic.cursor_positions())
        self._refresh_measure_table()

    def _on_rebuild_index(self) -> None:
        if not self._asc_path:
            QMessageBox.information(self, "提示", "请先打开一个 ASC 文件。")
            return
        logger.info("手动重建索引：%s", self._asc_path)
        self._open_asc(self._asc_path, force=True)

    def _export_range(self) -> tuple:
        """导出范围：若双光标已定位则用两光标之间，否则整段。"""
        if self.graphic.cursors_enabled:
            x1, x2 = self.graphic.cursor_positions()
            if not (x1 != x1 or x2 != x2):          # 排除 NaN
                return (min(x1, x2), max(x1, x2))
        return (None, None)

    def _axis_labels(self) -> tuple:
        if self.graphic.time_mode == MODE_INDEX:
            return ("帧序号", "帧")
        return ("时间", "s")

    def export_png(self) -> None:
        if not self.graphic.row_count():
            QMessageBox.information(self, "提示", "图形区还没有行，先加入报文再导出。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出 PNG", "graphic.png",
                                              "PNG 图片 (*.png)")
        if not path:
            return
        self.graphic.render_png(path)
        logger.info("导出 PNG：%s（%.1f KB）", path, os.path.getsize(path) / 1024)
        self.status_label.setText("已导出 PNG：%s（%.1f KB）"
                                 % (path, os.path.getsize(path) / 1024))

    def export_csv(self) -> None:
        rows = self.graphic.rows()
        if not rows:
            QMessageBox.information(self, "提示", "图形区还没有行，先加入报文再导出。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "graphic.csv",
                                              "CSV 文件 (*.csv)")
        if not path:
            return
        t0, t1 = self._export_range()
        x_label, x_unit = self._axis_labels()
        try:
            stats = exporter.export_wide_csv(path, rows, t0, t1, x_label, x_unit)
        except ValueError as exc:
            logger.warning("导出 CSV 被护栏拦下：%s", exc)
            QMessageBox.warning(self, "导出范围过大", str(exc))
            return
        scope = "全部范围" if t0 is None else "光标区间 %.6f~%.6f" % (t0, t1)
        logger.info("导出 CSV：%s（%d 行 × %d 列，%s，%.1f KB）", path,
                    stats["rows"], stats["columns"], scope, stats["bytes"] / 1024)
        self.status_label.setText(
            "已导出 CSV：%s（%d 行 × %d 列，%s，%.1f KB）"
            % (os.path.basename(path), stats["rows"], stats["columns"], scope,
               stats["bytes"] / 1024))

    def save_layout(self) -> None:
        if not self.graphic.row_count():
            QMessageBox.information(self, "提示", "图形区还没有行，先加入报文再保存布局。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存布局", "layout.json",
                                              "布局文件 (*.json)")
        if not path:
            return
        x_range = self.graphic.x_range()
        cursors = self.graphic.cursor_positions() if self.graphic.cursors_enabled else None
        exporter.save_layout(path, self.graphic.specs(), self.graphic.time_mode,
                             x_range, cursors,
                             self._reader.source_path if self._reader else "")
        logger.info("保存布局：%s（%d 行，时间轴 %s）", path, self.graphic.row_count(),
                    self.graphic.time_mode)
        self.status_label.setText("已保存布局：%s（%d 行）"
                                 % (path, self.graphic.row_count()))

    def load_layout(self) -> None:
        if self._reader is None:
            QMessageBox.information(self, "提示", "请先打开一个 ASC 文件。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "加载布局", "",
                                              "布局文件 (*.json)")
        if not path:
            return
        try:
            layout = exporter.load_layout(path)
        except (OSError, ValueError, KeyError) as exc:
            logger.error("加载布局失败：%s", exc, exc_info=True)
            QMessageBox.critical(self, "布局加载失败", "%s: %s" % (type(exc).__name__, exc))
            return
        self.mode_box.blockSignals(True)
        for index in range(self.mode_box.count()):
            if self.mode_box.itemData(index) == layout["time_mode"]:
                self.mode_box.setCurrentIndex(index)
        self.mode_box.blockSignals(False)
        added = self.graphic.set_specs(layout["rows"], layout["time_mode"])
        if layout["x_range"]:
            self.graphic.set_x_range(*layout["x_range"])
        if layout["cursors"]:
            self.cursor_action.setChecked(True)
            self.graphic.set_cursor_positions(*layout["cursors"])
        self._refresh_measure_table()
        logger.info("加载布局：%s（%d/%d 行加入成功）", path, added, len(layout["rows"]))
        self.status_label.setText("已加载布局：%s（%d/%d 行加入成功）"
                                 % (os.path.basename(path), added, len(layout["rows"])))

    # ---------------------------------------------------------------- 打开
    def choose_asc(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ASC 日志", "", "ASC 日志 (*.asc *.ASC);;所有文件 (*)")
        if path:
            self._open_asc(path)

    def _open_asc(self, path: str, force: bool = False) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "请稍候", "正在建索引，请等待完成。")
            return
        self._asc_path = path
        index_path = default_index_path(path)
        logger.info("打开 ASC：%s%s", path, "（强制重建索引）" if force else "")

        if not force and index_is_fresh(path, index_path):
            logger.debug("索引可直接复用：%s", index_path)
            self._activate_reader(path)
            return

        self._dialog = QProgressDialog("正在建立索引（首次打开较慢）…", "取消", 0, 100, self)
        self._dialog.setWindowTitle("建索引")
        self._dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._dialog.setMinimumDuration(0)
        self._dialog.canceled.connect(self._on_cancel_index)

        self._worker = IndexWorker(path, self)
        self._worker.progressed.connect(self._on_progress)
        self._worker.finishedOk.connect(lambda stats, p=path: self._on_index_done(p, stats))
        self._worker.failed.connect(self._on_index_failed)
        self._worker.start()

    def _on_cancel_index(self) -> None:
        self.status_label.setText("建索引已被取消（已完成的索引仍可复用）")

    def _on_progress(self, done: int, total: int) -> None:
        if self._dialog is not None and total:
            self._dialog.setValue(int(done / total * 100))

    def _on_index_failed(self, message: str) -> None:
        self._close_dialog()
        QMessageBox.critical(self, "建索引失败", message)
        self.status_label.setText("建索引失败：%s" % message)

    def _on_index_done(self, path: str, stats: dict) -> None:
        self._close_dialog()
        cached = "命中缓存" if stats.get("cached") else "新建 %.1f s" % stats.get("build_seconds", 0.0)
        self.status_label.setText("索引完成（%s）：%.1f MB" % (cached, stats.get("index_bytes", 0) / MB))
        self._activate_reader(path)

    def _close_dialog(self) -> None:
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None

    def _activate_reader(self, path: str) -> None:
        started = time.time()
        try:
            reader = IndexReader.open(default_index_path(path))
        except Exception as exc:                     # noqa: BLE001
            logger.error("打开索引失败：%s", exc, exc_info=True)
            QMessageBox.critical(self, "打开索引失败", str(exc))
            return
        self._reader = reader
        self.tree.set_dbc_library(self.dbc)      # 先挂 DBC，树填充时就能显示信号名
        self.tree.set_reader(reader)
        self.graphic.set_reader(reader)
        self.setWindowTitle("%s — %s" % (APP_NAME, os.path.basename(path)))
        self._update_status()
        logger.info("已激活日志：%s | %d 帧 / %d 通道 / %d 报文 | 界面就绪耗时 %s",
                    reader.source_path, reader.frames, len(reader.channels()),
                    len(reader.catalog()), fmt_seconds(time.time() - started))

    # ---------------------------------------------------------------- 状态/交互
    def _on_mode_changed(self, index: int) -> None:
        mode = self.mode_box.itemData(index)
        logger.debug("切换时间轴模式：%s", mode)
        self.graphic.set_time_mode(mode)

    def _on_remove_message(self, info) -> None:
        removed = self.graphic.remove_message_rows(info)
        logger.debug("移除报文行：%s ch%d（%d 行）", info.id_text, info.channel, removed)
        self.status_label.setText("已移除 %s 的 %d 行" % (info.id_text, removed))

    def _update_status(self) -> None:
        if self._reader is None:
            return
        channels = len(self._reader.channels())
        messages = len(self._reader.catalog())
        self.status_label.setText(
            "文件：%s    帧数：%d    通道：%d    报文：%d    图形行数：%d    索引：%s"
            % (os.path.basename(self._reader.source_path), self._reader.frames,
               channels, messages, self.graphic.row_count(),
               self._reader.metadata.get("index_format", "?")))


def resolve_asc_path(value: str) -> str:
    """命令行/上游工具传入的路径：asc 文件直接用，目录则取里面最新的 asc。

    这样与 Vehicle_log_tool_AI 联动时，把「解压目录」直接传进来即可。
    """
    if not value:
        return ""
    if os.path.isdir(value):
        candidates = [os.path.join(value, name) for name in os.listdir(value)
                      if name.lower().endswith(".asc")]
        if not candidates:
            return ""
        return max(candidates, key=os.path.getmtime)
    return value


def parse_channel_spec(text: str) -> list:
    """解析 --channels 参数：形如 "5,9"、"canfd:5,9" 或 "canfd:5,classic:3"。

    只写通道号时帧类型留空，由 DBC 自带的 VFrameFormat 自动识别；
    仅在需要强制指定时才写 "canfd:" / "classic:" 前缀。
    """
    groups = []                     # [[帧类型, [通道…]], …]
    current = None
    for token in (text or "").replace(" ", "").split(","):
        if not token:
            continue
        if ":" in token:
            kind, values = token.split(":", 1)
            kind = kind.lower()
            if kind not in KINDS:
                current = None
                continue
            current = [kind, [int(v) for v in values.split("+") if v.isdigit()]]
            groups.append(current)
        elif token.isdigit():
            if current is None:                     # 未写类型 → 自动
                current = ["", []]
                groups.append(current)
            current[1].append(int(token))
    return [(kind, channels) for kind, channels in groups if channels]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="%s — ASC Graphic Viewer" % APP_NAME)
    parser.add_argument("asc", nargs="?", help="要打开的 asc 文件或目录（目录取最新 asc）")
    parser.add_argument("--dbc", help="顺带加载该 DBC 文件（写入 dbc/channel_map.json）")
    parser.add_argument("--channels",
                        help="配合 --dbc 指定映射通道，形如 5,9（帧类型自动识别）"
                             "或 canfd:5,classic:3（强制指定）")
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    args = parser.parse_args(argv)

    setup_logging()                      # 日志：默认全量 DEBUG，写 logs/ 目录
    logger.debug("启动参数：asc=%r dbc=%r channels=%r", args.asc, args.dbc, args.channels)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    window = MainWindow()

    if args.dbc:
        path = args.dbc if os.path.isabs(args.dbc) else os.path.join(APP_DIR, args.dbc)
        if not os.path.exists(path) and os.path.exists(args.dbc):
            path = args.dbc
        try:
            window.dbc.load_dbc(path)
            for kind, channels in parse_channel_spec(args.channels):
                window.dbc.add_mapping(path, channels, kind=kind, comment="命令行加载")
            window.dbc.save_config()
            window._apply_dbc()
        except Exception as exc:                          # noqa: BLE001
            logger.error("命令行加载 DBC 失败：%s", exc, exc_info=True)
            print("DBC 加载失败：%s" % exc, file=sys.stderr)

    window.show()
    asc_path = resolve_asc_path(args.asc)
    if asc_path:
        window._open_asc(asc_path)
    elif args.asc:
        logger.warning("目录里没有找到 .asc 文件：%s", args.asc)
        QMessageBox.information(window, "提示",
                                "目录里没有找到 .asc 文件：\n%s" % args.asc)
    app.aboutToQuit.connect(lambda: logger.info("退出 %s", APP_NAME))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
