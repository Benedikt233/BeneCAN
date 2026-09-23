# -*- coding: utf-8 -*-
"""ASC → Parquet 索引（M1）。

设计要点
--------
1. **列式存储 + 排序**：按 (kind, channel, can_id, ts) 升序写入，使"取单个报文/信号的全量序列"
   只命中连续 row group，M2/M3 查询无需全表扫描。
2. **seq 列保留原始行序**：trace/回放视图可按 seq 还原文件顺序（形态 B 文件时间戳不单调，
   排序后时间轴才可用）。
3. **payload 定长 64 字节**：CAN FD 最大 64 字节，不足补 0；补零对 zstd 高度友好。
4. **缓存**：默认写入系统临时目录 `<TEMP>\\BeneCAN\\index\\`（可写、不进 OneDrive 同步），
   可用环境变量 `BENECAN_INDEX_DIR` 覆盖；用「源文件大小 + mtime」判定能否复用。

Parquet schema
--------------
    ts       float64   秒；ASC 原始相对时间戳
    channel  int16     通道号（CAN/CANFD 为 ASC 通道号；LIN 为 L{n} 的 n）
    kind     uint8     1=CANFD  2=经典CAN  3=LIN
    flags    uint8     位标志：bit0=扩展帧（ID 带 x 后缀）
    can_id   uint32    报文 ID
    dlc      uint8     原始 DLC（CANFD 为 hex 值 0..15；经典 CAN/LIN 为长度）
    length   uint8     payload 字节数 0..64
    seq      uint32    原始出现顺序（从 0 开始）
    data     fixed_size_binary(64)   payload

索引格式版本：格式变更时递增，旧索引会被自动判定为不可复用（重建）。
"""

from __future__ import annotations

import array
import hashlib
import json
import logging
import os
import tempfile
import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from config import APP_NAME, INDEX_DIR_ENV, TOOL_VERSION
from core.asc_parser import (KIND_CANFD, KIND_CLASSIC, KIND_LIN, KIND_UNKNOWN,
                             classify)

INDEX_FORMAT = 2          # 1: 初版；2: 增加 flags 列（bit0=扩展帧）
FLAG_EXTENDED = 0x01
META_PREFIX = "benecan."  # Parquet 元数据键前缀（工具名 + 版本等）

KIND_CODES = {KIND_CANFD: 1, KIND_CLASSIC: 2, KIND_LIN: 3}
KIND_NAMES = {code: name for name, code in KIND_CODES.items()}

logger = logging.getLogger(__name__)

PAYLOAD_SIZE = 64
ROW_GROUP_SIZE = 200_000
PROGRESS_LINES = 200_000
MAX_SAMPLES = 8
INDEX_SCHEMA = pa.schema([
    pa.field("ts", pa.float64()),
    pa.field("channel", pa.int16()),
    pa.field("kind", pa.uint8()),
    pa.field("flags", pa.uint8()),
    pa.field("can_id", pa.uint32()),
    pa.field("dlc", pa.uint8()),
    pa.field("length", pa.uint8()),
    pa.field("seq", pa.uint32()),
    pa.field("data", pa.binary(PAYLOAD_SIZE)),
])

_PAD = b"\x00" * PAYLOAD_SIZE


# ---------------------------------------------------------------- 路径与缓存

def cache_dir() -> str:
    override = os.environ.get(INDEX_DIR_ENV)
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), APP_NAME, "index")


def source_fingerprint(asc_path: str) -> str:
    """源文件的规范化路径指纹。

    必须先 normcase + realpath：Windows 路径不区分大小写，
    `C:\\A\\x.asc` 与 `c:\\a\\x.asc` 是同一个文件，不做规范化会各建一份索引。
    """
    normalized = os.path.normcase(os.path.realpath(os.path.abspath(asc_path)))
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def default_index_path(asc_path: str) -> str:
    # stem 也统一小写：否则 "logCAN8.asc" 与 "logCAN8.ASC" 会各建一份索引
    stem = os.path.splitext(os.path.basename(asc_path))[0].lower()
    return os.path.join(cache_dir(), "%s_%s.parquet" % (stem, source_fingerprint(asc_path)))


