#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基准脚本：在给定并发下跑 N 个账号，打印单号平均耗时 + 成功/失败计数。

⚠️  这是"真实运行"脚本，不是单元测试。
    它会启动 Camoufox 浏览器并真实走 xAI 注册流程，
    会消耗 outlook 邮箱池别名 / 代理，请确认环境就绪后再运行。

前置与不能用时的失败原因：
1. config.json 必须存在且邮箱服务商已配置可用邮箱池（如 outlook_pool_file）。
2. 代理可用（config.proxy / 面板代理池），否则浏览器启动失败。
3. 默认会执行 curl 启动连通性检查；若你的环境 curl_cffi 探测失败/卡住，
   加 --skip-connectivity-checks 跳过（内部把 run_connectivity_checks
   monkeypatch 成返回空，等价于手动替换该函数）。
4. 无 GUI 的 Linux 服务器上 tkinter 导入可能失败，可参考
   run_batch_headless.py 的 _install_tkinter_stub 注入假 tkinter 再运行。
5. 单号平均耗时受 account_interval（账号间间隔，默认 60-120s）与
   OTP 轮询间隔 outlook_poll_interval（默认 3s）影响；想压真实单号耗时，
   把 account_interval 调小、outlook_poll_interval 调小后再跑基准。

用法：
    python scripts/bench_workers.py --count 10 --workers 3
    python scripts/bench_workers.py --count 5 --workers 1 --skip-connectivity-checks
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

import grok_register_ttk as g  # noqa: E402


def _parse_summary(lines):
    for line in reversed(lines):
        m = re.search(r"任务结束。成功 (\d+) \| 失败 (\d+)", str(line))
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="真实注册基准：测并发下单号平均耗时")
    ap.add_argument("--count", type=int, default=5, help="要注册的账号数（默认 5）")
    ap.add_argument("--workers", type=int, default=1, help="并发 worker 数（默认 1）")
    ap.add_argument(
        "--skip-connectivity-checks",
        action="store_true",
        help="跳过 curl 启动连通性检查（monkeypatch run_connectivity_checks 返回空）",
    )
    args = ap.parse_args()

    if args.skip_connectivity_checks:
        # 绕过 curl 启动检查：把连通性检查替换为空函数，等价于无检查直接开跑
        g._conn.run_connectivity_checks = lambda *a, **k: []

    g.load_config()
    g.config["register_count"] = int(args.count)
    g.config["register_workers"] = int(args.workers)

    captured = []
    _orig_cli_log = g.cli_log

    def _cap(message):
        if not g.should_emit_log(message):
            return
        captured.append(str(message))
        _orig_cli_log(message)

    g.cli_log = _cap
    print(f"[bench] start: count={args.count} workers={args.workers}", flush=True)
    t0 = time.time()
    try:
        g.run_registration_cli(args.count)
    finally:
        g.cli_log = _orig_cli_log
    wall = time.time() - t0

    summary = _parse_summary(captured)
    ok = fail = None
    if summary:
        ok, fail = summary
        print(f"[bench] 成功={ok} 失败={fail}")
    done = (ok or 0) + (fail or 0) or 1
    print(
        f"[bench] 总耗时={wall:.1f}s | 按任务数每号平均={wall / max(args.count, 1):.1f}s"
        f" | 按实际完成每号平均={wall / done:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
