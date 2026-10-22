# -*- coding: utf-8 -*-
"""DBC 接入层测试（M6 前置）。

关键验证方式：**用 cantools 自己的编码结果做权威对照** ——
cantools 编码出帧数据，我们的 SignalSpec 取数后应得到与原始物理值一致的结果。
（这覆盖 Intel/Motorola、字节对齐/跨字节、有符号、缩放偏移、枚举。）

运行： python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from importlib.util import find_spec

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.indexer import build_index  # noqa: E402
from core.query import IndexReader  # noqa: E402
from signals.dbc_loader import (ChannelMapping, DbcFile, DbcLibrary,  # noqa: E402
                                DbcSignalRef)

# 注意：这里只探测是否安装（find_spec），**不真正 import cantools**。
# 实测：cantools 的 bitstruct C 扩展若在 Qt 首次初始化之前加载，进程会以
# 0xC0000005 崩溃（unittest discover 按字母序先导入本模块）；改用懒加载后，
# Qt 在收集阶段先初始化，cantools 直到跑测试时才加载，顺序与 main.py 一致。
HAS_CANTOOLS = find_spec("cantools") is not None

DBC_TEXT = '''VERSION "1.0"

NS_ :

BS_:

BU_: ECU1 ECU2

BO_ 256 MsgA: 8 ECU1
 SG_ Speed : 0|16@1+ (0.1,0) [0|6553.5] "km/h" ECU2
 SG_ Temp : 16|8@1- (1,-40) [-40|215] "degC" ECU2
 SG_ Gear : 24|4@1+ (1,0) [0|15] "" ECU2
 SG_ Flag : 28|1@1+ (1,0) [0|1] "" ECU2
 SG_ Checksum : 56|8@1+ (1,0) [0|255] "" ECU2

BO_ 512 MsgB: 8 ECU1
 SG_ MotorRpm : 0|8@0+ (10,0) [0|2550] "rpm" ECU2
 SG_ Ratio : 8|16@0+ (0.001,0) [0|65.535] "" ECU2

BO_ 768 MsgC: 8 ECU1
 SG_ BigA : 7|16@0+ (1,0) [0|65535] "" ECU2
 SG_ BigC : 39|8@0+ (1,0) [0|255] "" ECU2

VAL_ 256 Gear 0 "P" 1 "R" 2 "N" 3 "D" ;
'''

TAIL = "213015  355   327000 80162294 50140850 50050850 2007030e 20070002"


def dbc_with_frame_formats(*formats) -> str:
    """给 DBC_TEXT 里的三个报文写上 VFrameFormat（Vector 约定：14 = CANFDStandard）。

    DBC 自带帧类型信息，所以用户不需要手选 CAN / CANFD。
    """
    frame_ids = (256, 512, 768)
    lines = ['BA_DEF_ BO_  "VFrameFormat" INT 0 15;',
             'BA_DEF_DEF_  "VFrameFormat" 0;']
    lines += ['BA_ "VFrameFormat" BO_ %d %d;' % (frame_id, value)
              for frame_id, value in zip(frame_ids, formats)]
    return DBC_TEXT.replace("BU_: ECU1 ECU2",
                            "BU_: ECU1 ECU2\n\n" + "\n".join(lines))


def canfd_line(ts, channel, can_id, data):
    return ("   %.6f   CANFD  %d RX    %s   1 1 %X %d %s     %s\n"
            % (ts, channel, can_id, len(data), len(data),
               " ".join("%02x" % b for b in data), TAIL))


@unittest.skipUnless(HAS_CANTOOLS, "cantools 未安装，跳过 DBC 测试")
class TestDbcMapping(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="asc_dbc_test_")
        cls.dbc_path = os.path.join(cls.tmpdir, "test.dbc")
        with open(cls.dbc_path, "w", encoding="utf-8") as fh:
            fh.write(DBC_TEXT)
        cls.dbc = DbcFile(cls.dbc_path)

    @classmethod
    def tearDownClass(cls):
        for name in os.listdir(cls.tmpdir):
            os.remove(os.path.join(cls.tmpdir, name))
        os.rmdir(cls.tmpdir)

    def _signal(self, can_id, name) -> DbcSignalRef:
        for sig in self.dbc.messages[can_id].signals:
            if sig.name == name:
                return sig
        raise AssertionError("信号不存在：%s" % name)

    # -------------------------------------------------- 解析与位序映射
    def test_messages_parsed(self):
        self.assertEqual(len(self.dbc.messages), 3)
        self.assertEqual(self.dbc.messages[0x100].name, "MsgA")
        self.assertEqual(self.dbc.messages[0x100].length, 8)
        self.assertEqual(len(self.dbc.messages[0x100].signals), 5)
        self.assertEqual(self.dbc.signal_count, 9)

    def test_intel_bit_mapping(self):
        speed = self._signal(0x100, "Speed")
        self.assertEqual((speed.byte_offset, speed.bit_index, speed.length, speed.byte_order),
                         (0, 0, 16, "little"))
        self.assertTrue(speed.is_byte_aligned)
        temp = self._signal(0x100, "Temp")
        self.assertEqual((temp.byte_offset, temp.bit_index, temp.length), (2, 0, 8))
        self.assertTrue(temp.is_signed)
        gear = self._signal(0x100, "Gear")
        self.assertEqual((gear.byte_offset, gear.bit_index, gear.length), (3, 0, 4))
        self.assertFalse(gear.is_byte_aligned)          # 4 位 → 位路径
        flag = self._signal(0x100, "Flag")
        self.assertEqual((flag.byte_offset, flag.bit_index, flag.length), (3, 4, 1))

    def test_motorola_bit_mapping(self):
        """Motorola 起始位是 MSB，需要换算成网络位号（已用 cantools 编码验证）。"""
        rpm = self._signal(0x200, "MotorRpm")
        self.assertEqual((rpm.byte_offset, rpm.bit_index, rpm.length, rpm.byte_order),
                         (0, 7, 8, "big"))
        ratio = self._signal(0x200, "Ratio")
        self.assertEqual((ratio.byte_offset, ratio.bit_index, ratio.length), (1, 7, 16))
        big_a = self._signal(0x300, "BigA")
        self.assertEqual((big_a.byte_offset, big_a.bit_index, big_a.length), (0, 0, 16))
        self.assertTrue(big_a.is_byte_aligned)          # 7|16@0 → 网络位 8 对齐
        self.assertEqual(big_a.byte_order, "big")

    def test_choices_and_unit(self):
        gear = self._signal(0x100, "Gear")
        self.assertEqual(gear.choices, {0: "P", 1: "R", 2: "N", 3: "D"})
        self.assertEqual(self._signal(0x100, "Speed").unit, "km/h")
        self.assertEqual(self._signal(0x200, "Ratio").unit, "")
        self.assertAlmostEqual(self._signal(0x100, "Speed").scale, 0.1)
        self.assertAlmostEqual(self._signal(0x100, "Temp").offset, -40.0)

    # -------------------------------------------------- SignalSpec 转换
    def test_to_spec_byte_path(self):
        spec = self._signal(0x100, "Speed").to_spec(("canfd", 5, 0x100))
        self.assertEqual(spec.byte_offset, 0)
        self.assertEqual(spec.size, 2)
        self.assertEqual(spec.dtype, "uint16")
        self.assertEqual(spec.bit_index, -1)
        self.assertAlmostEqual(spec.scale, 0.1)
        self.assertEqual(spec.unit, "km/h")
        self.assertEqual(spec.source, "dbc")
        self.assertIn("MsgA.Speed", spec.display_name())
        self.assertIn("km/h", spec.display_name())

    def test_to_spec_signed_and_big_endian(self):
        temp = self._signal(0x100, "Temp").to_spec(("canfd", 5, 0x100))
        self.assertEqual(temp.dtype, "int8")
        big = self._signal(0x300, "BigA").to_spec(("canfd", 5, 0x300))
        self.assertEqual((big.dtype, big.size, big.byte_order), ("uint16", 2, "big"))

    def test_to_spec_bit_path(self):
        gear = self._signal(0x100, "Gear").to_spec(("canfd", 5, 0x100))
        self.assertEqual((gear.bit_index, gear.bit_length), (0, 4))
        self.assertTrue(gear.step)                       # 有枚举 → 阶梯绘制
        self.assertEqual(gear.labels, {0: "P", 1: "R", 2: "N", 3: "D"})

    # -------------------------------------------------- 端到端：cantools 编码 → 我们取值
    def test_end_to_end_against_cantools_encoding(self):
        database = self.dbc.database
        cases = [
            (0x100, {"Speed": 123.4, "Temp": 25, "Gear": 3, "Flag": 1, "Checksum": 0xAB}),
            (0x200, {"MotorRpm": 200, "Ratio": 1.5}),
            (0x300, {"BigA": 0x1234, "BigC": 0x9A}),
        ]
        for frame_id, values in cases:
            message = database.get_message_by_frame_id(frame_id)
            payload = message.encode(values)
            for name, expected in values.items():
                ref = self._signal(frame_id, name)
                raw = self._extract_ref(payload, ref)
                physical = raw * ref.scale + ref.offset
                self.assertAlmostEqual(physical, expected, places=6,
                                       msg="%s.%s" % (message.name, name))

    def _extract_ref(self, payload: bytes, ref: DbcSignalRef) -> float:
        """按 DbcSignalRef 的字段直接取值（等价于 SignalSpec 走的底层路径）。"""
        from core.query import extract_bits, extract_bytes

        data = np.frombuffer(payload, dtype=np.uint8).reshape(1, -1)
        if ref.is_float:
            from core.query import extract_float
            return float(extract_float(data, ref.byte_offset, ref.byte_order)[0])
        if ref.is_byte_aligned:
            dtype = ("int" if ref.is_signed else "uint") + str(ref.length)
            return float(extract_bytes(data, ref.byte_offset, ref.size, dtype,
                                       ref.byte_order)[0])
        raw = int(extract_bits(data, ref.byte_offset, ref.bit_index, ref.length,
                               ref.byte_order)[0])
        if ref.is_signed and raw >= (1 << (ref.length - 1)):
            raw -= 1 << ref.length
        return float(raw)

    # -------------------------------------------------- 与 ASC 索引打通（真实管线）
    def test_spec_extract_through_index(self):
        """合成 ASC（含 MsgA 三帧）→ 建索引 → 用 DBC 生成的 SignalSpec 取物理值。"""
        database = self.dbc.database
        tmpdir = tempfile.mkdtemp(prefix="asc_dbc_pipe_")
        try:
            asc_path = os.path.join(tmpdir, "pipe.asc")
            index_path = os.path.join(tmpdir, "pipe.parquet")
            expected = []
            lines = ["base hex  timestamps absolute\n", "no internal events logged\n"]
            frames = [{"Speed": 10.0, "Temp": -10, "Gear": 1, "Flag": 0, "Checksum": 1},
                      {"Speed": 65.5, "Temp": 85, "Gear": 3, "Flag": 1, "Checksum": 2},
                      {"Speed": 0.0, "Temp": 25, "Gear": 0, "Flag": 0, "Checksum": 3}]
            for index, values in enumerate(frames):
                payload = database.get_message_by_frame_id(0x100).encode(values)
                lines.append(canfd_line(1.0 + index * 0.1, 5, "100", list(payload)))
                expected.append(values)
            with open(asc_path, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            build_index(asc_path, index_path=index_path)
            reader = IndexReader.open(index_path)

            speed = self._signal(0x100, "Speed").to_spec(("canfd", 5, 0x100))
            gear = self._signal(0x100, "Gear").to_spec(("canfd", 5, 0x100))
            temp = self._signal(0x100, "Temp").to_spec(("canfd", 5, 0x100))

            for spec, key in ((speed, "Speed"), (gear, "Gear"), (temp, "Temp")):
                series = spec.extract(reader)
                self.assertEqual(len(series), 3)
                for index, values in enumerate(expected):
                    self.assertAlmostEqual(series.y[index], values[key], places=6,
                                           msg="%s 第 %d 帧" % (key, index))
            # 枚举文本可直接用于显示
            gear_series = gear.extract(reader)
            self.assertEqual(gear_series.value_text, ["R", "D", "P"])
        finally:
            for name in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, name))
            os.rmdir(tmpdir)


class TestChannelTemplate(unittest.TestCase):
    """默认通道模板与配置 v2（不依赖 cantools）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="asc_channel_tpl_")

    def tearDown(self):
        for root, _dirs, files in os.walk(self.tmpdir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)

    def test_default_template_matches_attachment(self):
        from signals.channel_catalog import default_channels

        channels = default_channels()
        self.assertEqual(sorted(channels), list(range(1, 22)))
        self.assertEqual(channels[1].name, "I CAN")
        self.assertEqual(channels[6].name, "IB CAN/AD CAN")
        self.assertEqual(channels[17].name, "BL_CAN")
        self.assertEqual(channels[18].name, "LDCU LIN")
        self.assertEqual(channels[18].remark, "LIN to CAN")
        self.assertEqual(channels[19].remark, "LIN to CAN")
        self.assertEqual(channels[20].remark, "LIN to CAN")
        self.assertEqual(channels[21].name, "NGX_LIN")
        self.assertEqual(channels[21].remark, "")

    def test_v1_config_uses_default_template(self):
        path = os.path.join(self.tmpdir, "channel_map.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1,
                       "mappings": [{"dbc": "CH5.dbc", "channels": [5],
                                     "kind": "canfd", "comment": "旧备注"}]},
                      fh, ensure_ascii=False)
        library = DbcLibrary(self.tmpdir)
        library.load_config()
        self.assertEqual(len(library.channels), 21)
        self.assertEqual(library.channel_caption(5), "ch5 E CAN")
        self.assertEqual(library.mappings[0].comment, "旧备注")
        self.assertEqual(library.mappings[0].channels, [5])

    def test_v2_round_trip_names_remarks_and_mappings(self):
        library = DbcLibrary(self.tmpdir)
        library.set_channel_meta(5, name="E CAN TEST", remark="整车网络")
        library.set_channel_meta(22, name="EXTRA", remark="模板外通道")
        library.add_mapping("CH5.dbc", [5], "canfd")
        path = library.save_config()

        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["channels"]["5"],
                         {"name": "E CAN TEST", "remark": "整车网络"})
        self.assertEqual(payload["channels"]["22"],
                         {"name": "EXTRA", "remark": "模板外通道"})
        self.assertNotIn("1", payload["channels"])       # 默认值不写盘

        reloaded = DbcLibrary(self.tmpdir)
        reloaded.load_config()
        self.assertEqual(reloaded.channel_meta(5).name, "E CAN TEST")
        self.assertEqual(reloaded.channel_meta(5).remark, "整车网络")
        self.assertEqual(reloaded.channel_meta(22).name, "EXTRA")
        self.assertEqual(reloaded.channel_meta(1).name, "I CAN")
        self.assertEqual(reloaded.channels_for("CH5.dbc"), [5])

    def test_extra_channels_append_without_overwriting_template(self):
        library = DbcLibrary(self.tmpdir)
        library.ensure_channels([22, "23", 5])
        self.assertIn(22, library.channels)
        self.assertIn(23, library.channels)
        self.assertEqual(library.channel_caption(22), "ch22")
        self.assertEqual(library.channel_caption(5), "ch5 E CAN")

    def test_remove_mapping_only_from_one_channel(self):
        library = DbcLibrary(self.tmpdir)
        library.add_mapping("A.dbc", [5, 9], "canfd")
        library.add_mapping("B.dbc", [5], "canfd")
        self.assertTrue(library.remove_mapping_for_channel("A.dbc", 5))
        self.assertEqual(library.channels_for("A.dbc"), [9])
        self.assertEqual(library.channels_for("B.dbc"), [5])
        self.assertFalse(library.remove_mapping_for_channel("A.dbc", 5))

    def test_reset_template_keeps_mappings_and_extra_channels(self):
        library = DbcLibrary(self.tmpdir)
        library.set_channel_meta(5, name="自定义", remark="自定义备注")
        library.set_channel_meta(22, name="EXTRA", remark="模板外")
        library.add_mapping("A.dbc", [5], "canfd")
        library.reset_channel_template()
        self.assertEqual(library.channel_meta(5).name, "E CAN")
        self.assertEqual(library.channel_meta(5).remark, "")
        self.assertEqual(library.channel_meta(22).name, "EXTRA")
        self.assertEqual(library.channels_for("A.dbc"), [5])

    def test_save_config_creates_backup(self):
        library = DbcLibrary(self.tmpdir)
        library.add_mapping("A.dbc", [5], "canfd")
        library.save_config()
        library.save_config()
        self.assertTrue(os.path.exists(library.config_path + ".bak"))


