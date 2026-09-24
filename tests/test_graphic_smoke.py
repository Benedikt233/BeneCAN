# -*- coding: utf-8 -*-
"""M3 图形窗口离屏冒烟测试（不弹窗、可在无桌面环境跑）。

覆盖：打开 ASC → 报文树填充 → 加入报文/字节行 → 增删行 → 时间轴三种模式 →
PNG 渲染 → 过滤。运行： python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.indexer import build_index, default_index_path  # noqa: E402
from core.query import MODE_INDEX, MODE_TIME, IndexReader  # noqa: E402
from ui import graphic_window  # noqa: E402
from ui.msg_tree import ROLE_KIND  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except Exception:                                    # noqa: BLE001
    HAS_QT = False

TAIL = "213015  355   327000 80162294 50140850 50050850 2007030e 20070002"


def canfd_line(ts, channel, can_id, data):
    return ("   %.6f   CANFD  %d RX    %s   1 1 %X %d %s     %s\n"
            % (ts, channel, can_id, len(data), len(data),
               " ".join("%02x" % b for b in data), TAIL))


def build_sample_asc(path: str) -> None:
    """三条报文：10ms 周期模拟量、20ms 枚举量、64 字节长报文。"""
    lines = ["base hex  timestamps absolute\n",
             "no internal events logged\n",
             "Begin TriggerBlock Tue Sep 22 01:50:11 PM 2026\n"]
    for index in range(200):
        lines.append(canfd_line(1.0 + index * 0.01, 5, "17A", [0x11, index % 256, 0x02]))
    for index in range(100):
        lines.append(canfd_line(2.0 + index * 0.02, 4, "100", [index % 3, 0x00, 0x7F]))
    for index in range(10):
        lines.append(canfd_line(3.0 + index * 0.1, 9, "1D9", list(range(64))))
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


@unittest.skipUnless(HAS_QT, "PySide6 未安装，跳过 GUI 冒烟测试")
class TestGraphicSmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import main as app_main

        cls.app_main = app_main
        cls.app = QApplication.instance() or QApplication([])
        cls.tmpdir = tempfile.mkdtemp(prefix="asc_graphic_test_")
        cls.asc_path = os.path.join(cls.tmpdir, "sample.asc")
        build_sample_asc(cls.asc_path)
        cls.index_path = default_index_path(cls.asc_path)     # 与真实打开流程一致
        build_index(cls.asc_path, index_path=cls.index_path)
        cls.reader = IndexReader.open(cls.index_path)

    @classmethod
    def tearDownClass(cls):
        for name in os.listdir(cls.tmpdir):
            os.remove(os.path.join(cls.tmpdir, name))
        os.rmdir(cls.tmpdir)

    def setUp(self):
        self.window = self.app_main.MainWindow()
        self.window._activate_reader(self.asc_path)
        self.window.show()                    # 离屏模式：让布局生效，几何尺寸可用
        self.app.processEvents()
        self.graphic = self.window.graphic
        self.tree = self.window.tree

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()

    # ---------------------------------------------------------------- 树
    def test_tree_populated(self):
        self.assertEqual(self.tree.topLevelItemCount(), self.reader.channels().__len__())
        first_channel = self.tree.topLevelItem(0)
        self.assertGreater(first_channel.childCount(), 0)
        self.assertIn("ch", first_channel.text(0))

    def test_tree_lazy_profile_children(self):
        message_item = self.tree.topLevelItem(0).child(0)
        self.assertEqual(message_item.childCount(), 1)      # 占位子节点
        self.tree._on_expanded(message_item)
        self.assertGreater(message_item.childCount(), 0)
        payload = message_item.child(0).data(0, ROLE_KIND)
        self.assertEqual(payload[0], "byte")

    def test_filter_hides_non_matching(self):
        self.tree.apply_filter("17a")
        visible = [self.tree.topLevelItem(i).isHidden()
                   for i in range(self.tree.topLevelItemCount())]
        self.assertTrue(any(not hidden for hidden in visible))
        self.tree.apply_filter("")
        self.assertTrue(all(not self.tree.topLevelItem(i).isHidden()
                            for i in range(self.tree.topLevelItemCount())))

    # ---------------------------------------------------------------- 图形
    def test_add_message_rows_and_header(self):
        info = self.reader.find("canfd", 5, 0x17A)
        added = self.graphic.add_message(info)
        self.assertGreater(added, 0)
        self.assertEqual(self.graphic.row_count(), added)
        self.assertIn("共 %d 行" % added, self.graphic.header.text())
        # 每行都有对应的 PlotItem 与曲线
        for row in self.graphic.rows():
            self.assertIsNotNone(row.plot)
            self.assertIsNotNone(row.curve)

    def test_add_same_spec_twice_is_ignored(self):
        info = self.reader.find("canfd", 5, 0x17A)
        self.graphic.add_message(info)
        before = self.graphic.row_count()
        specs = [row.spec for row in self.graphic.rows()]
        self.graphic.add_specs(specs[0])            # 重复加入同一行
        self.assertEqual(self.graphic.row_count(), before)

    def test_remove_row_and_clear(self):
        info = self.reader.find("canfd", 5, 0x17A)
        self.graphic.add_message(info)
        rows = self.graphic.row_count()
        self.graphic.remove_row(0)
        self.assertEqual(self.graphic.row_count(), rows - 1)
        self.graphic.clear()
        self.assertEqual(self.graphic.row_count(), 0)
        self.assertEqual(self.graphic.point_counts(), [])

    def test_remove_message_rows(self):
        self.graphic.add_message(self.reader.find("canfd", 5, 0x17A))
        self.graphic.add_message(self.reader.find("canfd", 4, 0x100))
        total = self.graphic.row_count()
        removed = self.graphic.remove_message_rows(self.reader.find("canfd", 5, 0x17A))
        self.assertGreater(removed, 0)
        self.assertEqual(self.graphic.row_count(), total - removed)

    def test_time_axis_modes(self):
        self.graphic.add_message(self.reader.find("canfd", 5, 0x17A))
        row = self.graphic.rows()[0]

        self.assertEqual(self.graphic.time_mode, MODE_TIME)
        self.assertGreaterEqual(row.x[0], 0.0)                    # 已减去文件起点
        self.assertLess(row.x[0], 5.0)

        self.graphic.set_time_mode(MODE_INDEX)
        row = self.graphic.rows()[0]
        self.assertTrue((row.x == row.series.seq.astype(float)).all())
        self.assertTrue(bool((row.x[1:] >= row.x[:-1]).all()))

    def test_rows_share_x_axis(self):
        self.graphic.add_message(self.reader.find("canfd", 5, 0x17A))
        self.graphic.add_message(self.reader.find("canfd", 9, 0x1D9))
        rows = self.graphic.rows()
        self.assertIsNotNone(rows[1].plot.getViewBox().state["linkedViews"])

    # ---------------------------------------------------------------- 纵轴单位 / 单坐标轴
    def test_row_shows_name_and_unit_horizontally(self):
        """信号名横排在行内、单位挂在 Y 轴标题上（DBC 规定的值/单位，不是纯数字）。"""
        from signals.model import SignalSpec

        spec = SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="车速",
                          unit="km/h")
        self.graphic.add_specs(spec)
        self.graphic.resize(900, 400)
        self.app.processEvents()
        row = self.graphic.rows()[0]
        self.assertIsNotNone(row.caption)                       # 横排名称（旋转标题会被截掉）
        self.assertIn("车速", row.caption.textItem.toPlainText())
        self.assertEqual(row.plot.getAxis("left").labelText, "[km/h]")
        self.assertEqual(row.unit_text, "[km/h]")
        # 说明必须真的落在绘图区内（anchor 写错就会被裁掉，等于看不到名字）
        viewbox_rect = row.plot.getViewBox().sceneBoundingRect()
        caption_rect = row.caption.sceneBoundingRect()
        self.assertGreaterEqual(caption_rect.left(), viewbox_rect.left() - 1.0)
        self.assertGreaterEqual(caption_rect.top(), viewbox_rect.top() - 1.0)
        self.assertLessEqual(caption_rect.bottom(), viewbox_rect.bottom() + 1.0)
        self.assertLessEqual(caption_rect.right(), viewbox_rect.right() + 1.0)

    def test_row_without_unit_shows_short_name_on_axis(self):
        """没有单位的信号（DBC 里很常见）→ Y 轴显示短信号名，不能是一片空白。"""
        from signals.model import SignalSpec

        spec = SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="计数器")
        self.graphic.add_specs(spec)
        row = self.graphic.rows()[0]
        self.assertEqual(row.unit_text, "")
        self.assertEqual(row.plot.getAxis("left").labelText, "计数器")

        # DBC 名字形如「报文.信号」→ 轴上只取信号段，过长则截断
        spec2 = SignalSpec(key=("canfd", 5, 0x17A), byte_offset=0,
                           name="E_IPUR_General.E_IPUR_TorqueLimit")
        self.graphic.clear()
        self.graphic.add_specs(spec2)
        axis2 = self.graphic.rows()[0].plot.getAxis("left")
        self.assertEqual(axis2.labelText, "E_IPUR_TorqueLimit")
        self.assertEqual(self.graphic.rows()[0].label_text,
                         "E_IPUR_General.E_IPUR_TorqueLimit")     # 行内仍是完整名字

    def test_overlay_mode_shares_one_y_axis(self):
        self.graphic.add_message(self.reader.find("canfd", 5, 0x17A))
        self.graphic.add_message(self.reader.find("canfd", 9, 0x1D9))
        self.assertFalse(self.graphic.overlay)
        self.assertNotEqual(self.graphic.rows()[0].plot, self.graphic.rows()[1].plot)

        self.window.overlay_action.setChecked(True)             # 走主窗口动作
        self.assertTrue(self.graphic.overlay)
        rows = self.graphic.rows()
        self.assertIs(rows[0].plot, rows[1].plot)               # 同一坐标系 → 共用一个 Y 轴
        self.assertIn("单坐标轴叠加", self.graphic.header.text())
        self.assertIsNone(rows[0].caption)                      # 叠加模式用图例，不用行内说明
        # 行数据与光标测量在叠加模式下照常可用
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_positions(0.2, 0.8)
        self.assertEqual(len(self.graphic.measurements()), self.graphic.row_count())

        self.window.overlay_action.setChecked(False)
        self.assertFalse(self.graphic.overlay)
        rows = self.graphic.rows()
        self.assertNotEqual(rows[0].plot, rows[1].plot)
        self.assertIsNotNone(rows[0].caption)
        self.assertEqual(self.graphic.cursor_positions(), (0.2, 0.8))

    def test_overlay_mode_render_png(self):
        self.graphic.add_message(self.reader.find("canfd", 5, 0x17A))
        self.graphic.add_message(self.reader.find("canfd", 4, 0x100))
        self.graphic.set_overlay(True)
        self.graphic.resize(900, 600)
        target = os.path.join(self.tmpdir, "graphic_overlay.png")
        self.graphic.render_png(target)
        self.assertGreater(os.path.getsize(target), 2000)

    def test_render_png(self):
        self.graphic.add_message(self.reader.find("canfd", 5, 0x17A))
        self.graphic.resize(900, 600)
        target = os.path.join(self.tmpdir, "graphic.png")
        self.graphic.render_png(target)
        self.assertTrue(os.path.exists(target))
        self.assertGreater(os.path.getsize(target), 2000)

    def test_tree_double_click_adds_row(self):
        message_item = self.tree.topLevelItem(0).child(0)
        self.tree._on_expanded(message_item)
        byte_item = message_item.child(0)
        before = self.graphic.row_count()
        self.tree._on_double_clicked(byte_item, 0)
        self.assertEqual(self.graphic.row_count(), before + 1)

    def test_window_status_line(self):
        self.assertIn("帧数", self.window.status_label.text())
        self.assertIn("报文", self.window.status_label.text())

    # ---------------------------------------------------------------- M4：双光标测量
    def _add_counter_row(self):
        """加入 ch5 0x17A 的 byte1（值 = 帧序号 %256，10ms 周期）作为可预测的行。"""
        from signals.model import SignalSpec

        spec = SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="计数器")
        self.assertEqual(self.graphic.add_specs(spec), 1)
        return self.graphic.rows()[0]

    def test_cursor_toggle_and_initial_positions(self):
        self._add_counter_row()
        self.assertFalse(self.graphic.cursors_enabled)
        self.assertIsNone(self.graphic.delta_t())

        self.window.cursor_action.setChecked(True)          # 走主窗口动作
        self.assertTrue(self.graphic.cursors_enabled)
        x1, x2 = self.graphic.cursor_positions()
        self.assertLess(x1, x2)
        self.assertGreater(self.graphic.delta_t(), 0.0)

    def test_measurement_values_match_manual_lookup(self):
        row = self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_positions(0.505, 1.005)

        # 手工口径：0.505 -> 最后一个 x<=0.505 的帧（第 50 帧，值 50）；1.005 -> 第 100 帧，值 100
        self.assertAlmostEqual(self.graphic.delta_t(), 0.5, places=9)
        measured = self.graphic.measurements()
        self.assertEqual(len(measured), 1)
        self.assertEqual(measured[0]["text1"], "50")
        self.assertEqual(measured[0]["text2"], "100")
        self.assertEqual(measured[0]["text_delta"], "50")
        self.assertEqual(int(row.values[50]), 50)
        self.assertEqual(int(row.values[100]), 100)

    def test_measurement_before_first_sample_is_dash(self):
        self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_positions(-1.0, 0.5)
        measured = self.graphic.measurements()
        self.assertEqual(measured[0]["text1"], "—")
        self.assertEqual(measured[0]["text_delta"], "—")

    def test_header_shows_delta_t(self):
        self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_positions(0.25, 0.75)
        self.assertIn("ΔT = 0.500000 s", self.graphic.header.text())

    def test_measure_table_follows_cursors(self):
        self._add_counter_row()
        self.window.cursor_action.setChecked(True)
        self.graphic.set_cursor_positions(0.505, 1.005)
        self.window._refresh_measure_table()
        self.assertEqual(self.window.measure_table.rowCount(), 1)
        self.assertEqual(self.window.measure_table.item(0, 0).text(), "计数器")
        self.assertEqual(self.window.measure_table.item(0, 1).text(), "50")
        self.assertEqual(self.window.measure_table.item(0, 3).text(), "50")
        self.assertIn("ΔT = 0.500000 s", self.window.delta_label.text())

    def test_cursor_positions_survive_row_rebuild(self):
        self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_positions(0.2, 0.8)
        self.graphic.add_message(self.reader.find("canfd", 4, 0x100))   # 会再自动加若干行
        self.assertEqual(self.graphic.cursor_positions(), (0.2, 0.8))
        self.assertEqual(len(self.graphic.measurements()), self.graphic.row_count())
        self.assertGreater(self.graphic.row_count(), 1)

    # ---------------------------------------------------------------- M4：导出与布局
    def test_export_csv_from_window_rows(self):
        from utils import export as exporter

        self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_positions(0.0, 0.5)
        t0, t1 = self.window._export_range()
        self.assertAlmostEqual(t0, 0.0, places=9)
        self.assertAlmostEqual(t1, 0.5, places=9)

        target = os.path.join(self.tmpdir, "graphic.csv")
        x_label, x_unit = self.window._axis_labels()
        stats = exporter.export_wide_csv(target, self.graphic.rows(), t0, t1,
                                        x_label, x_unit)
        self.assertEqual(stats["columns"], 2)
        self.assertGreater(stats["rows"], 40)
        with open(target, "r", encoding="utf-8-sig", newline="") as handle:
            lines = handle.readlines()
        self.assertEqual(len(lines), stats["rows"] + 2)      # 双行表头 + 数据

    def test_layout_round_trip_through_window(self):
        from signals.model import SignalSpec
        from utils import export as exporter

        self._add_counter_row()
        spec = SignalSpec(key=("canfd", 4, 0x100), byte_offset=0, name="枚举")
        self.graphic.add_specs(spec)
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_positions(0.3, 0.9)
        self.graphic.set_time_mode(MODE_INDEX)

        target = os.path.join(self.tmpdir, "layout.json")
        exporter.save_layout(target, self.graphic.specs(), self.graphic.time_mode,
                             self.graphic.x_range(), self.graphic.cursor_positions())

        self.graphic.clear()
        self.assertEqual(self.graphic.row_count(), 0)

        layout = exporter.load_layout(target)
        added = self.graphic.set_specs(layout["rows"], layout["time_mode"])
        self.graphic.set_x_range(*layout["x_range"])
        self.graphic.set_cursor_positions(*layout["cursors"])
        self.assertEqual(added, 2)
        self.assertEqual(self.graphic.row_count(), 2)
        self.assertEqual(self.graphic.time_mode, MODE_INDEX)
        self.assertEqual(self.graphic.cursor_positions(), (0.3, 0.9))
        self.assertEqual(len(self.graphic.measurements()), 2)

    def test_render_png_with_cursors(self):
        self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        self.graphic.resize(900, 500)
        target = os.path.join(self.tmpdir, "graphic_cursor.png")
        self.graphic.render_png(target)
        self.assertGreater(os.path.getsize(target), 2000)

    # ---------------------------------------------------------------- 行排序
    def test_move_row_reorders_and_keeps_cursors(self):
        from signals.model import SignalSpec

        first = SignalSpec(key=("canfd", 5, 0x17A), byte_offset=0, name="A")
        second = SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="B")
        self.graphic.add_specs([first, second])
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_positions(0.1, 0.2)
        self.assertEqual([row.spec.name for row in self.graphic.rows()], ["A", "B"])

        self.assertEqual(self.graphic.move_row(1, -1), 0)         # B 上移
        self.assertEqual([row.spec.name for row in self.graphic.rows()], ["B", "A"])
        self.assertEqual(self.graphic.move_row(0, -1), 0)         # 已在顶部，不动
        self.assertEqual(self.graphic.move_row(0, 1), 1)          # B 下移
        self.assertEqual([row.spec.name for row in self.graphic.rows()], ["A", "B"])
        # 重排后仍能测量（光标位置与行数据都保留）
        self.assertEqual(self.graphic.cursor_positions(), (0.1, 0.2))
        self.assertEqual(len(self.graphic.measurements()), 2)


    # ---------------------------------------------------------------- M4 优化：光标手柄条
    def test_cursor_bar_visibility_follows_toggle(self):
        bar = self.graphic.cursor_bar
        self._add_counter_row()
        self.assertTrue(bar.isHidden())            # 未启用时不显示
        self.graphic.set_cursors_enabled(True)
        self.assertFalse(bar.isHidden())
        self.graphic.set_cursors_enabled(False)
        self.assertTrue(bar.isHidden())

    def test_cursor_bar_linear_mapping_round_trip(self):
        bar = self.graphic.cursor_bar
        bar.set_plot_ratio(0.2, 0.95)               # 绘图区占控件宽度的比例
        bar.set_x_range(10.0, 20.0)
        self.assertTrue(bar.span_ready)
        width = float(bar.width())
        self.assertAlmostEqual(bar.x_to_px(10.0), 0.2 * width, places=6)
        self.assertAlmostEqual(bar.x_to_px(20.0), 0.95 * width, places=6)
        self.assertAlmostEqual(bar.x_to_px(15.0), 0.575 * width, places=6)
        for x in (10.0, 12.345, 19.999):
            self.assertAlmostEqual(bar.px_to_x(bar.x_to_px(x)), x, places=9)

    def test_cursor_bar_press_snaps_when_far_from_handle(self):
        bar = self.graphic.cursor_bar
        bar.resize(1000, 22)
        bar.set_plot_ratio(0.0, 1.0)               # 0..1 → 0..1000 px
        bar.set_x_range(0.0, 1.0)
        bar.set_cursor_values(0.3, 0.7)
        # 离 T1（x=0.3 → 300px）5px：视为抓住 T1，不吸附
        self.assertEqual(bar.press_target(305.0), (0, None))
        # 离最近手柄 > 26px：返回吸附目标
        index, snap = bar.press_target(0.0)
        self.assertEqual(index, 0)
        self.assertAlmostEqual(snap, 0.0, places=9)
        # 点击位置更靠 T2 时选 T2（650px → x=0.65，距 T2 手柄 50px → 也触发吸附）
        index, snap = bar.press_target(650.0)
        self.assertEqual(index, 1)
        self.assertAlmostEqual(snap, 0.65, places=9)

    def test_cursor_bar_drag_updates_cursors_and_measurements(self):
        self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_snap(False)               # 关吸附 → 光标停在拖到的位置
        self.graphic.set_cursor_positions(0.1, 1.005)
        # 等价于"在条上把 T1 拖到 0.505 处"
        self.graphic.cursor_bar.dragged.emit(0, 0.505)
        self.assertAlmostEqual(self.graphic.cursor_positions()[0], 0.505, places=9)
        measured = self.graphic.measurements()
        self.assertEqual(measured[0]["text1"], "50")     # 第 50 帧值 = 50
        self.assertEqual(measured[0]["text2"], "100")    # 第 100 帧值 = 100
        self.assertIn("ΔT = 0.500000 s", self.graphic.header.text())

    def test_cursor_bar_ignores_drag_when_disabled(self):
        self._add_counter_row()
        self.graphic.set_cursors_enabled(False)
        self.graphic.cursor_bar.dragged.emit(0, 0.9)
        x1, x2 = self.graphic.cursor_positions()
        self.assertTrue(np.isnan(x1) and np.isnan(x2))    # 未启用时不接受拖动
        # 未启用双光标时仍给出行统计，但 T1/T2 列为 "—"
        measured = self.graphic.measurements()
        self.assertEqual(len(measured), self.graphic.row_count())
        self.assertEqual(measured[0]["text1"], "—")
        self.assertEqual(measured[0]["text_delta"], "—")

    def test_cursor_bar_drag_clamped_to_view_range(self):
        self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        lo, hi = self.graphic.x_range()
        self.graphic.cursor_bar.dragged.emit(1, hi + 1000.0)
        self.assertLessEqual(self.graphic.cursor_positions()[1], hi)
        self.graphic.cursor_bar.dragged.emit(0, lo - 1000.0)
        self.assertGreaterEqual(self.graphic.cursor_positions()[0], lo)

    def test_cursor_bar_span_synced_to_plot_area(self):
        self._add_counter_row()
        self.graphic.resize(1000, 600)
        self.app.processEvents()
        self.graphic.set_cursors_enabled(True)
        self.graphic._sync_cursor_bar()
        bar = self.graphic.cursor_bar
        self.assertTrue(bar.span_ready, "手柄条未取到绘图区比例边界")
        self.assertGreater(bar.px_left, 0.0)             # 左侧轴标签区之后
        self.assertGreater(bar.px_right, bar.px_left)
        # 手柄条像素位置随控件宽度自适应（窗口缩放后无需重新同步）
        narrow = bar.x_to_px(0.0)
        bar.resize(bar.width() + 400, 22)
        self.assertGreater(bar.x_to_px(0.0), narrow)

    # ------------------------------------------------- P0（Thursday）：读值 / 吸附 / 统计 / 极值
    def test_readout_follows_mouse_position(self):
        """鼠标读值：自由位置取"保持前一值"（与值表口径一致）。"""
        self._add_counter_row()
        self.graphic.set_cursor_snap(False)
        self.graphic.set_readout_x(0.505)
        self.assertEqual(self.graphic.readouts(), ["50"])        # 0.505 → 第 50 帧
        self.assertIn("鼠标 t = 0.505000", self.graphic.header.text())
        self.assertEqual(self.graphic.rows()[0].readout.textItem.toPlainText(), "50")

    def test_readout_snaps_to_sample_by_default(self):
        """默认吸附：读值位置落在真实采样点上，避免"看起来 40、其实 39"。"""
        row = self._add_counter_row()
        self.assertTrue(self.graphic.cursor_snap)
        self.graphic.set_readout_x(0.505)
        self.assertAlmostEqual(self.graphic.readout_x, float(row.x[50]), places=12)
        self.assertIn("已吸附采样点", self.graphic.header.text())
        self.assertEqual(self.graphic.readouts(), ["50"])

    def test_readout_can_be_cleared_and_disabled(self):
        self._add_counter_row()
        self.graphic.set_readout_x(0.5)
        self.assertIsNotNone(self.graphic.readout_x)
        self.graphic.set_readout_x(None)
        self.assertIsNone(self.graphic.readout_x)
        self.assertEqual(self.graphic.readouts(), [""])
        self.assertNotIn("鼠标", self.graphic.header.text())

        self.window.readout_action.setChecked(False)             # 走主窗口动作
        self.graphic.set_readout_x(0.5)
        self.assertIsNone(self.graphic.readout_x)                # 关闭后不接受读值
        self.window.readout_action.setChecked(True)

    def test_mouse_move_maps_scene_position_to_readout(self):
        from PySide6.QtCore import QPointF

        row = self._add_counter_row()
        self.graphic.resize(900, 400)
        self.app.processEvents()
        viewbox = row.plot.getViewBox()
        target = float(row.x[50])
        scene_pos = viewbox.mapViewToScene(QPointF(target, float(row.values[50])))
        self.graphic._on_scene_mouse_moved(scene_pos)
        self.assertIsNotNone(self.graphic.readout_x)
        self.assertEqual(self.graphic.readouts(), ["50"])

        self.graphic._on_scene_mouse_moved(QPointF(-5000.0, -5000.0))   # 移出绘图区
        self.assertIsNone(self.graphic.readout_x)

    def test_cursor_drag_snaps_to_sample(self):
        row = self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_snap(True)
        self.graphic.cursor_bar.dragged.emit(0, 0.505)
        self.assertAlmostEqual(self.graphic.cursor_positions()[0], float(row.x[50]),
                               places=12)
        self.assertEqual(self.graphic.measurements()[0]["text1"], "50")

    def test_cursor_drag_free_when_snap_disabled(self):
        self._add_counter_row()
        self.graphic.set_cursors_enabled(True)
        self.graphic.set_cursor_snap(False)
        self.graphic.cursor_bar.dragged.emit(0, 0.505)
        self.assertAlmostEqual(self.graphic.cursor_positions()[0], 0.505, places=9)
        self.assertEqual(self.graphic.measurements()[0]["text1"], "50")   # hold-last

    def test_row_stats_match_manual(self):
        row = self._add_counter_row()
        values = row.values
        self.assertEqual(row.stats["count"], len(values))
        for key, expected in (("min", values.min()), ("max", values.max()),
                              ("mean", values.mean()), ("std", values.std())):
            self.assertAlmostEqual(row.stats[key], float(expected), places=9, msg=key)
        self.assertEqual(row.min_text, "%d" % int(values.min()))
        self.assertEqual(row.max_text, "%d" % int(values.max()))

    def test_value_table_shows_stats_without_cursors(self):
        row = self._add_counter_row()
        self.assertFalse(self.graphic.cursors_enabled)
        self.window._refresh_measure_table()
        self.assertEqual(self.window.measure_table.rowCount(), 1)
        self.assertEqual(self.window.measure_table.item(0, 1).text(), "—")     # T1
        self.assertEqual(self.window.measure_table.item(0, 2).text(), "—")     # T2
        self.assertEqual(self.window.measure_table.item(0, 4).text(), row.min_text)
        self.assertEqual(self.window.measure_table.item(0, 5).text(), row.max_text)
        self.assertIn("行统计", self.window.delta_label.text())

    def test_extreme_markers_show_and_toggle(self):
        row = self._add_counter_row()
        self.assertTrue(self.graphic.show_extremes)
        self.assertEqual(len(row.extreme_lines), 2)              # min + max
        values = sorted(line.value() for line in row.extreme_lines)
        self.assertAlmostEqual(values[0], row.stats["min"], places=9)
        self.assertAlmostEqual(values[1], row.stats["max"], places=9)

        self.window.extremes_action.setChecked(False)            # 走主窗口动作
        self.assertEqual(row.extreme_lines, [])
        self.window.extremes_action.setChecked(True)
        self.assertEqual(len(row.extreme_lines), 2)

    def test_row_level_extreme_override(self):
        """单行可以单独关掉极值线，不影响别的行；也能恢复跟随全局。"""
        from signals.model import SignalSpec

        # A = 0x17A 的 byte1（帧序号，变化）；B = 0x100 的 byte0（0/1/2 变化）
        self.graphic.add_specs([
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="A"),
            SignalSpec(key=("canfd", 4, 0x100), byte_offset=0, name="B")])
        first, second = self.graphic.rows()
        self.assertEqual(len(first.extreme_lines), 2)
        self.assertEqual(len(second.extreme_lines), 2)

        self.graphic.set_row_extremes(0, False)                  # 只关第一行
        self.assertEqual(first.extreme_lines, [])
        self.assertEqual(len(second.extreme_lines), 2)
        self.assertFalse(self.graphic.row_extremes(0))
        self.assertTrue(self.graphic.row_extremes(1))
        self.assertIs(first.spec.extreme_marks, False)

        self.window.extremes_action.setChecked(False)            # 全局关：行级 False 的行不受影响
        self.graphic.set_row_extremes(0, True)                   # 第一行强制显示
        self.assertEqual(len(first.extreme_lines), 2)
        self.assertEqual(second.extreme_lines, [])               # 第二行跟随全局 → 关
        self.window.extremes_action.setChecked(True)

        self.graphic.set_row_extremes(0, None)                   # 恢复跟随全局
        self.assertIsNone(first.spec.extreme_marks)
        self.assertEqual(len(first.extreme_lines), 2)

    def test_value_table_extreme_checkbox_controls_row(self):
        from PySide6.QtCore import Qt as QtCore

        from signals.model import SignalSpec

        self.graphic.add_specs([
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="A"),
            SignalSpec(key=("canfd", 4, 0x100), byte_offset=0, name="B")])
        self.window._refresh_measure_table()
        self.assertEqual(self.window.measure_table.rowCount(), 2)
        first, second = self.graphic.rows()
        self.assertEqual(len(first.extreme_lines), 2)

        # 取消第一行的勾选 → 只关这一行
        cell = self.window.measure_table.item(0, self.app_main.EXTREME_COLUMN)
        cell.setCheckState(QtCore.CheckState.Unchecked)
        self.assertEqual(first.extreme_lines, [])
        self.assertEqual(len(second.extreme_lines), 2)
        self.assertIs(first.spec.extreme_marks, False)

        # 再勾回来 → 该行恢复
        cell.setCheckState(QtCore.CheckState.Checked)
        self.assertEqual(len(first.extreme_lines), 2)
        self.assertIs(first.spec.extreme_marks, True)

    def test_extreme_override_survives_layout_round_trip(self):
        from signals.model import SignalSpec
        from utils import export as exporter

        spec = SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="A")
        self.graphic.add_specs(spec)
        self.graphic.set_row_extremes(0, False)

        target = os.path.join(self.tmpdir, "layout_extreme.json")
        exporter.save_layout(target, self.graphic.specs(), self.graphic.time_mode)
        layout = exporter.load_layout(target)
        self.assertIs(layout["rows"][0].extreme_marks, False)

        self.graphic.clear()
        self.graphic.set_specs(layout["rows"], layout["time_mode"])
        self.assertEqual(self.graphic.rows()[0].spec.extreme_marks, False)
        self.assertEqual(self.graphic.rows()[0].extreme_lines, [])

    # ------------------------------------------------- 采样点标记（默认显示，按密度自动出现）
    def test_point_markers_shown_by_default(self):
        row = self._add_counter_row()
        self.assertTrue(self.graphic.show_symbols)
        self.assertTrue(row.marker_on)                           # 200 点 ≪ 阈值 → 显示
        self.assertEqual(row.curve.opts["symbol"], "o")
        self.assertEqual(row.visible_points, len(row.x))

    def test_point_markers_hidden_when_too_dense(self):
        from unittest import mock

        row = self._add_counter_row()
        self.assertTrue(row.marker_on)
        with mock.patch.object(graphic_window, "SYMBOL_MAX_POINTS", 100):
            self.graphic._update_point_markers(force=True)       # 200 点 > 100 → 自动隐藏
            self.assertFalse(row.marker_on)
            self.assertIsNone(row.curve.opts["symbol"])
        self.graphic._update_point_markers(force=True)           # 还原阈值 → 又显示
        self.assertTrue(row.marker_on)

    def test_point_markers_follow_global_switch(self):
        row = self._add_counter_row()
        self.window.symbols_action.setChecked(False)
        self.assertFalse(row.marker_on)
        self.assertIsNone(row.curve.opts["symbol"])
        self.window.symbols_action.setChecked(True)
        self.assertTrue(row.marker_on)
        self.assertEqual(row.curve.opts["symbol"], "o")

    def test_mouse_move_through_real_event_chain(self):
        """真机链路回归：真实鼠标事件 → scene.sigMouseMoved → SignalProxy → 读值。

        历史 bug：SignalProxy 把参数打包成 tuple 转发，槽函数按单个 QPointF 解析
        → `QRectF.contains(tuple)` 抛 TypeError，异常被 Qt 吞掉，表现为"鼠标划过没反应"。
        """
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest

        row = self._add_counter_row()
        self.window.resize(900, 500)
        self.window.show()
        self.app.processEvents()

        viewport = self.graphic.glw.viewport()
        rect = viewport.rect()
        QTest.mouseMove(viewport, QPoint(4, 4))
        QTest.qWait(60)                        # SignalProxy 有 60 Hz 限流，等它 flush
        QTest.mouseMove(viewport, QPoint(int(rect.width() * 0.5),
                                         int(rect.height() * 0.4)))
        QTest.qWait(60)

        self.assertIsNotNone(self.graphic.readout_x, "真实鼠标移动没有触发读值")
        self.assertIn("已吸附采样点", self.graphic.header.text())
        self.assertTrue(self.graphic.readouts()[0], "读值文本为空")
        self.assertEqual(row.readout.textItem.toPlainText(), self.graphic.readouts()[0])

    def test_y_axis_title_prefers_dbc_name_and_unit(self):
        from signals.model import SignalSpec

        self.graphic.add_specs([
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="ActRotSpd", unit="rpm"),
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=2, name="TorqueLim", unit=""),
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=0,
                       name="E_IPUR_General.E_AVeryLongSignalName", unit="")])
        rows = self.graphic.rows()
        self.assertEqual(rows[0].plot.getAxis("left").labelText, "[rpm]")   # 优先单位
        self.assertEqual(rows[1].plot.getAxis("left").labelText, "TorqueLim")
        # 太长（竖排轴标题受行高限制）→ 截断，完整名字仍在行内左上角
        self.assertEqual(rows[2].plot.getAxis("left").labelText, "E_AVeryLongSignal…")
        self.assertEqual(rows[2].label_text, "E_IPUR_General.E_AVeryLongSignalName")

    def test_y_axis_uses_dbc_enum_text_ticks(self):
        """枚举信号（DBC 的 VAL_）→ Y 轴刻度直接用文本，而不是 0/1/2 数字。

        setTicks 的自定义刻度存在 AxisItem._tickLevels 里，绘制时直接取用
        （tickValues/tickStrings 是"自动刻度"路径，看不到自定义刻度）。
        """
        from signals.model import SignalSpec

        self.graphic.add_specs(
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="StMode", unit="",
                       labels={0: "Off", 1: "Standby", 2: "Ready"}))
        axis = self.graphic.rows()[0].plot.getAxis("left")
        self.assertEqual(axis._tickLevels,
                         [[(0.0, "Off"), (1.0, "Standby"), (2.0, "Ready")]])

        # 枚举值过多时不做文本刻度（否则挤成一团）
        many = {value: "S%d" % value for value in range(graphic_window.MAX_TICK_LABELS + 1)}
        self.graphic.clear()
        self.graphic.add_specs(
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="TooMany", labels=many))
        self.assertIsNone(self.graphic.rows()[0].plot.getAxis("left")._tickLevels)

    def test_overlay_axis_text_reports_shared_unit(self):
        from signals.model import SignalSpec

        self.graphic.add_specs([
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="A", unit="rpm"),
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=2, name="B", unit="rpm")])
        self.window.overlay_action.setChecked(True)
        self.assertEqual(self.graphic.rows()[0].plot.getAxis("left").labelText, "[rpm]")

        self.graphic.clear()
        self.graphic.add_specs([
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="A", unit="rpm"),
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=2, name="B", unit="V")])
        self.assertEqual(self.graphic.rows()[0].plot.getAxis("left").labelText, "多信号叠加")

    def test_constant_row_has_no_extreme_markers(self):
        from signals.model import SignalSpec

        # 样本里 0x17A 的 byte0 恒为 0x11
        self.graphic.add_specs(SignalSpec(key=("canfd", 5, 0x17A), byte_offset=0,
                                          name="常量字节"))
        row = self.graphic.rows()[0]
        self.assertEqual(row.stats["span"], 0.0)
        self.assertEqual(row.extreme_lines, [])                  # 常量行不画两条重合线

    def test_stats_survive_mode_and_overlay_switch(self):
        row = self._add_counter_row()
        before = dict(row.stats)
        self.graphic.set_time_mode(MODE_INDEX)
        self.window.overlay_action.setChecked(True)
        self.assertEqual(row.stats, before)                      # 统计只与数值有关
        self.assertEqual(len(row.extreme_lines), 2)              # 叠加模式同样标记极值
        self.window.overlay_action.setChecked(False)
        self.assertEqual(row.stats, before)

    def test_every_row_gets_both_cursor_lines(self):
        """双光标在每一行都要有镜像线，且各行位置一致。"""
        from signals.model import SignalSpec

        self.graphic.add_specs([
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="A"),
            SignalSpec(key=("canfd", 4, 0x100), byte_offset=0, name="B")])
        self.assertEqual(self.graphic.row_count(), 2)
        self.window.cursor_action.setChecked(True)
        self.assertEqual(len(self.graphic._cursor_lines), 4)     # 每行 T1 + T2
        self.graphic.cursor_bar.dragged.emit(0, 0.505)
        values = {round(line.value(), 6) for _i, _c, line in self.graphic._cursor_lines}
        self.assertEqual(len(values), 2)                         # 只有 T1/T2 两个位置

    def test_overlay_readout_merges_all_rows(self):
        """叠加模式共用一处读值：多行合并显示 "信号 = 值"。"""
        from signals.model import SignalSpec

        self.graphic.add_specs([
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=1, name="A"),
            SignalSpec(key=("canfd", 5, 0x17A), byte_offset=0, name="B")])
        self.window.overlay_action.setChecked(True)
        self.graphic.set_readout_x(0.505)
        owners = [row for row in self.graphic.rows() if row.readout is not None]
        self.assertEqual(len(owners), 1)                         # 只有一行的图元承载读值
        text = owners[0].readout.textItem.toPlainText()
        self.assertIn("A = ", text)
        self.assertIn("B = ", text)
        self.assertIn("\n", text)                                # 多行合并


DBC_TEXT = '''VERSION "1.0"

NS_ :

BS_:

BU_: ECU

BO_ 378 TestMsg: 3 ECU
 SG_ Counter : 8|8@1+ (1,0) [0|255] "次" ECU
 SG_ Flag : 16|1@1+ (1,0) [0|1] "" ECU

VAL_ 378 Flag 0 "关" 1 "开" ;
'''


@unittest.skipUnless(HAS_QT, "PySide6 未安装，跳过 GUI 冒烟测试")
class TestDbcIntegration(unittest.TestCase):
    """DBC 接入：加载 → 映射通道 → 树里显示信号 → 双击成行（端到端）。"""

    @classmethod
    def setUpClass(cls):
        import main as app_main

        cls.app_main = app_main
        cls.app = QApplication.instance() or QApplication([])
        cls.tmpdir = tempfile.mkdtemp(prefix="asc_graphic_dbc_")
        cls.asc_path = os.path.join(cls.tmpdir, "sample.asc")
        build_sample_asc(cls.asc_path)
        build_index(cls.asc_path, index_path=default_index_path(cls.asc_path))
        cls.dbc_dir = os.path.join(cls.tmpdir, "dbc")
        os.makedirs(cls.dbc_dir)
        with open(os.path.join(cls.dbc_dir, "CH5.dbc"), "w", encoding="utf-8") as fh:
            fh.write(DBC_TEXT)

    @classmethod
    def tearDownClass(cls):
        for root, _dirs, files in os.walk(cls.tmpdir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)

    def setUp(self):
        self.window = self.app_main.MainWindow()
        self.window._activate_reader(self.asc_path)
        self.window.show()
        self.app.processEvents()
        self.graphic = self.window.graphic
        self.tree = self.window.tree
        self._enable_dbc()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()

    def _enable_dbc(self):
        from signals.dbc_loader import DbcLibrary

        library = DbcLibrary(self.dbc_dir)
        library.add_mapping("CH5.dbc", [5], "canfd", "测试")
        library.load_all()
        self.window.dbc = library
        self.window._apply_dbc()

    def _message_item(self, can_id_text="0x17A"):
        for index in range(self.window.tree.topLevelItemCount()):
            channel_item = self.window.tree.topLevelItem(index)
            for child_index in range(channel_item.childCount()):
                item = channel_item.child(child_index)
                if item.text(0).startswith(can_id_text):
                    return item
        raise AssertionError("树里找不到 %s" % can_id_text)

    def test_tree_shows_dbc_message_and_signals(self):
        item = self._message_item()
        self.assertIn("TestMsg", item.text(0))                 # 报文名来自 DBC
        self.assertIn("CH5.dbc", item.text(3))                 # 来源列标注 DBC
        self.window.tree._on_expanded(item)
        children = [item.child(i) for i in range(item.childCount())]
        kinds = [child.data(0, ROLE_KIND)[0] for child in children if child.data(0, ROLE_KIND)]
        self.assertEqual(kinds, ["dbc-signal", "dbc-signal"])   # 不再是字节画像
        self.assertIn("Counter", children[0].text(0))
        self.assertIn("[次]", children[0].text(0))              # 单位来自 DBC

    def test_double_click_dbc_signal_adds_row(self):
        item = self._message_item()
        self.tree._on_expanded(item)
        spec = item.child(0).data(0, ROLE_KIND)[1]
        self.assertIn("TestMsg.Counter", spec.display_name())
        self.assertEqual(spec.unit, "次")
        self.assertEqual(spec.source, "dbc")

        before = self.graphic.row_count()
        self.tree._on_double_clicked(item.child(0), 0)
        self.assertEqual(self.graphic.row_count(), before + 1)
        row = self.graphic.rows()[-1]
        self.assertIn("TestMsg.Counter", row.name)
        # 数据来自真实索引：byte1 = 帧序号 % 256
        self.assertAlmostEqual(float(row.values[0]), 0.0, places=6)
        self.assertAlmostEqual(float(row.values[7]), 7.0, places=6)

    def test_enum_labels_apply_to_values(self):
        item = self._message_item()
        self.tree._on_expanded(item)
        flag_spec = item.child(1).data(0, ROLE_KIND)[1]
        self.assertEqual(flag_spec.labels, {0: "关", 1: "开"})
        self.assertTrue(flag_spec.step)
        self.graphic.add_specs(flag_spec)
        series = self.graphic.rows()[0].series
        self.assertEqual(series.value_text[0], "关")            # 合成数据 byte2 = 0x02 → bit0 = 0

    def test_clear_dbc_falls_back_to_byte_profile(self):
        self.window.dbc.clear()
        self.window._apply_dbc()
        item = self._message_item()
        self.assertNotIn("TestMsg", item.text(0))
        self.tree._on_expanded(item)
        kinds = {child.data(0, ROLE_KIND)[0] for child in
                 (item.child(i) for i in range(item.childCount()))
                 if child.data(0, ROLE_KIND)}
        self.assertEqual(kinds, {"byte"})                       # 回到逐字节画像

    def test_mapping_dialog_needs_no_frame_type_choice(self):
        """导入 DBC 只需选文件 + 通道；帧类型由 DBC 自动识别并显示出来。"""
        from PySide6.QtCore import Qt as QtCore

        from ui.dialogs import DbcMappingDialog

        dialog = DbcMappingDialog(self.dbc_dir,
                                  [("canfd", 5, 30000), ("classic", 3, 10)],
                                  library=self.window.dbc)
        self.assertFalse(hasattr(dialog, "kind_box"))           # 不再有帧类型下拉框
        self.assertEqual(dialog.channel_list.count(), 2)
        dialog.discovered.setCurrentIndex(1)                    # 选中 CH5.dbc
        summary = dialog.summary_label.text()
        self.assertIn("自动识别", summary)
        self.assertIn("1 报文 / 2 信号", summary)               # DBC 规模摘要
        for index in range(dialog.channel_list.count()):
            dialog.channel_list.item(index).setCheckState(QtCore.CheckState.Checked)
        path, channels, comment = dialog.result_mapping()
        self.assertEqual(os.path.basename(path), "CH5.dbc")
        self.assertEqual(channels, [3, 5])
        self.assertEqual(comment, "")

    def test_length_mismatch_warning_shown(self):
        """DBC 报文长度与日志不一致时给出提示节点。"""
        from signals.dbc_loader import DbcLibrary

        mismatch_dir = os.path.join(self.tmpdir, "dbc_mismatch")
        os.makedirs(mismatch_dir, exist_ok=True)
        with open(os.path.join(mismatch_dir, "M.dbc"), "w", encoding="utf-8") as fh:
            fh.write(DBC_TEXT.replace("TestMsg: 3", "TestMsg: 8"))
        library = DbcLibrary(mismatch_dir)
        library.add_mapping("M.dbc", [5], "canfd")
        library.load_all()
        self.window.dbc = library
        self.window._apply_dbc()
        item = self._message_item()
        self.tree._on_expanded(item)
        self.assertIn("不一致", item.child(0).text(0))


@unittest.skipUnless(HAS_QT, "PySide6 未安装，跳过 GUI 冒烟测试")
class TestLaunchHelpers(unittest.TestCase):
    """命令行联动辅助函数（与 Vehicle_log_tool_AI 对接用）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="asc_launch_")
        import main as app_main

        self.app_main = app_main

    def tearDown(self):
        for root, _dirs, files in os.walk(self.tmpdir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)

    def test_resolve_asc_path_file_and_dir(self):
        old = os.path.join(self.tmpdir, "a.asc")
        new = os.path.join(self.tmpdir, "b.asc")
        for path, mtime in ((old, 1000), (new, 2000)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("base hex  timestamps absolute\n")
            os.utime(path, (mtime, mtime))
        with open(os.path.join(self.tmpdir, "note.txt"), "w") as fh:
            fh.write("x")

        self.assertEqual(self.app_main.resolve_asc_path(new), new)
        self.assertEqual(self.app_main.resolve_asc_path(self.tmpdir), new)  # 取最新
        self.assertEqual(self.app_main.resolve_asc_path(""), "")
        self.assertEqual(self.app_main.resolve_asc_path(os.path.join(self.tmpdir, "no.asc")),
                         os.path.join(self.tmpdir, "no.asc"))

    def test_resolve_asc_path_dir_without_asc(self):
        empty = os.path.join(self.tmpdir, "empty")
        os.makedirs(empty)
        self.assertEqual(self.app_main.resolve_asc_path(empty), "")

    def test_parse_channel_spec(self):
        parse = self.app_main.parse_channel_spec
        self.assertEqual(parse("canfd:5"), [("canfd", [5])])
        self.assertEqual(parse("canfd:5,9"), [("canfd", [5, 9])])
        self.assertEqual(parse("canfd:5,classic:3"),
                         [("canfd", [5]), ("classic", [3])])
        self.assertEqual(parse("canfd:5,9,classic:3"),
                         [("canfd", [5, 9]), ("classic", [3])])
        self.assertEqual(parse("canfd:5+9"), [("canfd", [5, 9])])
        self.assertEqual(parse("5,9"), [("", [5, 9])])          # 只给通道号 → 帧类型自动
        self.assertEqual(parse("bogus:5"), [])
        self.assertEqual(parse(""), [])


if __name__ == "__main__":
    unittest.main()
