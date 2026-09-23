# -*- coding: utf-8 -*-
"""通用弹窗：DBC 通道映射选择等。"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout)

from signals.dbc_loader import DBC_SUFFIXES, KIND_TEXTS


class DbcMappingDialog(QDialog):
    """选择 DBC 文件并指定它作用于哪些通道。

    帧类型**不需要用户选择**：DBC 自己带着（Vector `VFrameFormat` 属性），
    加载时自动识别，本对话框会把识别结果显示出来。
    需要用户决定的只有"这个 DBC 描述的是哪几条总线"——即通道映射，
    因为一个 DBC 通常只描述一条总线，而 ASC 里有多个通道。
    """

    def __init__(self, dbc_dir: str, channels, parent=None, preset_dbc: str = "",
                 library=None):
        super().__init__(parent)
        self.setWindowTitle("加载 DBC 并映射通道")
        self.resize(600, 500)
        self._dbc_dir = dbc_dir
        self._library = library              # DbcLibrary：复用已解析结果（可为 None）
        self._summaries = {}                 # 绝对路径 -> (摘要文本, 帧类型, 解析错误)
        self._channels = self._sorted_channels(channels)

        self.path_edit = QLineEdit(preset_dbc)
        self.path_edit.setReadOnly(True)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._choose_file)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)

        self.discovered = QComboBox()
        self.discovered.addItem("（选择 dbc/ 目录下的文件）", "")
        for path in self._discover():
            self.discovered.addItem(os.path.basename(path), path)
        self.discovered.currentIndexChanged.connect(self._on_pick_discovered)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color:#444; background:#f6f6f6; padding:4px;")

        self.channel_list = QListWidget()
        self.channel_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.channel_list.itemChanged.connect(lambda _item: self._refresh_summary())
        self._fill_channels()

        self.comment_edit = QLineEdit()
        self.comment_edit.setPlaceholderText("备注（可选），例如：CH5 整车网络")

        form = QFormLayout()
        form.addRow("dbc/ 目录文件", self.discovered)
        form.addRow("DBC 文件", path_row)
        form.addRow("DBC 自带的帧类型", self.summary_label)
        form.addRow("作用通道", self.channel_list)
        form.addRow("备注", self.comment_edit)

        hint = QLabel("提示：帧类型由 DBC 自动识别（VFrameFormat），无需选择；"
                      "同一 DBC 可重复加载到不同通道，多个 DBC 可以同时生效。"
                      "同一报文 ID 被多个 DBC 命中时以第一个为准并在状态栏提示。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("加载并映射")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addStretch(1)
        layout.addWidget(buttons)

        self._refresh_summary()

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _sorted_channels(channels) -> list:
        """(帧类型, 通道, 帧数) → 按帧类型 + 通道号排序。"""
        entries = [(str(kind), int(channel), int(frames))
                   for kind, channel, frames in (channels or [])]
        return sorted(entries, key=lambda item: (item[0], item[1]))

    def _discover(self) -> list:
        if not os.path.isdir(self._dbc_dir):
            return []
        return sorted(os.path.join(self._dbc_dir, name)
                      for name in os.listdir(self._dbc_dir)
                      if name.endswith(DBC_SUFFIXES))

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 DBC 文件", self._dbc_dir,
                                              "DBC 文件 (*.dbc *.DBC);;所有文件 (*)")
        if path:
            self.path_edit.setText(path)
            self._fill_channels()             # 换成该 DBC 已映射过的通道做预勾选
            self._refresh_summary()

    def _on_pick_discovered(self, _index: int) -> None:
        self.path_edit.setText(self.discovered.currentData() or "")
        self._fill_channels()
        self._refresh_summary()

    def _fill_channels(self) -> None:
        """列出全部通道（带帧类型与帧数），已映射过的通道预勾选。"""
        self.channel_list.clear()
        preset = set(self._library.channels_for(self.path_edit.text())) \
            if (self._library is not None and self.path_edit.text().strip()) else set()
        if not self._channels:
            placeholder = QListWidgetItem("（尚未打开 ASC 文件，无法选择通道）")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.channel_list.addItem(placeholder)
            return
        for kind, channel, frames in self._channels:
            item = QListWidgetItem("%s ch%d    %s 帧" % (
                KIND_TEXTS.get(kind, kind.upper()), channel, "{:,}".format(frames)))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if channel in preset
                               else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, channel)
            item.setData(Qt.ItemDataRole.UserRole + 1, kind)
            self.channel_list.addItem(item)

    def _describe_dbc(self, path: str) -> tuple:
        """解析 DBC（走 library 缓存）→ (摘要文本, 帧类型, 错误信息)。"""
        key = os.path.abspath(path)
        if key in self._summaries:
            return self._summaries[key]
        try:
            if self._library is not None:
                file = self._library.load_dbc(path)
            else:
                from signals.dbc_loader import DbcFile
                file = DbcFile(path)
            result = (file.describe(), file.auto_kind, "")
        except Exception as exc:                          # noqa: BLE001
            result = ("", "", "%s: %s" % (type(exc).__name__, exc))
        self._summaries[key] = result
        return result

    def _refresh_summary(self) -> None:
        """显示 DBC 自带的帧格式与规模；顺带检查与已选通道是否匹配。"""
        path = self.path_edit.text().strip()
        if not path:
            self.summary_label.setStyleSheet(
                "color:#666; background:#f6f6f6; padding:4px;")
            self.summary_label.setText("选择 DBC 文件后，这里显示它自带的帧类型（自动识别，无需手选）")
            return
        text, kind, error = self._describe_dbc(path)
        if error:
            self.summary_label.setStyleSheet(
                "color:#b00; background:#fdf2f2; padding:4px;")
            self.summary_label.setText("解析失败：%s" % error)
            return
        line = "自动识别：%s" % (KIND_TEXTS.get(kind, "未标注（按任意帧类型匹配）"))
        mismatch = self._mismatched_channels(kind)
        if mismatch:
            line += "\n⚠ 已选通道 %s 不是 %s，DBC 不会作用于这些通道" % (
                "、".join("ch%d" % c for c in mismatch), KIND_TEXTS.get(kind, kind))
        self.summary_label.setStyleSheet(
            "color:#b06000; background:#fff8e8; padding:4px;" if mismatch else
            "color:#256029; background:#f2faf2; padding:4px;")
        self.summary_label.setText("%s\n%s" % (line, text))

    def _mismatched_channels(self, kind: str) -> list:
        """已勾选但帧类型与 DBC 不符的通道（kind 为空表示不限制）。"""
        if not kind:
            return []
        kinds = {channel: entry_kind for entry_kind, channel, _frames in self._channels}
        return [channel for channel in self.checked_channels()
                if kinds.get(channel) and kinds[channel] != kind]

    def checked_channels(self) -> list:
        out = []
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return sorted(out)

    def _on_accept(self) -> None:
        if not self.path_edit.text().strip():
            self.path_edit.setFocus()
            return
        if not self.checked_channels():
            self.channel_list.setFocus()
            return
        self.accept()

    # ---------------------------------------------------------------- 结果
    def result_mapping(self) -> tuple:
        """返回 (DBC 路径, 通道列表, 备注)；帧类型由加载方自动识别。"""
        return (self.path_edit.text().strip(), self.checked_channels(),
                self.comment_edit.text().strip())
