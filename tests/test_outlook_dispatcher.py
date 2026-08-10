# -*- coding: utf-8 -*-
"""Dispatcher seams S4-S5: grok_register_ttk wiring for the outlook provider.

S4: get_email_and_token (provider=outlook) returns create_mailbox's (email, token).
S5: get_oai_code (provider=outlook) delegates email + dev_token to wait_code.

Pure delegation tests: no browser, no IMAP, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import grok_register_ttk as g  # noqa: E402
from email_providers import outlook  # noqa: E402


@pytest.fixture(autouse=True)
def _fail_fast_http(monkeypatch):
    """Any accidental HTTP fall-through (e.g. duckmail default) must fail fast."""
    def _boom(*_args, **_kwargs):
        raise AssertionError("unexpected network call in outlook dispatcher test")

    monkeypatch.setattr(g, "http_get", _boom)
    monkeypatch.setattr(g, "http_post", _boom)


def _reset_provider(provider="outlook"):
    g.config["email_provider"] = provider


# ------------------------------------------------------------- S4
def test_get_email_and_token_delegates_to_outlook_create_mailbox(monkeypatch):
    _reset_provider("outlook")
    captured = {}

    def fake_create(config):
        captured["config"] = config
        return ("fiona+gtag@outlook.com", "outlook:fiona+gtag@outlook.com")

    monkeypatch.setattr(outlook, "create_mailbox", fake_create)
    email, token = g.get_email_and_token()

    assert email == "fiona+gtag@outlook.com"
    assert token == "outlook:fiona+gtag@outlook.com"
    # the panel config dict is handed to the provider module
    assert captured["config"] is g.config


# ------------------------------------------------------------- S5
def test_get_oai_code_delegates_to_outlook_wait_code(monkeypatch):
    _reset_provider("outlook")
    captured = {}

    def fake_wait(
        email,
        refresh="",
        client_id="",
        config=None,
        *,
        timeout=180,
        poll_interval=3,
        log_callback=None,
        cancel_callback=None,
        resend_callback=None,
        submitted_at=None,
    ):
        captured.update(
            email=email,
            refresh=refresh,
            client_id=client_id,
            config=config,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
            submitted_at=submitted_at,
        )
        return "ABC-DEF"

    monkeypatch.setattr(outlook, "wait_code", fake_wait)
    log_cb = lambda m: None
    cancel_cb = lambda: False
    resend_cb = lambda: None

    code = g.get_oai_code(
        "outlook:fiona+gtag@outlook.com",
        "fiona+gtag@outlook.com",
        timeout=90,
        poll_interval=5,
        log_callback=log_cb,
        cancel_callback=cancel_cb,
        resend_callback=resend_cb,
    )

    assert code == "ABC-DEF"
    assert captured["email"] == "fiona+gtag@outlook.com"
    assert captured["refresh"] == "outlook:fiona+gtag@outlook.com"
    assert captured["config"] is g.config
    assert captured["timeout"] == 90
    assert captured["poll_interval"] == 5
    assert captured["log_callback"] is log_cb
    assert captured["cancel_callback"] is cancel_cb
    assert captured["resend_callback"] is resend_cb


def test_get_oai_code_default_timeout_passthrough(monkeypatch):
    _reset_provider("outlook")
    captured = {}

    def fake_wait(email, refresh="", client_id="", config=None, **kwargs):
        captured.update(email=email, refresh=refresh, config=config, kwargs=kwargs)
        return "AAA-BBB"

    monkeypatch.setattr(outlook, "wait_code", fake_wait)
    code = g.get_oai_code("tok", "someone@outlook.com")
    assert code == "AAA-BBB"
    assert captured["kwargs"]["timeout"] == 180
    assert captured["kwargs"]["poll_interval"] == 3


if __name__ == "__main__":
    from unittest.mock import patch

    class _Noop:
        def setattr(self, *_args):
            return None

    _reset_provider("outlook")
    with patch.object(
        outlook, "create_mailbox", return_value=("fiona+gtag@outlook.com", "outlook:fiona+gtag@outlook.com")
    ):
        email, token = g.get_email_and_token()
        assert email == "fiona+gtag@outlook.com"
        assert token == "outlook:fiona+gtag@outlook.com"
    with patch.object(outlook, "wait_code", return_value="ABC-DEF"):
        code = g.get_oai_code("tok", "someone@outlook.com")
        assert code == "ABC-DEF"
    print("OK test_outlook_dispatcher")