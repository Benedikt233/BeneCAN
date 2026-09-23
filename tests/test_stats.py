# -*- coding: utf-8 -*-
"""行统计与采样点吸附的纯函数测试（不依赖 Qt / 索引）。

运行： python -m unittest tests.test_stats
（也可整体跑： python -m unittest discover -s tests）
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.stats import STATS_KEYS, format_number, nearest_index, nearest_sample, stats_of  # noqa: E402


class TestStatsOf(unittest.TestCase):

    def test_none_and_empty(self):
        for value in (None, [], np.array([])):
            result = stats_of(value)
            self.assertEqual(result["count"], 0)
            self.assertEqual(tuple(result.keys()), STATS_KEYS)
            for key in STATS_KEYS[1:]:
                self.assertIsNone(result[key], key)

    def test_single_value(self):
        result = stats_of([42.0])
        self.assertEqual(result["count"], 1)
        self.assertEqual((result["min"], result["max"], result["mean"], result["span"]),
                         (42.0, 42.0, 42.0, 0.0))
        self.assertEqual(result["changes"], 0)

    def test_basic_series(self):
        result = stats_of([0, 1, 2, 3, 4])
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["min"], 0.0)
        self.assertEqual(result["max"], 4.0)
        self.assertEqual(result["mean"], 2.0)
        self.assertAlmostEqual(result["std"], float(np.std([0, 1, 2, 3, 4])), places=12)
        self.assertEqual(result["span"], 4.0)
        self.assertEqual((result["first"], result["last"]), (0.0, 4.0))

    def test_changes_counts_transitions(self):
        self.assertEqual(stats_of([0, 0, 1, 1, 0])["changes"], 2)
        self.assertEqual(stats_of([7, 7, 7])["changes"], 0)

    def test_nan_is_ignored(self):
        result = stats_of([1.0, np.nan, 3.0])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["min"], 1.0)
        self.assertEqual(result["max"], 3.0)
        self.assertEqual(result["mean"], 2.0)
        self.assertEqual(result["last"], 3.0)

    def test_all_nan(self):
        result = stats_of([np.nan, np.nan])
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["min"])


class TestNearestSample(unittest.TestCase):

    def setUp(self):
        self.x = np.array([0.0, 1.0, 2.0, 3.0])

    def test_exact_hit(self):
        self.assertEqual(nearest_index(self.x, 2.0), 2)
        self.assertEqual(nearest_sample(self.x, 2.0), (2, 2.0))

    def test_middle_picks_nearer(self):
        self.assertEqual(nearest_index(self.x, 0.4), 0)
        self.assertEqual(nearest_index(self.x, 0.6), 1)
        self.assertEqual(nearest_index(self.x, 2.8), 3)

    def test_tie_prefers_earlier(self):
        self.assertEqual(nearest_index(self.x, 0.5), 0)

    def test_outside_range_clamps_to_ends(self):
        self.assertEqual(nearest_index(self.x, -5.0), 0)
        self.assertEqual(nearest_index(self.x, 99.0), 3)

    def test_empty_and_none(self):
        self.assertEqual(nearest_index([], 1.0), -1)
        self.assertEqual(nearest_index(None, 1.0), -1)
        self.assertEqual(nearest_index(self.x, None), -1)
        self.assertEqual(nearest_sample([], 1.0), (-1, None))

    def test_accepts_list(self):
        self.assertEqual(nearest_sample([10.0, 20.0], 14.0), (0, 10.0))


class TestFormatNumber(unittest.TestCase):

    def test_integer_stays_integer(self):
        self.assertEqual(format_number(42.0), "42")
        self.assertEqual(format_number(0.0), "0")

    def test_float_keeps_digits(self):
        self.assertEqual(format_number(1.23456), "1.2346")
        self.assertEqual(format_number(-0.5), "-0.5000")

    def test_none(self):
        self.assertEqual(format_number(None), "—")


if __name__ == "__main__":
    unittest.main()
