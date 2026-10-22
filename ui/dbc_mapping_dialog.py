# -*- coding: utf-8 -*-
"""DBC 映射工作台：左侧 DBC 库，右侧通道，拖拽完成映射。

设计要点
--------
* `dbc/` 目录就是默认 DBC 库；对话框只展示目录内文件（以及配置里引用的缺失文件）；
* 拖 DBC 到通道即建立映射；同一通道可放多个 DBC，同一 DBC 可拖到多条通道；
* 通道名称/备注行内编辑，随 `channel_map.json` v2 持久化；
* 从资源管理器拖 `.dbc` 进来会自动复制到 `dbc/`，同名文件询问覆盖或跳过；
* 所有编辑先进入待提交状态，只有“应用”才写配置；“取消”不落盘。
* 帧类型仍由 DBC 的 VFrameFormat 自动识别，不提供手选。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil

from PySide6.QtCore import QMimeData, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QDrag, QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog,
                               QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox,
                               QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QToolButton, QVBoxLayout, QWidget)

from signals.channel_catalog import default_channels
from signals.dbc_loader import DBC_SUFFIXES, KIND_TEXTS, DbcLibrary

logger = logging.getLogger(__name__)

DBC_MIME = "application/x-benecan-dbc"
MAX_VISIBLE_CHIPS = 2
META_KIND = {"canfd": "CANFD", "classic": "经典", "lin": "LIN"}


def _repolish(widget: QWidget) -> None:
    """属性变化后让 QSS 重新生效。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _kind_short(kind: str) -> str:
    return META_KIND.get(kind, kind.upper() if kind else "")


def _unique_paths(paths) -> list:
    seen, out = set(), []
    for path in paths or []:
        if not path:
            continue
        absolute = os.path.abspath(path)
        key = os.path.normcase(absolute)
        if key in seen:
            continue
        seen.add(key)
        out.append(absolute)
    return out


def _paths_from_mime(mime: QMimeData) -> list:
    """从内部拖拽 MIME 或资源管理器文件 URL 中取 .dbc 路径。"""
    paths = []
    if mime.hasFormat(DBC_MIME):
        try:
            data = json.loads(bytes(mime.data(DBC_MIME)).decode("utf-8"))
            paths.extend(data if isinstance(data, list) else [data])
        except (TypeError, ValueError, UnicodeDecodeError):
            pass
    if mime.hasUrls():
        paths.extend(url.toLocalFile() for url in mime.urls() if url.isLocalFile())
    return _unique_paths([path for path in paths
                          if str(path).lower().endswith(DBC_SUFFIXES)])


class EditableLineEdit(QLineEdit):
    """回车/失焦提交，Esc 交给外层还原；不接受拖放，避免抢通道行的 drop。"""

    escapePressed = Signal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setAcceptDrops(False)
        self.setDragEnabled(False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.escapePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class DbcListItemWidget(QWidget):
    """左侧 DBC 卡片内容。鼠标事件穿透给 QListWidget，保证选择/拖拽正常。

    解析完成后只更新内容，不重新调用 setItemWidget()——重复设置 item widget
    会留下旧的子控件叠在同一位置，表现为文字重影/裁切。
    """

    def __init__(self, name: str, kind: str, kind_text: str, stats: str,
                 map_text: str, error: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("dbcItem")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(62)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(1)

        self.name_label = QLabel(name)
        self.name_label.setObjectName("dbcName")
        self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        layout.addWidget(self.name_label)

        self.meta_widget = QWidget()
        self.meta_widget.setObjectName("dbcMetaRow")
        meta_row = QHBoxLayout(self.meta_widget)
        meta_row.setContentsMargins(0, 0, 0, 0)
        meta_row.setSpacing(5)
        self.kind_label = QLabel()
        self.kind_label.setObjectName("kindBadge")
        meta_row.addWidget(self.kind_label)
        self.stats_label = QLabel()
        self.stats_label.setObjectName("dbcStats")
        self.stats_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Preferred)
        meta_row.addWidget(self.stats_label, 1)
        layout.addWidget(self.meta_widget)

        self.map_label = QLabel()
        self.map_label.setObjectName("dbcMap")
        self.map_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                     QSizePolicy.Policy.Preferred)
        layout.addWidget(self.map_label)

        self.error_label = QLabel()
        self.error_label.setObjectName("dbcError")
        self.error_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Preferred)
        layout.addWidget(self.error_label)

        self.update_content(kind, kind_text, stats, map_text, error)
        # 标签/子布局会挡住 QListWidget 的鼠标事件，导致“选得中但拖不动”；
        # 让整张卡片（含所有子控件）对鼠标透明，事件直接落到列表 viewport。
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def update_content(self, kind: str, kind_text: str, stats: str,
                       map_text: str, error: str = "") -> None:
        self.name_label.setProperty("error", bool(error))
        _repolish(self.name_label)
        if error:
            self.meta_widget.setVisible(False)
            self.map_label.setVisible(False)
            self.error_label.setText(error)
            self.error_label.setVisible(True)
            return
        self.meta_widget.setVisible(True)
        self.map_label.setVisible(True)
        self.error_label.setVisible(False)
        self.kind_label.setText(kind_text or "待解析")
        self.kind_label.setProperty("kind", kind or "pending")
        _repolish(self.kind_label)
        self.stats_label.setText(stats)
        self.map_label.setText(map_text)
        self.map_label.setProperty("unmapped", not map_text.startswith("→"))
        _repolish(self.map_label)


