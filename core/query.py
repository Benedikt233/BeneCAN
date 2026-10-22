# -*- coding: utf-8 -*-
"""查询层（M2）：从 Parquet 索引里取"报文序列 / 信号序列 / 统计 / 目录"。

给 UI（M3）用的最小完备接口：
    IndexReader.catalog()           通道 → 报文 → 帧数/时长/长度集合   （树的填充）
    IndexReader.frame_series(...)   单报文全量帧（ts + 64 字节 payload）
    IndexReader.signal_series(...)  从 payload 里抽字节/位/浮点 → 模拟量或枚举量
    IndexReader.byte_profile(...)   逐字节画像（常量/枚举/模拟），用于信号树展开
    IndexReader.message_stats(...)  周期/抖动/丢包估计/长度与 DLC 集合

时间轴模式：
    TIME    原样使用 ASC 时间戳（索引内 ts 已按该报文升序，可直接绘图）
    SESSION 把"空档/回退"切成段后拼接，压缩空闲时间（长空档报文更紧凑）
    INDEX   使用帧序号，完全不依赖时间戳
    GPS     由 0x3A5 RTC 推出的绝对时间（见 signals/gps_time.py；X 仍由 UI 映射）

注意：索引已按 (kind, channel, can_id, ts) 排序，所以单个报文的 ts 天然递增；
源文件本身乱序的情况由 FrameSeries.file_time_ordered 暴露。

设计约定：所有序列查询都带 (kind, channel, can_id) 三要素 + 可选时间窗，
命中索引里物理连续的 row group，实测单报文 16 万行查询 0.03 s 量级。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from core.indexer import (FLAG_EXTENDED, KIND_NAMES, PAYLOAD_SIZE, build_index,
                          default_index_path, index_is_fresh,
                          read_index_metadata)

logger = logging.getLogger(__name__)

MODE_TIME = "time"
MODE_SESSION = "session"
MODE_INDEX = "index"
MODE_GPS = "gps"

# 会话切段阈值：时间戳回退超过该值（秒）或正向空档超过该值，即认为换段
SESSION_BACKWARD = 1e-6
SESSION_GAP = 2.0

_DTYPES = {
    "uint8":  (1, False), "int8":  (1, True),
    "uint16": (2, False), "int16": (2, True),
    "uint32": (4, False), "int32": (4, True),
    "uint64": (8, False), "int64": (8, True),
}
BYTE_ORDERS = ("little", "big")
BYTE_ORDER_ALIASES = {"little": "little", "le": "little", "big": "big", "be": "big"}


# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class MessageInfo:
    """目录条目：一个 (kind, channel, can_id) 报文。"""
    kind: str
    channel: int
    can_id: int
    extended: bool
    frames: int
    ts_first: float
    ts_last: float
    length_min: int
    length_max: int

    @property
    def duration(self) -> float:
        return self.ts_last - self.ts_first

    @property
    def key(self) -> tuple:
        return (self.kind, self.channel, self.can_id)

    @property
    def id_text(self) -> str:
        return ("0x%03X" if self.can_id <= 0x7FF else "0x%08X") % self.can_id

    @property
    def label(self) -> str:
        return "%s ch%d %s%s" % (self.kind.upper(), self.channel, self.id_text,
                                " (扩展)" if self.extended else "")


@dataclass(frozen=True)
class ChannelInfo:
    """一个通道的汇总（kind + channel）。"""
    kind: str
    channel: int
    messages: int
    frames: int
    ts_first: float
    ts_last: float

    @property
    def duration(self) -> float:
        return self.ts_last - self.ts_first


@dataclass
class FrameSeries:
    """单报文的帧序列。ts/x 为时间轴，data 为 (N, 64) 的 uint8 视图。"""
    kind: str
    channel: int
    can_id: int
    extended: bool
    ts: np.ndarray            # float64, 原始时间戳
    x: np.ndarray             # float64, 按时间轴模式处理后的 X 轴
    seq: np.ndarray           # uint32, 原始行序
    data: np.ndarray          # (N, 64) uint8
    length: np.ndarray        # uint8
    dlc: np.ndarray           # uint8
    time_mode: str = MODE_TIME
    segments: np.ndarray = field(default=None)   # 会话分段号（MODE_SESSION 时有效）

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def file_time_ordered(self) -> bool:
        """按**源文件出现顺序**看，时间戳是否单调。

        索引已按 ts 排序，所以 ts 本身一定递增；本属性用于回答
        "源文件里这个报文的时间戳是否乱序"（形态 B 的通道会出现 False，
        即源文件本身乱序、索引已按时间重排）。
        """
        if len(self.ts) < 2:
            return True
        order = np.argsort(self.seq, kind="stable")
        ordered = self.ts[order]
        return bool(np.all(np.diff(ordered) >= -1e-9))

    @property
    def idle_gap_count(self) -> int:
        """按 SESSION 阈值统计的空档段数（分段拼接用）。"""
        if len(self.ts) < 2:
            return 0
        gaps = np.diff(self.ts)
        return int(np.count_nonzero((gaps < SESSION_BACKWARD) | (gaps > SESSION_GAP)))


@dataclass
class SignalSeries:
    """从 payload 抽出的信号序列。y 为 float64（数值量）；枚举/十六进制另存 labels。"""
    message: MessageInfo
    x: np.ndarray
    y: np.ndarray             # float64（数值/缩放后）；hex 模式时为 float('nan')
    raw: np.ndarray           # int64 原始整数值（未缩放）
    byte_offset: int
    size: int
    dtype: str
    byte_order: str
    seq: np.ndarray = None    # 原始行序（全局帧序号，UI 的"帧序号"时间轴用）
    bit_index: int = -1       # >=0 表示按位取值
    bit_length: int = 0
    scale: float = 1.0
    offset: float = 0.0
    labels: dict = field(default_factory=dict)   # 枚举值 -> 文本
    hex_text: list = field(default=None)         # hex 模式下的原始文本

    def __len__(self) -> int:
        return len(self.x)

    @property
    def value_text(self) -> list:
        """供 UI 显示的值列表（数值或枚举文本或十六进制）。"""
        if self.hex_text is not None:
            return self.hex_text
        if self.labels:
            return [self.labels.get(int(v), str(int(v))) for v in self.raw]
        return ["%g" % v for v in self.y]


# ---------------------------------------------------------------- 索引读取

class IndexReader:
    """一个 ASC 索引的只读查询入口。"""

    def __init__(self, index_path: str):
        self.index_path = index_path
        self.metadata = read_index_metadata(index_path)
        self._catalog = None
        logger.debug("打开索引：%s（%s 帧，源文件 %s）", index_path,
                     self.metadata.get("frames", "?"),
                     self.metadata.get("source_path", "?"))

    # -------------------------------------------------- 打开 / 缓存
    @classmethod
    def open(cls, index_path: str) -> "IndexReader":
        if not index_path or not index_path.endswith(".parquet"):
            raise ValueError("index_path 必须是 .parquet 索引文件")
        return cls(index_path)

    @classmethod
    def open_for_asc(cls, asc_path: str, build: bool = True) -> "IndexReader":
        """按 ASC 打开对应索引；缺失/过期时可选自动重建（M3 的入口）。"""
        index_path = default_index_path(asc_path)
        if build and not index_is_fresh(asc_path, index_path):
            build_index(asc_path, index_path=index_path)
        return cls(index_path)

    def is_fresh_for(self, asc_path: str) -> bool:
        return index_is_fresh(asc_path, self.index_path)

    @property
    def frames(self) -> int:
        return int(self.metadata.get("frames", 0))

    @property
    def source_path(self) -> str:
        return self.metadata.get("source_path", "")

    # -------------------------------------------------- 目录
    def catalog(self, refresh: bool = False) -> list:
        """全部报文条目，按 (kind, channel, can_id) 排序。"""
        if self._catalog is None or refresh:
            self._catalog = self._build_catalog()
        return self._catalog

    def _build_catalog(self) -> list:
        table = pq.read_table(self.index_path,
                              columns=["kind", "channel", "can_id", "flags",
                                       "ts", "length"])
        grouped = table.group_by(["kind", "channel", "can_id", "flags"]).aggregate([
            ("ts", "min"), ("ts", "max"),
            ("length", "min"), ("length", "max"), ("length", "count"),
        ])
        rows = grouped.to_pylist()
        out = []
        for row in rows:
            out.append(MessageInfo(
                kind=KIND_NAMES.get(row["kind"], "kind%d" % row["kind"]),
                channel=row["channel"],
                can_id=row["can_id"],
                extended=bool(row["flags"] & FLAG_EXTENDED),
                frames=row["length_count"],
                ts_first=row["ts_min"],
                ts_last=row["ts_max"],
                length_min=row["length_min"],
                length_max=row["length_max"],
            ))
        out.sort(key=lambda m: (m.kind, m.channel, m.can_id))
        return out

    def channels(self) -> list:
        """按 (kind, channel) 汇总，供树的顶层。"""
        buckets = {}
        for msg in self.catalog():
            key = (msg.kind, msg.channel)
            cur = buckets.get(key)
            if cur is None:
                buckets[key] = {"messages": 1, "frames": msg.frames,
                                "ts_first": msg.ts_first, "ts_last": msg.ts_last}
            else:
                cur["messages"] += 1
                cur["frames"] += msg.frames
                cur["ts_first"] = min(cur["ts_first"], msg.ts_first)
                cur["ts_last"] = max(cur["ts_last"], msg.ts_last)
        return [ChannelInfo(kind=k[0], channel=k[1], **v)
                for k, v in sorted(buckets.items())]

    def find(self, kind: str, channel: int, can_id: int) -> MessageInfo:
        for msg in self.catalog():
            if msg.key == (kind, channel, can_id):
                return msg
        raise KeyError("索引中没有 %s ch%s id=%s" % (kind, channel, hex(can_id)))

    def handle(self, message: MessageInfo) -> list:
        """从目录条目生成 (kind, channel, can_id) 查询选择器。"""
        return self.selector(message.kind, message.channel, message.can_id)

    @staticmethod
    def selector(kind: str, channel: int, can_id: int) -> list:
        kind_code = {v: k for k, v in KIND_NAMES.items()}[kind]
        return [("kind", "=", kind_code), ("channel", "=", channel),
                ("can_id", "=", can_id)]

    # -------------------------------------------------- 帧序列
    def frame_series(self, kind: str, channel: int, can_id: int,
                     t0: float = None, t1: float = None,
                     time_mode: str = MODE_TIME) -> FrameSeries:
        """取单报文的帧序列（可选时间窗），并按时间轴模式生成 X 轴。"""
        filters = self.selector(kind, channel, can_id)
        if t0 is not None:
            filters.append(("ts", ">=", float(t0)))
        if t1 is not None:
            filters.append(("ts", "<=", float(t1)))
        table = pq.read_table(self.index_path, filters=filters)

        ts = _numpy(table, "ts", np.float64)
        seq = _numpy(table, "seq", np.uint32)
        length = _numpy(table, "length", np.uint8)
        dlc = _numpy(table, "dlc", np.uint8)
        data = _payload_view(table)

        info = _info_from_table(table, kind, channel, can_id)
        x, segments = build_time_axis(ts, time_mode)
        return FrameSeries(kind=kind, channel=channel, can_id=can_id,
                           extended=info.extended if info else False,
                           ts=ts, x=x, seq=seq, data=data,
                           length=length, dlc=dlc, time_mode=time_mode,
                           segments=segments)

    def frame_series_of(self, message: MessageInfo, **kwargs) -> FrameSeries:
        return self.frame_series(message.kind, message.channel, message.can_id,
                                 **kwargs)

    # -------------------------------------------------- 信号序列
    def signal_series(self, kind: str, channel: int, can_id: int,
                      byte_offset: int = 0, size: int = 1,
                      dtype: str = "uint8", byte_order: str = "little",
                      bit_index: int = -1, bit_length: int = 0,
                      scale: float = 1.0, offset: float = 0.0,
                      labels: dict = None, as_hex: bool = False,
                      t0: float = None, t1: float = None,
                      time_mode: str = MODE_TIME) -> SignalSeries:
        """抽取信号：字节/位/浮点，可选缩放与枚举文本。"""
        series = self.frame_series(kind, channel, can_id, t0=t0, t1=t1,
                                   time_mode=time_mode)
        info = _info_from_series(series)
        if info is None:
            raise KeyError("索引中没有 %s ch%s id=%s" % (kind, channel, hex(can_id)))

        if as_hex:
            # 十六进制显示模式：size 只决定显示宽度，数值按同宽无符号整数给出
            auto = {1: "uint8", 2: "uint16", 4: "uint32", 8: "uint64"}.get(size)
            raw = (extract_bytes(series.data, byte_offset, size, auto)
                   if auto else np.zeros(len(series), dtype=np.int64))
            hex_text = [" ".join("%02X" % b for b in row)
                        for row in series.data[:, byte_offset:byte_offset + size]]
            return SignalSeries(message=info, x=series.x, y=np.full(len(raw), np.nan),
                                raw=raw, byte_offset=byte_offset, size=size,
                                dtype=auto or "uint8", byte_order=byte_order,
                                seq=series.seq, hex_text=hex_text)

        if bit_index >= 0:
            raw = extract_bits(series.data, byte_offset, bit_index, bit_length,
                               byte_order)
            size, dtype = 1, "uint8"
        elif dtype in ("float32", "float"):
            raw = extract_float(series.data, byte_offset, byte_order)
            size, dtype = 4, "float32"
        else:
            raw = extract_bytes(series.data, byte_offset, size, dtype, byte_order)

        y = raw.astype(np.float64) * scale + offset
        return SignalSeries(message=info, x=series.x, y=y, raw=raw,
                            byte_offset=byte_offset, size=size, dtype=dtype,
                            byte_order=byte_order, seq=series.seq,
                            bit_index=bit_index, bit_length=bit_length,
                            scale=scale, offset=offset,
                            labels=dict(labels or {}), hex_text=None)

    def byte_profile(self, kind: str, channel: int, can_id: int) -> list:
        """逐字节画像：判断常量 / 枚举 / 模拟，供信号树展开。"""
        series = self.frame_series(kind, channel, can_id)
        out = []
        for index in range(PAYLOAD_SIZE):
            column = series.data[:, index]
            uniq = np.unique(column)
            if len(uniq) == 1:
                kind_text = "constant"
            elif len(uniq) <= 16:
                kind_text = "enum"
            else:
                kind_text = "analog"
            out.append({
                "byte": index,
                "kind": kind_text,
                "unique": int(len(uniq)),
                "min": int(column.min()) if len(column) else 0,
                "max": int(column.max()) if len(column) else 0,
                "mean": float(column.mean()) if len(column) else 0.0,
                "changes": int(np.count_nonzero(np.diff(column))) if len(column) > 1 else 0,
            })
        return out

    # -------------------------------------------------- 统计
    def message_stats(self, kind: str, channel: int, can_id: int) -> dict:
        """周期/抖动/丢包估计/长度与 DLC 集合。

        区间统计基于**原始时间戳**（不做时间轴拼接，保证数值真实）；
        `file_time_ordered=False` 表示源文件里该报文的时间戳本身乱序
        （形态 B 的通道），此时区间统计仅供参考；`segments` 为按空档切出的段数。
        """
        series = self.frame_series(kind, channel, can_id, time_mode=MODE_TIME)
        ts = series.ts
        segments = build_time_axis(series.ts, MODE_SESSION)[1]
        stats = {
            "kind": kind, "channel": channel, "can_id": can_id,
            "frames": len(ts),
            "ts_first": float(ts[0]) if len(ts) else 0.0,
            "ts_last": float(ts[-1]) if len(ts) else 0.0,
            "lengths": sorted(int(v) for v in np.unique(series.length)),
            "dlcs": sorted(int(v) for v in np.unique(series.dlc)),
            "file_time_ordered": series.file_time_ordered,
            "segments": int(segments.max()) + 1 if segments is not None else 1,
        }
        if len(ts) > 1:
            gaps = np.diff(ts)
            stats["interval_median"] = float(np.median(gaps))
            stats["interval_min"] = float(gaps.min())
            stats["interval_max"] = float(gaps.max())
            stats["interval_std"] = float(gaps.std())
            stats["cycle_hz"] = (1.0 / stats["interval_median"]
                                 if stats["interval_median"] > 0 else 0.0)
            if stats["interval_median"] > 0:
                expected = (ts[-1] - ts[0]) / stats["interval_median"]
                stats["missing_estimate"] = int(max(0, round(expected - (len(ts) - 1))))
                threshold = max(2.0 * stats["interval_median"], SESSION_GAP)
                stats["gap_count"] = int(np.count_nonzero(gaps > threshold))
            else:
                stats["missing_estimate"] = 0
                stats["gap_count"] = 0
        else:
            stats.update({"interval_median": 0.0, "interval_min": 0.0,
                          "interval_max": 0.0, "interval_std": 0.0,
                          "cycle_hz": 0.0, "missing_estimate": 0, "gap_count": 0})
        return stats


# ---------------------------------------------------------------- 时间轴

def build_time_axis(ts: np.ndarray, mode: str):
    """按时间轴模式生成 X 轴。

    Returns:
        (x, segments)；segments 仅 SESSION 模式非 None，为每帧所属段号。
    """
    if mode == MODE_INDEX:
        return np.arange(len(ts), dtype=np.float64), None
    if mode != MODE_SESSION or len(ts) < 2:
        return ts.astype(np.float64, copy=False), None

    new_segment = np.zeros(len(ts), dtype=bool)
    new_segment[1:] = (np.diff(ts) < SESSION_BACKWARD) | (np.diff(ts) > SESSION_GAP)
    segments = np.cumsum(new_segment)
    if segments[-1] == 0:
        return ts.astype(np.float64, copy=False), segments

    x = np.empty(len(ts), dtype=np.float64)
    offset = 0.0
    start = 0
    for index in range(1, len(ts) + 1):
        if index == len(ts) or segments[index] != segments[start]:
            chunk = ts[start:index]
            x[start:index] = chunk - chunk[0] + offset
            offset = x[index - 1] + SESSION_GAP
            start = index
    return x, segments


# ---------------------------------------------------------------- 取值原语

def extract_bytes(data: np.ndarray, byte_offset: int, size: int,
                  dtype: str = "uint8", byte_order: str = "little") -> np.ndarray:
    """按字节偏移取整数值（默认小端）。"""
    if dtype not in _DTYPES:
        raise ValueError("不支持的 dtype：%s" % dtype)
    size_of, signed = _DTYPES[dtype]
    if size != size_of:
        raise ValueError("dtype=%s 需要 size=%d，收到 %d" % (dtype, size_of, size))
    if byte_offset < 0 or byte_offset + size > PAYLOAD_SIZE:
        raise ValueError("字节范围越界：offset=%d size=%d" % (byte_offset, size))
    byte_order = _normalize_byte_order(byte_order)

    out = data[:, byte_offset].astype(np.uint64)
    if byte_order == "little":
        for index in range(1, size):
            out |= data[:, byte_offset + index].astype(np.uint64) << (8 * index)
    else:
        for index in range(1, size):
            out = (out << 8) | data[:, byte_offset + index].astype(np.uint64)
    if not signed:
        return out

    bits = size * 8
    sign_bit = np.uint64(1) << np.uint64(bits - 1)
    if bits == 64:
        return out.view(np.int64)
    negative = (out & sign_bit) != 0
    return np.where(negative, out.astype(np.int64) - (1 << bits), out.astype(np.int64))


def extract_float(data: np.ndarray, byte_offset: int,
                  byte_order: str = "little") -> np.ndarray:
    """按 IEEE754 取 float32。"""
    if byte_offset < 0 or byte_offset + 4 > PAYLOAD_SIZE:
        raise ValueError("字节范围越界：offset=%d size=4" % byte_offset)
    byte_order = _normalize_byte_order(byte_order)
    raw = np.ascontiguousarray(data[:, byte_offset:byte_offset + 4])
    return np.frombuffer(raw.tobytes(), dtype="<f4" if byte_order == "little" else ">f4")


def extract_bits(data: np.ndarray, byte_offset: int, bit_index: int,
                 bit_length: int = 1, byte_order: str = "little") -> np.ndarray:
    """按位取值。

    little（Intel，默认）：bit_index 为字节内位号（0=LSB），跨字节向高位字节延伸。
    big（Motorola）：bit_index 为字节内位号（0=MSB），跨字节向低位字节延伸。
    """
    if not 0 <= bit_index <= 7:
        raise ValueError("bit_index 必须在 0..7")
    if not 1 <= bit_length <= 64:
        raise ValueError("bit_length 必须在 1..64")
    byte_order = _normalize_byte_order(byte_order)

    source = np.zeros(len(data), dtype=np.uint64)
    current_byte, current_bit = byte_offset, bit_index

    if byte_order == "little":
        shift = 0
        need = bit_length
        while need > 0:
            if current_byte >= PAYLOAD_SIZE:
                break
            take = min(8 - current_bit, need)
            part = (data[:, current_byte].astype(np.uint64) >> current_bit) & ((1 << take) - 1)
            source |= part << shift
            shift += take
            need -= take
            current_byte += 1
            current_bit = 0
        return source

    # Motorola：从 MSB 侧开始，按位序拼装
    need = bit_length
    while need > 0:
        if current_byte >= PAYLOAD_SIZE:
            break
        take = min(8 - current_bit, need)
        chunk = (data[:, current_byte].astype(np.uint64)
                 >> (8 - current_bit - take)) & ((1 << take) - 1)
        source = (source << take) | chunk
        need -= take
        current_byte += 1
        current_bit = 0
    return source


def _normalize_byte_order(byte_order: str) -> str:
    try:
        return BYTE_ORDER_ALIASES[byte_order.lower()]
    except (KeyError, AttributeError):
        raise ValueError("byte_order 只能是 little/big（le/be 亦可）")


# ---------------------------------------------------------------- 内部工具

def _numpy(table: pa.Table, column: str, dtype):
    array = table.column(column)
    if isinstance(array, pa.ChunkedArray):
        array = array.combine_chunks()
    return array.to_numpy(zero_copy_only=False).astype(dtype, copy=False)


def _payload_view(table: pa.Table) -> np.ndarray:
    """把 fixed_size_binary(64) 列还原为 (N, 64) 的 uint8 视图（尽量零拷贝）。"""
    array = table.column("data")
    if isinstance(array, pa.ChunkedArray):
        array = array.combine_chunks()
    if len(array) == 0:
        return np.empty((0, PAYLOAD_SIZE), dtype=np.uint8)
    buffer = array.buffers()[1]
    if buffer is not None:
        try:
            flat = np.frombuffer(buffer, dtype=np.uint8,
                                 count=len(array) * PAYLOAD_SIZE)
            return flat.reshape(len(array), PAYLOAD_SIZE)
        except TypeError:
            pass
    joined = b"".join(array.to_pylist())
    return np.frombuffer(joined, dtype=np.uint8).reshape(len(array), PAYLOAD_SIZE)


def _lookup(catalog: list, kind: str, channel: int, can_id: int):
    for msg in catalog:
        if msg.key == (kind, channel, can_id):
            return msg
    return None


def _info_from_table(table: pa.Table, kind: str, channel: int, can_id: int):
    """从查询结果直接构造 MessageInfo，避免为每次查询构建全量目录。"""
    if table.num_rows == 0:
        return None
    ts = _numpy(table, "ts", np.float64)
    length = _numpy(table, "length", np.uint8)
    flags = _numpy(table, "flags", np.uint8)
    return MessageInfo(
        kind=kind, channel=channel, can_id=can_id,
        extended=bool(flags[0] & FLAG_EXTENDED),
        frames=table.num_rows,
        ts_first=float(ts[0]), ts_last=float(ts[-1]),
        length_min=int(length.min()), length_max=int(length.max()),
    )


def _info_from_series(series: FrameSeries):
    """从已取到的帧序列构造 MessageInfo。"""
    if len(series) == 0:
        return None
    return MessageInfo(
        kind=series.kind, channel=series.channel, can_id=series.can_id,
        extended=series.extended, frames=len(series),
        ts_first=float(series.ts[0]), ts_last=float(series.ts[-1]),
        length_min=int(series.length.min()), length_max=int(series.length.max()),
    )
