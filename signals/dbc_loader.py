# -*- coding: utf-8 -*-
"""DBC 加载与映射（可选插件：不加载 DBC 时所有功能照常可用）。

它解决两件事
------------
1. **信号定义**：从 DBC 取报文与信号（名称/单位/缩放/偏移/枚举/取值范围），
   转成与"原始字节行"共用的 `SignalSpec`，于是图形/测量/导出整套管线无需改动。
2. **通道映射（channel mapping）**：一个 DBC 通常只描述某条总线，
   而我们的 ASC 有多个通道（CANFD ch1/4/5/9/17/18/19/20 + 经典 ch3），
   所以需要"哪个 DBC 作用于哪个 (帧类型, 通道)"的配置，且支持多个 DBC 并存。

位序映射（已用 cantools 自身编码做端到端对照，10/10 一致）
--------------------------------------------------------
    network = cantools.database.utils.start_bit(signal)
        Intel(小端)   : network = dbc_start
        Motorola(大端) : network = 8*(dbc_start//8) + (7 - dbc_start%8)
    字段占据 network .. network+length-1（递增），首比特为 MSB。
    于是 → byte_offset = network // 8，bit_index = network % 8（自该字节 MSB 数），
    byte_order 与 DBC 一致；当 network%8==0 且 length%8==0 时走字节快路径。

文件与配置约定
--------------
    dbc/                  随工具分发的 DBC 库（把 CH5.dbc 等放这里）
    dbc/channel_map.json  通道映射配置（可由 UI 维护，也可手改）
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Iterable

from signals.channel_catalog import (ChannelMeta, channel_caption,
                                     default_channels, default_meta)
from signals.model import SRC_DBC, SignalSpec

logger = logging.getLogger(__name__)

CONFIG_NAME = "channel_map.json"
CONFIG_VERSION = 2
CONFIG_VERSION_MIN = 1
DBC_SUFFIXES = (".dbc", ".DBC")
KINDS = ("canfd", "classic", "lin")
KIND_TEXTS = {"canfd": "CANFD 帧", "classic": "经典 CAN 帧", "lin": "LIN 帧"}

# Vector 约定：BA_ "VFrameFormat" 的取值（0 起）→ 我们的帧类型
FRAME_FORMAT_KINDS = {
    0: ("classic", "StandardCAN"),
    1: ("classic", "ExtendedCAN"),
    3: ("classic", "J1939PG"),
    14: ("canfd", "CANFDStandard"),
    15: ("canfd", "CANFDExtended"),
}


def _frame_format(message) -> tuple:
    """读 DBC 里的 VFrameFormat 属性 → (帧类型, 显示文本)。

    DBC 本身带着帧类型信息（Vector 的 VFrameFormat / FrameFormat），
    所以不需要让用户手工选 CAN / CANFD。属性值可能是数字（14）
    也可能是文本（"CANFDStandard"），两种都要认。
    """
    attributes = getattr(getattr(message, "dbc", None), "attributes", None) or {}
    for name in ("VFrameFormat", "FrameFormat"):
        entry = attributes.get(name)
        if entry is None:
            continue
        raw = getattr(entry, "value", entry)
        text = raw if isinstance(raw, str) else ""
        upper = text.upper()
        if "CANFD" in upper:
            return "canfd", text
        if "CAN" in upper:
            return "classic", text
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return "", text
        kind, label = FRAME_FORMAT_KINDS.get(value, ("", ""))
        return kind, label or text or str(value)
    return "", ""


# DBC 规范是单字节编码，cantools 默认按 latin-1 读；中文/日文 DBC 会变乱码，
# 所以这里自行探测编码再交给 cantools 解析（见 detect_dbc_encoding）。
DBC_ENCODINGS = ("utf-8-sig", "gbk", "latin-1")


def detect_dbc_encoding(path: str) -> str:
    """探测 DBC 文本编码。

    不能只用 try/except：不少 GBK 字节序列恰好是合法 UTF-8
    （例如 "次" 的 GBK 是 B4 CE，能被 UTF-8 解成 U+04CE 而不报错）。
    规则：
        1) 纯 ASCII（绝大多数 DBC）→ utf-8
        2) utf-8 / gbk 都能解时，取"解出的中日韩字符更多"的那个（平手取 utf-8）
        3) 都解不出中日韩字符 → latin-1（德/法注释里的 ä、°、µ 等）
    """
    with open(path, "rb") as handle:
        raw = handle.read()

    if not raw or max(raw) < 0x80:
        return "utf-8-sig"

    counts = {}
    for encoding in ("utf-8-sig", "gbk"):
        try:
            counts[encoding] = _cjk_count(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    if not counts:
        return "latin-1"
    best_encoding, best_count = max(counts.items(),
                                    key=lambda item: (item[1], item[0] == "utf-8-sig"))
    return best_encoding if best_count > 0 else "latin-1"


def _cjk_count(text: str, limit: int = 400_000) -> int:
    """统计中日韩字符数量（用于判断编码是否解对了）。"""
    return sum(1 for char in text[:limit]
               if 0x4E00 <= ord(char) <= 0x9FFF or 0x3040 <= ord(char) <= 0x30FF)


def _choice_text(value) -> str:
    """把 VAL_ 枚举文本统一成纯字符串。

    cantools 39+ 把枚举值包成 `NamedSignalValue`（带 .name/.value/.comments），
    它不是 str 子类：直接传给 Qt（如 setPlainText）会抛类型错误。
    这里一次性归一化，下游（树/值表/读值/CSV）就都是纯字符串了。
    """
    return str(getattr(value, "name", value))


def _require_cantools():
    try:
        import cantools
    except ImportError as exc:                     # pragma: no cover - 依赖缺失分支
        raise RuntimeError("未安装 cantools，无法解析 DBC（pip install cantools）") from exc
    return cantools


# ---------------------------------------------------------------- 数据模型

@dataclass
class DbcSignalRef:
    """DBC 里的一条信号（已换算成我们可直接取数的形式）。"""
    dbc_name: str                 # DBC 文件名（用于显示来源）
    message_name: str
    can_id: int
    extended: bool
    message_length: int
    name: str
    byte_offset: int
    bit_index: int                # -1 表示按字节取（byte_offset + size）
    length: int                   # 位长度
    byte_order: str               # little / big
    is_signed: bool
    is_float: bool = False
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    choices: dict = field(default_factory=dict)
    minimum: float = None
    maximum: float = None
    comment: str = ""

    @property
    def size(self) -> int:
        return max(1, self.length // 8) if self.is_byte_aligned else 1

    @property
    def is_byte_aligned(self) -> bool:
        """整字节对齐（可走字节快路径；大端此时按字节序整取）。"""
        return self.bit_index == 0 and self.length % 8 == 0

    @property
    def field_text(self) -> str:
        if self.is_float:
            return "byte%d float32" % self.byte_offset
        if self.is_byte_aligned:
            return "byte%d-%d %s" % (self.byte_offset,
                                     self.byte_offset + self.size - 1,
                                     ("int" if self.is_signed else "uint") + str(self.length))
        return "byte%d.bit%d-%d" % (self.byte_offset, self.bit_index,
                                    self.bit_index + self.length - 1)

    def to_spec(self, key: tuple) -> SignalSpec:
        """转成图形行规格（key = (kind, channel, can_id)）。"""
        if self.is_float:
            size, dtype, bit_index, bit_length = 4, "float32", -1, 0
        elif self.is_byte_aligned:
            size = self.size
            dtype = ("int" if self.is_signed else "uint") + str(self.length)
            bit_index, bit_length = -1, 0
        else:
            size, dtype = 1, "uint8"
            bit_index, bit_length = self.bit_index, self.length
        return SignalSpec(
            key=key, byte_offset=self.byte_offset, size=size, dtype=dtype,
            byte_order=self.byte_order, bit_index=bit_index, bit_length=bit_length,
            scale=self.scale, offset=self.offset, labels=dict(self.choices),
            name="%s.%s" % (self.message_name, self.name), unit=self.unit,
            source=SRC_DBC, step=bool(self.choices),
        )


@dataclass
class DbcMessageRef:
    """DBC 里的一个报文。"""
    dbc_name: str
    name: str
    can_id: int
    extended: bool
    length: int
    comment: str
    signals: list = field(default_factory=list)
    frame_kind: str = ""            # 来自 VFrameFormat：canfd / classic / ""（未知）
    frame_label: str = ""           # 原始文本，如 CANFDStandard


# ---------------------------------------------------------------- 单个 DBC

class DbcFile:
    """一个已解析的 DBC 文件。"""

    def __init__(self, path: str):
        cantools = _require_cantools()
        self.path = os.path.abspath(path)
        self.name = os.path.basename(path)
        self.encoding = detect_dbc_encoding(self.path)
        self.database = cantools.database.load_file(self.path, encoding=self.encoding)
        self.messages = {}
        self._load()
        logger.debug("DBC 解析：%s（%.1f KB，编码 %s）→ %s", self.name,
                     os.path.getsize(self.path) / 1024.0, self.encoding, self.describe())

    def _load(self) -> None:
        from cantools.database.utils import start_bit

        for message in self.database.messages:
            frame_kind, frame_label = _frame_format(message)
            signals = []
            for sig in message.signals:
                network = start_bit(sig)
                bit_index = network % 8
                if network % 8 == 0 and sig.length % 8 == 0:
                    bit_index = 0                     # 字节快路径
                signals.append(DbcSignalRef(
                    dbc_name=self.name,
                    message_name=message.name,
                    can_id=message.frame_id,
                    extended=bool(message.is_extended_frame),
                    message_length=message.length,
                    name=sig.name,
                    byte_offset=network // 8,
                    bit_index=bit_index,
                    length=sig.length,
                    byte_order=("little" if sig.byte_order == "little_endian" else "big"),
                    is_signed=bool(sig.is_signed),
                    # 只支持对齐的 32 位浮点（float32）；其余按整数处理并留痕
                    is_float=bool(getattr(sig, "is_float", False)) and sig.length == 32,
                    scale=float(sig.scale),
                    offset=float(sig.offset),
                    unit=sig.unit or "",
                    choices={int(k): _choice_text(v)
                             for k, v in (sig.choices or {}).items()},
                    minimum=sig.minimum,
                    maximum=sig.maximum,
                    comment=sig.comment or "",
                ))
            self.messages[message.frame_id] = DbcMessageRef(
                dbc_name=self.name, name=message.name, can_id=message.frame_id,
                extended=bool(message.is_extended_frame), length=message.length,
                comment=message.comment or "", signals=signals,
                frame_kind=frame_kind, frame_label=frame_label)

    def messages_for(self, can_id: int):
        ref = self.messages.get(can_id)
        return [ref] if ref else []

    @property
    def signal_count(self) -> int:
        return sum(len(m.signals) for m in self.messages.values())

    @property
    def frame_formats(self) -> dict:
        """DBC 自带的帧格式分布，如 {"CANFDStandard": 172}。"""
        summary = {}
        for message in self.messages.values():
            if message.frame_label:
                summary[message.frame_label] = summary.get(message.frame_label, 0) + 1
        return summary

    @property
    def frame_kinds(self) -> set:
        return {message.frame_kind for message in self.messages.values()
                if message.frame_kind}

    @property
    def auto_kind(self) -> str:
        """由 DBC 自身判定的帧类型（供通道映射自动使用）。

        全部报文都是同一种 → 返回该类型；混用或没写 VFrameFormat → ""（不限制），
        此时按"任何帧类型都匹配"处理，避免 DBC 不生效。
        """
        kinds = self.frame_kinds
        return next(iter(kinds)) if len(kinds) == 1 else ""

    def describe(self) -> str:
        """一行摘要，供 UI 显示（让用户看到 DBC 自带的帧格式与规模）。"""
        formats = self.frame_formats
        parts = ["%d 报文 / %d 信号" % (len(self.messages), self.signal_count)]
        if formats:
            parts.append("帧格式 " + "、".join(
                "%s×%d" % (label, count) for label, count in sorted(formats.items())))
        parts.append("编码 %s" % self.encoding)
        return " · ".join(parts)


# ---------------------------------------------------------------- 映射配置

@dataclass
class ChannelMapping:
    """一条映射：某个 DBC 作用于某些通道。

    kind 一般留空 —— 帧类型由"日志里该通道实际承载的帧类型"与 DBC 的
    VFrameFormat 自动判定，不需要用户选择；仅在需要强制指定（例如同名通道
    在不同日志里含义不同）时才填写。
    """
    dbc: str                       # 相对 dbc/ 目录或绝对路径
    channels: list = field(default_factory=list)
    kind: str = ""                 # "" = 自动（任意帧类型）
    comment: str = ""

    def matches(self, kind: str, channel: int) -> bool:
        return channel in self.channels and (not self.kind or self.kind == kind)

    def to_dict(self) -> dict:
        payload = {"dbc": self.dbc, "channels": list(self.channels)}
        if self.kind:
            payload["kind"] = self.kind
        if self.comment:
            payload["comment"] = self.comment
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelMapping":
        kind = data.get("kind", "")
        return cls(dbc=data["dbc"], channels=[int(c) for c in data.get("channels", [])],
                   kind=kind if kind in KINDS else "",
                   comment=data.get("comment", ""))


class DbcLibrary:
    """多 DBC + 通道映射：供 UI 使用（加载/保存/查询）。"""

    def __init__(self, dbc_dir: str):
        self.dbc_dir = os.path.abspath(dbc_dir)
        self.mappings = []
        self.channels = default_channels()   # 通道号 -> ChannelMeta（名称/备注）
        self._files = {}                # 绝对路径 -> DbcFile
        self._errors = {}               # 绝对路径 -> 错误信息

    # -------------------------------------------------- 配置读写
    @property
    def config_path(self) -> str:
        return os.path.join(self.dbc_dir, CONFIG_NAME)

    def load_config(self) -> None:
        """读取 v1/v2 配置；v1 没有 channels 段时使用默认模板。"""
        self.channels = default_channels()
        if not os.path.exists(self.config_path):
            self.mappings = []
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.error("读取通道映射配置失败，按空配置启动：%s -> %s",
                         self.config_path, exc)
            self.mappings = []
            return
        if not isinstance(payload, dict):
            logger.error("通道映射配置不是 JSON 对象，按空配置启动：%s",
                         self.config_path)
            self.mappings = []
            return
        version = payload.get("version", CONFIG_VERSION_MIN)
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = CONFIG_VERSION_MIN
        if version > CONFIG_VERSION:
            logger.warning("通道映射配置版本 %s 高于当前支持的 %s，按兼容方式读取：%s",
                           version, CONFIG_VERSION, self.config_path)
        self._load_channels(payload.get("channels") or {})
        self.mappings = [ChannelMapping.from_dict(item)
                         for item in payload.get("mappings", [])]
        for mapping in self.mappings:
            self.ensure_channels(mapping.channels)
        logger.debug("读取通道映射配置：%s（v%s，%d 通道 / %d 条映射）",
                     self.config_path, version, len(self.channels), len(self.mappings))

    def _load_channels(self, data: dict) -> None:
        """合并配置里的通道名称/备注；非法条目跳过并留日志。"""
        if not isinstance(data, dict):
            logger.warning("channels 段不是对象，忽略：%r", type(data).__name__)
            return
        for key, value in data.items():
            try:
                channel = int(key)
            except (TypeError, ValueError):
                logger.warning("忽略非法通道号：%r", key)
                continue
            meta = self.channels.get(channel) or ChannelMeta(channel)
            if isinstance(value, str):
                name, remark = value, meta.remark
            elif isinstance(value, dict):
                name = value.get("name", meta.name)
                remark = value.get("remark", meta.remark)
            else:
                logger.warning("忽略通道 %s 的非法配置：%r", channel, value)
                continue
            meta.name = str(name).strip()
            meta.remark = str(remark).strip()
            self.channels[channel] = meta

    def save_config(self) -> str:
        """原子写入配置，并在覆盖前留一份 .bak。

        只写入相对默认模板发生过变化的通道（含模板外通道），
        这样以后调整默认模板时，已有配置不会被旧默认值“钉死”。
        """
        os.makedirs(self.dbc_dir, exist_ok=True)
        payload = {"version": CONFIG_VERSION,
                   "channels": self._channel_payload(),
                   "mappings": [m.to_dict() for m in self.mappings]}
        directory = self.dbc_dir
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".tmp", prefix=".channel_map.",
            dir=directory, delete=False)
        temp_path = handle.name
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if os.path.exists(self.config_path):
                try:
                    shutil.copy2(self.config_path, self.config_path + ".bak")
                except OSError as exc:
                    logger.warning("备份通道映射配置失败（继续写入）：%s", exc)
            os.replace(temp_path, self.config_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        logger.debug("写入通道映射配置：%s（%d 通道 / %d 条映射）",
                     self.config_path, len(self.channels), len(self.mappings))
        return self.config_path

    def _channel_payload(self) -> dict:
        payload = {}
        for channel, meta in sorted(self.channels.items()):
            default = default_meta(channel)
            if default is not None and meta.name == default.name \
                    and meta.remark == default.remark:
                continue
            payload[str(channel)] = {"name": meta.name, "remark": meta.remark}
        return payload

    # -------------------------------------------------- 通道名称/备注
    def ensure_channels(self, channels: Iterable[int]) -> None:
        """把 ASC 发现的新通道追加进列表（名称留空，备注可编辑）。"""
        for channel in channels or []:
            try:
                channel = int(channel)
            except (TypeError, ValueError):
                continue
            self.channels.setdefault(channel, ChannelMeta(channel))

    def channel_meta(self, channel: int) -> ChannelMeta:
        channel = int(channel)
        return self.channels.get(channel) or ChannelMeta(channel)

    def channel_caption(self, channel: int) -> str:
        meta = self.channel_meta(channel)
        return channel_caption(channel, meta)

    def set_channel_meta(self, channel: int, name: str = None,
                         remark: str = None) -> ChannelMeta:
        channel = int(channel)
        meta = self.channels.get(channel) or ChannelMeta(channel)
        if name is not None:
            meta.name = str(name).strip()
        if remark is not None:
            meta.remark = str(remark).strip()
        self.channels[channel] = meta
        return meta

    def reset_channel_template(self) -> None:
        """恢复 1~21 的默认名称/备注；映射和模板外通道保持不变。"""
        for channel, default in default_channels().items():
            self.channels[channel] = default
        logger.debug("恢复默认通道模板：%d 条", len(default_channels()))

    # -------------------------------------------------- 映射维护
    def resolve_path(self, dbc: str) -> str:
        if os.path.isabs(dbc):
            return dbc
        return os.path.abspath(os.path.join(self.dbc_dir, dbc))

    def _as_config_path(self, dbc: str) -> str:
        """配置里尽量存相对路径（相对 dbc/），便于整个目录搬迁；不在目录内才存绝对路径。"""
        absolute = self.resolve_path(dbc)          # 相对路径按 dbc/ 目录解释
        try:
            relative = os.path.relpath(absolute, self.dbc_dir)
        except ValueError:                        # 跨盘符
            return absolute
        if relative.startswith(".."):
            return absolute
        return relative.replace("\\", "/")

    def add_mapping(self, dbc: str, channels, kind: str = "",
                    comment: str = "") -> ChannelMapping:
        """新增映射（同 DBC + 帧类型的已存在条目则合并通道）。

        kind 留空 = 自动：用 DBC 自带的 VFrameFormat 判定帧类型
        （该 DBC 已加载过时可用；拿不到就按"任意帧类型"处理）。
        """
        channels = sorted({int(c) for c in channels})
        self.ensure_channels(channels)
        dbc = self._as_config_path(dbc)
        if not kind:
            file = self._files.get(self.resolve_path(dbc))
            kind = file.auto_kind if file is not None else ""
        for existing in self.mappings:
            if existing.dbc == dbc and existing.kind == kind:
                existing.channels = sorted(set(existing.channels) | set(channels))
                if comment:
                    existing.comment = comment
                return existing
        mapping = ChannelMapping(dbc=dbc, kind=kind, channels=channels, comment=comment)
        self.mappings.append(mapping)
        logger.debug("新增通道映射：%s → 通道 %s（帧类型 %s）", dbc, channels,
                     kind or "自动")
        return mapping

    def remove_mapping(self, index: int) -> None:
        if 0 <= index < len(self.mappings):
            self.mappings.pop(index)

    def mappings_for_channel(self, channel: int) -> list:
        """该通道上的映射条目（按配置顺序，去重同一 DBC）。"""
        channel = int(channel)
        out, seen = [], set()
        for mapping in self.mappings:
            if channel not in mapping.channels or mapping.dbc in seen:
                continue
            seen.add(mapping.dbc)
            out.append(mapping)
        return out

    def remove_mapping_for_channel(self, dbc: str, channel: int) -> bool:
        """只移除某个 DBC 在指定通道上的映射，其他通道保持不变。"""
        target = self._as_config_path(dbc)
        channel = int(channel)
        changed = False
        for mapping in list(self.mappings):
            if mapping.dbc != target or channel not in mapping.channels:
                continue
            mapping.channels = [c for c in mapping.channels if c != channel]
            changed = True
            if not mapping.channels:
                self.mappings.remove(mapping)
        if changed:
            logger.debug("移除通道映射：%s ↛ ch%d", target, channel)
        return changed

    def channels_for(self, dbc: str) -> list:
        """该 DBC 已映射的通道（供 UI 预勾选）。"""
        target = self._as_config_path(dbc)
        channels = set()
        for mapping in self.mappings:
            if mapping.dbc == target:
                channels |= set(mapping.channels)
        return sorted(channels)

    def apply_editor_state(self, channel_dbc: dict, names: dict = None,
                           remarks: dict = None) -> dict:
        """用 UI 的待提交状态整体替换映射，并更新通道名称/备注。

        `channel_dbc` 形如 {5: ["E CAN.dbc"], 9: []}；值可以写绝对路径，
        写入配置时统一转成相对 dbc/ 的路径。每个通道内的 DBC 顺序即
        chip/拖入顺序，也决定同 ID 冲突时“第一个为准”的优先级。

        解析失败的 DBC 会被跳过并记进返回值 errors，不会写入映射。
        调用方负责随后 save_config()。
        """
        new_mappings = []
        errors = {}
        self._errors = {}
        names = names or {}
        remarks = remarks or {}
        # v1 的 mappings[].comment 是历史字段：UI 不再编辑，但重建映射时不能丢。
        legacy_comments = {}
        for mapping in self.mappings:
            if mapping.comment:
                legacy_comments.setdefault(
                    self._as_config_path(mapping.dbc), mapping.comment)
        normalized = {}
        for key, paths in (channel_dbc or {}).items():
            try:
                normalized[int(key)] = list(paths or [])
            except (TypeError, ValueError):
                logger.warning("忽略非法通道号：%r", key)

        for channel in sorted(normalized):
            self.ensure_channels([channel])
            seen = set()
            for dbc in normalized[channel]:
                absolute = self.resolve_path(dbc)
                key = os.path.abspath(absolute)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    file = self.load_dbc(key)
                    kind = file.auto_kind
                except Exception as exc:                 # noqa: BLE001
                    errors[key] = "%s: %s" % (type(exc).__name__, exc)
                    logger.error("DBC 解析失败，跳过映射：%s -> %s", key, exc)
                    continue
                config_path = self._as_config_path(key)
                new_mappings.append(ChannelMapping(
                    dbc=config_path, channels=[channel], kind=kind,
                    comment=legacy_comments.get(config_path, "")))
            if channel in names:
                self.set_channel_meta(channel, name=names[channel])
            if channel in remarks:
                self.set_channel_meta(channel, remark=remarks[channel])

        self.mappings = new_mappings
        self._errors.update(errors)
        logger.debug("应用通道映射编辑：%d 通道 / %d 条映射 / %d 个解析失败",
                     len(self.channels), len(self.mappings), len(errors))
        return {"mappings": len(self.mappings), "errors": errors}

    def channel_conflicts(self, kind: str, channel: int) -> list:
        """同通道上被多个 DBC 定义同一报文 ID 的列表 [(can_id, [message, ...])]。"""
        buckets = {}
        for file in self.files_for(kind, channel):
            for can_id, message in file.messages.items():
                bucket = buckets.setdefault(can_id, [])
                if all(item.dbc_name != message.dbc_name for item in bucket):
                    bucket.append(message)
        return [(can_id, messages) for can_id, messages in sorted(buckets.items())
                if len(messages) > 1]

    def clear(self) -> None:
        self.mappings = []

    # -------------------------------------------------- 加载与查询
    def load_dbc(self, path: str) -> DbcFile:
        """加载（带缓存）一个 DBC。解析失败会抛异常，调用方负责提示。"""
        key = os.path.abspath(path)
        if key in self._files:
            logger.debug("复用已解析的 DBC：%s", os.path.basename(key))
            return self._files[key]
        logger.info("解析 DBC：%s", key)
        file = DbcFile(key)
        self._files[key] = file
        self._errors.pop(key, None)
        return file

    def invalidate_cache(self, path: str = None) -> None:
        """丢弃解析缓存（重新扫描/文件被替换时用）。"""
        if path is None:
            self._files.clear()
            self._errors.clear()
            return
        key = os.path.abspath(path)
        self._files.pop(key, None)
        self._errors.pop(key, None)

    def load_all(self) -> dict:
        """按配置加载全部 DBC，返回 {"loaded": n, "errors": {path: msg}}。"""
        self._errors = {}
        for mapping in self.mappings:
            path = self.resolve_path(mapping.dbc)
            try:
                self.load_dbc(path)
            except Exception as exc:                     # noqa: BLE001
                self._errors[path] = "%s: %s" % (type(exc).__name__, exc)
                logger.error("DBC 加载失败：%s -> %s", path, exc)
        logger.debug("按配置加载 DBC 完成：%d 个文件，%d 个失败",
                     len(self._files), len(self._errors))
        return {"loaded": len(self._files), "errors": dict(self._errors)}

    def files_for(self, kind: str, channel: int) -> list:
        """该 (帧类型, 通道) 上生效的 DBC 文件列表。"""
        out = []
        for mapping in self.mappings:
            if not mapping.matches(kind, channel):
                continue
            file = self._files.get(self.resolve_path(mapping.dbc))
            if file is not None:
                out.append(file)
        return out

    def resolve_message(self, kind: str, channel: int, can_id: int):
        """按通道映射找到报文定义（多个 DBC 命中时取第一个，其余记在 conflicts）。"""
        hits = []
        for file in self.files_for(kind, channel):
            hits += file.messages_for(can_id)
        if not hits:
            return None, []
        return hits[0], hits[1:]

    def resolve_signals(self, kind: str, channel: int, can_id: int) -> list:
        message, _ = self.resolve_message(kind, channel, can_id)
        return list(message.signals) if message else []

    @property
    def loaded_files(self) -> list:
        return list(self._files.values())

    @property
    def errors(self) -> dict:
        return dict(self._errors)

    def stats(self) -> dict:
        messages = sum(len(f.messages) for f in self._files.values())
        signals = sum(f.signal_count for f in self._files.values())
        mapped_channels = len({channel for mapping in self.mappings
                               for channel in mapping.channels})
        return {"dbc_files": len(self._files), "mappings": len(self.mappings),
                "channels": len(self.channels), "mapped_channels": mapped_channels,
                "messages": messages, "signals": signals,
                "errors": len(self._errors)}

    # -------------------------------------------------- 便捷入口
    @classmethod
    def open(cls, dbc_dir: str) -> "DbcLibrary":
        """读配置并加载全部 DBC（目录不存在时返回空库，不报错）。"""
        library = cls(dbc_dir)
        if os.path.isdir(library.dbc_dir):
            library.load_config()
            library.load_all()
        return library

    def discover(self) -> list:
        """列出 dbc/ 目录下所有 DBC 文件路径（供 UI 选择）。"""
        if not os.path.isdir(self.dbc_dir):
            return []
        return sorted(os.path.join(self.dbc_dir, name)
                      for name in os.listdir(self.dbc_dir)
                      if name.endswith(DBC_SUFFIXES))