class DbcListWidget(QListWidget):
    """只负责选择与起拖；条目内容由 DbcListItemWidget 渲染。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dbcList")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(False)
        self.setUniformItemSizes(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def startDrag(self, supported_actions):        # noqa: N802 - Qt 命名
        items = [item for item in self.selectedItems()
                 if not item.data(Qt.ItemDataRole.UserRole + 1)]
        paths = [item.data(Qt.ItemDataRole.UserRole) for item in items]
        paths = [path for path in paths if path]
        if not paths:
            logger.debug("DBC 拖拽未开始：没有可拖拽的选中项")
            return
        logger.debug("开始拖动 DBC：%s", [os.path.basename(path) for path in paths])
        mime = QMimeData()
        mime.setData(DBC_MIME, json.dumps(paths).encode("utf-8"))
        mime.setText("\n".join(paths))
        drag = QDrag(self)
        drag.setMimeData(mime)
        current = self.currentItem() or items[0]
        rect = self.visualItemRect(current)
        if rect.isValid():
            drag.setPixmap(self.viewport().grab(rect))
            drag.setHotSpot(rect.center())
        drag.exec(Qt.DropAction.CopyAction)


class ChannelRow(QFrame):
    """一条通道：状态点 / 编号 / 名称 / 帧信息 / 映射 chips / 备注 / 清空。"""

    changed = Signal(int, str, str)         # channel, field(name/remark), value
    rowClicked = Signal(int, bool)          # channel, ctrl
    clearRequested = Signal(int)
    removeRequested = Signal(int, str)      # channel, dbc absolute path
    dropRequested = Signal(int, list)       # channel, [dbc absolute paths]

    def __init__(self, channel: int, name: str, remark: str, meta_text: str,
                 warn_text: str, mappings: list, summaries: dict,
                 channel_kind: str, parent=None):
        super().__init__(parent)
        self.channel = int(channel)
        self._name = name or ""
        self._remark = remark or ""
        self._mappings = list(mappings or [])
        self._summaries = summaries or {}
        self._channel_kind = channel_kind or ""

        self.setObjectName("channelRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self.dot = QLabel("●")
        self.dot.setObjectName("chDot")
        self.dot.setFixedWidth(10)
        self.dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.dot)

        self.ch_label = QLabel("ch%d" % self.channel)
        self.ch_label.setObjectName("chId")
        self.ch_label.setFixedWidth(38)
        self.ch_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.ch_label)

        self.name_edit = EditableLineEdit(self._name)
        self.name_edit.setObjectName("chName")
        self.name_edit.setFixedWidth(120)
        self.name_edit.setPlaceholderText("通道名称")
        self.name_edit.setToolTip("通道名称（可编辑）")
        layout.addWidget(self.name_edit)

        self.meta_label = QLabel(meta_text + ("  ⚠" if warn_text else ""))
        self.meta_label.setObjectName("chMeta")
        self.meta_label.setProperty("warn", bool(warn_text))
        self.meta_label.setFixedWidth(110)
        self.meta_label.setToolTip(warn_text)
        self.meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.meta_label)

        self.map_area = QWidget()
        self.map_area.setObjectName("chMap")
        self.map_area.setMinimumWidth(0)
        self.map_area.setSizePolicy(QSizePolicy.Policy.Ignored,
                                    QSizePolicy.Policy.Preferred)
        self.map_layout = QHBoxLayout(self.map_area)
        self.map_layout.setContentsMargins(0, 0, 0, 0)
        self.map_layout.setSpacing(4)
        layout.addWidget(self.map_area, 1)

        self.remark_edit = EditableLineEdit(self._remark)
        self.remark_edit.setObjectName("chRemark")
        self.remark_edit.setFixedWidth(130)
        self.remark_edit.setPlaceholderText("备注（可选）")
        self.remark_edit.setToolTip("通道备注（可编辑）")
        layout.addWidget(self.remark_edit)

        self.clear_btn = QToolButton()
        self.clear_btn.setObjectName("rowClear")
        self.clear_btn.setText("清空")
        self.clear_btn.setFixedWidth(34)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setToolTip("清空本通道的全部映射")
        layout.addWidget(self.clear_btn)

        self.name_edit.editingFinished.connect(self._commit_name)
        self.remark_edit.editingFinished.connect(self._commit_remark)
        self.name_edit.escapePressed.connect(self._revert_name)
        self.remark_edit.escapePressed.connect(self._revert_remark)
        self.clear_btn.clicked.connect(lambda: self.clearRequested.emit(self.channel))

        self._rebuild_chips()
        self._update_dot(warn_text)

    # ------------------------------------------------------------ 状态
    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", bool(selected))
        _repolish(self)

    def set_drop_active(self, active: bool) -> None:
        self.setProperty("dropActive", bool(active))
        _repolish(self)

    def _update_dot(self, warn_text: str = "") -> None:
        state = "warn" if warn_text else ("mapped" if self._mappings else "empty")
        self.dot.setProperty("state", state)
        _repolish(self.dot)

    def commit(self) -> None:
        self._commit_name()
        self._commit_remark()

    def _commit_name(self) -> None:
        value = self.name_edit.text().strip()
        if value != self.name_edit.text():
            self.name_edit.setText(value)
        if value == self._name:
            return
        self._name = value
        self.changed.emit(self.channel, "name", value)

    def _commit_remark(self) -> None:
        value = self.remark_edit.text().strip()
        if value != self.remark_edit.text():
            self.remark_edit.setText(value)
        if value == self._remark:
            return
        self._remark = value
        self.changed.emit(self.channel, "remark", value)

    def _revert_name(self) -> None:
        self.name_edit.setText(self._name)
        self.name_edit.clearFocus()

    def _revert_remark(self) -> None:
        self.remark_edit.setText(self._remark)
        self.remark_edit.clearFocus()

    # ------------------------------------------------------------ chips
    def _rebuild_chips(self) -> None:
        while self.map_layout.count():
            item = self.map_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.clear_btn.setText("清空" if self._mappings else "")
        self.clear_btn.setEnabled(bool(self._mappings))
        if not self._mappings:
            hint = QLabel("拖入 DBC")
            hint.setObjectName("dropHint")
            hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.map_layout.addWidget(hint)
            self.map_layout.addStretch(1)
            return

        visible = self._mappings[:MAX_VISIBLE_CHIPS]
        for path in visible:
            self.map_layout.addWidget(self._make_chip(path))
        if len(self._mappings) > MAX_VISIBLE_CHIPS:
            more = QToolButton()
            more.setObjectName("chipMore")
            more.setText("+%d" % (len(self._mappings) - MAX_VISIBLE_CHIPS))
            more.setToolTip("更多映射：" + "、".join(
                os.path.basename(path) for path in self._mappings[MAX_VISIBLE_CHIPS:]))
            more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(more)
            for path in self._mappings[MAX_VISIBLE_CHIPS:]:
                action = menu.addAction("移除  " + os.path.basename(path))
                action.triggered.connect(
                    lambda _checked=False, p=path: self.removeRequested.emit(self.channel, p))
            more.setMenu(menu)
            self.map_layout.addWidget(more)
        self.map_layout.addStretch(1)

    def _make_chip(self, path: str) -> QToolButton:
        summary = self._summaries.get(os.path.abspath(path), {})
        dbc_kind = summary.get("kind", "")
        mismatch = bool(self._channel_kind and dbc_kind
                        and dbc_kind != self._channel_kind)
        name = os.path.basename(path)
        if len(name) > 24:
            name = name[:12] + "…" + name[-10:]
        button = QToolButton()
        button.setObjectName("chip")
        button.setText(name + "  ×")
        button.setProperty("warn", mismatch)
        button.setToolTip("%s\n%s\n点击移除该映射" % (
            path, "帧类型与通道不符" if mismatch else "点击移除"))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        button.setMinimumWidth(54)
        button.setMaximumWidth(150)
        button.clicked.connect(
            lambda _checked=False, p=path: self.removeRequested.emit(self.channel, p))
        return button

    # ------------------------------------------------------------ 拖放
    def _can_accept(self, mime: QMimeData) -> bool:
        return bool(_paths_from_mime(mime))

    def dragEnterEvent(self, event):        # noqa: N802 - Qt 命名
        if self._can_accept(event.mimeData()):
            event.acceptProposedAction()
            self.set_drop_active(True)
            logger.debug("ch%d 接受 DBC 拖入：%s", self.channel,
                         [os.path.basename(path) for path in _paths_from_mime(event.mimeData())])
        else:
            event.ignore()

    def dragMoveEvent(self, event):         # noqa: N802 - Qt 命名
        if self._can_accept(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):        # noqa: N802 - Qt 命名
        self.set_drop_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):             # noqa: N802 - Qt 命名
        self.set_drop_active(False)
        paths = _paths_from_mime(event.mimeData())
        if not paths:
            logger.debug("ch%d 的 drop 被忽略：没有有效 DBC 路径", self.channel)
            event.ignore()
            return
        logger.debug("ch%d 收到 DBC 拖放：%s", self.channel,
                     [os.path.basename(path) for path in paths])
        event.acceptProposedAction()
        self.dropRequested.emit(self.channel, paths)

    def mousePressEvent(self, event):       # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.rowClicked.emit(
                self.channel, bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))
        super().mousePressEvent(event)


class DbcMappingDialog(QDialog):
    """DBC 映射设置对话框。"""

    def __init__(self, dbc_dir: str, channels, parent=None, library=None):
        super().__init__(parent)
        self.setWindowTitle("DBC 映射设置")
        self.resize(1180, 780)
        self.setMinimumSize(1024, 640)
        self.setAcceptDrops(True)
        self.setFont(QFont("Microsoft YaHei UI", 9))

        self._dbc_dir = os.path.abspath(dbc_dir)
        self._has_asc = bool(channels)
        self._channel_info = {}
        for kind, channel, frames in channels or []:
            self._channel_info[int(channel)] = (str(kind), int(frames))

        self._library = library or DbcLibrary.open(self._dbc_dir)
        self._library.ensure_channels(self._channel_info)
        for mapping in self._library.mappings:
            self._library.ensure_channels(mapping.channels)

        self._summaries = {}                # 绝对路径 -> 解析摘要
        for file in self._library.loaded_files:
            self._summaries[os.path.abspath(file.path)] = {
                "kind": file.auto_kind,
                "msgs": len(file.messages),
                "signals": file.signal_count,
                "describe": file.describe(),
                "error": "",
            }
        for path, error in self._library.errors.items():
            self._summaries[os.path.abspath(path)] = {
                "kind": "", "msgs": 0, "signals": 0,
                "describe": "", "error": error,
            }
        self._dirty = False
        self._selected_channels = set()
        self._rows = {}
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._refresh_status)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._restore_channel_scroll)
        self._scroll_target = 0

        self._all_channels = sorted(self._library.channels)
        self._names = {ch: self._library.channel_meta(ch).name
                       for ch in self._all_channels}
        self._remarks = {ch: self._library.channel_meta(ch).remark
                         for ch in self._all_channels}
        self._mappings = {ch: self._mapping_paths(ch) for ch in self._all_channels}
        self._dbc_paths = []
        self._discover_dbc_paths()

        self._build_ui()
        self._apply_style()
        self._refresh_dbc_list()
        self._refresh_channel_rows()
        self._update_footer()

        # 供主窗口读取（应用后显示摘要）
        self.applied_summary = {}

    # ------------------------------------------------------------ 初始化数据
    def _mapping_paths(self, channel: int) -> list:
        paths = []
        for mapping in self._library.mappings_for_channel(channel):
            paths.append(self._library.resolve_path(mapping.dbc))
        return _unique_paths(paths)

    def _discover_dbc_paths(self) -> None:
        paths = set(self._library.discover())
        for channel_paths in self._mappings.values():
            paths.update(os.path.abspath(path) for path in channel_paths)
        self._dbc_paths = sorted(paths, key=lambda path: (
            os.path.basename(path).lower(), path.lower()))

    def _mapped_channels_for(self, path: str) -> list:
        key = os.path.normcase(os.path.abspath(path))
        channels = []
        for channel in self._all_channels:
            if any(os.path.normcase(os.path.abspath(item)) == key
                   for item in self._mappings.get(channel, [])):
                channels.append(channel)
        return channels

    def _is_in_dbc_dir(self, path: str) -> bool:
        try:
            return os.path.commonpath(
                [os.path.abspath(path), self._dbc_dir]) == self._dbc_dir
        except ValueError:
            return False

    # ------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("dialogHeader")
        header.setFixedHeight(46)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 9, 12, 9)
        header_layout.setSpacing(8)

        path_label = QLabel("dbc/ 目录")
        path_label.setObjectName("pathLabel")
        header_layout.addWidget(path_label)

        self.path_value = QLabel(self._dbc_dir)
        self.path_value.setObjectName("pathValue")
        self.path_value.setToolTip(self._dbc_dir)
        self.path_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_value.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        header_layout.addWidget(self.path_value, 1)

        self.open_dir_btn = QPushButton("打开目录")
        self.open_dir_btn.setObjectName("normalButton")
        self.open_dir_btn.clicked.connect(self._open_dir)
        header_layout.addWidget(self.open_dir_btn)

        self.rescan_btn = QPushButton("重新扫描")
        self.rescan_btn.setObjectName("normalButton")
        self.rescan_btn.clicked.connect(self._rescan)
        header_layout.addWidget(self.rescan_btn)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchEdit")
        self.search_edit.setPlaceholderText("搜索 DBC…")
        self.search_edit.setFixedWidth(180)
        self.search_edit.textChanged.connect(self._apply_filter)
        header_layout.addWidget(self.search_edit)
        root.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        # 左：DBC 库
        left = QWidget()
        left.setObjectName("leftPanel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        left_head = QWidget()
        left_head.setObjectName("paneHead")
        left_head.setFixedHeight(38)
        left_head_layout = QHBoxLayout(left_head)
        left_head_layout.setContentsMargins(12, 0, 12, 0)
        left_head_layout.setSpacing(6)
        left_title = QLabel("DBC 库")
        left_title.setObjectName("paneTitle")
        left_head_layout.addWidget(left_title)
        self.dbc_count_label = QLabel("0")
        self.dbc_count_label.setObjectName("countBadge")
        left_head_layout.addWidget(self.dbc_count_label)
        left_head_layout.addStretch(1)
        left_hint = QLabel("拖到右侧通道")
        left_hint.setObjectName("hintLabel")
        left_head_layout.addWidget(left_hint)
        left_layout.addWidget(left_head)

        self.dbc_list = DbcListWidget()
        self.dbc_list.currentItemChanged.connect(self._on_current_dbc_changed)
        left_layout.addWidget(self.dbc_list, 1)

        note = QLabel("从资源管理器拖入 .dbc 会自动复制到 dbc/；同名文件询问覆盖/跳过。")
        note.setObjectName("panelNote")
        note.setWordWrap(True)
        note.setContentsMargins(12, 7, 12, 7)
        left_layout.addWidget(note)
        splitter.addWidget(left)

        # 右：通道
        right = QWidget()
        right.setObjectName("rightPanel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        right_head = QWidget()
        right_head.setObjectName("paneHead")
        right_head.setFixedHeight(38)
        right_head_layout = QHBoxLayout(right_head)
        right_head_layout.setContentsMargins(12, 0, 12, 0)
        right_head_layout.setSpacing(6)
        right_title = QLabel("通道")
        right_title.setObjectName("paneTitle")
        right_head_layout.addWidget(right_title)
        self.channel_count_label = QLabel("0")
        self.channel_count_label.setObjectName("countBadge")
        right_head_layout.addWidget(self.channel_count_label)
        right_head_layout.addStretch(1)
        right_hint = QLabel("拖动 DBC 到通道 · Ctrl 点击通道行可多选")
        right_hint.setObjectName("hintLabel")
        right_head_layout.addWidget(right_hint)
        right_layout.addWidget(right_head)

        self.channel_scroll = QScrollArea()
        self.channel_scroll.setObjectName("channelScroll")
        self.channel_scroll.setWidgetResizable(True)
        self.channel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.channel_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.channel_scroll.setAcceptDrops(False)
        self.channel_scroll.viewport().setAcceptDrops(False)

        self.channel_container = QWidget()
        self.channel_container.setObjectName("channelContainer")
        self.channel_layout = QVBoxLayout(self.channel_container)
        self.channel_layout.setContentsMargins(0, 0, 0, 0)
        self.channel_layout.setSpacing(0)
        self.channel_layout.addStretch(1)
        self.channel_scroll.setWidget(self.channel_container)
        right_layout.addWidget(self.channel_scroll, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 800])

        # 底栏
        footer = QWidget()
        footer.setObjectName("dialogFooter")
        footer.setFixedHeight(52)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 8, 12, 8)
        footer_layout.setSpacing(10)

        self.stat_label = QLabel()
        self.stat_label.setObjectName("statLabel")
        footer_layout.addWidget(self.stat_label)

        self.status_label = QLabel()
        self.status_label.setObjectName("statusText")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                        QSizePolicy.Policy.Preferred)
        footer_layout.addWidget(self.status_label, 1)

        self.reset_btn = QPushButton("恢复默认模板")
        self.reset_btn.setObjectName("ghostButton")
        self.reset_btn.clicked.connect(self._on_reset_template)
        footer_layout.addWidget(self.reset_btn)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("normalButton")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.clicked.connect(self.reject)
        footer_layout.addWidget(self.cancel_btn)

        self.apply_btn = QPushButton("应用")
        self.apply_btn.setObjectName("primaryButton")
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self._on_accept)
        footer_layout.addWidget(self.apply_btn)
        root.addWidget(footer)

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QDialog { background: #ffffff; }
            QWidget#dialogHeader { background: #fbfcfd; border-bottom: 1px solid #dbe3e8; }
            QWidget#dialogFooter { background: #fbfcfd; border-top: 1px solid #dbe3e8; }
            QLabel#pathLabel { color: #83929a; font-size: 10px; }
            QLabel#pathValue {
                color: #52636c; background: #eef4f6; border: 1px solid #dbe3e8;
                border-radius: 4px; padding: 4px 7px; font-family: Consolas;
                font-size: 10px;
            }
            QLineEdit#searchEdit {
                border: 1px solid #dbe3e8; border-radius: 4px;
                padding: 4px 7px; background: #ffffff; color: #16232a;
            }
            QLineEdit#searchEdit:focus { border-color: #147688; }
            QPushButton#normalButton {
                border: 1px solid #c4d0d7; border-radius: 4px;
                background: #ffffff; color: #16232a; padding: 4px 10px;
            }
            QPushButton#normalButton:hover { background: #f3f7f9; border-color: #aebdc5; }
            QPushButton#primaryButton {
                border: 1px solid #147688; border-radius: 4px;
                background: #147688; color: #ffffff; padding: 5px 15px;
                font-weight: 600;
            }
            QPushButton#primaryButton:hover { background: #0c5f6e; border-color: #0c5f6e; }
            QPushButton#ghostButton {
                border: 0; background: transparent; color: #52636c; padding: 5px 8px;
            }
            QPushButton#ghostButton:hover { background: #eef4f6; color: #16232a; }
            QWidget#leftPanel { background: #f7fafb; }
            QWidget#rightPanel { background: #ffffff; }
            QWidget#paneHead { background: #fbfcfd; border-bottom: 1px solid #dbe3e8; }
            QWidget#leftPanel QWidget#paneHead { background: #f7fafb; }
            QLabel#paneTitle { font-size: 11px; font-weight: 700; color: #16232a; }
            QLabel#countBadge {
                color: #0f5c6a; background: #e6f3f6; border: 1px solid #b7d9e0;
                border-radius: 8px; padding: 1px 7px; font-family: Consolas;
                font-size: 9px; font-weight: 600;
            }
            QLabel#hintLabel { color: #83929a; font-size: 9px; }
            QLabel#panelNote { color: #83929a; font-size: 9px; background: #f7fafb; }
            QListWidget#dbcList {
                background: #f7fafb; border: 0; outline: 0; padding: 4px;
            }
            QListWidget#dbcList::item {
                border: 1px solid transparent; border-radius: 6px;
                margin: 2px 4px; padding: 0; background: transparent;
            }
            QListWidget#dbcList::item:hover { background: #f1f8fa; }
            QListWidget#dbcList::item:selected {
                background: #e6f3f6; border: 1px solid #b7d9e0;
            }
            QWidget#dbcItem { background: transparent; }
            QLabel#dbcName { color: #16232a; font-size: 10px; font-weight: 600; }
            QLabel#dbcName[error="true"] { color: #b42318; }
            QLabel#dbcError { color: #b42318; font-size: 8.5px; }
            QLabel#dbcStats { color: #83929a; font-size: 8.5px; }
            QLabel#dbcMap { color: #0f5c6a; font-size: 8.5px; }
            QLabel#dbcMap[unmapped="true"] { color: #93a3aa; }
            QLabel#kindBadge {
                border-radius: 3px; padding: 0 4px; font-size: 8px; font-weight: 600;
            }
            QLabel#kindBadge[kind="canfd"] {
                color: #0f5c6a; background: #e2f2f5; border: 1px solid #b7d9e0;
            }
            QLabel#kindBadge[kind="classic"] {
                color: #5d6570; background: #eef1f3; border: 1px solid #d7dee2;
            }
            QLabel#kindBadge[kind="lin"] {
                color: #6b4c00; background: #fff5e0; border: 1px solid #ecd39a;
            }
            QLabel#kindBadge[kind="pending"] {
                color: #8a6500; background: #fff6df; border: 1px solid #eed59b;
            }
            QLabel#kindBadge[kind="auto"] {
                color: #52636c; background: #eef1f3; border: 1px solid #d7dee2;
            }
            QScrollArea#channelScroll { background: #ffffff; border: 0; }
            QWidget#channelContainer { background: #ffffff; }
            QFrame#channelRow {
                background: #ffffff; border-bottom: 1px solid #eef2f4;
            }
            QFrame#channelRow:hover { background: #f8fbfc; }
            QFrame#channelRow[selected="true"] {
                background: #edf8fa; border-left: 3px solid #147688;
            }
            QFrame#channelRow[dropActive="true"] {
                background: #e2f4f7; border: 1px solid #147688;
            }
            QLabel#chDot { color: #c0ccd2; font-size: 8px; }
            QLabel#chDot[state="mapped"] { color: #147688; }
            QLabel#chDot[state="warn"] { color: #9a5b00; }
            QLabel#chId {
                color: #52636c; font-family: Consolas; font-size: 9px; font-weight: 600;
            }
            QLabel#chMeta { color: #83929a; font-size: 8.5px; }
            QLabel#chMeta[warn="true"] { color: #9a5b00; }
            QLineEdit#chName, QLineEdit#chRemark {
                border: 1px solid transparent; border-radius: 3px;
                padding: 2px 4px; background: transparent; color: #16232a;
            }
            QLineEdit#chName { font-size: 9.5px; font-weight: 600; }
            QLineEdit#chRemark { font-size: 9px; color: #52636c; }
            QLineEdit#chName:hover, QLineEdit#chRemark:hover {
                border-color: #dbe3e8; background: #ffffff;
            }
            QLineEdit#chName:focus, QLineEdit#chRemark:focus {
                border-color: #147688; background: #ffffff;
            }
            QWidget#chMap { background: transparent; }
            QToolButton#chip {
                border: 1px solid #bddfe5; border-radius: 4px;
                background: #eaf5f7; color: #0f5c6a;
                font-family: Consolas; font-size: 8.5px; font-weight: 600;
                padding: 2px 5px;
            }
            QToolButton#chip:hover { background: #dff0f4; }
            QToolButton#chip[warn="true"] {
                border-color: #efcf97; background: #fff5e3; color: #9a5b00;
            }
            QToolButton#chipMore {
                border: 1px solid #dbe3e8; border-radius: 4px;
                background: #ffffff; color: #52636c;
                font-family: Consolas; font-size: 8.5px; padding: 2px 5px;
            }
            QToolButton#chipMore:hover { background: #f3f7f9; }
            QLabel#dropHint {
                color: #93a3aa; border: 1px dashed #c8d5db; border-radius: 3px;
                padding: 2px 7px; font-size: 8.5px; background: #fbfdfd;
            }
            QToolButton#rowClear {
                border: 0; background: transparent; color: #9aabb2; font-size: 8.5px;
            }
            QToolButton#rowClear:hover { background: #fdecea; color: #b42318; }
            QToolButton#rowClear:disabled { color: #d2dbe0; }
            QLabel#statLabel { color: #52636c; font-size: 9px; }
            QLabel#statLabel[error="true"] { color: #b42318; }
            QLabel#statusText { color: #83929a; font-size: 9px; }
            QLabel#statusText[dirty="true"] { color: #9a5b00; }
            QLabel#statusText[tone="success"] { color: #0f5c6a; }
            QLabel#statusText[tone="warn"] { color: #9a5b00; }
            QLabel#statusText[tone="danger"] { color: #b42318; }
            QSplitter::handle { background: #dbe3e8; width: 1px; }
        """)

    # ------------------------------------------------------------ DBC 列表
    def _refresh_dbc_list(self) -> None:
        selected = {item.data(Qt.ItemDataRole.UserRole)
                    for item in self.dbc_list.selectedItems()}
        current = self.dbc_list.currentItem()
        current_path = current.data(Qt.ItemDataRole.UserRole) if current else None
        scroll = self.dbc_list.verticalScrollBar().value()

        self.dbc_list.blockSignals(True)
        self.dbc_list.clear()
        paths = sorted(self._dbc_paths, key=lambda path: (
            bool((self._summaries.get(os.path.abspath(path)) or {}).get("error")),
            os.path.basename(path).lower(), path.lower()))
        for path in paths:
            summary = self._summaries.get(os.path.abspath(path))
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, path)
            error = (summary or {}).get("error", "")
            if error:
                item.setData(Qt.ItemDataRole.UserRole + 1, error)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setBackground(QColor("#fdf1f0"))
            else:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable
                              | Qt.ItemFlag.ItemIsDragEnabled)
            widget = self._make_dbc_widget(path, summary)
            item.setSizeHint(QSize(0, 66))
            item.setToolTip(self._dbc_tooltip(path, summary))
            self.dbc_list.addItem(item)
            self.dbc_list.setItemWidget(item, widget)
            if path in selected:
                item.setSelected(True)
        self.dbc_list.blockSignals(False)

        for index in range(self.dbc_list.count()):
            item = self.dbc_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == current_path:
                self.dbc_list.setCurrentItem(item)
                break
        else:
            if current_path is None and self.dbc_list.count():
                self.dbc_list.setCurrentRow(0)
        self.dbc_count_label.setText(str(len(self._dbc_paths)))
        self.dbc_list.verticalScrollBar().setValue(scroll)
        self._apply_filter()

    def _dbc_widget_values(self, path: str, summary: dict = None) -> tuple:
        """返回卡片需要的 (name, kind_key, kind_text, stats, map_text, error)。"""
        summary = summary or {}
        error = summary.get("error", "")
        if error:
            return os.path.basename(path), "", "", "", "", error
        kind = summary.get("kind", "")
        if summary:
            kind_key = kind or "auto"
            kind_text = _kind_short(kind) or "自动"
            stats = "%s 报文 / %s 信号" % (
                "{:,}".format(summary.get("msgs", 0)),
                "{:,}".format(summary.get("signals", 0)))
        else:
            kind_key, kind_text, stats = "pending", "待解析", "选中或拖入后解析"
        channels = self._mapped_channels_for(path)
        map_text = ("→ " + " · ".join("ch%d" % ch for ch in channels)
                    if channels else "未映射")
        return os.path.basename(path), kind_key, kind_text, stats, map_text, ""

    def _make_dbc_widget(self, path: str, summary: dict = None) -> DbcListItemWidget:
        return DbcListItemWidget(*self._dbc_widget_values(path, summary))

    def _dbc_tooltip(self, path: str, summary: dict = None) -> str:
        summary = summary or {}
        if summary.get("error"):
            return "%s\n%s" % (path, summary["error"])
        lines = [path]
        if summary.get("describe"):
            lines.append(summary["describe"])
        channels = self._mapped_channels_for(path)
        lines.append("已映射：" + ("、".join("ch%d" % ch for ch in channels)
                                   if channels else "无"))
        return "\n".join(lines)

    def _on_current_dbc_changed(self, current, _previous) -> None:
        if current is None:
            return
        path = current.data(Qt.ItemDataRole.UserRole)
        if not path or current.data(Qt.ItemDataRole.UserRole + 1):
            return
        summary = self._ensure_parsed(path)
        item = current
        values = self._dbc_widget_values(path, summary)
        item.setSizeHint(QSize(0, 66))
        item.setToolTip(self._dbc_tooltip(path, summary))
        widget = self.dbc_list.itemWidget(item)
        if widget is None:
            self.dbc_list.setItemWidget(item, DbcListItemWidget(*values))
        else:
            widget.update_content(values[1], values[2], values[3],
                                  values[4], values[5])
        if summary.get("error"):
            item.setData(Qt.ItemDataRole.UserRole + 1, summary["error"])
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setBackground(QColor("#fdf1f0"))
        self._refresh_channel_rows()

    def _ensure_parsed(self, path: str) -> dict:
        key = os.path.abspath(path)
        if key in self._summaries:
            return self._summaries[key]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            file = self._library.load_dbc(key)
            summary = {"kind": file.auto_kind,
                       "msgs": len(file.messages),
                       "signals": file.signal_count,
                       "describe": file.describe(),
                       "error": ""}
        except Exception as exc:                     # noqa: BLE001
            summary = {"kind": "", "msgs": 0, "signals": 0,
                       "describe": "",
                       "error": "%s: %s" % (type(exc).__name__, exc)}
            logger.error("DBC 解析失败：%s -> %s", key, exc)
        finally:
            QApplication.restoreOverrideCursor()
        self._summaries[key] = summary
        return summary

    def _apply_filter(self) -> None:
        keyword = (self.search_edit.text() or "").strip().lower()
        for index in range(self.dbc_list.count()):
            item = self.dbc_list.item(index)
            path = item.data(Qt.ItemDataRole.UserRole) or ""
            item.setHidden(bool(keyword) and keyword not in os.path.basename(path).lower())

    # ------------------------------------------------------------ 通道行
    def _channel_meta_text(self, channel: int) -> str:
        info = self._channel_info.get(channel)
        if info:
            kind, frames = info
            return "%s · %s" % (_kind_short(kind), "{:,}".format(frames))
        return "日志中无此通道" if self._has_asc else "模板通道"

    def _channel_warn_text(self, channel: int) -> str:
        info = self._channel_info.get(channel)
        if not info:
            return ""
        channel_kind = info[0]
        mismatched = []
        for path in self._mappings.get(channel, []):
            summary = self._summaries.get(os.path.abspath(path))
            if summary and summary.get("kind") and summary["kind"] != channel_kind:
                mismatched.append(os.path.basename(path))
        if not mismatched:
            return ""
        return ("帧类型不符：%s 与通道（%s）不一致；仍可保存"
                % ("、".join(mismatched), KIND_TEXTS.get(channel_kind, channel_kind)))

    def _refresh_channel_rows(self, commit: bool = True) -> None:
        if commit and self._rows:
            self._commit_row_edits()
        scroll = self.channel_scroll.verticalScrollBar().value()
        while self.channel_layout.count() > 1:
            item = self.channel_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = {}
        for channel in self._all_channels:
            info = self._channel_info.get(channel)
            row = ChannelRow(
                channel=channel,
                name=self._names.get(channel, ""),
                remark=self._remarks.get(channel, ""),
                meta_text=self._channel_meta_text(channel),
                warn_text=self._channel_warn_text(channel),
                mappings=self._mappings.get(channel, []),
                summaries=self._summaries,
                channel_kind=info[0] if info else "")
            row.changed.connect(self._on_meta_changed)
            row.rowClicked.connect(self._on_row_clicked)
            row.clearRequested.connect(self._on_clear_channel)
            row.removeRequested.connect(self._on_remove_mapping)
            row.dropRequested.connect(self._on_drop)
            row.set_selected(channel in self._selected_channels)
            self.channel_layout.insertWidget(self.channel_layout.count() - 1, row)
            self._rows[channel] = row
        self.channel_count_label.setText(str(len(self._all_channels)))
        self._scroll_target = scroll
        self._scroll_timer.start(0)

    def _restore_channel_scroll(self) -> None:
        self.channel_scroll.verticalScrollBar().setValue(self._scroll_target)

    def _commit_row_edits(self) -> None:
        for row in self._rows.values():
            row.commit()

    # ------------------------------------------------------------ 状态与底栏
    def _mark_dirty(self) -> None:
        self._dirty = True
        self.setWindowTitle("DBC 映射设置 *")
        self._update_footer()

    def _update_footer(self) -> None:
        links = sum(len(paths) for paths in self._mappings.values())
        mapped = sum(1 for paths in self._mappings.values() if paths)
        errors = sum(1 for summary in self._summaries.values()
                     if summary.get("error"))
        self.stat_label.setText(
            "%d 通道 · %d 已映射 · %d 条映射 · %d 解析失败"
            % (len(self._all_channels), mapped, links, errors))
        self.stat_label.setProperty("error", errors > 0)
        _repolish(self.stat_label)
        if not self._status_timer.isActive():
            self._refresh_status()

    def _refresh_status(self) -> None:
        self.status_label.setProperty("tone", "")
        self.status_label.setProperty("dirty", self._dirty)
        self.status_label.setText("有未应用的更改 · channel_map.json v2"
                                  if self._dirty
                                  else "已保存 · channel_map.json v2")
        self.status_label.setToolTip("")
        _repolish(self.status_label)

    def _notify(self, message: str, tone: str = "success") -> None:
        self.status_label.setProperty("tone", tone)
        self.status_label.setProperty("dirty", False)
        self.status_label.setText(message)
        self.status_label.setToolTip(message)
        _repolish(self.status_label)
        self._status_timer.start(4200)

    def _on_meta_changed(self, channel: int, field: str, value: str) -> None:
        target = self._names if field == "name" else self._remarks
        if target.get(channel) == value:
            return
        target[channel] = value
        self._mark_dirty()

    def _on_row_clicked(self, channel: int, ctrl: bool) -> None:
        if ctrl:
            if channel in self._selected_channels:
                self._selected_channels.remove(channel)
            else:
                self._selected_channels.add(channel)
        else:
            self._selected_channels = {channel}
        for ch, row in self._rows.items():
            row.set_selected(ch in self._selected_channels)

    # ------------------------------------------------------------ 映射变更
    def _on_drop(self, channel: int, paths: list) -> None:
        self._commit_row_edits()
        logger.debug("处理拖放：目标 ch%d，原始路径 %s", channel,
                     [os.path.basename(path) for path in paths])
        final_paths = []
        for path in paths:
            if self._is_in_dbc_dir(path):
                final_paths.append(os.path.abspath(path))
            else:
                final_paths.extend(self.import_external_paths([path], ask=True))
        final_paths = _unique_paths(final_paths)
        if not final_paths:
            logger.debug("拖放无有效路径，已忽略")
            return

        valid, invalid = [], []
        for path in final_paths:
            summary = self._ensure_parsed(path)
            if summary.get("error"):
                invalid.append((path, summary["error"]))
            else:
                valid.append(path)
        if invalid:
            detail = "\n".join("%s：%s" % (os.path.basename(path), error)
                               for path, error in invalid)
            QMessageBox.warning(self, "DBC 解析失败",
                                "以下 DBC 无法解析，未加入映射：\n\n" + detail)

        if not valid:
            return
        targets = (sorted(self._selected_channels)
                   if channel in self._selected_channels else [channel])
        for target in targets:
            paths_at_target = self._mappings.setdefault(target, [])
            for path in valid:
                if not any(os.path.normcase(os.path.abspath(item))
                           == os.path.normcase(os.path.abspath(path))
                           for item in paths_at_target):
                    paths_at_target.append(os.path.abspath(path))
        self._selected_channels.clear()
        self._mark_dirty()
        self._refresh_channel_rows()
        self._refresh_dbc_list()
        self._update_footer()
        self._notify("已映射 %s → %s" % (
            "、".join(os.path.basename(path) for path in valid),
            "、".join("ch%d" % ch for ch in targets)))

    def _on_remove_mapping(self, channel: int, path: str) -> None:
        self._commit_row_edits()
        key = os.path.normcase(os.path.abspath(path))
        self._mappings[channel] = [
            item for item in self._mappings.get(channel, [])
            if os.path.normcase(os.path.abspath(item)) != key]
        self._mark_dirty()
        self._refresh_channel_rows()
        self._refresh_dbc_list()
        self._update_footer()
        self._notify("已移除 %s → ch%d" % (os.path.basename(path), channel))

    def _on_clear_channel(self, channel: int) -> None:
        self._commit_row_edits()
        if not self._mappings.get(channel):
            return
        self._mappings[channel] = []
        self._mark_dirty()
        self._refresh_channel_rows()
        self._refresh_dbc_list()
        self._update_footer()
        self._notify("已清空 ch%d 的映射" % channel, "warn")

    # ------------------------------------------------------------ 外部文件
    def import_external_paths(self, paths, ask: bool = True,
                              overwrite: bool = False) -> list:
        """把 dbc/ 之外的 .dbc 复制进来；返回最终使用的绝对路径列表。

        ask=True 时同名文件弹窗询问；ask=False 时按 overwrite 决定覆盖或复用。
        """
        copied = []
        for source in paths or []:
            source = os.path.abspath(source)
            if not source.lower().endswith(DBC_SUFFIXES):
                continue
            if not os.path.isfile(source):
                continue
            if self._is_in_dbc_dir(source):
                copied.append(source)
                continue
            destination = os.path.join(self._dbc_dir, os.path.basename(source))
            if os.path.exists(destination) and not overwrite:
                if ask:
                    answer = QMessageBox.question(
                        self, "同名文件",
                        "dbc/ 中已存在 %s，是否覆盖？\n\n"
                        "选择“否”会保留原文件并直接使用它。"
                        % os.path.basename(destination),
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.No
                        | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.No)
                    if answer == QMessageBox.StandardButton.Cancel:
                        continue
                    if answer == QMessageBox.StandardButton.Yes:
                        try:
                            shutil.copy2(source, destination)
                        except OSError as exc:
                            QMessageBox.critical(
                                self, "复制失败",
                                "%s\n\n%s" % (destination, exc))
                            continue
                copied.append(destination)
                continue
            try:
                shutil.copy2(source, destination)
            except OSError as exc:
                QMessageBox.critical(self, "复制失败",
                                     "%s\n\n%s" % (destination, exc))
                continue
            copied.append(destination)

        if copied:
            self._discover_dbc_paths()
            self._refresh_dbc_list()
            self._notify("已复制 %d 个 DBC 到 dbc/" % len(copied))
        return copied

    def dragEnterEvent(self, event):            # noqa: N802 - Qt 命名
        if _paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):             # noqa: N802 - Qt 命名
        if _paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):                 # noqa: N802 - Qt 命名
        paths = _paths_from_mime(event.mimeData())
        if not paths:
            super().dropEvent(event)
            return
        external = [path for path in paths if not self._is_in_dbc_dir(path)]
        event.acceptProposedAction()
        if not external:
            self._notify("请把 DBC 拖到右侧通道完成映射", "warn")
            return
        self.import_external_paths(external, ask=True)

    # ------------------------------------------------------------ 动作
    def _open_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._dbc_dir))

    def _rescan(self) -> None:
        self._library.invalidate_cache()
        self._summaries.clear()
        self._discover_dbc_paths()
        self._refresh_dbc_list()
        self._refresh_channel_rows()
        self._update_footer()
        errors = sum(1 for summary in self._summaries.values()
                     if summary.get("error"))
        self._notify("已重新扫描 dbc/：%d 个 DBC%s" % (
            len(self._dbc_paths), "，%d 个解析失败" % errors if errors else ""),
            "warn" if errors else "success")

    def _on_reset_template(self, confirm: bool = True) -> None:
        if confirm:
            answer = QMessageBox.question(
                self, "恢复默认模板",
                "恢复 21 个通道的默认名称/备注？\n\n映射关系不会改变。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._commit_row_edits()
        for channel, meta in default_channels().items():
            self._names[channel] = meta.name
            self._remarks[channel] = meta.remark
        self._mark_dirty()
        self._refresh_channel_rows(commit=False)
        self._notify("已恢复 21 个通道的默认名称/备注", "warn")

    def _on_accept(self) -> None:
        self._commit_row_edits()
        empty = [channel for channel in self._all_channels
                 if not self._names.get(channel, "").strip()]
        if empty:
            channel = empty[0]
            QMessageBox.warning(self, "通道名称不能为空",
                                "请填写 ch%d 的通道名称。" % channel)
            row = self._rows.get(channel)
            if row is not None:
                row.name_edit.setFocus()
            return

        if not self._dirty:
            self.accept()
            return

        # 解析所有待写入的 DBC，避免把坏文件写进配置
        failures = []
        for channel in self._all_channels:
            for path in self._mappings.get(channel, []):
                summary = self._ensure_parsed(path)
                if summary.get("error"):
                    failures.append((path, summary["error"]))
        failures = {path: error for path, error in failures}
        if failures:
            detail = "\n".join("%s：%s" % (os.path.basename(path), error)
                               for path, error in failures.items())
            QMessageBox.critical(self, "DBC 解析失败",
                                 "以下 DBC 无法解析，不能写入映射：\n\n" + detail)
            return

        old_mappings = copy.deepcopy(self._library.mappings)
        old_channels = copy.deepcopy(self._library.channels)
        try:
            result = self._library.apply_editor_state(
                self._mappings, names=self._names, remarks=self._remarks)
            if result.get("errors"):
                self._library.mappings = old_mappings
                self._library.channels = old_channels
                detail = "\n".join(result["errors"].values())
                QMessageBox.critical(self, "DBC 解析失败",
                                     "写入前解析失败：\n\n" + detail)
                return
            self._library.save_config()
        except Exception as exc:                    # noqa: BLE001
            self._library.mappings = old_mappings
            self._library.channels = old_channels
            logger.error("写入 DBC 映射配置失败：%s", exc, exc_info=True)
            QMessageBox.critical(
                self, "保存失败",
                "无法写入 %s：\n\n%s\n\n请确认文件没有被 OneDrive/杀毒软件占用。"
                % (self._library.config_path, exc))
            return

        links = sum(len(paths) for paths in self._mappings.values())
        mapped = sum(1 for paths in self._mappings.values() if paths)
        multi = sum(1 for paths in self._mappings.values() if len(paths) > 1)
        conflicts = 0
        for channel, paths in self._mappings.items():
            info = self._channel_info.get(channel)
            if info and len(paths) > 1:
                conflicts += len(self._library.channel_conflicts(info[0], channel))
        self.applied_summary = {
            "channels": len(self._all_channels),
            "mapped_channels": mapped,
            "mappings": links,
            "multi_dbc_channels": multi,
            "conflicts": conflicts,
        }
        logger.info("DBC 映射已保存：%d 通道 / %d 已映射 / %d 条映射 / "
                    "%d 个多 DBC 通道 / %d 个同 ID 冲突",
                    len(self._all_channels), mapped, links, multi, conflicts)
        self._dirty = False
        self.accept()