@unittest.skipUnless(HAS_CANTOOLS, "cantools 未安装，跳过 DBC 测试")
class TestDbcLibrary(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="asc_dbc_lib_")
        self.dbc_dir = os.path.join(self.tmpdir, "dbc")
        os.makedirs(self.dbc_dir)
        with open(os.path.join(self.dbc_dir, "CH5.dbc"), "w", encoding="utf-8") as fh:
            fh.write(DBC_TEXT)
        with open(os.path.join(self.dbc_dir, "Other.dbc"), "w", encoding="utf-8") as fh:
            # 同样的报文 ID，消息名不同（用于验证多 DBC 命中同一 ID 的冲突提示）
            fh.write(DBC_TEXT.replace("MsgA", "MsgAAlt").replace("MsgB", "MsgBAlt"))

    def tearDown(self):
        for root, _dirs, files in os.walk(self.tmpdir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)

    def test_add_mapping_merges_channels(self):
        library = DbcLibrary(self.dbc_dir)
        library.add_mapping("CH5.dbc", [5], "canfd", "CH5 网络")
        library.add_mapping("CH5.dbc", [5, 9], "canfd")
        self.assertEqual(len(library.mappings), 1)
        self.assertEqual(library.mappings[0].channels, [5, 9])
        library.add_mapping("CH5.dbc", [3], "classic")
        self.assertEqual(len(library.mappings), 2)

    def test_config_round_trip(self):
        library = DbcLibrary(self.dbc_dir)
        library.add_mapping("CH5.dbc", [5, 9], "canfd", "整车")
        library.add_mapping("Other.dbc", [3], "classic")
        library.save_config()

        reloaded = DbcLibrary.open(self.dbc_dir)
        self.assertEqual(len(reloaded.mappings), 2)
        self.assertEqual(reloaded.mappings[0].dbc, "CH5.dbc")
        self.assertEqual(reloaded.mappings[0].channels, [5, 9])
        self.assertEqual(reloaded.mappings[0].comment, "整车")
        self.assertEqual(reloaded.mappings[1].kind, "classic")

    def test_resolve_respects_channel_mapping(self):
        library = DbcLibrary.open(self.dbc_dir)
        library.add_mapping("CH5.dbc", [5], "canfd")
        library.load_all()
        # ch5 命中
        message, conflicts = library.resolve_message("canfd", 5, 0x100)
        self.assertIsNotNone(message)
        self.assertEqual(message.name, "MsgA")
        self.assertEqual(conflicts, [])
        # 未映射的通道不命中
        self.assertIsNone(library.resolve_message("canfd", 17, 0x100)[0])
        self.assertEqual(library.resolve_signals("canfd", 17, 0x100), [])
        # 信号数量正确
        self.assertEqual(len(library.resolve_signals("canfd", 5, 0x100)), 5)

    def test_resolve_reports_conflicts_across_dbc(self):
        library = DbcLibrary.open(self.dbc_dir)
        library.add_mapping("CH5.dbc", [5], "canfd")
        library.add_mapping("Other.dbc", [5], "canfd")
        library.load_all()
        message, conflicts = library.resolve_message("canfd", 5, 0x100)
        self.assertIsNotNone(message)
        self.assertEqual(len(conflicts), 1)              # 第二个 DBC 也定义了同 ID

    def test_discover_and_stats(self):
        library = DbcLibrary.open(self.dbc_dir)
        names = [os.path.basename(p) for p in library.discover()]
        self.assertEqual(names, ["CH5.dbc", "Other.dbc"])
        library.add_mapping("CH5.dbc", [5], "canfd")
        library.load_all()
        stats = library.stats()
        self.assertEqual(stats["dbc_files"], 1)
        self.assertEqual(stats["mappings"], 1)
        self.assertEqual(stats["messages"], 3)
        self.assertEqual(stats["signals"], 9)

    def test_missing_dbc_recorded_as_error(self):
        library = DbcLibrary(self.dbc_dir)
        library.add_mapping("NoSuch.dbc", [5])
        result = library.load_all()
        self.assertEqual(result["loaded"], 0)
        self.assertEqual(len(library.errors), 1)

    def test_absolute_path_mapping_stored_relative_when_inside_dir(self):
        """dbc/ 目录内的文件（无论传相对还是绝对路径）都存成相对路径，便于整体搬迁。"""
        library = DbcLibrary(self.dbc_dir)
        absolute = os.path.join(self.dbc_dir, "CH5.dbc")
        library.add_mapping(absolute, [5], "canfd")
        self.assertEqual(library.mappings[0].dbc, "CH5.dbc")
        self.assertFalse(os.path.isabs(library.mappings[0].dbc))
        library.add_mapping("CH5.dbc", [3], "classic")
        self.assertEqual(len(library.mappings), 2)          # 视为同一文件的不同帧类型映射
        library.load_all()
        self.assertEqual(len(library.resolve_signals("canfd", 5, 0x100)), 5)

    def test_outside_dir_mapping_keeps_absolute_path(self):
        library = DbcLibrary(self.dbc_dir)
        outside = os.path.join(self.tmpdir, "outside.dbc")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write(DBC_TEXT)
        library.add_mapping(outside, [5], "canfd")
        self.assertTrue(os.path.isabs(library.mappings[0].dbc))
        library.load_all()
        self.assertEqual(len(library.resolve_signals("canfd", 5, 0x100)), 5)

    def test_apply_editor_state_preserves_chip_order_and_metadata(self):
        library = DbcLibrary(self.dbc_dir)
        other = os.path.join(self.dbc_dir, "Other.dbc")
        ch5 = os.path.join(self.dbc_dir, "CH5.dbc")
        library.add_mapping("Other.dbc", [5], "canfd", "旧版备注")
        result = library.apply_editor_state(
            {5: [other, ch5], 9: [ch5]},
            names={5: "E CAN TEST"},
            remarks={5: "整车网络"})
        self.assertEqual(result["errors"], {})
        self.assertEqual(len(library.mappings), 3)
        self.assertEqual([os.path.basename(m.dbc) for m in
                          library.mappings_for_channel(5)], ["Other.dbc", "CH5.dbc"])
        self.assertEqual(library.mappings_for_channel(5)[0].comment, "旧版备注")
        self.assertEqual(library.channel_meta(5).name, "E CAN TEST")
        self.assertEqual(library.channel_meta(5).remark, "整车网络")
        self.assertEqual([os.path.basename(m.dbc) for m in
                          library.mappings_for_channel(9)], ["CH5.dbc"])

        library.save_config()
        reloaded = DbcLibrary.open(self.dbc_dir)
        self.assertEqual(reloaded.channel_meta(5).name, "E CAN TEST")
        self.assertEqual([os.path.basename(m.dbc) for m in
                          reloaded.mappings_for_channel(5)],
                         ["Other.dbc", "CH5.dbc"])

    def test_apply_editor_state_skips_unparsable_dbc(self):
        library = DbcLibrary(self.dbc_dir)
        result = library.apply_editor_state({5: ["NoSuch.dbc"]})
        self.assertEqual(result["mappings"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("NoSuch.dbc", next(iter(result["errors"])))

    def test_channel_conflicts_detects_same_id(self):
        library = DbcLibrary.open(self.dbc_dir)
        library.apply_editor_state({
            5: [os.path.join(self.dbc_dir, "CH5.dbc"),
                os.path.join(self.dbc_dir, "Other.dbc")]})
        library.load_all()
        conflicts = library.channel_conflicts("canfd", 5)
        self.assertEqual(len(conflicts), 3)              # DBC_TEXT 里有 3 个报文
        self.assertTrue(all(len(messages) == 2 for _can_id, messages in conflicts))


class TestDbcEncoding(unittest.TestCase):
    """DBC 编码探测：cantools 默认按 latin-1 读，中文 DBC 会乱码。

    注意：DBC 语法要求报文/信号**名字**是 ASCII 标识符，
    中文只能出现在单位、VAL_ 枚举文本与 CM_ 注释里（真实 DBC 也是这样写的）。
    """

    CN_DBC = '''VERSION "1.0"

NS_ :

BS_:

BU_: ECU

BO_ 256 BodyStatus: 8 ECU
 SG_ Speed : 0|16@1+ (0.1,0) [0|6553.5] "千米/小时" ECU

CM_ BO_ 256 "车身状态报文，含中文注释";
VAL_ 256 Speed 0 "关闭" 1 "打开" ;
'''

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="asc_dbc_enc_")

    def tearDown(self):
        for name in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, name))
        os.rmdir(self.tmpdir)

    def _write(self, name, text, encoding):
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as fh:
            fh.write(text.encode(encoding))
        return path

    def test_ascii_dbc_uses_utf8(self):
        from signals.dbc_loader import detect_dbc_encoding

        path = self._write("plain.dbc", DBC_TEXT, "ascii")
        self.assertEqual(detect_dbc_encoding(path), "utf-8-sig")

    def test_utf8_chinese_dbc(self):
        from signals.dbc_loader import detect_dbc_encoding

        path = self._write("cn_utf8.dbc", self.CN_DBC, "utf-8")
        self.assertEqual(detect_dbc_encoding(path), "utf-8-sig")

    def test_gbk_chinese_dbc_not_mistaken_as_utf8(self):
        """GBK 的 "次"/"关" 等字节序列可能恰好是合法 UTF-8，必须识别为 GBK。"""
        from signals.dbc_loader import detect_dbc_encoding

        path = self._write("cn_gbk.dbc", self.CN_DBC, "gbk")
        self.assertEqual(detect_dbc_encoding(path), "gbk")

    def test_latin1_accents_fall_back(self):
        """德文 DBC（含 ä / ° 等）既不是 UTF-8 也不是 GBK 中文 → latin-1。"""
        from signals.dbc_loader import detect_dbc_encoding

        text = DBC_TEXT.replace('"km/h"', '"°C ä"')
        path = self._write("de_latin1.dbc", text, "latin-1")
        self.assertEqual(detect_dbc_encoding(path), "latin-1")

    @unittest.skipUnless(HAS_CANTOOLS, "cantools 未安装")
    def test_chinese_content_survives_parsing(self):
        """UTF-8 与 GBK 两种中文 DBC 解析后，中文（单位/枚举/注释）都不应乱码。"""
        for encoding in ("utf-8", "gbk"):
            path = self._write("cn_%s.dbc" % encoding, self.CN_DBC, encoding)
            dbc = DbcFile(path)
            message = dbc.messages[0x100]
            self.assertEqual(message.name, "BodyStatus")
            self.assertIn("车身状态报文", message.comment)
            signal = message.signals[0]
            self.assertEqual(signal.name, "Speed")
            self.assertEqual(signal.unit, "千米/小时")
            self.assertEqual(signal.choices, {0: "关闭", 1: "打开"})


