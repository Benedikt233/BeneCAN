# -*- coding: utf-8 -*-
"""信号模型回归测试（M3，纯 Python，不依赖 Qt）。

运行： python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals import model as sigmodel  # noqa: E402

KEY = ("canfd", 5, 0x17A)


def profile_item(byte, kind, unique, changes, low=0, high=255):
    return {"byte": byte, "kind": kind, "unique": unique, "min": low, "max": high,
            "mean": (low + high) / 2.0, "changes": changes}


class TestSignalModel(unittest.TestCase):

    def test_field_text(self):
        spec = sigmodel.SignalSpec(key=KEY, byte_offset=14, size=2, dtype="uint16")
        self.assertEqual(spec.field_text, "byte14-15 uint16")
        single = sigmodel.SignalSpec(key=KEY, byte_offset=3)
        self.assertEqual(single.field_text, "byte3")
        bit = sigmodel.SignalSpec(key=KEY, byte_offset=16, bit_index=0, bit_length=7)
        self.assertEqual(bit.field_text, "byte16.bit0-6")
        self.assertTrue(bit.is_bit)

    def test_display_name_includes_unit(self):
        spec = sigmodel.SignalSpec(key=KEY, byte_offset=1, name="车速", unit="km/h")
        self.assertEqual(spec.display_name(), "车速 [km/h]")
        plain = sigmodel.SignalSpec(key=KEY, byte_offset=5)
        self.assertEqual(plain.display_name(), "0x17A ch5 byte5")

    def test_auto_specs_prefers_enum_then_analog_and_skips_constant(self):
        profile = [
            profile_item(0, "constant", 1, 0),
            profile_item(1, "analog", 200, 900),
            profile_item(2, "enum", 4, 300),
            profile_item(3, "analog", 256, 1500),
            profile_item(4, "constant", 1, 0),
            profile_item(5, "enum", 2, 12),
        ]
        specs = sigmodel.auto_specs(profile, KEY, max_rows=3)
        # 枚举优先（变化多的在前），且结果按字节号排序；常量不占额度
        self.assertEqual([spec.byte_offset for spec in specs], [2, 3, 5])
        self.assertNotIn(0, [spec.byte_offset for spec in specs])
        # 枚举/状态量用阶梯绘制，模拟量用折线
        by_byte = {spec.byte_offset: spec for spec in specs}
        self.assertTrue(by_byte[2].step)
        self.assertTrue(by_byte[5].step)
        self.assertFalse(by_byte[3].step)

    def test_auto_specs_falls_back_to_constant_when_needed(self):
        profile = [profile_item(0, "constant", 1, 0), profile_item(1, "constant", 1, 0)]
        specs = sigmodel.auto_specs(profile, KEY, max_rows=2)
        self.assertEqual(len(specs), 2)

    def test_apply_colors_is_stable(self):
        specs = [sigmodel.SignalSpec(key=KEY, byte_offset=i) for i in range(3)]
        sigmodel.apply_colors(specs)
        colors = [spec.color for spec in specs]
        self.assertEqual(len(set(colors)), 3)
        first = colors[0]
        sigmodel.apply_colors(specs)          # 已有颜色不应被覆盖
        self.assertEqual(specs[0].color, first)

    def test_group_specs_by_message(self):
        specs = [
            sigmodel.SignalSpec(key=KEY, byte_offset=1),
            sigmodel.SignalSpec(key=KEY, byte_offset=2),
            sigmodel.SignalSpec(key=("canfd", 4, 0x100), byte_offset=1),
        ]
        groups = sigmodel.group_specs_by_message(specs)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[KEY]), 2)

    def test_spec_default_source_and_scale(self):
        spec = sigmodel.SignalSpec(key=KEY, byte_offset=0)
        self.assertEqual(spec.source, sigmodel.SRC_AUTO_BYTE)
        self.assertEqual(spec.scale, 1.0)
        self.assertEqual(spec.offset, 0.0)
        self.assertEqual(spec.labels, {})


if __name__ == "__main__":
    unittest.main()