def read_index_metadata(index_path: str) -> dict:
    """只读 schema 元数据（不读数据，毫秒级）。"""
    meta = pq.read_metadata(index_path).metadata or {}
    out = {}
    for key, value in meta.items():
        k = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        if not k.startswith(META_PREFIX):
            continue
        v = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        out[k[len(META_PREFIX):]] = v
    return out


def index_is_fresh(asc_path: str, index_path: str) -> bool:
    """索引是否可直接复用（源文件大小 + mtime 未变）。"""
    if not os.path.exists(index_path):
        logger.debug("索引不存在，需要新建：%s", index_path)
        return False
    stat = os.stat(asc_path)
    meta = read_index_metadata(index_path)
    if not meta:
        logger.debug("索引缺少元数据，判定不可用：%s", index_path)
        return False
    if meta.get("source_size") != str(stat.st_size):
        logger.debug("索引已过期（源文件大小变化）：%s != %s",
                     meta.get("source_size"), stat.st_size)
        return False
    if meta.get("source_mtime") != "%.6f" % stat.st_mtime:
        logger.debug("索引已过期（源文件 mtime 变化）：%s",
                     meta.get("source_mtime"))
        return False
    if meta.get("index_format") != str(INDEX_FORMAT):
        logger.debug("索引格式不匹配（需 %s，实际 %s），将重建",
                     INDEX_FORMAT, meta.get("index_format"))
        return False
    return True


# ---------------------------------------------------------------- 内存统计

def _peak_rss_mb():
    try:
        import psutil
        return round(psutil.Process().memory_info().peak_wset / 1048576.0, 1)
    except Exception:
        pass
    try:  # Windows 回退：PeakWorkingSetSize
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(counters), counters.cb)
        return round(counters.PeakWorkingSetSize / 1048576.0, 1)
    except Exception:
        return None


# ---------------------------------------------------------------- 核心

