# -*- coding: utf-8 -*-
"""ASC 语法解析回归测试（M0 语法契约的守护者）。

运行： python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.asc_parser import (  # noqa: E402
    KIND_CANFD, KIND_CLASSIC, KIND_HEADER, KIND_LIN, KIND_SKIP,
    KIND_SOA, KIND_UNKNOWN, classify,
)

CANFD_LINE = ("   11.484600   CANFD  19 RX    217   1 1 8 8 27 04 40 26 02 00 00 00 "
              "    213015  355   327000 80162294 50140850 50050850 2007030e 20070002")
CANFD_FD_LINE = ("   521.628300   CANFD  5 RX    16b   1 1 C 24 00 00 00 40 00 00 47 d0 "
                 "00 07 d0 00 f2 b4 00 00 03 3d b8 fe 73 00 00 00 "
                 "    213015  355   327000 80162294 50140850 50050850 2007030e 20070002")
CLASSIC_LINE = "   521.620200 3   1b4   Rx   d 8 48 00 00 00 00 00 01 86 "
LIN_LINE = ("   2784.067449   L3  12       Rx    8 10 20 30 40 50 60 70 80    checksum = 3c  "
            " header time =  34, full time =  54  SOF = 2784.065652   BR = 19200")
SOA_LINE = ("   6253.571600    SV: 99 0 1 ::SOA::ThermalSysActuator1_Event_Tx = "
            "[20 20 0c 80 05 00 00 00 7c 00 00 f4 48 02 13 83]")


class TestClassify(unittest.TestCase):

    def test_canfd_basic(self):
        info = classify(CANFD_LINE, keep_data=True)
        self.assertEqual(info.kind, KIND_CANFD)
        self.assertEqual(info.channel, "19")
        self.assertEqual(info.can_id, "217")
        self.assertEqual(info.dlc, "8")
        self.assertEqual(info.length, 8)
        self.assertEqual(len(info.data), 8)
        self.assertEqual(info.data[0], "27")
        self.assertTrue(info.len_ok)
        self.assertTrue(info.dlc_ok)

    def test_canfd_trailing_columns_are_detected_not_data(self):
        info = classify(CANFD_LINE)
        # 尾部 8 列为硬编码常量，必须与数据分离
        self.assertEqual(len(info.trailing), 8)
        self.assertEqual(info.trailing[0], "213015")
        self.assertEqual(info.trailing[-1], "20070002")

    def test_canfd_long_payload_and_dlc_mapping(self):
        info = classify(CANFD_FD_LINE)
        self.assertEqual(info.dlc, "C")          # DLC 0xC -> 24 字节
        self.assertEqual(info.length, 24)
        self.assertTrue(info.dlc_ok)
        self.assertEqual(len(info.trailing), 8)

    def test_canfd_dlc_f_is_64_bytes(self):
        line = ("   1.000000   CANFD  1 RX    123   1 1 F 64 " + " ".join(["00"] * 64)
                + "    213015  355   327000 80162294 50140850 50050850 2007030e 20070002")
        info = classify(line)
        self.assertEqual(info.length, 64)
        self.assertTrue(info.dlc_ok)
        self.assertTrue(info.len_ok)

    def test_classic_can(self):
        info = classify(CLASSIC_LINE)
        self.assertEqual(info.kind, KIND_CLASSIC)
        self.assertEqual(info.channel, "3")
        self.assertEqual(info.can_id, "1b4")
        self.assertEqual(info.length, 8)
        self.assertEqual(info.direction, "Rx")

    def test_lin_frame(self):
        info = classify(LIN_LINE)
        self.assertEqual(info.kind, KIND_LIN)
        self.assertEqual(info.channel, "L3")
        self.assertEqual(info.length, 8)
        self.assertTrue(info.len_ok)
        # LIN 属性列（checksum 等）不得混入数据
        self.assertEqual(len(info.trailing) > 0, True)

    def test_soa_line_recognised_and_named(self):
        info = classify(SOA_LINE)
        self.assertEqual(info.kind, KIND_SOA)
        self.assertEqual(info.name, "ThermalSysActuator1_Event_Tx")

    def test_header_lines(self):
        for line in ("date Tue Sep 22 01:50:11 PM 2026",
                     "base hex  timestamps absolute",
                     "no internal events logged",
                     "// version 11.0.0",
                     "Begin TriggerBlock Mon Jan  1 1:1:1 2999",
                     "End TriggerBlock"):
            self.assertEqual(classify(line).kind, KIND_HEADER, line)

    def test_bom_is_tolerated(self):
        info = classify("\ufeffdate Tue Sep 22 01:50:11 PM 2026")
        self.assertEqual(info.kind, KIND_HEADER)

    def test_uppercase_and_empty_lines(self):
        self.assertEqual(classify("   ").kind, KIND_SKIP)
        info = classify("   1.000000   CANFD  4 TX    1a2   1 1 8 8 01 02 03 04 05 06 07 08")
        self.assertEqual(info.kind, KIND_CANFD)
        self.assertEqual(info.direction, "TX")

    def test_bracket_tag_lines_are_skippable(self):
        self.assertEqual(classify("[ERROR\t] ---       Error1 123").kind, KIND_SKIP)

    def test_garbage_is_unknown(self):
        self.assertEqual(classify("this is not an asc line").kind, KIND_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
