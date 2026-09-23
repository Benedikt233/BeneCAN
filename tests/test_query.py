# -*- coding: utf-8 -*-
"""查询层回归测试（M2）。

合成 ASC（已知周期/数值/枚举/位模式/非单调时间轴），建索引后验证：
    * 目录与通道汇总
    * 帧序列筛选与时间轴三模式（time / session / index）
    * 字节、位、浮点取值（大小端/有符号）
    * 枚举与空值画像
    * 周期/抖动/丢包统计
    * 边界：DLC 8~64、扩展帧、LIN、越界报错

运行： python -m unittest discover -s tests
"""

import os
import struct
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.indexer import build_index  # noqa: E402
from core.query import (MODE_INDEX, MODE_SESSION, MODE_TIME, IndexReader,  # noqa: E402
                        extract_bits, extract_bytes)

HEADER = [
    "base hex  timestamps absolute\n",
    "no internal events logged\n",
    "Begin TriggerBlock Tue Sep 22 01:50:11 PM 2026\n",
]

TAIL_CONST = "213015  355   327000 80162294 50140850 50050850 2007030e 20070002"


def canfd_line(ts, channel, can_id, data, dlc=None):
    """构造一条 CANFD 行。dlc 默认按长度推导（>8 用 DLC 编码）。"""
    length = len(data)
    return ("   %.6f   CANFD  %d RX    %s   1 1 %s %d %s     %s\n"
            % (ts, channel, can_id,
               dlc if dlc is not None else "%X" % length, length,
               " ".join("%02x" % b for b in data), TAIL_CONST))


def payload_from_u16(value, extra=None):
    """小端 uint16 放在 byte0-1。"""
    raw = list(struct.pack("<H", value))
    if extra:
        raw += list(extra)
    return raw


