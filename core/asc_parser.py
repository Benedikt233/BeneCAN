# -*- coding: utf-8 -*-
"""ASC 行级语法解析（纯函数、无 I/O、无第三方依赖）。

本模块是 ASC 语法的唯一真源：tools/asc_inspect.py（M0 体检）与
core/indexer.py（M1 索引）都基于它做行分类，避免语法逻辑重复实现。

实测行类型（4 份样本，2026-09-23）：

    头部      date ... / base hex  timestamps absolute / no internal events logged
              // version 11.0.0 / Begin TriggerBlock ...
    CANFD     {t}   CANFD  {ch} RX    {id}   1 1 {DLC_hex} {len_dec} {data…}  {8 列常量}
    经典 CAN  {t} {ch}   {id}   Rx   d {len} {data…}
    LIN       {t}   L{n}  {id}       Rx    {len} {data…}  checksum = ...
    SOA       {t}    SV: {node} {a} {b} ::SOA::{SignalName} = [{hex…}]
    可跳过    [ERROR] / bus statistics 等
"""

from __future__ import annotations

from config import DLC_TO_LEN

KIND_HEADER = "header"
KIND_CANFD = "canfd"
KIND_CLASSIC = "classic"
KIND_LIN = "lin"
KIND_SOA = "soa"
KIND_SKIP = "skip"
KIND_UNKNOWN = "unknown"

KIND_LABELS = {
    KIND_HEADER: "头部/注释",
    KIND_CANFD: "CANFD 帧",
    KIND_CLASSIC: "经典 CAN 帧",
    KIND_LIN: "LIN 帧",
    KIND_SOA: "SOA 信号行",
    KIND_SKIP: "可跳过行",
    KIND_UNKNOWN: "未识别行",
}

HEADER_PREFIXES = (
    "date ",
    "base ",
    "no internal events",
    "//",
    "Begin TriggerBlock",
    "End TriggerBlock",
    "Start of measurement",
    "End of measurement",
    "Bus statistics",
)

DIRECTIONS = ("Rx", "RX", "rx", "Tx", "TX", "tx")
RX_DIRECTIONS = ("Rx", "RX", "rx")


def _as_int(token: str) -> int:
    """十进制优先、退化为十六进制（上游 DLC 字段时十六进制、长度字段时十进制）。"""
    try:
        return int(token)
    except ValueError:
        return int(token, 16)


class LineInfo:
    """单行解析结果。"""

    __slots__ = (
        "kind", "ts", "channel", "direction", "can_id", "dlc", "length",
        "data", "name", "trailing", "len_ok", "dlc_ok", "extended", "text",
    )

    def __init__(self) -> None:
        self.kind = KIND_UNKNOWN
        self.ts = 0.0
        self.channel = ""
        self.direction = ""
        self.can_id = ""
        self.dlc = ""
        self.length = -1
        self.data = ()
        self.name = ""
        self.trailing = ()
        self.len_ok = True
        self.dlc_ok = True
        self.extended = False   # 扩展帧（ID 带 x 后缀）
        self.text = ""      # 仅未识别行保留原文（截断）


def _split_id(info: LineInfo, token: str) -> None:
    """解析报文 ID：Vector ASC 用 `123x` 表示扩展帧。"""
    if token[-1:] in ("x", "X"):
        info.can_id = token[:-1]
        info.extended = True
    else:
        info.can_id = token


def classify(line: str, keep_data: bool = False) -> LineInfo:
    """把一行 ASC 文本分类并解析出关键字段。

    Args:
        line: 原始行（可含换行/首尾空格）。
        keep_data: 是否保留数据字节元组（体检统计不需要，可省内存）。

    Returns:
        LineInfo；无法识别时 kind == KIND_UNKNOWN。
    """
    info = LineInfo()
    s = line.strip()
    if s[:1] == "\ufeff":          # 兼容带 BOM 的 ASC（其他工具导出常见）
        s = s.lstrip("\ufeff").strip()
    if not s:
        info.kind = KIND_SKIP
        return info
    if s[0] == "/" or s.startswith(HEADER_PREFIXES):
        info.kind = KIND_HEADER
        return info
    if s[0] == "[":
        info.kind = KIND_SKIP
        return info

    parts = s.split()
    try:
        info.ts = float(parts[0])
    except (ValueError, IndexError):
        info.kind = KIND_UNKNOWN
        info.text = s[:150]
        return info

    if len(parts) >= 3 and parts[1] == "SV:":
        info.kind = KIND_SOA
        _, sep, rest = s.partition("::SOA::")
        if sep:
            info.name = rest.split("=", 1)[0].strip()
        return info

    if len(parts) >= 5 and parts[1] == "CANFD" and parts[3] in DIRECTIONS:
        info.kind = KIND_CANFD
        info.channel = parts[2]
        info.direction = parts[3]
        _split_id(info, parts[4])
        if len(parts) >= 9:
            info.dlc = parts[7]
            info.length = _as_int(parts[8])
            rest_tokens = parts[9:]
            info.len_ok = len(rest_tokens) >= info.length
            if info.len_ok:
                info.data = tuple(rest_tokens[:info.length]) if keep_data else ()
                info.trailing = tuple(rest_tokens[info.length:])
            else:
                info.data = tuple(rest_tokens) if keep_data else ()
            try:
                info.dlc_ok = DLC_TO_LEN.get(_as_int(info.dlc), _as_int(info.dlc)) == info.length
            except ValueError:
                info.dlc_ok = False
        else:
            info.len_ok = False
        return info

    if len(parts) >= 4 and parts[1][:1] == "L" and parts[2] not in DIRECTIONS and parts[3] in DIRECTIONS:
        # LIN：{t} L{n} {id} Rx {len} {data…} checksum = ...
        info.kind = KIND_LIN
        info.channel = parts[1]
        info.direction = parts[3]
        _split_id(info, parts[2])
        _fill_classic_like(info, parts, 4, keep_data)
        return info

    if len(parts) >= 6 and parts[3] in DIRECTIONS and parts[4] in ("d", "D"):
        # 经典 CAN：{t} {ch} {id} Rx d {len} {data…}
        info.kind = KIND_CLASSIC
        info.channel = parts[1]
        info.direction = parts[3]
        _split_id(info, parts[2])
        _fill_classic_like(info, parts, 5, keep_data)
        return info

    info.kind = KIND_UNKNOWN
    info.text = s[:150]
    return info


def _fill_classic_like(info: LineInfo, parts: list, len_index: int, keep_data: bool) -> None:
    """经典 CAN / LIN 的长度字段 + 数据字段（后可跟 checksum 等属性列）。"""
    try:
        info.length = _as_int(parts[len_index])
    except (ValueError, IndexError):
        info.length = -1
        info.len_ok = False
        return
    rest_tokens = parts[len_index + 1:]
    info.len_ok = len(rest_tokens) >= info.length
    if info.len_ok:
        info.data = tuple(rest_tokens[:info.length]) if keep_data else ()
        info.trailing = tuple(rest_tokens[info.length:])
    else:
        info.data = tuple(rest_tokens) if keep_data else ()