class TestChannelMappingSerialization(unittest.TestCase):

    def test_dict_round_trip(self):
        mapping = ChannelMapping(dbc="dbc/CH5.dbc", kind="canfd", channels=[5, 9],
                                 comment="备注")
        self.assertEqual(ChannelMapping.from_dict(mapping.to_dict()), mapping)

    def test_auto_kind_omitted_in_config(self):
        """kind 留空（自动）时不写进配置，配置更干净。"""
        mapping = ChannelMapping(dbc="CH5.dbc", channels=[5])
        self.assertEqual(mapping.to_dict(), {"dbc": "CH5.dbc", "channels": [5]})
        self.assertEqual(ChannelMapping.from_dict(mapping.to_dict()), mapping)

    def test_unknown_kind_falls_back_to_auto(self):
        mapping = ChannelMapping.from_dict({"dbc": "x.dbc", "kind": "weird",
                                            "channels": ["7"]})
        self.assertEqual(mapping.kind, "")
        self.assertEqual(mapping.channels, [7])

    def test_matches_ignores_kind_when_auto(self):
        auto = ChannelMapping(dbc="x.dbc", channels=[5])
        self.assertTrue(auto.matches("canfd", 5))
        self.assertTrue(auto.matches("classic", 5))
        self.assertFalse(auto.matches("canfd", 9))
        forced = ChannelMapping(dbc="x.dbc", channels=[5], kind="canfd")
        self.assertTrue(forced.matches("canfd", 5))
        self.assertFalse(forced.matches("classic", 5))

    def test_config_file_is_readable_json(self):
        tmpdir = tempfile.mkdtemp(prefix="asc_dbc_cfg_")
        try:
            library = DbcLibrary(tmpdir)
            library.add_mapping("a.dbc", [5], "canfd")
            path = library.save_config()
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertEqual(payload["version"], 2)
            self.assertEqual(payload["mappings"][0]["dbc"], "a.dbc")
        finally:
            for name in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, name))
            os.rmdir(tmpdir)


