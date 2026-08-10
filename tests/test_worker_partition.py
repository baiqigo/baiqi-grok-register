# -*- coding: utf-8 -*-
"""Worker 账号分区纯逻辑：plan_worker_partition + worker_start_stagger。

并发铺路：GUI(_run_registration_entry) 与 CLI(run_registration_cli) 共用同一
分区函数，保证 register_workers > 1 时账号被均分到 N 个独立浏览器线程。
纯逻辑测试：不启动浏览器、不联网。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import grok_register_ttk as g  # noqa: E402


def _assert_partition(count, workers, expected):
    result = g.plan_worker_partition(count, workers)
    assert result == expected, f"plan({count},{workers}) = {result}, want {expected}"
    assert sum(result) == count


# ------------------------------------------------------------ 分区
def test_partition_single_worker_serial():
    _assert_partition(10, 1, [10])


def test_partition_even_split():
    _assert_partition(8, 4, [2, 2, 2, 2])


def test_partition_remainder_goes_to_front():
    _assert_partition(10, 3, [4, 3, 3])


def test_partition_two_workers_odd_count():
    _assert_partition(7, 2, [4, 3])


def test_partition_workers_more_than_count_caps_to_count():
    _assert_partition(5, 8, [1, 1, 1, 1, 1])


def test_partition_worker_zero_or_none_falls_back_to_one():
    assert g.plan_worker_partition(7, 0) == [7]
    assert g.plan_worker_partition(7, None) == [7]
    assert g.plan_worker_partition(7, "") == [7]


def test_partition_non_numeric_workers_falls_back_to_one():
    assert g.plan_worker_partition(7, "abc") == [7]


def test_partition_zero_or_negative_count_returns_empty():
    assert g.plan_worker_partition(0, 4) == []
    assert g.plan_worker_partition(-3, 4) == []


def test_partition_hard_cap_max_workers():
    chunks = g.plan_worker_partition(100, 1000, max_workers=24)
    assert len(chunks) == 24
    assert sum(chunks) == 100


def test_partition_preserves_total_and_all_positive():
    for count, workers in [(13, 5), (31, 7), (99, 24), (6, 6)]:
        chunks = g.plan_worker_partition(count, workers)
        assert sum(chunks) == count
        assert all(n > 0 for n in chunks)


# ------------------------------------------------------- worker_start_stagger
def test_worker_start_stagger_default_and_config():
    _saved = dict(g.config)
    try:
        g.config.pop("worker_start_stagger", None)
        assert g.worker_start_stagger() == 2.0
        g.config["worker_start_stagger"] = 0.5
        assert g.worker_start_stagger() == 0.5
        g.config["worker_start_stagger"] = 0
        assert g.worker_start_stagger() == 0.0
        g.config["worker_start_stagger"] = "abc"
        assert g.worker_start_stagger() == 2.0
        g.config["worker_start_stagger"] = -1
        assert g.worker_start_stagger() == 2.0
    finally:
        g.config.clear()
        g.config.update(_saved)


if __name__ == "__main__":
    for fn in list(globals()):
        if fn.startswith("test_"):
            globals()[fn]()
    print("OK test_worker_partition")
