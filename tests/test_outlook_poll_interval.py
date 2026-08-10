# -*- coding: utf-8 -*-
"""OTP 轮询间隔 config 化纯逻辑：resolve_outlook_poll_interval。

outlook_poll_interval（默认 3s，可调小）在 get_oai_code 分发电平生效，
覆盖所有邮箱提供商的 OTP 轮询。纯逻辑测试：不联网、不启动浏览器。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import grok_register_ttk as g  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_config():
    saved = dict(g.config)
    yield
    g.config.clear()
    g.config.update(saved)


# ------------------------------------------------------------ 纯逻辑
def test_poll_interval_default_3_when_config_unset():
    assert g.resolve_outlook_poll_interval() == 3.0
    assert g.resolve_outlook_poll_interval(3) == 3.0


def test_poll_interval_reads_config_when_default_used():
    g.config["outlook_poll_interval"] = 1
    assert g.resolve_outlook_poll_interval() == 1.0
    assert g.resolve_outlook_poll_interval(3) == 1.0


def test_poll_interval_explicit_non_default_wins_over_config():
    g.config["outlook_poll_interval"] = 1
    assert g.resolve_outlook_poll_interval(5) == 5.0


def test_poll_interval_invalid_config_falls_back_to_default():
    for bad in ("abc", -2, 0, None):
        g.config["outlook_poll_interval"] = bad
        assert g.resolve_outlook_poll_interval() == 3.0, bad


def test_poll_interval_negative_current_falls_back():
    assert g.resolve_outlook_poll_interval(-1) == 3.0


# ------------------------------------------------------------ 接线
def test_get_oai_code_applies_config_poll_interval(monkeypatch):
    g.config["email_provider"] = "outlook"
    g.config["outlook_poll_interval"] = 1
    captured = {}

    def fake_wait(email, refresh="", client_id="", config=None, **kwargs):
        captured.update(kwargs=kwargs)
        return "ABC"

    monkeypatch.setattr(g.outlook_provider, "wait_code", fake_wait)
    assert g.get_oai_code("tok", "someone@outlook.com") == "ABC"
    assert captured["kwargs"]["poll_interval"] == 1.0


def test_get_oai_code_default_stays_3_when_config_unset(monkeypatch):
    g.config["email_provider"] = "outlook"
    captured = {}

    def fake_wait(email, refresh="", client_id="", config=None, **kwargs):
        captured.update(kwargs=kwargs)
        return "ABC"

    monkeypatch.setattr(g.outlook_provider, "wait_code", fake_wait)
    assert g.get_oai_code("tok", "someone@outlook.com") == "ABC"
    assert captured["kwargs"]["poll_interval"] == 3.0


if __name__ == "__main__":
    for fn in list(globals()):
        if fn.startswith("test_"):
            globals()[fn]()
    print("OK test_outlook_poll_interval")