def build_index(asc_path: str, index_path: str = None, progress=None,
                force: bool = False) -> dict:
    """把 ASC 解析并写成 Parquet 索引。

    Args:
        asc_path: 源 ASC 文件。
        index_path: 输出路径；None 时用 default_index_path()。
        progress: 回调 progress(done_lines, done_bytes, total_bytes)。
        force: 索引已存在且新鲜时是否强制重建。

    Returns:
        统计字典（含 cached 标记）。
    """
    index_path = index_path or default_index_path(asc_path)
    stat = os.stat(asc_path)
    logger.debug("开始建索引：%s（%.1f MB）→ %s",
                 asc_path, stat.st_size / 1048576.0, index_path)

    if not force and index_is_fresh(asc_path, index_path):
        meta = read_index_metadata(index_path)
        logger.info("命中索引缓存：%s（%s 帧，%.1f MB）", os.path.basename(index_path),
                    meta.get("frames", "?"),
                    os.path.getsize(index_path) / 1048576.0)
        return {
            "path": asc_path,
            "index_path": index_path,
            "cached": True,
            "source_bytes": stat.st_size,
            "index_bytes": os.path.getsize(index_path),
            "lines": int(meta.get("lines", 0)),
            "frames": int(meta.get("frames", 0)),
            "malformed": int(meta.get("malformed", 0)),
            "unknown_lines": 0,
            "kind_counts": json.loads(meta.get("kind_counts", "{}")),
            "ts_first": float(meta.get("ts_first", 0.0)),
            "ts_last": float(meta.get("ts_last", 0.0)),
            "parse_seconds": 0.0,
            "sort_seconds": 0.0,
            "write_seconds": 0.0,
            "build_seconds": float(meta.get("build_seconds", 0.0)),
            "peak_rss_mb": None,
        }

    started = time.time()
    ts_arr = array.array("d")
    ch_arr = array.array("h")
    kind_arr = array.array("B")
    flags_arr = array.array("B")
    id_arr = array.array("I")
    dlc_arr = array.array("B")
    len_arr = array.array("B")
    data_buf = bytearray()

    lines = frames = malformed = truncated = repaired_hex = 0
    unknown_lines = 0
    unknown_samples = []
    kind_counts = {}
    done_bytes = 0
    total_bytes = stat.st_size

    with open(asc_path, "r", encoding="utf-8-sig", errors="ignore") as fh:
        for line in fh:
            lines += 1
            done_bytes += len(line)
            if lines % PROGRESS_LINES == 0:
                logger.debug("解析进度：%s 行 / %.1f MB / 已收 %d 帧",
                             lines, done_bytes / 1048576.0, frames)
                if progress:
                    progress(lines, done_bytes, total_bytes)

            info = classify(line, keep_data=True)
            kind = info.kind
            if kind not in KIND_CODES:
                if kind == KIND_UNKNOWN:
                    unknown_lines += 1
                    if len(unknown_samples) < MAX_SAMPLES:
                        unknown_samples.append(info.text)
                continue
            if not info.len_ok:
                malformed += 1
                continue
            try:
                can_id = int(info.can_id, 16)
            except ValueError:
                malformed += 1
                continue

            raw = b""
            if info.data:
                try:
                    raw = bytes.fromhex("".join(info.data))
                except ValueError:          # 兼容单字符 hex（如 "5"）
                    try:
                        raw = bytes(int(tok, 16) for tok in info.data)
                        repaired_hex += 1
                    except ValueError:
                        malformed += 1
                        continue
            if len(raw) > PAYLOAD_SIZE:
                raw = raw[:PAYLOAD_SIZE]
                truncated += 1

            ts_arr.append(info.ts)
            ch_arr.append(_channel_int(info.channel))
            kind_arr.append(KIND_CODES[kind])
            flags_arr.append(FLAG_EXTENDED if info.extended else 0)
            id_arr.append(can_id)
            dlc_arr.append(_dlc_int(info.dlc, len(raw)))
            len_arr.append(len(raw))
            data_buf.extend(raw)
            data_buf.extend(_PAD[:PAYLOAD_SIZE - len(raw)])
            frames += 1
            kind_counts[kind] = kind_counts.get(kind, 0) + 1

    if frames == 0:
        raise ValueError("未解析到任何帧行：%s" % asc_path)

    parse_seconds = time.time() - started
    ts_np = np.frombuffer(ts_arr, dtype=np.float64)
    ts_first, ts_last = float(ts_np.min()), float(ts_np.max())

    arrays = _build_sorted_arrays(ts_arr, ch_arr, kind_arr, flags_arr, id_arr,
                                  dlc_arr, len_arr, data_buf, frames)
    sort_seconds = time.time() - started - parse_seconds
    table = pa.Table.from_arrays(arrays, schema=INDEX_SCHEMA)

    build_seconds = time.time() - started
    meta = {
        META_PREFIX + "tool": "asc_index",
        META_PREFIX + "version": TOOL_VERSION,
        META_PREFIX + "index_format": str(INDEX_FORMAT),
        META_PREFIX + "source_path": os.path.abspath(asc_path),
        META_PREFIX + "source_size": str(stat.st_size),
        META_PREFIX + "source_mtime": "%.6f" % stat.st_mtime,
        META_PREFIX + "lines": str(lines),
        META_PREFIX + "frames": str(frames),
        META_PREFIX + "malformed": str(malformed),
        META_PREFIX + "kind_counts": json.dumps(kind_counts, ensure_ascii=False),
        META_PREFIX + "ts_first": "%.6f" % ts_first,
        META_PREFIX + "ts_last": "%.6f" % ts_last,
        META_PREFIX + "build_seconds": "%.2f" % build_seconds,
        META_PREFIX + "sort": "kind,channel,can_id,ts",
    }
    table = table.replace_schema_metadata(meta)

    os.makedirs(os.path.dirname(os.path.abspath(index_path)), exist_ok=True)
    pq.write_table(table, index_path, compression="zstd",
                   row_group_size=ROW_GROUP_SIZE)
    write_seconds = time.time() - started - parse_seconds - sort_seconds

    stats = {
        "path": asc_path,
        "index_path": index_path,
        "cached": False,
        "source_bytes": stat.st_size,
        "index_bytes": os.path.getsize(index_path),
        "lines": lines,
        "frames": frames,
        "malformed": malformed,
        "truncated": truncated,
        "repaired_hex": repaired_hex,
        "unknown_lines": unknown_lines,
        "unknown_samples": unknown_samples,
        "kind_counts": kind_counts,
        "ts_first": ts_first,
        "ts_last": ts_last,
        "parse_seconds": round(parse_seconds, 1),
        "sort_seconds": round(sort_seconds, 1),
        "write_seconds": round(write_seconds, 1),
        "build_seconds": round(time.time() - started, 1),
        "peak_rss_mb": _peak_rss_mb(),
    }
    logger.debug("索引完成：%s 行 / %s 帧（畸形 %s、截断 %s、十六进制修复 %s、未识别 %s）",
                 lines, frames, malformed, truncated, repaired_hex, unknown_lines)
    logger.debug("分通道帧数：%s", kind_counts)
    logger.debug("耗时：解析 %.1f s · 排序 %.1f s · 写盘 %.1f s · 合计 %.1f s | 输出 %.1f MB"
                 "（原始 %.1f MB，压缩比 %.1f×）| 峰值内存 %s MB",
                 stats["parse_seconds"], stats["sort_seconds"],
                 stats["write_seconds"], stats["build_seconds"],
                 stats["index_bytes"] / 1048576.0, stat.st_size / 1048576.0,
                 (stat.st_size / max(stats["index_bytes"], 1)),
                 stats["peak_rss_mb"])
    if malformed or unknown_lines:
        logger.warning("索引含无法解析的行：畸形 %d、未识别 %d，样本 %s",
                       malformed, unknown_lines, unknown_samples[:3])
    return stats


