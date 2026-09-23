# -*- coding: utf-8 -*-
"""统一信号模型（与数据源解耦）。

M3 里"图形窗口的一行"就是一条 SignalSpec：
    * 现在由"原始字节/位自动识别"生成（不依赖任何描述文件）；
    * 后续接入 DBC（M6）时，同一个 SignalSpec 直接由 DBC 信号生成，UI 层不变。

约定：SignalSpec 只描述"怎么取值"，取数交给 core.query.IndexReader，
所以本模块不 import Qt、也不 import pyarrow，可单独单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 取值来源
SRC_AUTO_BYTE = "auto-byte"     # 逐字节画像自动识别
SRC_MANUAL = "manual"           # 用户手动指定字节/位
SRC_DBC = "dbc"                 # 来自 DBC（M6）

# 逐字节画像的判定结果（与 core.query.byte_profile 一致）
KIND_CONSTANT = "constant"
KIND_ENUM = "enum"
KIND_ANALOG = "analog"

# 自动挑选行的排序权重：枚举/状态量优先，其次变化多的模拟量
_KIND_WEIGHT = {KIND_ENUM: 0, KIND_ANALOG: 1, KIND_CONSTANT: 2}

DEFAULT_COLORS = [
    "#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd",
    "#8c564b", "#17becf", "#e377c2", "#7f7f7f", "#bcbd22",
]


@dataclass
class SignalSpec:
    """图形窗口里的一行。"""
    key: tuple                      # (kind, channel, can_id)
    byte_offset: int
    size: int = 1
    dtype: str = "uint8"
    byte_order: str = "little"
    bit_index: int = -1             # >=0 表示按位取值
    bit_length: int = 0
    scale: float = 1.0
    offset: float = 0.0
    labels: dict = field(default_factory=dict)   # 枚举值 -> 文本（可选）
    color: str = ""
    source: str = SRC_AUTO_BYTE
    name: str = ""
    unit: str = ""
    step: bool = False             # True = 阶梯绘制（数字量/枚举，CANoe 风格）
    extreme_marks: bool | None = None   # 极值标记：None = 跟随全局开关，True/False = 本行强制

    @property
    def kind(self) -> str:
        return self.key[0]

    @property
    def channel(self) -> int:
        return self.key[1]

    @property
    def can_id(self) -> int:
        return self.key[2]

    @property
    def is_bit(self) -> bool:
        return self.bit_index >= 0

    @property
    def field_text(self) -> str:
        """字段描述，如 "byte14-15 uint16" / "byte16.bit0-6"。"""
        if self.is_bit:
            return "byte%d.bit%d-%d" % (self.byte_offset, self.bit_index,
                                        self.bit_index + self.bit_length - 1)
        if self.size == 1:
            return "byte%d" % self.byte_offset
        return "byte%d-%d %s" % (self.byte_offset, self.byte_offset + self.size - 1,
                                 self.dtype)

    def default_name(self) -> str:
        prefix = "0x%03X" % self.can_id if self.can_id <= 0x7FF else "0x%08X" % self.can_id
        return "%s ch%d %s" % (prefix, self.channel, self.field_text)

    def display_name(self) -> str:
        body = self.name or self.default_name()
        return "%s%s" % (body, " [%s]" % self.unit if self.unit else "")

    def extract(self, reader):
        """按本规格取数（reader 为 core.query.IndexReader）。"""
        return reader.signal_series(
            self.kind, self.channel, self.can_id,
            byte_offset=self.byte_offset, size=self.size, dtype=self.dtype,
            byte_order=self.byte_order, bit_index=self.bit_index,
            bit_length=self.bit_length, scale=self.scale, offset=self.offset,
            labels=self.labels)

    # ---------------------------------------------------------------- 序列化（布局保存用）
    def to_dict(self) -> dict:
        return {
            "key": [self.kind, self.channel, self.can_id],
            "byte_offset": self.byte_offset,
            "size": self.size,
            "dtype": self.dtype,
            "byte_order": self.byte_order,
            "bit_index": self.bit_index,
            "bit_length": self.bit_length,
            "scale": self.scale,
            "offset": self.offset,
            "labels": {str(k): v for k, v in self.labels.items()},
            "color": self.color,
            "source": self.source,
            "name": self.name,
            "unit": self.unit,
            "step": self.step,
            "extreme_marks": self.extreme_marks,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SignalSpec":
        key = data["key"]
        labels = {int(k): v for k, v in (data.get("labels") or {}).items()}
        extreme = data.get("extreme_marks")            # 老布局没有该字段 → None（跟随全局）
        return cls(
            key=(key[0], int(key[1]), int(key[2])),
            byte_offset=int(data.get("byte_offset", 0)),
            size=int(data.get("size", 1)),
            dtype=data.get("dtype", "uint8"),
            byte_order=data.get("byte_order", "little"),
            bit_index=int(data.get("bit_index", -1)),
            bit_length=int(data.get("bit_length", 0)),
            scale=float(data.get("scale", 1.0)),
            offset=float(data.get("offset", 0.0)),
            labels=labels,
            color=data.get("color", ""),
            source=data.get("source", SRC_MANUAL),
            name=data.get("name", ""),
            unit=data.get("unit", ""),
            step=bool(data.get("step", False)),
            extreme_marks=None if extreme is None else bool(extreme),
        )


def spec_from_profile(key: tuple, item: dict, source: str = SRC_AUTO_BYTE) -> SignalSpec:
    """把 byte_profile 的一条画像结果转成 SignalSpec。

    枚举/状态量（取值少）用阶梯绘制，模拟量用折线 —— 与 CANoe Graphic 的习惯一致。
    """
    return SignalSpec(key=key, byte_offset=int(item["byte"]), size=1,
                      dtype="uint8", source=source,
                      step=item["kind"] == KIND_ENUM)


def auto_specs(profile: list, key: tuple, max_rows: int = 6) -> list:
    """从逐字节画像里挑出"有信息量"的行。

    排序：枚举/状态量（取值少但变化多）优先，其次变化多的模拟量；
    完全不变的字节只在前两类不足时作兜底展示（便于确认报文确实全常量）。
    """
    scored = []
    for item in profile:
        kind = item["kind"]
        scored.append((_KIND_WEIGHT.get(kind, 9), -int(item["changes"]),
                       int(item["byte"]), item))
    scored.sort(key=lambda row: (row[0], row[1], row[2]))

    chosen = [row for row in scored if row[3]["kind"] != KIND_CONSTANT][:max_rows]
    if len(chosen) < max_rows:
        chosen += [row for row in scored if row[3]["kind"] == KIND_CONSTANT][:max_rows - len(chosen)]
    return [spec_from_profile(key, row[3]) for row in sorted(chosen, key=lambda r: r[2])]


def apply_colors(specs: list) -> list:
    """给没有颜色的行分配调色板颜色（原地设置）。"""
    for index, spec in enumerate(specs):
        if not spec.color:
            spec.color = DEFAULT_COLORS[index % len(DEFAULT_COLORS)]
    return specs


def group_specs_by_message(specs: list) -> dict:
    """按报文分组，供 UI 显示与批量操作。"""
    groups = {}
    for spec in specs:
        groups.setdefault(spec.key, []).append(spec)
    return groups
