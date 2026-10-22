# -*- coding: utf-8 -*-
"""报文树：通道 → 报文 → 字节/位（懒加载画像）。

交互：
    * 双击报文 → 自动挑选该报文最有信息量的前 N 个字节加入图形
    * 展开报文 → 懒加载逐字节画像（常量/枚举/模拟），双击某字节加入图形
    * 右键 → 加入图形 / 加入该报文全部非常量字节 / 移除该报文的所有行 / 清空
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QApplication, QHeaderView, QMenu, QTreeWidget,
                               QTreeWidgetItem)

from signals import model as sigmodel

logger = logging.getLogger(__name__)

ROLE_KIND = Qt.ItemDataRole.UserRole
NODE_CHANNEL = "channel"
NODE_MESSAGE = "message"
NODE_BYTE = "byte"
NODE_DBC_SIGNAL = "dbc-signal"
MAX_BYTE_ITEMS = 24

KIND_LABELS = {"constant": "常量", "enum": "枚举", "analog": "模拟"}


class MessageTree(QTreeWidget):
    """左侧报文树。"""

    addMessageRequested = Signal(object)      # MessageInfo
    addSpecRequested = Signal(object)         # SignalSpec
    removeMessageRequested = Signal(object)   # MessageInfo
    clearRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["通道 / 报文 ID", "帧数", "长度", "分类 / DBC"])
        self.setColumnCount(4)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.setColumnWidth(0, 260)
        self.setUniformRowHeights(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.itemExpanded.connect(self._on_expanded)
        self.itemDoubleClicked.connect(self._on_double_clicked)

        self._reader = None
        self._library = None                 # DbcLibrary（可选）
        self._max_auto_rows = 6
        self._dbc_hits = 0                   # 本次填充里命中 DBC 的报文数（日志用）

    # ---------------------------------------------------------------- 数据
    def set_dbc_library(self, library) -> None:
        """挂上 DBC 库（可为 None）；挂上后报文会优先展示 DBC 信号。"""
        self._library = library
        if self._reader is not None:
            self.set_reader(self._reader)

    def set_reader(self, reader):
        self._reader = reader
        self.clear()
        if reader is None:
            return
        started = time.time()
        self._dbc_hits = 0
        for channel in reader.channels():
            rate = channel.frames / channel.duration if channel.duration > 0 else 0.0
            caption = ("ch%d" % channel.channel if self._library is None
                       else self._library.channel_caption(channel.channel))
            node = QTreeWidgetItem(self, [
                "%s %s" % (channel.kind.upper(), caption),
                "%d" % channel.frames, "", "%.0f 帧/秒" % rate])
            node.setData(0, ROLE_KIND, (NODE_CHANNEL, channel.kind, channel.channel))
            if self._library is not None:
                meta = self._library.channel_meta(channel.channel)
                tooltip = caption
                if meta.remark:
                    tooltip += "\n备注：" + meta.remark
                node.setToolTip(0, tooltip)
            node.setExpanded(False)
            self.addTopLevelItem(node)
            for msg in reader.catalog():
                if msg.kind != channel.kind or msg.channel != channel.channel:
                    continue
                dbc_message = self._dbc_message(msg)
                label = msg.id_text + ("（扩展）" if msg.extended else "")
                source = ""
                if dbc_message is not None:
                    self._dbc_hits += 1
                    label = "%s  %s" % (label, dbc_message.name)
                    source = "DBC:%s（%d 信号）" % (dbc_message.dbc_name,
                                                len(dbc_message.signals))
                child = QTreeWidgetItem(node, [
                    label, "%d" % msg.frames,
                    "%d~%d" % (msg.length_min, msg.length_max), source])
                child.setData(0, ROLE_KIND, (NODE_MESSAGE, msg))
                child.setData(1, ROLE_KIND + 1, dbc_message)
                QTreeWidgetItem(child, ["展开以加载信号…"])
                node.addChild(child)
        self.expandToDepth(0)
        logger.debug("报文树填充完成：%d 通道 / %d 报文 / %d 个 DBC 命中 | 耗时 %.0f ms",
                     self.topLevelItemCount(), len(reader.catalog()),
                     self._dbc_hits, (time.time() - started) * 1000.0)

    def _dbc_message(self, msg):
        if self._library is None:
            return None
        try:
            message, _conflicts = self._library.resolve_message(msg.kind, msg.channel,
                                                               msg.can_id)
        except Exception:                                # noqa: BLE001
            return None
        return message

    @property
    def reader(self):
        return self._reader

    # ---------------------------------------------------------------- 过滤
    def apply_filter(self, text: str) -> None:
        text = (text or "").strip().lower()

        def visit(item: QTreeWidgetItem) -> bool:
            """返回该节点是否有可见子节点。"""
            payload = item.data(0, ROLE_KIND)
            self_hit = (not text) or (text in item.text(0).lower())
            child_hit = False
            for index in range(item.childCount()):
                child_hit = visit(item.child(index)) or child_hit
            visible = self_hit or child_hit
            item.setHidden(not visible)
            if child_hit:
                item.setExpanded(True)
            return visible

        for index in range(self.topLevelItemCount()):
            visit(self.topLevelItem(index))

    # ---------------------------------------------------------------- 懒加载画像 / DBC 信号
    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        payload = item.data(0, ROLE_KIND)
        if not payload or payload[0] != NODE_MESSAGE or item.childCount() != 1:
            return
        if item.child(0).data(0, ROLE_KIND) is not None:
            return

        msg = payload[1]
        dbc_message = item.data(1, ROLE_KIND + 1)
        item.takeChildren()
        if dbc_message is not None:
            self._fill_dbc_signals(item, msg, dbc_message)
            return
        if self._reader is None:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            profile = self._reader.byte_profile(msg.kind, msg.channel, msg.can_id)
        finally:
            QApplication.restoreOverrideCursor()
        logger.debug("懒加载逐字节画像：%s ch%d（%d 个字段）",
                     msg.id_text, msg.channel, len(profile))

        by_byte = {entry["byte"]: entry for entry in profile}
        specs = sigmodel.auto_specs(profile, msg.key, max_rows=MAX_BYTE_ITEMS)
        for spec in sorted(specs, key=lambda item_: item_.byte_offset):
            entry = by_byte[spec.byte_offset]
            spec.name = "%s %s" % (msg.id_text, spec.field_text)
            text = "byte%-3d %-4s 取值 %-4d 范围 %d~%d" % (
                spec.byte_offset, KIND_LABELS.get(entry["kind"], entry["kind"]),
                entry["unique"], entry["min"], entry["max"])
            node = QTreeWidgetItem(item, [text])
            node.setData(0, ROLE_KIND, (NODE_BYTE, spec))
        if item.childCount() == 0:
            QTreeWidgetItem(item, ["（无可展示字段）"])

    def _fill_dbc_signals(self, item: QTreeWidgetItem, msg, dbc_message) -> None:
        """展开 DBC 报文：列出信号（名称/单位/取值域/物理范围）。"""
        if dbc_message.length != msg.length_max:
            logger.warning("DBC 报文长度与日志不一致：%s ch%d 0x%X → DBC %d / 日志 %d",
                           dbc_message.name, msg.channel, msg.can_id,
                           dbc_message.length, msg.length_max)
            warn = QTreeWidgetItem(item, [
                "⚠ DBC 报文长度 %d 与日志长度 %d 不一致，请确认映射"
                % (dbc_message.length, msg.length_max)])
            warn.setForeground(0, Qt.GlobalColor.darkYellow)
        for ref in dbc_message.signals:
            spec = ref.to_spec(msg.key)
            detail = ""
            if ref.choices:
                detail = "枚举 %d 项" % len(ref.choices)
            elif ref.minimum is not None and ref.maximum is not None:
                detail = "范围 %g~%g" % (ref.minimum, ref.maximum)
            text = "%-24s %-8s %-18s %s" % (
                ref.name, "[%s]" % ref.unit if ref.unit else "", ref.field_text, detail)
            node = QTreeWidgetItem(item, [text])
            node.setData(0, ROLE_KIND, (NODE_DBC_SIGNAL, spec))
        if item.childCount() == 0:
            QTreeWidgetItem(item, ["（该报文在 DBC 中没有信号定义）"])

    # ---------------------------------------------------------------- 交互
    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        payload = item.data(0, ROLE_KIND)
        if not payload:
            return
        kind, value = payload[0], payload[1]
        if kind == NODE_MESSAGE:
            self.addMessageRequested.emit(value)
        elif kind in (NODE_BYTE, NODE_DBC_SIGNAL):
            self.addSpecRequested.emit(value)

    def _on_context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            payload = item.data(0, ROLE_KIND)
            if payload and payload[0] == NODE_MESSAGE:
                msg = payload[1]
                menu.addAction("加入图形（自动选 %d 个信号）" % self._max_auto_rows,
                               lambda: self.addMessageRequested.emit(msg))
                dbc_message = item.data(1, ROLE_KIND + 1)
                if dbc_message is not None:
                    menu.addAction("加入该报文全部 DBC 信号（%d 个）"
                                   % len(dbc_message.signals),
                                   lambda: self._add_dbc_signals(msg, dbc_message))
                else:
                    menu.addAction("加入该报文全部非常量字节",
                                   lambda: self._add_all_analog(msg))
                menu.addAction("移除该报文的所有行",
                               lambda: self.removeMessageRequested.emit(msg))
            elif payload and payload[0] in (NODE_BYTE, NODE_DBC_SIGNAL):
                menu.addAction("加入图形", lambda: self.addSpecRequested.emit(payload[1]))
            elif payload and payload[0] == NODE_CHANNEL:
                menu.addAction("展开该通道全部报文",
                               lambda: item.setExpanded(True))
        menu.addSeparator()
        menu.addAction("清空图形", self.clearRequested.emit)
        menu.exec(self.viewport().mapToGlobal(pos))

    def _add_dbc_signals(self, msg, dbc_message) -> None:
        specs = sigmodel.apply_colors([ref.to_spec(msg.key)
                                       for ref in dbc_message.signals])
        if specs:
            self.addSpecRequested.emit(specs)

    def _add_all_analog(self, msg) -> None:
        if self._reader is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            profile = self._reader.byte_profile(msg.kind, msg.channel, msg.can_id)
        finally:
            QApplication.restoreOverrideCursor()
        specs = sigmodel.apply_colors(
            sigmodel.auto_specs(profile, msg.key, max_rows=MAX_BYTE_ITEMS))
        for spec in specs:
            spec.name = "%s %s" % (msg.id_text, spec.field_text)
        if specs:
            self.addSpecRequested.emit(specs)
