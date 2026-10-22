# -*- coding: utf-8 -*-
"""DBC 映射工作台（V0.3）离屏测试：21 通道模板、拖拽、备注、外部复制。"""

import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QToolButton, QWidget  # noqa: E402

from signals.dbc_loader import DbcLibrary  # noqa: E402
from ui.dbc_mapping_dialog import DBC_MIME, DbcMappingDialog  # noqa: E402

DBC_TEXT = '''VERSION "1.0"

NS_ :

BS_:

BU_: ECU

BO_ 256 MsgA: 8 ECU
 SG_ Speed : 0|16@1+ (0.1,0) [0|6553.5] "km/h" ECU
 SG_ Gear : 24|4@1+ (1,0) [0|15] "" ECU

VAL_ 256 Gear 0 "P" 1 "R" 2 "N" 3 "D" ;
'''


def canfd_text() -> str:
    return DBC_TEXT.replace(
        "BU_: ECU",
        'BU_: ECU\n\nBA_DEF_ BO_  "VFrameFormat" INT 0 15;\n'
        'BA_DEF_DEF_  "VFrameFormat" 0;') + '\nBA_ "VFrameFormat" BO_ 256 14;\n'


@unittest.skipUnless(QApplication is not None, "PySide6 未安装，跳过 GUI 测试")
class TestDbcMappingDialog(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="asc_dbc_dialog_")
        self.dbc_dir = os.path.join(self.tmpdir, "dbc")
        os.makedirs(self.dbc_dir)
        self.ch5 = self._write("CH5.dbc", DBC_TEXT)
        self.other = self._write("Other.dbc",
                                 DBC_TEXT.replace("MsgA", "MsgAAlt"))
        self.fd = self._write("FD.dbc", canfd_text())
        self.dialog = None

    def tearDown(self):
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
        for root, _dirs, files in os.walk(self.tmpdir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)

    def _write(self, name, text):
        path = os.path.join(self.dbc_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _dialog(self, channels):
        self.dialog = DbcMappingDialog(
            self.dbc_dir, channels,
            library=DbcLibrary.open(self.dbc_dir))
        return self.dialog

    # ---------------------------------------------------------------- 模板
    def test_shows_21_template_channels_without_asc(self):
        dialog = self._dialog([])
        self.assertEqual(len(dialog._all_channels), 21)
        self.assertEqual(len(dialog._rows), 21)
        self.assertEqual(dialog._names[5], "E CAN")
        self.assertEqual(dialog._remarks[18], "LIN to CAN")
        self.assertEqual(dialog.channel_count_label.text(), "21")
        self.assertEqual(dialog._mappings[5], [])
        self.assertEqual(len(dialog._dbc_paths), 3)

    def test_dbc_items_are_drag_enabled_and_mouse_transparent(self):
        """回归：item widget 不能挡住鼠标，否则“选得中但拖不动”。"""
        dialog = self._dialog([])
        item = dialog.dbc_list.currentItem()
        self.assertTrue(item.flags() & Qt.ItemFlag.ItemIsDragEnabled)
        widget = dialog.dbc_list.itemWidget(item)
        self.assertTrue(widget.testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertTrue(widget.findChildren(QWidget))
        for child in widget.findChildren(QWidget):
            self.assertTrue(child.testAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_channel_drop_event_creates_mapping(self):
        dialog = self._dialog([("canfd", 5, 30000)])
        mime = QMimeData()
        mime.setData(DBC_MIME, json.dumps([self.ch5]).encode("utf-8"))
        event = QDropEvent(
            QPointF(5, 5), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        dialog._rows[5].dropEvent(event)
        self.assertEqual(dialog._mappings[5], [self.ch5])
        self.assertTrue(dialog._dirty)

    def test_drop_maps_to_all_selected_channels(self):
        dialog = self._dialog([("canfd", 5, 100), ("canfd", 9, 50)])
        dialog._on_row_clicked(5, False)
        dialog._on_row_clicked(9, True)
        dialog._on_drop(5, [self.ch5, self.other])
        self.assertEqual([os.path.basename(path) for path in dialog._mappings[5]],
                         ["CH5.dbc", "Other.dbc"])
        self.assertEqual([os.path.basename(path) for path in dialog._mappings[9]],
                         ["CH5.dbc", "Other.dbc"])
        self.assertEqual(dialog._selected_channels, set())

    # ---------------------------------------------------------------- 应用
    def test_apply_writes_mapping_to_config_v2(self):
        dialog = self._dialog([("canfd", 5, 30000)])
        dialog._on_drop(5, [self.ch5])
        dialog._on_accept()
        self.assertTrue(os.path.exists(dialog._library.config_path))
        with open(dialog._library.config_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["mappings"][0]["dbc"], "CH5.dbc")
        self.assertEqual(payload["mappings"][0]["channels"], [5])
        self.assertFalse(dialog._dirty)

    def test_name_and_remark_edit_persist(self):
        dialog = self._dialog([("canfd", 5, 30000)])
        row = dialog._rows[5]
        row.name_edit.setText("E CAN TEST")
        row.name_edit.editingFinished.emit()
        row.remark_edit.setText("整车网络")
        row.remark_edit.editingFinished.emit()
        self.assertEqual(dialog._names[5], "E CAN TEST")
        self.assertEqual(dialog._remarks[5], "整车网络")

        dialog._on_accept()
        reloaded = DbcLibrary.open(self.dbc_dir)
        self.assertEqual(reloaded.channel_meta(5).name, "E CAN TEST")
        self.assertEqual(reloaded.channel_meta(5).remark, "整车网络")

    def test_cancel_does_not_write_config(self):
        dialog = self._dialog([("canfd", 5, 30000)])
        dialog._names[5] = "SHOULD NOT SAVE"
        dialog._mark_dirty()
        dialog.reject()
        self.assertFalse(os.path.exists(dialog._library.config_path))

    def test_external_dbc_is_copied_into_dbc_dir(self):
        dialog = self._dialog([])
        outside = os.path.join(self.tmpdir, "Outside.dbc")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write(DBC_TEXT)
        copied = dialog.import_external_paths([outside], ask=False,
                                              overwrite=False)
        expected = os.path.join(self.dbc_dir, "Outside.dbc")
        self.assertEqual(copied, [expected])
        self.assertTrue(os.path.exists(expected))
        self.assertIn(expected, dialog._dbc_paths)

    def test_external_drop_event_on_row_copies_and_maps(self):
        dialog = self._dialog([("canfd", 5, 30000)])
        outside = os.path.join(self.tmpdir, "Outside.dbc")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write(DBC_TEXT)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(outside)])
        event = QDropEvent(
            QPointF(5, 5), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        dialog._rows[5].dropEvent(event)
        expected = os.path.join(self.dbc_dir, "Outside.dbc")
        self.assertTrue(os.path.exists(expected))
        self.assertIn(expected, dialog._mappings[5])

    # ---------------------------------------------------------------- 异常态
    def test_frame_kind_mismatch_marks_row_and_chip(self):
        dialog = self._dialog([("classic", 3, 100)])
        dialog._on_drop(3, [self.fd])
        self.assertIn("帧类型不符", dialog._channel_warn_text(3))
        row = dialog._rows[3]
        self.assertTrue(row.meta_label.property("warn"))
        chips = row.findChildren(QToolButton, "chip")
        self.assertTrue(chips)
        self.assertTrue(chips[0].property("warn"))

    def test_rescan_reloads_changed_dbc(self):
        dialog = self._dialog([])
        self.assertEqual(dialog._ensure_parsed(self.ch5)["msgs"], 1)
        with open(self.ch5, "w", encoding="utf-8") as handle:
            handle.write(DBC_TEXT + '\nBO_ 512 MsgB: 8 ECU\n'
                                   ' SG_ Temp : 0|8@1+ (1,0) [0|255] "" ECU\n')
        dialog._rescan()
        self.assertEqual(dialog._ensure_parsed(self.ch5)["msgs"], 2)

    def test_parse_updates_existing_item_widget_in_place(self):
        """解析完成后更新原卡片，不能重新 setItemWidget 造成文字重影。"""
        dialog = self._dialog([])
        item = dialog.dbc_list.currentItem()
        self.assertIsNotNone(item)
        widget = dialog.dbc_list.itemWidget(item)
        self.assertIsNotNone(widget)
        dialog._on_current_dbc_changed(item, None)
        self.assertIs(dialog.dbc_list.itemWidget(item), widget)
        self.assertEqual(widget.name_label.text(),
                         os.path.basename(item.data(Qt.ItemDataRole.UserRole)))

    def test_parse_error_is_not_draggable(self):
        dialog = self._dialog([])
        bad = self._write("Bad.dbc", "BO_ 1 Broken\n")
        summary = dialog._ensure_parsed(bad)
        self.assertTrue(summary["error"])
        dialog._discover_dbc_paths()
        dialog._refresh_dbc_list()
        item = next(dialog.dbc_list.item(index)
                    for index in range(dialog.dbc_list.count())
                    if dialog.dbc_list.item(index).data(Qt.ItemDataRole.UserRole)
                    == os.path.abspath(bad))
        self.assertTrue(item.data(Qt.ItemDataRole.UserRole + 1))
        self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsSelectable)


if __name__ == "__main__":
    unittest.main()
