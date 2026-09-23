# -*- coding: utf-8 -*-
"""行的数值统计与采样点吸附（纯函数，无 Qt / 无 pyarrow，便于单测）。

为什么单独一个模块
------------------
图形区需要两类"算一下就完事"的小工具：
1. **行统计**（最小/最大/均值/标准差/变化次数）：用于"每行统计 + 极值标记"；
   数据已在内存（GraphicRow.values），不需要回查索引。
2. **采样点吸附**：鼠标读值与光标拖动都希望落在**真实帧时刻**上，
   否则会看到"看起来 40、其实是 39"的错觉（保持前一值语义下尤其容易误判）。

两者都是纯数组运算，放在这里可脱离 Qt 单独验证（tests/test_stats.py）。
"""

from __future__ import annotations

import numpy as np

STATS_KEYS = ("count", "min", "max", "mean", "std", "span", "first", "last", "changes")


def stats_of(values) -> dict:
    """一行的数值统计（忽略 NaN）。

    Returns:
        dict，键见 STATS_KEYS；count == 0（空数组/全 NaN）时除 count 外均为 None。
    """
    empty = {key: None for key in STATS_KEYS}
    empty["count"] = 0
    if values is None:
        return empty

    array = np.asarray(values, dtype=np.float64).reshape(-1)
    if array.size == 0:
        return empty
    valid = array[~np.isnan(array)]
    if valid.size == 0:
        return empty

    changes = int(np.count_nonzero(np.diff(valid))) if valid.size > 1 else 0
    return {
        "count": int(valid.size),
        "min": float(valid.min()),
        "max": float(valid.max()),
        "mean": float(valid.mean()),
        "std": float(valid.std()),
        "span": float(valid.max() - valid.min()),
        "first": float(valid[0]),
        "last": float(valid[-1]),
        "changes": changes,
    }


def nearest_index(x_array, x) -> int:
    """x 在（已排序的）x_array 里最接近的下标；数组为空返回 -1。"""
    if x_array is None or x is None:
        return -1
    array = np.asarray(x_array, dtype=np.float64)
    if array.size == 0:
        return -1
    position = int(np.searchsorted(array, float(x)))
    if position <= 0:
        return 0
    if position >= array.size:
        return array.size - 1
    before, after = array[position - 1], array[position]
    return position - 1 if abs(float(x) - before) <= abs(after - float(x)) else position


def nearest_sample(x_array, x):
    """吸附结果：返回 (下标, 采样点 x 值)；无数据时返回 (-1, None)。"""
    index = nearest_index(x_array, x)
    if index < 0:
        return -1, None
    return index, float(np.asarray(x_array, dtype=np.float64)[index])


def format_number(value, digits: int = 4) -> str:
    """统计/读值文本：整数按整数显示，其余保留有效小数。"""
    if value is None:
        return "—"
    number = float(value)
    if number.is_integer() and abs(number) < 1e15:
        return "%d" % int(number)
    return ("%%.%df" % digits) % number