class TestQueryLayer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="asc_query_test_")
        cls.asc_path = os.path.join(cls.tmpdir, "sample.asc")
        lines = list(HEADER)

        # 1) ch4 id0x100：10ms 周期，byte0=常量 0x11，byte1-2=小端递增计数（含 3s 空档）
        t = 1.0
        for index in range(100):
            if index == 60:
                t += 3.0                      # 人为制造 3s 空档
            lines.append(canfd_line(t, 4, "100", [0x11] + payload_from_u16(index + 1)))
            t += 0.01

        # 2) ch4 id0x200：枚举 + 位信号 + 大端 uint16 + float32
        cls.enum_values = [0, 1, 2, 1, 0]
        cls.floats = [1.5, -2.25, 0.0, 3.75, 100.5]
        for index, value in enumerate(cls.enum_values):
            raw = [value, 0b1010_0101]
            raw += list(struct.pack(">H", 0x1234 + index))     # 大端
            raw += list(struct.pack("<f", cls.floats[index]))
            lines.append(canfd_line(10.0 + index, 4, "200", raw))

        # 3) ch17 id0x300：64 字节长报文（DLC F），验证长度边界
        lines.append(canfd_line(20.0, 17, "300", list(range(64)), dlc="F"))

        # 4) 经典 CAN + 扩展帧 + LIN
        lines.append("   30.000000 3   1b4   Rx   d 8 48 00 00 00 00 00 01 86 \n")
        lines.append("   31.000000 5   18dax   Rx   d 2 aa bb \n")
        lines.append("   32.000000   L3  12       Rx    4 de ad be ef    checksum = 3c  \n")

        # 5) 非单调时间轴（形态 B）：ch9 先 100s 再回到 5s
        for ts in (100.0, 100.1, 5.0, 5.1, 5.2):
            lines.append(canfd_line(ts, 9, "400", [0x00, 0x01]))

        with open(cls.asc_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)

        cls.index_path = os.path.join(cls.tmpdir, "sample.parquet")
        build_index(cls.asc_path, index_path=cls.index_path)
        cls.reader = IndexReader.open(cls.index_path)

    @classmethod
    def tearDownClass(cls):
        for name in os.listdir(cls.tmpdir):
            os.remove(os.path.join(cls.tmpdir, name))
        os.rmdir(cls.tmpdir)

    # -------------------------------------------------- 目录
    def test_catalog_entries(self):
        catalog = self.reader.catalog()
        keys = {m.key for m in catalog}
        self.assertIn(("canfd", 4, 0x100), keys)
        self.assertIn(("classic", 3, 0x1B4), keys)
        self.assertIn(("classic", 5, 0x18DA), keys)   # 扩展帧（'x' 后缀）
        self.assertIn(("lin", 3, 0x12), keys)

    def test_catalog_counts_and_range(self):
        msg = self.reader.find("canfd", 4, 0x100)
        self.assertEqual(msg.frames, 100)
        self.assertAlmostEqual(msg.ts_first, 1.0, places=6)
        self.assertEqual(msg.length_min, 3)
        self.assertEqual(msg.length_max, 3)
        self.assertFalse(msg.extended)
        self.assertIn("0x100", msg.label)

    def test_extended_flag_preserved(self):
        msg = self.reader.find("classic", 5, 0x18DA)
        self.assertTrue(msg.extended)
        self.assertIn("扩展", msg.label)
        self.assertFalse(self.reader.find("classic", 3, 0x1B4).extended)

    def test_channels_summary(self):
        channels = {(c.kind, c.channel): c for c in self.reader.channels()}
        self.assertEqual(channels[("canfd", 4)].messages, 2)
        self.assertEqual(channels[("canfd", 4)].frames, 105)
        self.assertEqual(channels[("canfd", 17)].frames, 1)

    def test_find_missing_raises(self):
        with self.assertRaises(KeyError):
            self.reader.find("canfd", 4, 0x999)

    # -------------------------------------------------- 帧序列
    def test_frame_series_basic(self):
        series = self.reader.frame_series("canfd", 4, 0x100)
        self.assertEqual(len(series), 100)
        self.assertEqual(series.data.shape, (100, 64))
        self.assertTrue(series.file_time_ordered)     # 源文件本身是按时间写的
        self.assertEqual(series.time_mode, MODE_TIME)

    def test_frame_series_time_window(self):
        all_ts = self.reader.frame_series("canfd", 4, 0x100).ts
        # 用实测时间戳构造窗口，避免浮点累加带来的边界歧义
        series = self.reader.frame_series("canfd", 4, 0x100,
                                          t0=float(all_ts[10]) + 1e-9,
                                          t1=float(all_ts[20]) - 1e-9)
        self.assertEqual(len(series), 9)
        self.assertGreaterEqual(series.ts.min(), all_ts[10])
        self.assertLessEqual(series.ts.max(), all_ts[20])

    def test_index_resorts_file_out_of_order_frames(self):
        # ch9 在源文件里是 100.0,100.1,5.0,5.1,5.2（乱序）→ 索引按 ts 重排
        series = self.reader.frame_series("canfd", 9, 0x400)
        self.assertEqual(len(series), 5)
        self.assertFalse(series.file_time_ordered)
        self.assertTrue(bool((np.diff(series.ts) >= 0).all()))     # 索引已重排

    def test_time_axis_modes(self):
        series = self.reader.frame_series("canfd", 4, 0x100)        # 内有 3s 空档
        raw = self.reader.frame_series("canfd", 4, 0x100, time_mode=MODE_TIME)
        session = self.reader.frame_series("canfd", 4, 0x100, time_mode=MODE_SESSION)
        indexed = self.reader.frame_series("canfd", 4, 0x100, time_mode=MODE_INDEX)
        self.assertEqual(len(series), 100)
        self.assertGreaterEqual(series.idle_gap_count, 1)
        self.assertTrue(bool((np.diff(session.x) >= 0).all()))
        self.assertEqual(session.segments.max(), 1)                  # 切成 2 段
        self.assertLess(session.x[-1], raw.x[-1])                    # 空档被压缩
        self.assertEqual(list(indexed.x[:3]), [0.0, 1.0, 2.0])

    # -------------------------------------------------- 取值
    def test_extract_uint16_little_endian(self):
        series = self.reader.signal_series("canfd", 4, 0x100, byte_offset=1,
                                          size=2, dtype="uint16")
        self.assertEqual(list(series.raw[:5]), [1, 2, 3, 4, 5])
        self.assertEqual(list(series.raw[-1:]), [100])

    def test_extract_uint16_big_endian(self):
        series = self.reader.signal_series("canfd", 4, 0x200, byte_offset=2,
                                          size=2, dtype="uint16", byte_order="big")
        self.assertEqual(list(series.raw), [0x1234, 0x1235, 0x1236, 0x1237, 0x1238])

    def test_extract_signed_int8(self):
        # 129 -> 有符号 -127；直接调用底层函数
        data = np.array([[129]], dtype=np.uint8)
        self.assertEqual(int(extract_bytes(data, 0, 1, "int8")[0]), -127)
        self.assertEqual(int(extract_bytes(data, 0, 1, "uint8")[0]), 129)

    def test_extract_float32_with_scale(self):
        series = self.reader.signal_series("canfd", 4, 0x200, byte_offset=4,
                                          size=4, dtype="float32", scale=2.0, offset=1.0)
        for got, want in zip(series.y, self.floats):
            self.assertAlmostEqual(got, want * 2.0 + 1.0, places=4)

    def test_extract_bits_little_endian(self):
        series = self.reader.signal_series("canfd", 4, 0x200, byte_offset=1,
                                          bit_index=0, bit_length=4)
        self.assertEqual(list(series.raw), [0b0101] * 5)

    def test_extract_bits_big_endian(self):
        series = self.reader.signal_series("canfd", 4, 0x200, byte_offset=1,
                                          bit_index=0, bit_length=4, byte_order="big")
        self.assertEqual(list(series.raw), [0b1010] * 5)

    def test_extract_bits_standalone(self):
        # 同字节：0xA5 取低 4 位（Intel）与高 4 位（Motorola）
        data = np.array([[0b1010_0101]], dtype=np.uint8)
        self.assertEqual(int(extract_bits(data, 0, 0, 4)[0]), 0b0101)          # Intel
        self.assertEqual(int(extract_bits(data, 0, 0, 4, "big")[0]), 0b1010)   # Motorola
        # 跨字节：byte0=0x60(0110_0000) byte1=0x81(1000_0001)，从 byte0 位 6 起取 4 位
        #   Intel    : byte0 位6,7 = 1,0 → byte1 位0,1 = 1,0 → 0b0101
        #   Motorola : byte0 位6,7（MSB 侧）= 0,0 → byte1 位0,1（MSB 侧）= 1,0 → 0b0010
        cross = np.array([[0b0110_0000, 0b1000_0001]], dtype=np.uint8)
        self.assertEqual(int(extract_bits(cross, 0, 6, 4)[0]), 0b0101)          # Intel
        self.assertEqual(int(extract_bits(cross, 0, 6, 4, "big")[0]), 0b0010)   # Motorola

    def test_dtype_size_mismatch_raises(self):
        with self.assertRaises(ValueError):
            self.reader.signal_series("canfd", 4, 0x100, byte_offset=0,
                                      size=1, dtype="uint16")
        with self.assertRaises(ValueError):
            self.reader.signal_series("canfd", 4, 0x100, byte_offset=62,
                                      size=4, dtype="uint32")

    def test_hex_mode(self):
        series = self.reader.signal_series("canfd", 4, 0x200, byte_offset=0,
                                          size=2, as_hex=True)
        self.assertEqual(series.value_text[0], "00 A5")

    def test_labels_render_enum(self):
        series = self.reader.signal_series("canfd", 4, 0x200, byte_offset=0,
                                          labels={0: "关", 1: "开", 2: "故障"})
        self.assertEqual(series.value_text, ["关", "开", "故障", "开", "关"])

    # -------------------------------------------------- 画像与统计
    def test_byte_profile(self):
        profile = self.reader.byte_profile("canfd", 4, 0x100)
        self.assertEqual(profile[0]["kind"], "constant")
        self.assertEqual(profile[1]["kind"], "analog")
        self.assertEqual(profile[20]["unique"], 1)

    def test_message_stats_cycle_and_gap(self):
        stats = self.reader.message_stats("canfd", 4, 0x100)
        self.assertEqual(stats["frames"], 100)
        self.assertAlmostEqual(stats["interval_median"], 0.01, places=6)
        self.assertAlmostEqual(stats["cycle_hz"], 100.0, places=3)
        self.assertGreaterEqual(stats["gap_count"], 1)
        self.assertGreaterEqual(stats["missing_estimate"], 49)
        self.assertEqual(stats["lengths"], [3])

    def test_message_stats_long_frame(self):
        stats = self.reader.message_stats("canfd", 17, 0x300)
        self.assertEqual(stats["lengths"], [64])
        self.assertEqual(stats["dlcs"], [15])       # DLC F = 15

    def test_metadata_links_back_to_source(self):
        self.assertEqual(self.reader.source_path, os.path.abspath(self.asc_path))
        self.assertTrue(self.reader.is_fresh_for(self.asc_path))
        # 100 + 5 + 1(ch17) + 1(经典) + 1(扩展) + 1(LIN) + 5(ch9) = 114
        self.assertEqual(self.reader.frames, 114)


if __name__ == "__main__":
    unittest.main()
