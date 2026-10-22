# -*- coding: utf-8 -*-
"""导出与布局回归测试（M4，纯逻辑，不依赖 Qt）。

重点验证"保持前一值"语义（CANoe 的值表/导出口径）与布局往返一致性。
运行： python -m unittest discover -s tests
"""

import csv
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.model import SignalSpec  # noqa: E402
from utils import export as exporter  # noqa: E402


class FakeRow:
    """最小行对象：只需 name / x / values / spec。"""

    def __init__(self, name, x, values, spec=None):
        self.name = name
        self.x = np.asarray(x, dtype=np.float64)
        self.values = np.asarray(values, dtype=np.float64)
        self.spec = spec or SignalSpec(key=("canfd", 5, 0x17A), byte_offset=0, name=name)


class TestExportMath(unittest.TestCase):

    def setUp(self):
        self.row_a = FakeRow("A", [1.0, 2.0, 4.0], [10.0, 20.0, 40.0])
        self.row_b = FakeRow("B", [1.5, 3.0], [100.0, 300.0])

    def test_union_x_is_sorted_unique(self):
        merged = exporter.union_x([self.row_a, self.row_b])
        self.assertEqual(list(merged), [1.0, 1.5, 2.0, 3.0, 4.0])

    def test_union_x_time_range(self):
        merged = exporter.union_x([self.row_a, self.row_b], t0=1.5, t1=3.0)
        self.assertEqual(list(merged), [1.5, 2.0, 3.0])

    def test_hold_values_keeps_previous_sample(self):
        merged = exporter.union_x([self.row_a, self.row_b])
        held = exporter.hold_values(merged, self.row_a.x, self.row_a.values)
        # 1.0→10（本次）, 1.5→10（保持）, 2.0→20, 3.0→20（保持）, 4.0→40
        self.assertEqual(list(held), [10.0, 10.0, 20.0, 20.0, 40.0])

    def test_hold_values_before_first_sample_is_nan(self):
        merged = np.array([0.5, 1.0, 2.0])
        held = exporter.hold_values(merged, self.row_a.x, self.row_a.values)
        self.assertTrue(np.isnan(held[0]))
        self.assertEqual(list(held[1:]), [10.0, 20.0])

    def test_hold_values_empty_inputs(self):
        self.assertEqual(len(exporter.hold_values(np.array([]), self.row_a.x,
                                                  self.row_a.values)), 0)
        nan = exporter.hold_values(np.array([1.0, 2.0]), np.array([]), np.array([]))
        self.assertTrue(np.isnan(nan).all())

    def test_build_wide_table_shape_and_names(self):
        x_union, columns, names = exporter.build_wide_table([self.row_a, self.row_b])
        self.assertEqual(len(x_union), 5)
        self.assertEqual(names, ["A", "B"])
        self.assertEqual(list(columns[0][:3]), [10.0, 10.0, 20.0])      # A 全程有值
        self.assertTrue(np.isnan(columns[1][0]))                        # B 首个采样前为空
        self.assertEqual(list(columns[1][1:3]), [100.0, 100.0])

    def test_build_wide_table_row_limit(self):
        with self.assertRaises(ValueError) as ctx:
            exporter.build_wide_table([self.row_a], max_rows=2)
        self.assertIn("上限", str(ctx.exception))