def _build_sorted_arrays(ts_arr, ch_arr, kind_arr, flags_arr, id_arr, dlc_arr,
                         len_arr, data_buf, frames):
    """array.array → numpy → 按 (kind, channel, can_id, ts) 排序 → arrow 数组。

    排序目的：让"单个 (kind, channel, can_id) 的全量序列"落在连续 row group，
    查询时可按 row group 统计信息裁剪，避免全表扫描。
    """
    ts = np.frombuffer(ts_arr, dtype=np.float64)
    channel = np.frombuffer(ch_arr, dtype=np.int16)
    kind = np.frombuffer(kind_arr, dtype=np.uint8)
    flags = np.frombuffer(flags_arr, dtype=np.uint8)
    can_id = np.frombuffer(id_arr, dtype=np.uint32)
    dlc = np.frombuffer(dlc_arr, dtype=np.uint8)
    length = np.frombuffer(len_arr, dtype=np.uint8)
    seq = np.arange(frames, dtype=np.uint32)

    order = np.lexsort((ts, can_id, channel, kind))
    payload_dtype = np.dtype("S%d" % PAYLOAD_SIZE)
    payload = np.frombuffer(data_buf, dtype=payload_dtype)[order]

    return (
        pa.array(ts[order]),
        pa.array(channel[order]),
        pa.array(kind[order]),
        pa.array(flags[order]),
        pa.array(can_id[order]),
        pa.array(dlc[order]),
        pa.array(length[order]),
        pa.array(seq[order]),
        pa.FixedSizeBinaryArray.from_buffers(
            pa.binary(PAYLOAD_SIZE), frames, [None, pa.py_buffer(payload)]),
    )


def _channel_int(channel: str) -> int:
    """'19' -> 19；'L3' -> 3（LIN 用 kind 与 CAN 通道区分）。"""
    text = channel[1:] if channel[:1] in ("L", "l") else channel
    try:
        value = int(text)
    except ValueError:
        return -1
    return value if -32768 <= value <= 32767 else -1


def _dlc_int(dlc: str, length: int) -> int:
    try:
        return int(dlc, 16) if dlc else length
    except ValueError:
        return length
