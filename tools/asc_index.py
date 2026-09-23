# -*- coding: utf-8 -*-
"""ASC → Parquet 索引 CLI（M1 交付物）。

用法：
    python tools/asc_index.py <asc 文件或目录> [更多路径…]
        [--out 索引.parquet] [--force] [--json 报告.json] [--report 报告.txt] [--quiet]

产物：
    索引默认写入 %LOCALAPPDATA%\\BeneCAN\\index\\<文件名>_<路径哈希>.parquet；
    源文件未变时二次运行直接命中缓存（毫秒级返回）。

退出码：0 = 成功；1 = 存在畸形行/未识别行；2 = 参数或输入错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import INDEX_DIR_ENV, PERF_FIRST_INDEX_SECONDS, TOOL_VERSION  # noqa: E402
from core.indexer import build_index  # noqa: E402

MB = 1048576.0


def _progress_printer(path: str):
    """每 5% 打一次进度到 stderr。"""
    state = {"next": 0.05}

    def callback(done_lines, done_bytes, total_bytes):
        ratio = done_bytes / total_bytes if total_bytes else 0.0
        if ratio >= state["next"]:
            state["next"] += 0.05
            sys.stderr.write("    %s %5.1f%% (%d 行)\n" % (os.path.basename(path), ratio * 100, done_lines))
            sys.stderr.flush()

    return callback


def format_report(results: list, total_seconds: float) -> str:
    lines = []
    add = lines.append
    add("=" * 78)
    add("M1 索引报告（ASC → Parquet）   工具版本 %s" % TOOL_VERSION)
    add("=" * 78)
    add("")
    src_total = idx_total = 0
    for r in results:
        src_total += r["source_bytes"]
        idx_total += r["index_bytes"]
        add("── %s" % os.path.basename(r["path"]))
        add("   源文件      : %s  (%.1f MB)" % (r["path"], r["source_bytes"] / MB))
        add("   行数 / 帧数 : %d / %d" % (r["lines"], r["frames"]))
        add("   索引大小    : %.1f MB   压缩比 %.1fx" % (
            r["index_bytes"] / MB, r["source_bytes"] / max(r["index_bytes"], 1)))
        if r["cached"]:
            add("   索引构建    : 命中缓存（源文件未变，未重建）")
        else:
            fps = r["frames"] / max(r["build_seconds"], 0.001)
            add("   索引构建    : %.1f s  (%.0f 帧/秒)   解析 %.1f s / 排序 %.1f s / 写盘 %.1f s" % (
                r["build_seconds"], fps, r["parse_seconds"], r["sort_seconds"],
                r["write_seconds"]))
            peak = r.get("peak_rss_mb")
            add("   峰值内存    : %s" % ("%.1f MB" % peak if peak else "未采集"))
        add("   时间范围    : %.6f ~ %.6f s (%.1f h)" % (
            r["ts_first"], r["ts_last"], (r["ts_last"] - r["ts_first"]) / 3600))
        kinds = ", ".join("%s=%d" % (k, v) for k, v in r["kind_counts"].items())
        add("   帧类型      : %s" % kinds)
        add("   畸形 / 未识别行 : %d / %d" % (r["malformed"], r["unknown_lines"]))
        if r.get("truncated"):
            add("   超长截断    : %d 行" % r["truncated"])
        add("   索引路径    : %s" % r["index_path"])
        add("")
    add("── 合计")
    add("   源文件      : %.1f MB" % (src_total / MB))
    add("   索引        : %.1f MB   压缩比 %.1fx" % (
        idx_total / MB, src_total / max(idx_total, 1)))
    add("   总耗时      : %.1f s" % total_seconds)
    add("")
    slow = [r for r in results
            if not r["cached"] and r["build_seconds"] > PERF_FIRST_INDEX_SECONDS]
    if slow:
        add("   [注意] 以下文件超出性能基线（首次建索引 ≤ %d s）：" % PERF_FIRST_INDEX_SECONDS)
        for r in slow:
            add("      %s  %.1f s" % (os.path.basename(r["path"]), r["build_seconds"]))
    else:
        add("   [OK] 全部满足性能基线（首次建索引 ≤ %d s）" % PERF_FIRST_INDEX_SECONDS)
    add("")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ASC → Parquet 索引（M1）")
    parser.add_argument("paths", nargs="+", help="asc 文件或包含 asc 的目录")
    parser.add_argument("--out", help="索引输出路径（仅单文件时可用）")
    parser.add_argument("--index-dir", dest="index_dir",
                        help="索引缓存目录（默认 <TEMP>\\BeneCAN\\index，"
                             "也可用环境变量 %s 覆盖）" % INDEX_DIR_ENV)
    parser.add_argument("--force", action="store_true", help="忽略缓存强制重建")
    parser.add_argument("--json", dest="json_path", help="把统计写入 JSON")
    parser.add_argument("--report", dest="report_path", help="把文本报告写入文件")
    parser.add_argument("--quiet", action="store_true", help="不打印报告")
    args = parser.parse_args(argv)

    targets = []
    for p in args.paths:
        if os.path.isdir(p):
            targets += [os.path.join(p, n) for n in sorted(os.listdir(p))
                        if n.lower().endswith(".asc")]
        elif os.path.exists(p):
            targets.append(p)
        else:
            print("路径不存在：%s" % p, file=sys.stderr)
            return 2
    if not targets:
        print("没有找到 .asc 文件", file=sys.stderr)
        return 2
    if args.out and len(targets) > 1:
        print("--out 只支持单文件", file=sys.stderr)
        return 2
    if args.index_dir:
        os.environ[INDEX_DIR_ENV] = args.index_dir

    started = time.time()
    results = []
    exit_code = 0
    for path in targets:
        sys.stderr.write("索引 %s ...\n" % path)
        sys.stderr.flush()
        result = build_index(path, index_path=args.out, force=args.force,
                             progress=_progress_printer(path))
        result["tool_version"] = TOOL_VERSION
        results.append(result)
        if result["malformed"] or result["unknown_lines"]:
            exit_code = 1

    text = format_report(results, time.time() - started)
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
            json.dump(results, fh, ensure_ascii=False, indent=2)
        sys.stderr.write("统计已写入 %s\n" % args.json_path)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
