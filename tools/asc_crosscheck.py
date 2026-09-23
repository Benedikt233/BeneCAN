# -*- coding: utf-8 -*-
"""交叉验证：本方解析器 vs python-can ASCReader（M1 验收项）。

用法：
    python tools/asc_crosscheck.py <asc 文件> [-n 20000] [--json 结果.json]

只读取前 N 帧（文件顺序）逐帧比对：时间戳、通道、帧类型(FD/经典)、报文 ID、
payload 长度与内容。方向(Rx/Tx)单独统计——python-can 的 CANFD 分支对 "Rx"
大小写敏感（见其 asc.py 中 `direction == "Rx"`），上游写出的是大写 "RX"，
因此其方向字段会判错，属 python-can 侧已知限制，不计入不一致。

退出码：0 = 核心字段逐帧一致；1 = 存在不一致；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.asc_parser import KIND_CANFD, KIND_CLASSIC, KIND_LIN, classify  # noqa: E402

TS_TOLERANCE = 5e-7
MAX_SAMPLES = 3
FIELDS = ("ts", "channel", "is_fd", "can_id", "length", "data")


def read_ours(path: str, limit: int) -> list:
    """本方解析器读取前 limit 帧（文件顺序）。"""
    frames = []
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as fh:
        for line in fh:
            info = classify(line, keep_data=True)
            if info.kind not in (KIND_CANFD, KIND_CLASSIC, KIND_LIN):
                continue
            frames.append({
                "kind": info.kind,
                "ts": info.ts,
                "channel": _channel_int(info.channel),
                "is_fd": info.kind == KIND_CANFD,
                "can_id": int(info.can_id, 16),
                "length": info.length,
                "data": bytes(int(tok, 16) for tok in info.data),
                "is_rx": info.direction in ("Rx", "RX", "rx"),
            })
            if len(frames) >= limit:
                break
    return frames


def _channel_int(channel: str) -> int:
    text = channel[1:] if channel[:1] in ("L", "l") else channel
    try:
        return int(text)
    except ValueError:
        return -1


def read_python_can(path: str, limit: int) -> tuple:
    """python-can ASCReader 读取前 limit 帧；返回 (帧列表, 异常信息)。"""
    import can
    frames = []
    error = None
    try:
        reader = can.ASCReader(path)
        for msg in reader:
            frames.append({
                "ts": msg.timestamp,
                "channel": msg.channel,
                "is_fd": bool(getattr(msg, "is_fd", False)),
                "can_id": msg.arbitration_id,
                "length": len(msg.data),
                "data": bytes(msg.data),
                "is_rx": bool(msg.is_rx),
            })
            if len(frames) >= limit:
                break
    except Exception as exc:                      # noqa: BLE001
        error = "%s: %s" % (type(exc).__name__, exc)
    return frames, error


def compare(ours: list, theirs: list) -> dict:
    """逐帧比对，返回统计。"""
    result = {
        "ours_frames": len(ours),
        "theirs_frames": len(theirs),
        "compared": 0,
        "mismatch": {f: 0 for f in FIELDS},
        "direction_mismatch": 0,
        "samples": [],
    }
    for index, (mine, other) in enumerate(zip(ours, theirs)):
        result["compared"] += 1
        problems = []
        checks = [
            ("ts", abs(mine["ts"] - other["ts"]) <= TS_TOLERANCE,
             "ts %r != %r" % (mine["ts"], other["ts"])),
            ("channel", mine["channel"] == other["channel"] + 1,
             "channel %s != %s+1" % (mine["channel"], other["channel"])),
            ("is_fd", mine["is_fd"] == other["is_fd"],
             "is_fd %s != %s" % (mine["is_fd"], other["is_fd"])),
            ("can_id", mine["can_id"] == other["can_id"],
             "can_id %#x != %#x" % (mine["can_id"], other["can_id"])),
            ("length", mine["length"] == other["length"],
             "length %d != %d" % (mine["length"], other["length"])),
            ("data", mine["data"] == other["data"],
             "data %s != %s" % (mine["data"].hex(), other["data"].hex())),
        ]
        for field, ok, message in checks:
            if not ok:
                result["mismatch"][field] += 1
                problems.append(message)
        if mine["is_rx"] != other["is_rx"]:
            result["direction_mismatch"] += 1
        if problems and len(result["samples"]) < MAX_SAMPLES:
            result["samples"].append("第 %d 行: %s" % (index + 1, "; ".join(problems)))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ASC 解析交叉验证（本方 vs python-can）")
    parser.add_argument("path", help="asc 文件")
    parser.add_argument("-n", dest="limit", type=int, default=20000, help="比对帧数（默认 20000）")
    parser.add_argument("--json", dest="json_path", help="把结果写入 JSON")
    args = parser.parse_args(argv)

    if not os.path.exists(args.path):
        print("文件不存在：%s" % args.path, file=sys.stderr)
        return 2

    ours = read_ours(args.path, args.limit)
    theirs, error = read_python_can(args.path, args.limit)
    if error:
        print("[python-can 异常] %s" % error, file=sys.stderr)

    stats = compare(ours, theirs)
    stats["path"] = args.path
    stats["python_can_error"] = error

    total_bad = sum(stats["mismatch"].values())
    stats["core_fields_identical"] = total_bad == 0 and not error

    print("=" * 78)
    print("ASC 解析交叉验证   本方解析器  vs  python-can ASCReader")
    print("=" * 78)
    print("文件        : %s" % args.path)
    print("本方帧数    : %d" % stats["ours_frames"])
    print("python-can  : %d%s" % (stats["theirs_frames"], "  [异常]" if error else ""))
    print("逐帧比对    : %d 帧" % stats["compared"])
    for field in FIELDS:
        print("   %-10s 不一致 %d 帧" % (field, stats["mismatch"][field]))
    print("   %-10s 不一致 %d 帧  （python-can 已知限制：CANFD 分支 "
          "`direction == \"Rx\"` 大小写敏感，上游写的是大写 RX）"
          % ("direction", stats["direction_mismatch"]))
    for sample in stats["samples"]:
        print("   [不一致样本] %s" % sample)
    print("-" * 78)
    if error:
        print("结论: 未通过 —— python-can 读取过程中抛异常")
    elif total_bad == 0:
        print("结论: 通过 —— 时间戳/通道/帧类型/ID/长度/payload 逐帧完全一致（方向差异为 python-can 侧限制）")
    else:
        print("结论: 未通过 —— 核心字段存在 %d 处不一致" % total_bad)
    print("")

    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)), exist_ok=True)
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=2)
        sys.stderr.write("结果已写入 %s\n" % args.json_path)
    return 0 if stats["core_fields_identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
