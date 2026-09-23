# -*- coding: utf-8 -*-
"""ASC 语法体检 + 时间轴归因报告（M0 交付物）。

用法：
    python tools/asc_inspect.py <asc 文件或目录> [更多路径…]
        [--json 报告.json] [--report 报告.txt] [--dup-scan N] [--quiet]

作用：
    1. 全量扫描 ASC，输出行类型分布、通道分布、ID 统计、时间戳体检、数据特征；
    2. 识别一切未识别/畸形行（要求为 0），并给出样本，用于固化语法规范；
    3. 判定日志形态（A 单会话单调 / B 多时间基准），给出时间轴归因结论。

退出码：0 = 无未识别行；1 = 存在未识别/畸形行。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (  # noqa: E402
    CANFD_TAIL_COLUMNS, CHANNEL_MAP, TOOL_VERSION,
    TS_BACKWARD_THRESHOLD, TS_GAP_THRESHOLD,
)
from core.asc_parser import (  # noqa: E402
    KIND_CANFD, KIND_CLASSIC, KIND_HEADER, KIND_LABELS, KIND_LIN,
    KIND_SKIP, KIND_SOA, KIND_UNKNOWN, classify,
)

FRAME_KINDS = (KIND_CANFD, KIND_CLASSIC, KIND_LIN)
TOP_N_IDS = 15
SAMPLE_LIMIT = 8


def _bucket(delta: float) -> str:
    d = abs(delta)
    if d < 0.5:
        return "<0.5s"
    if d < 2.0:
        return "0.5~2s"
    if d < 10.0:
        return "2~10s"
    if d < 60.0:
        return "10~60s"
    return ">60s"


def inspect_file(path: str, dup_scan_lines: int) -> dict:
    """全量扫描单个 ASC 文件，返回指标字典。"""
    size = os.path.getsize(path)
    started = time.time()

    kind_counts = Counter()
    channel_kinds = defaultdict(Counter)
    ids_by_kind = defaultdict(set)
    id_counts = Counter()
    channel_ts_minmax = {}
    channel_backwards = Counter()
    channel_gaps = Counter()
    backward_buckets = Counter()
    backward_samples = []
    global_backward = 0
    prev_ts = {}
    prev_global = None
    precision = Counter()
    dlc_counts = Counter()
    dlc_bad = 0
    len_bad = 0
    bad_samples = []
    trailing_counter = Counter()
    trailing_deviations = 0
    soa_names = set()
    soa_ts_zero = 0
    soa_precision = Counter()
    header_lines = []
    first_lines = {}
    seen_frames = {}
    dup_cross = 0
    dup_same = 0
    dup_scanned = 0
    total_lines = 0
    first_ts = last_ts = None

    with open(path, "r", encoding="utf-8-sig", errors="ignore") as fh:
        for line in fh:
            total_lines += 1
            info = classify(line, keep_data=dup_scan_lines > dup_scanned)
            kind = info.kind
            kind_counts[kind] += 1
            if kind not in first_lines:
                first_lines[kind] = line.strip()[:150]

            if kind == KIND_HEADER:
                if len(header_lines) < 8 and line.strip() not in header_lines:
                    header_lines.append(line.strip()[:120])
                continue
            if kind in (KIND_SKIP, KIND_UNKNOWN):
                if kind == KIND_UNKNOWN and len(bad_samples) < SAMPLE_LIMIT:
                    bad_samples.append(info.text)
                continue

            if kind == KIND_SOA:
                soa_names.add(info.name)
                if info.ts == 0.0:
                    soa_ts_zero += 1
                soa_precision[len(_decimals(line))] += 1
                continue

            # 以下为帧行
            channel_kinds[info.channel][kind] += 1
            ids_by_kind[kind].add(info.can_id)
            id_counts[(kind, info.channel, info.can_id)] += 1
            precision[len(_decimals(line))] += 1
            first_ts = info.ts if first_ts is None else min(first_ts, info.ts)
            last_ts = info.ts if last_ts is None else max(last_ts, info.ts)

            lo, hi = channel_ts_minmax.get(info.channel, (info.ts, info.ts))
            channel_ts_minmax[info.channel] = (min(lo, info.ts), max(hi, info.ts))

            prev = prev_ts.get(info.channel)
            if prev is not None:
                delta = info.ts - prev
                if delta < -TS_BACKWARD_THRESHOLD:
                    channel_backwards[info.channel] += 1
                    backward_buckets[_bucket(delta)] += 1
                    if len(backward_samples) < SAMPLE_LIMIT:
                        backward_samples.append(
                            "ch=%-3s %s  %.6f -> %.6f" % (info.channel, KIND_LABELS[kind], prev, info.ts))
                elif delta > TS_GAP_THRESHOLD:
                    channel_gaps[info.channel] += 1
            prev_ts[info.channel] = info.ts

            if prev_global is not None and info.ts < prev_global - 1e-9:
                global_backward += 1
            prev_global = info.ts

            if kind == KIND_CANFD:
                dlc_counts[info.dlc] += 1
                if not info.dlc_ok:
                    dlc_bad += 1
                if info.trailing:
                    trailing_counter[info.trailing] += 1
                else:
                    trailing_deviations += 1
            if not info.len_ok:
                len_bad += 1

            if dup_scan_lines > dup_scanned:
                dup_scanned += 1
                key = hash((round(info.ts, 6), info.can_id, info.data))
                prev_ch = seen_frames.get(key)
                if prev_ch is None:
                    seen_frames[key] = info.channel
                elif prev_ch != info.channel:
                    dup_cross += 1
                else:
                    dup_same += 1

    elapsed = time.time() - started
    return {
        "path": path,
        "size_bytes": size,
        "total_lines": total_lines,
        "elapsed_seconds": round(elapsed, 1),
        "kind_counts": dict(kind_counts),
        "channel_kinds": {ch: dict(c) for ch, c in channel_kinds.items()},
        "unique_ids": {k: len(v) for k, v in ids_by_kind.items()},
        "top_ids": [
            {"kind": k, "channel": ch, "id": cid, "count": n}
            for (k, ch, cid), n in id_counts.most_common(TOP_N_IDS)
        ],
        "ts_first": first_ts,
        "ts_last": last_ts,
        "ts_precision": dict(precision),
        "channel_ts_minmax": {ch: list(v) for ch, v in channel_ts_minmax.items()},
        "channel_backwards": dict(channel_backwards),
        "channel_gaps": dict(channel_gaps),
        "backward_buckets": dict(backward_buckets),
        "backward_samples": backward_samples,
        "global_backward": global_backward,
        "dlc_counts": dict(dlc_counts),
        "dlc_bad": dlc_bad,
        "len_bad": len_bad,
        "bad_samples": bad_samples,
        "trailing_const": trailing_counter.most_common(1)[0][0] if trailing_counter else None,
        "trailing_const_hits": trailing_counter.most_common(1)[0][1] if trailing_counter else 0,
        "trailing_variants": len(trailing_counter),
        "trailing_missing": trailing_deviations,
        "soa_rows": kind_counts.get(KIND_SOA, 0),
        "soa_unique_names": len(soa_names),
        "soa_ts_zero": soa_ts_zero,
        "soa_precision": dict(soa_precision),
        "header_lines": header_lines,
        "first_lines": first_lines,
        "dup_scan_lines": dup_scanned,
        "dup_cross_channel": dup_cross,
        "dup_same_channel": dup_same,
    }


def _decimals(line: str) -> str:
    """取时间戳小数部分（不做 float 转换，避免精度噪声）。"""
    token = line.strip().split(" ", 1)[0]
    return token.split(".")[-1] if "." in token else ""


def _form_conclusion(m: dict) -> tuple:
    """时间轴归因：判定日志形态并给出结论与建议。"""
    backs = sum(m["channel_backwards"].values())
    spans = {ch: hi - lo for ch, (lo, hi) in m["channel_ts_minmax"].items()}
    if not spans:
        return "无数据", [], []
    span_min, span_max = min(spans.values()), max(spans.values())
    ratio = (span_max / span_min) if span_min > 0 else float("inf")

    if backs == 0:
        form = "A（单会话，时间轴单调）"
    else:
        form = "B（多时间基准 / 多段日志混合）"

    evidence = [
        "全局时间戳倒序 %d 次；同通道回退 >%.1fs 共 %d 次（分布 %s）" % (
            m["global_backward"], TS_BACKWARD_THRESHOLD, backs, m["backward_buckets"] or "{}"),
        "各通道覆盖时长 %.1f ~ %.1f s（最大/最小 = %s 倍，反映各 ECU 录制起止时间差异）" % (
            span_min, span_max, "%.1f" % ratio if ratio != float("inf") else "无穷"),
        "同通道正向空档 >%.1fs 共 %d 次" % (TS_GAP_THRESHOLD, sum(m["channel_gaps"].values())),
    ]
    if backs == 0:
        advice = [
            "时间轴可直接按原样用于 Graphic（无需额外处理）。",
        ]
    else:
        advice = [
            "X 轴不可直接当唯一时间基准使用：同一通道内存在多段时间基准，直接绘图会出现"
            "来回折返的锯齿。",
            "建议一（工具侧）：提供「原样 / 会话分段 / 序号」三种时间轴模式，默认自动推荐分段。",
            "建议二（上游侧）：确认 zst2asc 是否把多份日志（不同起始时间/多 ECU）合并进同一文件；"
            "若是，优先在上游统一时间基准或输出分段标记（成本最低、收益最大）。",
        ]
    return form, evidence, advice


def build_report(m: dict) -> list:
    """把指标字典渲染成文本报告。"""
    L = []
    add = L.append
    size_mb = m["size_bytes"] / 1048576.0
    add("=" * 78)
    add("ASC 语法体检报告   %s" % m["path"])
    add("=" * 78)
    add("文件大小   : %.1f MB (%d 字节)" % (size_mb, m["size_bytes"]))
    add("总行数     : %d" % m["total_lines"])
    add("扫描耗时   : %.1f s (%.0f 行/秒)" % (
        m["elapsed_seconds"],
        m["total_lines"] / max(m["elapsed_seconds"], 0.001)))
    add("")

    add("── 1. 头部行 ──")
    for h in m["header_lines"]:
        add("   %s" % h)
    year_warn = any(("299" in h or "999" in h) for h in m["header_lines"])
    if year_warn:
        add("   [警告] 头部年份不可信（实测出现过 2999）→ X 轴一律使用报文相对时间戳")
    add("")

    add("── 2. 行类型分布 ──")
    for kind in (KIND_HEADER, KIND_CANFD, KIND_CLASSIC, KIND_LIN, KIND_SOA, KIND_SKIP, KIND_UNKNOWN):
        n = m["kind_counts"].get(kind, 0)
        if n == 0 and kind in (KIND_LIN, KIND_SKIP, KIND_UNKNOWN):
            add("   %-12s %d   [OK 无此行]" % (KIND_LABELS[kind], n))
        else:
            add("   %-12s %d" % (KIND_LABELS[kind], n))
    if m["kind_counts"].get(KIND_UNKNOWN):
        add("   [失败] 存在未识别行，样本：")
        for s in m["bad_samples"]:
            add("      %s" % s)
    else:
        add("   [OK] 未识别行 0 行 —— 语法覆盖完整")
    add("")

    add("── 3. 通道分布 ──")
    add("   %-6s %-22s %-8s %s" % ("通道", "来源", "总行数", "行类型明细"))
    for ch, kinds in sorted(m["channel_kinds"].items(), key=lambda kv: -sum(kv[1].values())):
        try:
            origin = "%s(CHNum=%s)" % CHANNEL_MAP.get(int(ch), ("?", "?"))
        except ValueError:
            origin = "未知(非数字通道，可能是 LIN/SOA)"
        detail = ", ".join("%s=%d" % (KIND_LABELS[k], v) for k, v in sorted(kinds.items()))
        add("   %-6s %-22s %-8d %s" % (ch, origin, sum(kinds.values()), detail))
    add("")

    add("── 4. 报文 ID ──")
    for kind, n in sorted(m["unique_ids"].items()):
        add("   %-12s 唯一 ID 数 %d" % (KIND_LABELS[kind], n))
    add("   出现最多的 (类型/通道/ID/次数)：")
    for row in m["top_ids"]:
        add("      %-12s ch=%-4s id=%-6s %d" % (
            KIND_LABELS[row["kind"]], row["channel"], row["id"], row["count"]))
    add("")

    add("── 5. 时间戳体检 ──")
    add("   范围        : %.6f ~ %.6f (跨度 %.1f s / %.1f h)" % (
        m["ts_first"], m["ts_last"], m["ts_last"] - m["ts_first"],
        (m["ts_last"] - m["ts_first"]) / 3600))
    add("   小数位分布  : %s%s" % (
        m["ts_precision"],
        "   [注意] 位数不一致，不可按固定宽度切分" if len(m["ts_precision"]) > 1 else " (帧行统一)"))
    add("   各通道跨度  :")
    for ch, (lo, hi) in sorted(m["channel_ts_minmax"].items(),
                               key=lambda kv: kv[1][1] - kv[1][0], reverse=True):
        add("      ch=%-5s %.6f ~ %.6f  跨度 %.1f s" % (ch, lo, hi, hi - lo))
    add("   同通道回退 >%.1fs : %d 次  %s" % (
        TS_BACKWARD_THRESHOLD, sum(m["channel_backwards"].values()), m["channel_backwards"]))
    add("   回退幅度分布      : %s" % (m["backward_buckets"],))
    add("   全局时间戳倒序    : %d 次" % m["global_backward"])
    add("   同通道正向空档 >%.1fs : %d 次" % (TS_GAP_THRESHOLD, sum(m["channel_gaps"].values())))
    for s in m["backward_samples"]:
        add("      [回退样本] %s" % s)
    add("")

    add("── 6. 数据特征 ──")
    add("   CANFD DLC 分布 : %s" % (m["dlc_counts"],))
    add("   DLC/长度不符  : %d 行" % m["dlc_bad"])
    add("   长度字段不符  : %d 行" % m["len_bad"])
    if m["trailing_const"]:
        add("   尾部常量列    : %d 列，命中 %d 行，变体 %d 种，缺失 %d 行" % (
            len(m["trailing_const"]), m["trailing_const_hits"],
            m["trailing_variants"], m["trailing_missing"]))
        add("      常量内容   : %s" % " ".join(m["trailing_const"]))
        add("      （硬编码常量，非数据，解析时丢弃；见上游 file_convert.py 写行处）")
    add("   跨通道重复帧  : %d 行（同 ts+ID+数据出现在不同通道，多 ECU 录同一条总线）"
        % m["dup_cross_channel"])
    add("   同通道重复帧  : %d 行" % m["dup_same_channel"])
    add("   （重复统计采样行数：%d，用 --dup-scan 调整）" % m["dup_scan_lines"])
    if m["soa_rows"]:
        add("   SOA 行        : %d 行，唯一信号名 %d 个，时间戳为 0 的 %d 行 [本期跳过不解析]"
            % (m["soa_rows"], m["soa_unique_names"], m["soa_ts_zero"]))
        add("      SOA 时间戳小数位 : %s（位数不固定，与帧行不同）" % (m["soa_precision"],))
    add("")

    form, evidence, advice = _form_conclusion(m)
    add("── 7. 时间轴归因结论 ──")
    add("   判定形态 : %s" % form)
    for e in evidence:
        add("   证据     : %s" % e)
    for a in advice:
        add("   建议     : %s" % a)
    add("")

    add("── 8. 行样本（供语法规范比对）──")
    for kind, text in sorted(m["first_lines"].items()):
        add("   %-12s %s" % (KIND_LABELS.get(kind, kind), text))
    add("")
    return L


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="ASC 语法体检 + 时间轴归因报告（M0）")
    parser.add_argument("paths", nargs="+", help="asc 文件或包含 asc 的目录")
    parser.add_argument("--json", dest="json_path", help="把指标写入 JSON（做回归基线）")
    parser.add_argument("--report", dest="report_path", help="把文本报告写入文件")
    parser.add_argument("--dup-scan", dest="dup_scan", type=int, default=500_000,
                        help="重复帧检测的采样行数，0 = 全量（默认 50 万行）")
    parser.add_argument("--quiet", action="store_true", help="不打印报告，只写文件")
    args = parser.parse_args(argv)

    targets = []
    for p in args.paths:
        if os.path.isdir(p):
            targets += [os.path.join(p, n) for n in sorted(os.listdir(p))
                        if n.lower().endswith(".asc")]
        else:
            targets.append(p)
    if not targets:
        print("没有找到 .asc 文件", file=sys.stderr)
        return 2

    all_metrics = []
    report_lines = []
    exit_code = 0
    for path in targets:
        sys.stderr.write("扫描 %s ...\n" % path)
        sys.stderr.flush()
        metrics = inspect_file(path, args.dup_scan)
        metrics["tool_version"] = TOOL_VERSION
        all_metrics.append(metrics)
        report_lines += build_report(metrics)
        if metrics["kind_counts"].get(KIND_UNKNOWN) or metrics["len_bad"] or metrics["dlc_bad"]:
            exit_code = 1

    text = "\n".join(report_lines) + "\n"
    if not args.quiet:
        print(text)
    if args.report_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.report_path)), exist_ok=True)
        with open(args.report_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stderr.write("报告已写入 %s\n" % args.report_path)
    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)), exist_ok=True)
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(all_metrics, fh, ensure_ascii=False, indent=2)
        sys.stderr.write("指标已写入 %s\n" % args.json_path)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
