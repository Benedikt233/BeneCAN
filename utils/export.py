# -*- coding: utf-8 -*-
"""导出与布局：宽表 CSV（保持前一值）、图形布局 JSON 存取。

设计要点
--------
1. **宽表 + 保持前一值（hold-last）**：CANoe 导出/值表都是"某个时刻的信号值"语义 ——
   信号只在帧到达时更新，其余时间保持上一个值。所以导出时先取所有行 X 轴的并集，
   再对每行用"最后一条 <= 该时刻"的值填充；第一条之前留空。
2. **规模护栏**：并集可能远大于单行点数（12 行 × 16 万帧 → 上百万行），
   超过 `MAX_EXPORT_ROWS` 直接报错并提示用光标缩小范围，而不是静默抽样。
3. **布局 JSON**：只存"行的定义 + 时间轴模式 + 视图范围 + 光标位置"，
   不含数据；换一份 ASC 也能复用同一套图形布局。
"""

from __future__ import annotations

import csv
import json
import logging
import os

import numpy as np

from config import APP_NAME, TOOL_VERSION

logger = logging.getLogger(__name__)

MAX_EXPORT_ROWS = 500_000
EMPTY = ""


# ---------------------------------------------------------------- 数值对齐

def union_x(rows, t0: float = None, t1: float = None) -> np.ndarray:
    """所有行 X 轴的并集（升序去重），可按 [t0, t1] 裁剪。"""
    arrays = []
    for row in rows:
        if row.x is None or len(row.x) == 0:
            continue
        arrays.append(np.asarray(row.x, dtype=np.float64))
    if not arrays:
        return np.empty(0, dtype=np.float64)
    merged = np.unique(np.concatenate(arrays))
    if t0 is not None:
        merged = merged[merged >= t0]
    if t1 is not None:
        merged = merged[merged <= t1]
    return merged


def hold_values(x_union: np.ndarray, x: np.ndarray, values: np.ndarray) -> np.ndarray:
    """按"保持前一值"把信号对齐到 x_union；第一个采样点之前为 NaN。"""
    result = np.full(len(x_union), np.nan, dtype=np.float64)
    if len(x) == 0 or len(x_union) == 0:
        return result
    index = np.searchsorted(x, x_union, side="right") - 1
    valid = index >= 0
    result[valid] = values[index[valid]]
    return result


def build_wide_table(rows, t0: float = None, t1: float = None,
                     max_rows: int = MAX_EXPORT_ROWS) -> tuple:
    """构造宽表：返回 (x_union, 列数据列表, 名称列表)。

    Raises:
        ValueError: 并集行数超过 max_rows（提示用光标缩小范围）。
    """
    x_union = union_x(rows, t0, t1)
    if len(x_union) > max_rows:
        raise ValueError(
            "导出范围含 %d 行（上限 %d）：请用双光标缩小时间范围，或减少图形行数。"
            % (len(x_union), max_rows))
    columns = []
    names = []
    for row in rows:
        columns.append(hold_values(x_union, np.asarray(row.x, dtype=np.float64),
                                   np.asarray(row.values, dtype=np.float64)))
        names.append(row.name)
    return x_union, columns, names


# ---------------------------------------------------------------- CSV

def export_wide_csv(path: str, rows, t0: float = None, t1: float = None,
                    x_label: str = "时间", x_unit: str = "s",
                    max_rows: int = MAX_EXPORT_ROWS) -> dict:
    """把当前图形行导出为 CSV（双行表头：中文说明 / 英文标识）。

    Returns:
        {"path", "rows", "columns", "bytes", "t0", "t1"}
    """
    x_union, columns, names = build_wide_table(rows, t0, t1, max_rows)

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["%s(%s)" % (x_label, x_unit)] + names)
        writer.writerow([x_label + "_" + x_unit] + [row_ident(rows[i]) for i in range(len(rows))])
        for position, x in enumerate(x_union):
            line = ["%.6f" % x]
            for column in columns:
                value = column[position]
                line.append(EMPTY if np.isnan(value) else _format_value(value))
            writer.writerow(line)

    stats = {
        "path": path,
        "rows": len(x_union),
        "columns": len(columns) + 1,
        "bytes": os.path.getsize(path),
        "t0": float(x_union[0]) if len(x_union) else None,
        "t1": float(x_union[-1]) if len(x_union) else None,
    }
    logger.debug("导出宽表 CSV：%s（%d 行 × %d 列，%.1f KB，范围 %s~%s）",
                 path, stats["rows"], stats["columns"], stats["bytes"] / 1024.0,
                 stats["t0"], stats["t1"])
    return stats


def row_ident(row) -> str:
    """英文列标识：canfd ch5 0x17A byte14。"""
    spec = row.spec
    return "%s ch%d 0x%X %s" % (spec.kind, spec.channel, spec.can_id, spec.field_text)


def _format_value(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return "%d" % int(value)
    return "%.6g" % float(value)


# ---------------------------------------------------------------- 布局

def save_layout(path: str, specs, time_mode: str = "time",
                x_range: tuple = None, cursors: tuple = None,
                source: str = "") -> dict:
    """保存图形布局（行定义 + 时间轴模式 + 视图范围 + 光标）。"""
    payload = {
        "tool": APP_NAME,
        "version": TOOL_VERSION,
        "source": source,
        "time_mode": time_mode,
        "x_range": list(x_range) if x_range else None,
        "cursors": list(cursors) if cursors else None,
        "rows": [spec.to_dict() for spec in specs],
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    logger.debug("保存布局：%s（%d 行，时间轴 %s）", path, len(payload["rows"]), time_mode)
    return payload


def load_layout(path: str) -> dict:
    """读取布局，返回 {"rows": [SignalSpec], "time_mode", "x_range", "cursors", "source"}。"""
    from signals.model import SignalSpec

    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    logger.debug("读取布局：%s（工具 %s %s，%d 行，源 %s）", path,
                 payload.get("tool", "?"), payload.get("version", "?"),
                 len(payload.get("rows", [])), payload.get("source", ""))
    return {
        "rows": [SignalSpec.from_dict(item) for item in payload.get("rows", [])],
        "time_mode": payload.get("time_mode", "time"),
        "x_range": tuple(payload["x_range"]) if payload.get("x_range") else None,
        "cursors": tuple(payload["cursors"]) if payload.get("cursors") else None,
        "source": payload.get("source", ""),
    }
