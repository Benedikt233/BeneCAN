# -*- coding: utf-8 -*-
"""索引层回归测试（M1）。

用合成的小 ASC（覆盖 CANFD/经典 CAN/LIN/SOA/头部）验证：
    * 排序布局 (kind, channel, can_id, ts) 与 seq 原始序保留
    * payload 定长 64 字节补零
    * 元数据与缓存命中判定
运行： python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.indexer import (KIND_CODES, PAYLOAD_SIZE, build_index,  # noqa: E402
                          index_is_fresh, read_index_metadata)

HEADER = [
    "date Tue Sep 22 01:50:11 PM 2026\n",
    "base hex  timestamps absolute\n",
    "no internal events logged\n",
    "// version 11.0.0\n",
    "Begin TriggerBlock Tue Sep 22 01:50:11 PM 2026\n",
]
# 刻意打乱时间/通道/ID 顺序，用于验证排序
FRAMES = [
    "   2.000000   CANFD  5 RX    17a   1 1 8 8 01 02 03 04 05 06 07 08     "
    "213015  355   327000 80162294 50140850 50050850 2007030e 20070002\n",
    "   1.000000   CANFD  4 RX    10b   1 1 8 8 11 12 13 14 15 16 17 18     "
    "213015  355   327000 80162294 50140850 50050850 2007030e 20070002\n",
    "   3.000000   CANFD  4 RX    10b   1 1 4 4 aa bb cc dd     "
    "213015  355   327000 80162294 50140850 50050850 2007030e 20070002\n",
    "   4.000000 3   1b4   Rx   d 8 48 00 00 00 00 00 01 86 \n",
    "   5.000000   L3  12       Rx    4 de ad be ef    checksum = 3c  \n",
]
TAIL = [
    "   6.000000    SV: 99 0 1 ::SOA::SomeSt_Event_Tx = [20 20 0b 80 02 00 00 00]\n",
]


class TestIndexer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="asc_index_test_")
        cls.asc_path = os.path.join(cls.tmpdir, "sample.asc")
        with open(cls.asc_path, "w", encoding="utf-8") as fh:
            fh.writelines(HEADER + FRAMES + TAIL)
        cls.index_path = os.path.join(cls.tmpdir, "sample.parquet")
        cls.stats = build_index(cls.asc_path, index_path=cls.index_path)
        cls.table = pq.read_table(cls.index_path)

    @classmethod
    def tearDownClass(cls):
        for name in os.listdir(cls.tmpdir):
            os.remove(os.path.join(cls.tmpdir, name))
        os.rmdir(cls.tmpdir)

    def test_frame_and_line_counts(self):
        # 5 帧（SOA 行不建索引），头部 5 行 + SOA 1 行
        self.assertEqual(self.stats["frames"], 5)
        self.assertEqual(self.stats["lines"], 11)
        self.assertEqual(self.stats["malformed"], 0)
        self.assertEqual(self.stats["unknown_lines"], 0)

    def test_kind_counts(self):
        self.assertEqual(self.stats["kind_counts"]["canfd"], 3)
        self.assertEqual(self.stats["kind_counts"]["classic"], 1)
        self.assertEqual(self.stats["kind_counts"]["lin"], 1)

    def test_sorted_by_kind_channel_id_ts(self):
        rows = self.table.to_pylist()
        keys = [(r["kind"], r["channel"], r["can_id"], r["ts"]) for r in rows]
        self.assertEqual(keys, sorted(keys))
        # canfd(1) ch4 id10b 两条按 ts 升序
        self.assertEqual(keys[0], (KIND_CODES["canfd"], 4, 0x10B, 1.0))
        self.assertEqual(keys[1], (KIND_CODES["canfd"], 4, 0x10B, 3.0))
        self.assertEqual(keys[2], (KIND_CODES["canfd"], 5, 0x17A, 2.0))
        self.assertEqual(keys[3][0], KIND_CODES["classic"])
        self.assertEqual(keys[4][0], KIND_CODES["lin"])

    def test_seq_preserves_source_order(self):
        rows = self.table.to_pylist()
        by_ts = {r["ts"]: r["seq"] for r in rows}
        # 源文件里 2.0 的行在最前 -> seq 0；4.0 的行第 4 -> seq 3
        self.assertEqual(by_ts[2.0], 0)
        self.assertEqual(by_ts[1.0], 1)
        self.assertEqual(by_ts[3.0], 2)
        self.assertEqual(by_ts[4.0], 3)
        self.assertEqual(by_ts[5.0], 4)

    def test_payload_is_fixed_64_bytes_with_zero_padding(self):
        rows = {r["ts"]: r for r in self.table.to_pylist()}
        self.assertEqual(len(rows[2.0]["data"]), PAYLOAD_SIZE)
        self.assertEqual(rows[2.0]["data"][:8], bytes(range(1, 9)))
        self.assertEqual(rows[2.0]["data"][8:], b"\x00" * (PAYLOAD_SIZE - 8))
        self.assertEqual(rows[4.0]["data"][:8], bytes([0x48, 0, 0, 0, 0, 0, 1, 0x86]))
        self.assertEqual(rows[5.0]["data"][:4], bytes([0xDE, 0xAD, 0xBE, 0xEF]))

    def test_length_and_dlc(self):
        rows = {r["ts"]: r for r in self.table.to_pylist()}
        self.assertEqual(rows[2.0]["length"], 8)
        self.assertEqual(rows[2.0]["dlc"], 8)
        self.assertEqual(rows[3.0]["length"], 4)      # DLC 4 -> 4 字节
        self.assertEqual(rows[3.0]["dlc"], 4)
        self.assertEqual(rows[5.0]["length"], 4)      # LIN 长度列

    def test_metadata_and_cache_freshness(self):
        meta = read_index_metadata(self.index_path)
        self.assertEqual(meta["frames"], "5")
        self.assertEqual(meta["sort"], "kind,channel,can_id,ts")
        self.assertEqual(meta["source_path"], os.path.abspath(self.asc_path))
        self.assertTrue(index_is_fresh(self.asc_path, self.index_path))

    def test_cache_reuse_returns_cached_flag(self):
        again = build_index(self.asc_path, index_path=self.index_path)
        self.assertTrue(again["cached"])
        self.assertEqual(again["frames"], 5)

    def test_index_path_normalizes_path_case(self):
        """同一文件用不同大小写/写法打开必须命中同一份索引。"""
        from core.indexer import default_index_path, source_fingerprint

        variants = [self.asc_path, self.asc_path.upper(), self.asc_path.lower()]
        if os.name == "nt":
            fingerprints = {source_fingerprint(path) for path in variants}
            self.assertEqual(len(fingerprints), 1)
            self.assertEqual(len({default_index_path(p) for p in variants}), 1)
        # 相对路径与绝对路径也应归一
        relative = os.path.relpath(self.asc_path)
        self.assertEqual(source_fingerprint(self.asc_path), source_fingerprint(relative))


if __name__ == "__main__":
    unittest.main()