class TestExportCsv(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="asc_export_test_")
        self.path = os.path.join(self.tmpdir, "out.csv")

    def tearDown(self):
        for name in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, name))
        os.rmdir(self.tmpdir)

    def test_csv_content_and_headers(self):
        rows = [FakeRow("车速", [1.0, 2.0], [5.0, 15.0]),
                FakeRow("状态", [1.0, 2.0], [0.0, 1.0])]
        stats = exporter.export_wide_csv(self.path, rows, x_label="时间", x_unit="s")
        self.assertEqual(stats["rows"], 2)
        self.assertEqual(stats["columns"], 3)
        with open(self.path, "r", encoding="utf-8-sig", newline="") as handle:
            lines = list(csv.reader(handle))
        self.assertEqual(lines[0][0], "时间(s)")
        self.assertEqual(lines[0][1:], ["车速", "状态"])          # 中文表头
        self.assertEqual(lines[1][0], "时间_s")                   # 英文标识行
        self.assertIn("byte0", lines[1][1])
        self.assertEqual(lines[2], ["1.000000", "5", "0"])
        self.assertEqual(lines[3], ["2.000000", "15", "1"])

    def test_csv_respects_time_range(self):
        rows = [FakeRow("A", [1.0, 2.0, 3.0], [1.0, 2.0, 3.0])]
        exporter.export_wide_csv(self.path, rows, t0=2.0, t1=3.0)
        with open(self.path, "r", encoding="utf-8-sig", newline="") as handle:
            lines = list(csv.reader(handle))
        self.assertEqual(len(lines), 4)                            # 2 表头 + 2 数据
        self.assertEqual(lines[2][0], "2.000000")

    def test_csv_empty_before_first_sample(self):
        rows = [FakeRow("A", [2.0], [7.0]), FakeRow("B", [1.0], [9.0])]
        exporter.export_wide_csv(self.path, rows)
        with open(self.path, "r", encoding="utf-8-sig", newline="") as handle:
            lines = list(csv.reader(handle))
        # t=1.0 时 A 还没有值 → 空单元格
        self.assertEqual(lines[2][1], "")
        self.assertEqual(lines[2][2], "9")


class TestLayout(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="asc_layout_test_")
        self.path = os.path.join(self.tmpdir, "layout.json")
        self.spec = SignalSpec(key=("canfd", 5, 0x17A), byte_offset=14, size=2,
                              dtype="uint16", byte_order="big", scale=0.5,
                              offset=-10.0, labels={0: "关", 1: "开"},
                              color="#ff0000", name="车速", unit="km/h", step=True)

    def tearDown(self):
        for name in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, name))
        os.rmdir(self.tmpdir)

    def test_save_load_round_trip(self):
        exporter.save_layout(self.path, [self.spec], time_mode="session",
                             x_range=(1.0, 2.0), cursors=(1.2, 1.8), source="x.asc",
                             single_cursor=1.5, auto_y=False)
        layout = exporter.load_layout(self.path)
        self.assertEqual(layout["time_mode"], "session")
        self.assertEqual(layout["x_range"], (1.0, 2.0))
        self.assertEqual(layout["cursors"], (1.2, 1.8))
        self.assertEqual(layout["single_cursor"], 1.5)
        self.assertIs(layout["auto_y"], False)
        self.assertEqual(len(layout["rows"]), 1)
        restored = layout["rows"][0]
        self.assertEqual(restored.key, self.spec.key)
        self.assertEqual(restored.byte_order, "big")
        self.assertEqual(restored.scale, 0.5)
        self.assertEqual(restored.offset, -10.0)
        self.assertEqual(restored.labels, {0: "关", 1: "开"})   # 键类型要还原成 int
        self.assertEqual(restored.name, "车速")
        self.assertEqual(restored.unit, "km/h")
        self.assertTrue(restored.step)
        self.assertEqual(restored.to_dict(), self.spec.to_dict())

    def test_layout_json_is_readable(self):
        exporter.save_layout(self.path, [self.spec], source="x.asc")
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["tool"], "BeneCAN")
        self.assertEqual(payload["rows"][0]["key"], ["canfd", 5, 0x17A])

    def test_load_layout_without_optional_fields(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"rows": [self.spec.to_dict()]}, handle)
        layout = exporter.load_layout(self.path)
        self.assertEqual(layout["time_mode"], "time")
        self.assertIsNone(layout["x_range"])
        self.assertIsNone(layout["cursors"])
        self.assertIsNone(layout["single_cursor"])
        self.assertIsNone(layout["auto_y"])


if __name__ == "__main__":
    unittest.main()