@unittest.skipUnless(HAS_CANTOOLS, "cantools 未安装，跳过 DBC 测试")
class TestFrameFormatAutoDetect(unittest.TestCase):
    """帧类型（CAN / CANFD）由 DBC 自带信息自动识别，不需要用户选择。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="asc_dbc_frame_")
        cls.dbc_dir = os.path.join(cls.tmpdir, "dbc")
        os.makedirs(cls.dbc_dir)
        cls._write("FD.dbc", dbc_with_frame_formats(14, 14, 14))      # 全是 CANFD
        cls._write("Mixed.dbc", dbc_with_frame_formats(0, 14, 14))     # 混用
        cls._write("Plain.dbc", DBC_TEXT)                             # 未标注

    @classmethod
    def _write(cls, name, text):
        with open(os.path.join(cls.dbc_dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    @classmethod
    def tearDownClass(cls):
        for root, _dirs, files in os.walk(cls.tmpdir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)

    def test_frame_format_read_from_dbc(self):
        dbc = DbcFile(os.path.join(self.dbc_dir, "FD.dbc"))
        self.assertEqual(dbc.frame_kinds, {"canfd"})
        self.assertEqual(dbc.auto_kind, "canfd")
        self.assertEqual(dbc.frame_formats, {"CANFDStandard": 3})
        self.assertIn("CANFDStandard", dbc.describe())
        self.assertEqual(dbc.messages[0x100].frame_kind, "canfd")
        self.assertEqual(dbc.messages[0x100].frame_label, "CANFDStandard")

    def test_auto_kind_empty_when_mixed_or_absent(self):
        mixed = DbcFile(os.path.join(self.dbc_dir, "Mixed.dbc"))
        self.assertEqual(mixed.frame_kinds, {"canfd", "classic"})
        self.assertEqual(mixed.auto_kind, "")            # 混用 → 不限制
        plain = DbcFile(os.path.join(self.dbc_dir, "Plain.dbc"))
        self.assertEqual(plain.frame_kinds, set())
        self.assertEqual(plain.auto_kind, "")

    def test_add_mapping_derives_kind_from_dbc(self):
        library = DbcLibrary(self.dbc_dir)
        library.load_dbc(os.path.join(self.dbc_dir, "FD.dbc"))
        mapping = library.add_mapping("FD.dbc", [5])
        self.assertEqual(mapping.kind, "canfd")                 # 自动识别，无需用户选
        self.assertTrue(mapping.matches("canfd", 5))
        self.assertFalse(mapping.matches("classic", 5))
        self.assertEqual(library.files_for("canfd", 5)[0].name, "FD.dbc")
        self.assertEqual(library.files_for("classic", 5), [])
        self.assertEqual(library.channels_for("FD.dbc"), [5])

    def test_mapping_without_frame_format_matches_any(self):
        library = DbcLibrary(self.dbc_dir)
        library.load_dbc(os.path.join(self.dbc_dir, "Plain.dbc"))
        mapping = library.add_mapping("Plain.dbc", [5])
        self.assertEqual(mapping.kind, "")
        self.assertTrue(mapping.matches("canfd", 5))
        self.assertTrue(mapping.matches("classic", 5))


if __name__ == "__main__":
    unittest.main()
