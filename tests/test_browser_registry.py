# -*- coding: utf-8 -*-
"""Unit tests for the global live-browser registry + force-close helpers.

10 并发 batch 下 worker 崩溃/卡死时，主线程必须能强制关闭其浏览器，否则
Camoufox/Firefox 孤儿进程堆积。这些测试锁定注册/注销/按线程/全量清理逻辑。
"""
from __future__ import annotations

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_session import (  # noqa: E402
    _LIVE_BROWSERS,
    force_close_all_browsers,
    force_close_browser_for_thread,
    set_browser_session,
)


class FakeBrowser:
    def __init__(self):
        self.quit_called = 0

    def quit(self, del_data=True):
        self.quit_called += 1


class LiveBrowserRegistryTests(unittest.TestCase):
    def setUp(self):
        force_close_all_browsers()  # 清空注册表，隔离用例

    def test_register_tracks_browser(self):
        b = FakeBrowser()
        set_browser_session(b)
        self.assertEqual(len(_LIVE_BROWSERS), 1)
        self.assertIs(_LIVE_BROWSERS[0][0], b)

    def test_register_dedupes_same_object(self):
        b = FakeBrowser()
        set_browser_session(b)
        set_browser_session(b)
        self.assertEqual(len(_LIVE_BROWSERS), 1)

    def test_force_close_all_quits_and_clears(self):
        b1, b2 = FakeBrowser(), FakeBrowser()
        set_browser_session(b1)
        set_browser_session(b2)
        self.assertEqual(force_close_all_browsers(), 2)
        self.assertEqual(b1.quit_called, 1)
        self.assertEqual(b2.quit_called, 1)
        self.assertEqual(len(_LIVE_BROWSERS), 0)

    def test_force_close_by_thread_only_hits_that_thread(self):
        b1 = FakeBrowser()
        b2 = FakeBrowser()
        set_browser_session(b1)  # 本线程 tid
        # 用另一个"伪线程 tid"注册 b2（模拟另一 worker）
        other_tid = threading.get_ident() + 999999
        _LIVE_BROWSERS.append((b2, other_tid))
        n = force_close_browser_for_thread(other_tid)
        self.assertEqual(n, 1)
        self.assertEqual(b2.quit_called, 1)
        self.assertEqual(b1.quit_called, 0)  # 本线程的浏览器不动
        # b1 仍在注册表
        self.assertEqual(len(_LIVE_BROWSERS), 1)
        self.assertIs(_LIVE_BROWSERS[0][0], b1)

    def test_force_close_all_after_individual_close_is_idempotent(self):
        b = FakeBrowser()
        set_browser_session(b)
        force_close_browser_for_thread(threading.get_ident())
        self.assertEqual(len(_LIVE_BROWSERS), 0)
        self.assertEqual(b.quit_called, 1)
        self.assertEqual(force_close_all_browsers(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
