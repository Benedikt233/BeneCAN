# -*- coding: utf-8 -*-
"""从 0x3A5 的 RTC 信号推导 GPS/绝对时间轴。

为什么要单独做一层
------------------
ASC 里的时间戳是"文件内相对秒"，形态 B 的日志还会来回跳；而 0x3A5
（TBOX 时间信息）每秒带一组日期/时间字段，可以换算出真实墙上时间：

    byte0  year - 2015       byte3  hour
    byte1  month             byte4  minute
    byte2  day (低 5 位)      byte5  second
    byte6  timezone/状态（本版不参与换算）

拟合方式
--------
1. 逐帧解出 `datetime`，计算 ``offset = 墙上秒 - ASC 相对秒``；
2. 用"连续一致"的样本组成时间段：主时段里 offset 基本恒定；
   偶发/交织的另一套时间源（logCAN8 里 14:18:xx）和文件末尾的时间回退
   会被切成独立段，避免把整条时间轴带偏；
3. 每个时间段内用中位数 offset 把 ASC 相对秒映射成绝对时间；
4. 时间轴 X 采用**按段拼接的连续秒**（不插空档），刻度按段换算成
   ``HH:MM:SS``；时间段切换时标签会跳变，这与实际 RTC 行为一致。

本模块只依赖 numpy，不依赖 Qt/pyqtgraph，便于单测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

import numpy as np

logger = logging.getLogger(__name__)

GPS_MESSAGE_ID = 0x3A5
PREFERRED_CHANNELS = (1, 4, 5, 9, 17, 3, 2, 18, 19, 20)
DEFAULT_YEAR_BASE = 2015
OUTLIER_TOLERANCE = 5.0          # offset 偏离当前段超过该秒数，视为候选新段/异常
MIN_SEGMENT_SAMPLES = 5          # 连续多少帧才确认是“新时间段”
_EPOCH = datetime(1970, 1, 1)


def _wall_seconds(value: datetime) -> float:
    """naive datetime → 固定纪元的秒数（不依赖系统时区）。"""
    return (value - _EPOCH).total_seconds()


def _dt_from_wall(value: float) -> datetime:
    return _EPOCH + timedelta(seconds=float(value))


@dataclass(frozen=True)
class RtcLayout:
    """0x3A5 的字节布局。默认值对应现场日志（2026-07-13/2026-09-22 验证）。"""

    year_byte: int = 0
    month_byte: int = 1
    day_byte: int = 2
    hour_byte: int = 3
    minute_byte: int = 4
    second_byte: int = 5
    timezone_byte: int = 6
    year_base: int = DEFAULT_YEAR_BASE
    month_mask: int = 0x0F
    day_mask: int = 0x1F
    hour_mask: int = 0x1F
    minute_mask: int = 0x3F
    second_mask: int = 0x3F

    def decode(self, payload) -> datetime | None:
        """8 字节 payload → datetime；字段非法时返回 None。"""
        if payload is None or len(payload) < 7:
            return None
        try:
            year = self.year_base + int(payload[self.year_byte])
            month = int(payload[self.month_byte]) & self.month_mask
            day = int(payload[self.day_byte]) & self.day_mask
            hour = int(payload[self.hour_byte]) & self.hour_mask
            minute = int(payload[self.minute_byte]) & self.minute_mask
            second = int(payload[self.second_byte]) & self.second_mask
            if not (2000 <= year <= 2200):
                return None
            if not (1 <= month <= 12 and 1 <= day <= 31
                    and 0 <= hour <= 23 and 0 <= minute <= 59
                    and 0 <= second <= 60):
                return None
            return datetime(year, month, day, hour, minute, second)
        except (TypeError, ValueError, IndexError):
            return None

    def timezone_raw(self, payload) -> int | None:
        if payload is None or len(payload) <= self.timezone_byte:
            return None
        return int(payload[self.timezone_byte])


@dataclass(frozen=True)
class RtcSample:
    ts: float
    dt: datetime

    @property
    def wall(self) -> float:
        return _wall_seconds(self.dt)

    @property
    def offset(self) -> float:
        return self.wall - self.ts


@dataclass
class GpsSegment:
    """一个连续时间段：ASC 相对秒 [ts_start, ts_end] → 连续 X [x_start, x_end]。"""

    ts_start: float
    ts_end: float
    x_start: float
    x_end: float
    offset: float
    abs_start: datetime

    @property
    def x_duration(self) -> float:
        return self.x_end - self.x_start


def _med(values) -> float:
    return float(np.median(np.asarray(list(values), dtype=np.float64)))


def _group_samples(samples: Iterable[RtcSample],
                   tolerance: float = OUTLIER_TOLERANCE,
                   min_samples: int = MIN_SEGMENT_SAMPLES) -> list:
    """把样本切成连续一致的时间段；交织的异常样本会被丢弃。"""
    ordered = sorted(samples, key=lambda item: item.ts)
    groups = []
    accepted = []
    pending = []
    current_offset = None

    for sample in ordered:
        if current_offset is None:
            accepted = [sample]
            current_offset = sample.offset
            continue
        if abs(sample.offset - current_offset) <= tolerance:
            accepted.append(sample)
            pending = []
            recent = accepted[-10:]
            current_offset = _med(item.offset for item in recent)
            continue

        pending.append(sample)
        if len(pending) < min_samples:
            continue
        spread = max(item.offset for item in pending) - min(item.offset for item in pending)
        if spread > tolerance:
            pending = pending[-min_samples:]
            continue
        # 连续 min_samples 帧都落在新 offset 上 → 确认新时间段
        if accepted:
            groups.append(accepted)
        accepted = list(pending)
        pending = []
        current_offset = _med(item.offset for item in accepted)

    if accepted:
        groups.append(accepted)
    return groups


def build_gps_segments(samples: Iterable[RtcSample], ts_first: float,
                       ts_last: float, tolerance: float = OUTLIER_TOLERANCE,
                       min_samples: int = MIN_SEGMENT_SAMPLES) -> tuple:
    """返回 (segments, used_sample_count)。"""
    groups = _group_samples(samples, tolerance, min_samples)
    segments = []
    used = 0
    x_cursor = 0.0
    for index, group in enumerate(groups):
        if not group:
            continue
        offset = _med(item.offset for item in group)
        first_ts = group[0].ts
        start = float(ts_first) if index == 0 else float(first_ts)
        if index + 1 < len(groups) and groups[index + 1]:
            end = float(groups[index + 1][0].ts)
        else:
            end = max(float(ts_last), float(group[-1].ts))
        if end < start:
            end = start
        abs_start = _dt_from_wall(start + offset)
        segment = GpsSegment(ts_start=start, ts_end=end,
                             x_start=x_cursor,
                             x_end=x_cursor + max(0.0, end - start),
                             offset=offset, abs_start=abs_start)
        segments.append(segment)
        x_cursor = segment.x_end
        used += len(group)
    return segments, used


@dataclass
class GpsTimeAxis:
    """0x3A5 RTC → 绝对时间轴。"""

    segments: list
    source: str = ""
    layout: RtcLayout = field(default_factory=RtcLayout)
    sample_count: int = 0
    outlier_count: int = 0
    sample_first_dt: datetime = None
    sample_last_dt: datetime = None
    _ts_starts: np.ndarray = field(default=None, init=False, repr=False)
    _x_starts: np.ndarray = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if not self.segments:
            raise ValueError("GpsTimeAxis 至少需要一个时间段")
        self._ts_starts = np.asarray([s.ts_start for s in self.segments],
                                     dtype=np.float64)
        self._x_starts = np.asarray([s.x_start for s in self.segments],
                                    dtype=np.float64)

    @classmethod
    def from_samples(cls, samples, ts_first: float, ts_last: float,
                     source: str = "", layout: RtcLayout = None,
                     tolerance: float = OUTLIER_TOLERANCE) -> "GpsTimeAxis | None":
        samples = list(samples)
        if not samples:
            return None
        segments, used = build_gps_segments(samples, ts_first, ts_last,
                                            tolerance=tolerance)
        if not segments:
            return None
        ordered = sorted(samples, key=lambda item: item.ts)
        axis = cls(segments=segments, source=source,
                   layout=layout or RtcLayout(),
                   sample_count=len(samples),
                   outlier_count=len(samples) - used,
                   sample_first_dt=ordered[0].dt,
                   sample_last_dt=ordered[-1].dt)
        logger.info("GPS 时间轴：%s", axis.describe())
        return axis

    @classmethod
    def from_reader(cls, reader, message_id: int = GPS_MESSAGE_ID,
                    channels=None, layout: RtcLayout = None) -> "GpsTimeAxis | None":
        """扫描日志里的 0x3A5，选一条有有效 RTC 的通道建立时间轴。"""
        if reader is None:
            return None
        catalog = list(reader.catalog())
        by_channel = {}
        for message in catalog:
            if message.can_id == message_id:
                by_channel[message.channel] = message
        if not by_channel:
            logger.info("GPS 时间不可用：日志里没有 0x%X", message_id)
            return None

        preferred = list(channels or PREFERRED_CHANNELS)
        order = [ch for ch in preferred if ch in by_channel]
        order += [ch for ch in sorted(by_channel) if ch not in order]
        ts_first = float(reader.metadata.get("ts_first", 0.0))
        ts_last = float(reader.metadata.get("ts_last", ts_first))
        layout = layout or RtcLayout()

        for channel in order:
            message = by_channel[channel]
            try:
                series = reader.frame_series(message.kind, channel, message_id)
            except Exception as exc:                 # noqa: BLE001
                logger.warning("读取 0x%X ch%d 失败：%s", message_id, channel, exc)
                continue
            samples = []
            for index, ts in enumerate(series.ts):
                dt = layout.decode(series.data[index])
                if dt is not None:
                    samples.append(RtcSample(float(ts), dt))
            if len(samples) < 2:
                continue
            axis = cls.from_samples(
                samples, ts_first, ts_last,
                source="0x%X ch%d (%s)" % (message_id, channel, message.kind),
                layout=layout)
            if axis is not None:
                logger.info("选用 GPS 时间源：%s（%d 帧，%d 个异常/跳变样本）",
                            axis.source, axis.sample_count, axis.outlier_count)
                return axis
        logger.warning("GPS 时间不可用：0x%X 没有可解析的 RTC 字段", message_id)
        return None

    # ------------------------------------------------------------ 映射
    def to_x(self, ts) -> np.ndarray:
        """ASC 相对秒 → 时间轴 X（按段拼接，单调）。"""
        values = np.asarray(ts, dtype=np.float64)
        index = np.searchsorted(self._ts_starts, values, side="right") - 1
        index = np.clip(index, 0, len(self.segments) - 1)
        starts = self._ts_starts[index]
        return self._x_starts[index] + (values - starts)

    def x_to_dt(self, x: float) -> datetime:
        index = int(np.searchsorted(self._x_starts, float(x), side="right") - 1)
        index = max(0, min(index, len(self.segments) - 1))
        segment = self.segments[index]
        return segment.abs_start + timedelta(seconds=float(x) - segment.x_start)

    # ------------------------------------------------------------ 格式化
    def format_x(self, x: float, include_date: bool = False,
                 with_ms: bool = False) -> str:
        value = self.x_to_dt(x)
        text = value.strftime("%H:%M:%S")
        if with_ms:
            text += ".%03d" % (value.microsecond // 1000)
        if include_date:
            text = value.strftime("%m-%d ") + text
        return text

    def format_full(self, x: float) -> str:
        value = self.x_to_dt(x)
        text = value.strftime("%Y-%m-%d %H:%M:%S")
        if value.microsecond:
            text += ".%03d" % (value.microsecond // 1000)
        return text

    def describe(self) -> str:
        first = self.sample_first_dt or self.segments[0].abs_start
        last = self.sample_last_dt or self.x_to_dt(self.segments[-1].x_end)
        return ("%s · %d 段 · %d/%d 有效帧 · RTC %s ~ %s"
                % (self.source or "0x3A5", len(self.segments),
                   self.sample_count - self.outlier_count, self.sample_count,
                   first.strftime("%Y-%m-%d %H:%M:%S"),
                   last.strftime("%Y-%m-%d %H:%M:%S")))
