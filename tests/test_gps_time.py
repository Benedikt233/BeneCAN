# -*- coding: utf-8 -*-
"""0x3A5 RTC → GPS 时间轴：字段解码、异常样本过滤、分段与格式化。"""

import os
import sys
import unittest
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.gps_time import (GpsTimeAxis, RtcLayout, RtcSample,
                              build_gps_segments)


class TestRtcLayout(unittest.TestCase):

    def test_decode_known_payloads(self):
        layout = RtcLayout()
        # logCAN572：2026-07-13 09:31:07
        self.assertEqual(layout.decode([0x0b, 7, 0x8d, 9, 31, 7, 0x28, 0]),
                         datetime(2026, 7, 13, 9, 31, 7))
        # logCAN8：2026-09-22 13:59:07
        self.assertEqual(layout.decode([0x0b, 9, 0x96, 13, 59, 7, 0x28, 0]),
                         datetime(2026, 9, 22, 13, 59, 7))

    def test_day_is_low_five_bits(self):
        # 0x96 = 1001 0110 → day = 22；高位不参与 day
        layout = RtcLayout()
        self.assertEqual(layout.decode([0x0b, 9, 0x96, 13, 59, 7, 0x28, 0]).day, 22)
        self.assertEqual(layout.decode([0x0b, 9, 0x16, 13, 59, 7, 0x00, 0]).day, 22)

    def test_invalid_fields_return_none(self):
        layout = RtcLayout()
        self.assertIsNone(layout.decode([0x0b, 0, 0x96, 13, 59, 7, 0, 0]))   # month 0
        self.assertIsNone(layout.decode([0x0b, 13, 0x96, 13, 59, 7, 0, 0]))  # month 13
        self.assertIsNone(layout.decode([0x0b, 9, 0x80, 13, 59, 7, 0, 0]))   # day 0
        self.assertIsNone(layout.decode([0x0b, 9, 0x96, 24, 59, 7, 0, 0]))   # hour 24
        self.assertIsNone(layout.decode([0x0b, 9, 0x96, 13, 60, 7, 0, 0]))   # minute 60
        self.assertIsNone(layout.decode([0x0b, 9, 0x96, 13, 59, 61, 0, 0]))  # second 61
        self.assertIsNone(layout.decode([0x0b, 9, 0x96]))
        self.assertIsNone(layout.decode(None))

    def test_custom_year_base(self):
        layout = RtcLayout(year_base=2000)
        self.assertEqual(layout.decode([0x1a, 9, 0x96, 13, 59, 7, 0, 0]).year, 2026)


class TestGpsSegments(unittest.TestCase):

    def _samples(self):
        base = datetime(2026, 9, 22, 13, 59, 7)
        samples = []
        for index in range(11):                       # 主时段 0..10
            samples.append(RtcSample(float(index), base + timedelta(seconds=index)))
        for index in range(4, 10):                    # 交织的另一套时间源
            samples.append(RtcSample(index + 0.5,
                                     base + timedelta(minutes=19, seconds=39 + index)))
        reset = datetime(2026, 9, 22, 13, 58, 52)
        for index in range(5):                        # 5 帧连续回退 → 确认新段
            samples.append(RtcSample(20.0 + index, reset + timedelta(seconds=index)))
        return samples

    def test_outliers_are_ignored_and_reset_starts_new_segment(self):
        segments, used = build_gps_segments(self._samples(), 0.0, 30.0)
        self.assertEqual(len(segments), 2)
        self.assertEqual(used, 16)                    # 11 主 + 5 回退
        self.assertAlmostEqual(segments[0].ts_start, 0.0)
        self.assertAlmostEqual(segments[0].ts_end, 20.0)
        self.assertAlmostEqual(segments[1].ts_start, 20.0)

    def test_axis_is_monotonic_but_labels_jump_at_segment(self):
        axis = GpsTimeAxis.from_samples(self._samples(), 0.0, 30.0)
        self.assertIsNotNone(axis)
        x = axis.to_x([0.0, 10.0, 20.0, 24.0])
        self.assertTrue(np.all(np.diff(x) >= 0))
        self.assertEqual(axis.format_full(float(x[0])), "2026-09-22 13:59:07")
        self.assertEqual(axis.format_full(float(x[1])), "2026-09-22 13:59:17")
        self.assertEqual(axis.format_full(float(x[2])), "2026-09-22 13:58:52")
        self.assertEqual(axis.format_full(float(x[3])), "2026-09-22 13:58:56")
        self.assertEqual(axis.outlier_count, 6)

    def test_axis_interpolates_between_rtc_samples(self):
        axis = GpsTimeAxis.from_samples(self._samples(), 0.0, 30.0)
        value = axis.to_x([2.25])[0]
        self.assertEqual(axis.format_full(float(value)), "2026-09-22 13:59:09.250")
        self.assertEqual(axis.format_x(float(value)), "13:59:09")

    def test_format_x_switches_date_and_milliseconds(self):
        axis = GpsTimeAxis.from_samples(self._samples(), 0.0, 30.0)
        x = axis.to_x([0.0])[0]
        self.assertEqual(axis.format_x(float(x)), "13:59:07")
        self.assertEqual(axis.format_x(float(x), include_date=True), "09-22 13:59:07")
        self.assertEqual(axis.format_x(float(x), include_date=True, with_ms=True),
                         "09-22 13:59:07.000")


class FakeMessage:
    kind = "canfd"
    channel = 1
    can_id = 0x3A5


class FakeSeries:
    def __init__(self):
        self.ts = np.arange(6, dtype=np.float64)
        rows = []
        for second in range(7, 13):
            rows.append([0x0b, 9, 0x96, 13, 59, second, 0x28, 0])
        self.data = np.asarray(rows, dtype=np.uint8)


class FakeReader:
    metadata = {"ts_first": 0.0, "ts_last": 5.0}

    def catalog(self):
        return [FakeMessage()]

    def frame_series(self, kind, channel, can_id):
        return FakeSeries()


class TestGpsFromReader(unittest.TestCase):

    def test_from_reader_uses_0x3a5_channel(self):
        axis = GpsTimeAxis.from_reader(FakeReader())
        self.assertIsNotNone(axis)
        self.assertIn("ch1", axis.source)
        self.assertEqual(axis.sample_count, 6)
        self.assertEqual(axis.format_full(float(axis.to_x([0.0])[0])),
                         "2026-09-22 13:59:07")
        self.assertEqual(axis.format_full(float(axis.to_x([5.0])[0])),
                         "2026-09-22 13:59:12")

    def test_reader_without_0x3a5_returns_none(self):
        class EmptyReader(FakeReader):
            def catalog(self):
                return []

        self.assertIsNone(GpsTimeAxis.from_reader(EmptyReader()))


if __name__ == "__main__":
    unittest.main()
