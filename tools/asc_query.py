# -*- coding: utf-8 -*-
"""查询层 CLI（M2）：不开 GUI 也能验证/取数。

用法：
    python tools/asc_query.py <索引.parquet | asc 文件> catalog
    python tools/asc_query.py <...> stats   --kind canfd --channel 5 --id 0x17A
    python tools/asc_query.py <...> signals --kind canfd --channel 5 --id 0x17A \
        [--byte 3] [--size 2] [--dtype uint16] [--order little|big] \
        [--bit 0] [--bits 4] [--scale 0.1] [--offset 0] [--mode time|session|index] \
        [--limit 20]
    python tools/asc_query.py <...> profile --kind canfd --channel 5 --id 0x17A

参数里的 --id 支持 0x 前缀（十六进制）或十进制。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TOOL_VERSION  # noqa: E402
from core.indexer import build_index, default_index_path, index_is_fresh  # noqa: E402
from core.query import IndexReader  # noqa: E402


def parse_id(text: str) -> int:
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def open_reader(path: str) -> IndexReader:
    """索引或 ASC 都接受；ASC 会自动建/复用索引。"""
    if path.endswith(".parquet"):
        return IndexReader.open(path)
    index_path = default_index_path(path)
    if not index_is_fresh(path, index_path):
        sys.stderr.write("建立索引 %s ...\n" % index_path)
        build_index(path, index_path=index_path)
    return IndexReader.open(index_path)


def cmd_catalog(reader: IndexReader, args) -> int:
    t0 = time.time()
    channels = reader.channels()
    catalog = reader.catalog()
    cost = time.time() - t0
    print("源文件   : %s" % reader.source_path)
    print("帧数     : %d" % reader.frames)
    print("通道     : %d 个    报文: %d 个    目录耗时: %.3f s" % (
        len(channels), len(catalog), cost))
    print()
    print("%-8s %-6s %-8s %-12s %-10s %s" % (
        "类型", "通道", "报文数", "帧数", "时长(s)", "帧/秒"))
    for channel in channels:
        rate = channel.frames / channel.duration if channel.duration > 0 else 0.0
        print("%-8s %-6d %-8d %-12d %-10.1f %.1f" % (
            channel.kind, channel.channel, channel.messages, channel.frames,
            channel.duration, rate))
    print()
    top = sorted(catalog, key=lambda m: -m.frames)[: args.limit]
    print("帧数最多的 %d 个报文：" % len(top))
    print("  %-8s %-5s %-10s %-9s %-12s %s" % (
        "类型", "通道", "ID", "帧数", "时长(s)", "长度范围"))
    for msg in top:
        print("  %-8s %-5d %-10s %-9d %-12.1f %d~%d%s" % (
            msg.kind, msg.channel, msg.id_text, msg.frames, msg.duration,
            msg.length_min, msg.length_max, "  [扩展]" if msg.extended else ""))
    return 0


def cmd_stats(reader: IndexReader, args) -> int:
    t0 = time.time()
    stats = reader.message_stats(args.kind, args.channel, args.id)
    cost = time.time() - t0
    print("报文     : %s ch%d %s" % (args.kind, args.channel, hex(args.id)))
    print("帧数     : %d" % stats["frames"])
    print("时间范围 : %.6f ~ %.6f s (%.1f s)" % (
        stats["ts_first"], stats["ts_last"], stats["ts_last"] - stats["ts_first"]))
    print("周期     : 中位 %.6f s (%.2f Hz)  最小 %.6f  最大 %.6f  抖动 %.6f" % (
        stats["interval_median"], stats["cycle_hz"], stats["interval_min"],
        stats["interval_max"], stats["interval_std"]))
    print("丢帧估计 : %d 帧    空档(>2×周期) %d 处" % (
        stats["missing_estimate"], stats["gap_count"]))
    print("长度集合 : %s    DLC 集合: %s" % (stats["lengths"], stats["dlcs"]))
    print("源文件序 : 时间%s    空档分段: %d" % (
        "单调" if stats["file_time_ordered"] else "乱序（索引已按时间重排）",
        stats["segments"]))
    print("查询耗时 : %.3f s" % cost)
    return 0


def cmd_signals(reader: IndexReader, args) -> int:
    t0 = time.time()
    series = reader.signal_series(
        args.kind, args.channel, args.id, byte_offset=args.byte, size=args.size,
        dtype=args.dtype, byte_order=args.order, bit_index=args.bit,
        bit_length=args.bits, scale=args.scale, offset=args.offset,
        time_mode=args.mode)
    cost = time.time() - t0
    print("报文     : %s ch%d %s" % (args.kind, args.channel, hex(args.id)))
    if args.bit >= 0:
        print("信号     : byte%d 位%d 起 %d 位（%s）" % (
            args.byte, args.bit, args.bits, args.order))
    else:
        print("信号     : byte%d 起 %d 字节（%s）" % (args.byte, args.size, args.order))
    print("点数     : %d    时间轴: %s    查询耗时: %.3f s" % (
        len(series), args.mode, cost))
    values = series.y
    print("数值范围 : %.4f ~ %.4f    均值 %.4f" % (
        values.min(), values.max(), values.mean()))
    print("前 %d 个点：" % min(args.limit, len(series)))
    for x, text in list(zip(series.x, series.value_text))[: args.limit]:
        print("   x=%-12.6f  %s" % (x, text))
    return 0


def cmd_profile(reader: IndexReader, args) -> int:
    t0 = time.time()
    profile = reader.byte_profile(args.kind, args.channel, args.id)
    cost = time.time() - t0
    print("报文     : %s ch%d %s    画像耗时: %.3f s" % (
        args.kind, args.channel, hex(args.id), cost))
    print("%-6s %-10s %-8s %-6s %-6s %-8s %s" % (
        "byte", "类型", "取值数", "最小", "最大", "均值", "变化次数"))
    used = profile[: args.limit] if args.limit else profile
    for item in used:
        if item["kind"] == "constant" and args.hide_constant:
            continue
        print("%-6d %-10s %-8d %-6d %-6d %-8.2f %d" % (
            item["byte"], item["kind"], item["unique"], item["min"],
            item["max"], item["mean"], item["changes"]))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ASC 索引查询 CLI（M2）")
    parser.add_argument("path", help="索引 .parquet 或 .asc 文件")
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_selector(p):
        p.add_argument("--kind", default="canfd",
                       choices=["canfd", "classic", "lin"], help="帧类型")
        p.add_argument("--channel", type=int, required=True, help="通道号")
        p.add_argument("--id", type=parse_id, required=True, help="报文 ID（0x 前缀为十六进制）")

    p_cat = sub.add_parser("catalog", help="打印通道与报文目录")
    p_cat.add_argument("--limit", type=int, default=20, help="列出帧数最多的前 N 个报文")
    p_cat.set_defaults(func=cmd_catalog)

    p_stats = sub.add_parser("stats", help="报文周期/抖动/丢帧统计")
    add_selector(p_stats)
    p_stats.set_defaults(func=cmd_stats)

    p_sig = sub.add_parser("signals", help="抽取并打印信号序列")
    add_selector(p_sig)
    p_sig.add_argument("--byte", type=int, default=0, help="起始字节偏移")
    p_sig.add_argument("--size", type=int, default=1, help="字节数")
    p_sig.add_argument("--dtype", default="uint8",
                       choices=["uint8", "int8", "uint16", "int16",
                                "uint32", "int32", "uint64", "int64", "float32"])
    p_sig.add_argument("--order", default="little", choices=["little", "big"])
    p_sig.add_argument("--bit", type=int, default=-1, help="位号（>=0 时按位取值）")
    p_sig.add_argument("--bits", type=int, default=1, help="位长度")
    p_sig.add_argument("--scale", type=float, default=1.0)
    p_sig.add_argument("--offset", type=float, default=0.0)
    p_sig.add_argument("--mode", default="time", choices=["time", "session", "index"])
    p_sig.add_argument("--limit", type=int, default=10, help="打印前 N 个点")
    p_sig.set_defaults(func=cmd_signals)

    p_prof = sub.add_parser("profile", help="逐字节画像")
    add_selector(p_prof)
    p_prof.add_argument("--limit", type=int, default=16, help="只看前 N 个字节（0=全部）")
    p_prof.add_argument("--hide-constant", action="store_true", help="隐藏常量字节")
    p_prof.set_defaults(func=cmd_profile)

    args = parser.parse_args(argv)
    if not os.path.exists(args.path):
        print("文件不存在：%s" % args.path, file=sys.stderr)
        return 2
    reader = open_reader(args.path)
    return args.func(reader, args)


if __name__ == "__main__":
    sys.exit(main())
