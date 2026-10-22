# -*- coding: utf-8 -*-
"""默认通道模板：ASC 通道号 → 显示名称与默认备注。

模板来自用户提供的 21 条通道对照表：
    CAN.No ASC / Channel Name / Remark
其中 18~20 是 LIN 转 CAN，默认备注固定为 "LIN to CAN"。

这里只负责**显示元数据**，不涉及帧类型判断：
帧类型来自 DBC 的 VFrameFormat 和 ASC 中该通道实际承载的帧，
不要把 `config.CHANNEL_MAP`（通道号 → 来源 ECU）混进来。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChannelMeta:
    """一条通道的显示信息。"""

    channel: int
    name: str = ""
    remark: str = ""


# 顺序即 UI 默认顺序：1..21
DEFAULT_CHANNELS = {
    1: ("I CAN", ""),
    2: ("D CAN", ""),
    3: ("B CAN", ""),
    4: ("C CAN", ""),
    5: ("E CAN", ""),
    6: ("IB CAN/AD CAN", ""),
    7: ("TP CAN", ""),
    8: ("HP CAN", ""),
    9: ("LP CAN", ""),
    10: ("P CAN", ""),
    11: ("S CAN1", ""),
    12: ("S CAN2", ""),
    13: ("S CAN3", ""),
    14: ("S CAN4", ""),
    15: ("S CAN5", ""),
    16: ("BP CAN", ""),
    17: ("BL_CAN", ""),
    18: ("LDCU LIN", "LIN to CAN"),
    19: ("RDCU LIN", "LIN to CAN"),
    20: ("CDCU LIN", "LIN to CAN"),
    21: ("NGX_LIN", ""),
}


def default_channels() -> dict:
    """返回一份全新的默认通道元数据（调用方可随意修改）。"""
    return {channel: ChannelMeta(channel, name, remark)
            for channel, (name, remark) in DEFAULT_CHANNELS.items()}


def default_meta(channel: int) -> ChannelMeta | None:
    """单个默认通道；模板外通道返回 None。"""
    entry = DEFAULT_CHANNELS.get(int(channel))
    if entry is None:
        return None
    return ChannelMeta(int(channel), entry[0], entry[1])


def channel_caption(channel: int, meta: ChannelMeta | None = None) -> str:
    """通道短标题：有名称时 "ch5 E CAN"，否则 "ch5"。"""
    name = (meta.name if meta is not None else "").strip()
    return "ch%d %s" % (int(channel), name) if name else "ch%d" % int(channel)
