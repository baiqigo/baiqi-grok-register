# -*- coding: utf-8 -*-
"""Connectivity seam S7: non-destructive outlook pool preflight in check_email_api.

No network, no IMAP — only local pool/used file inspection with temp files.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import connectivity  # noqa: E402


def _noop(*_args, **_kwargs):
    return None


def test_check_email_api_outlook_ok_with_valid_pool(tmp_path):
    pool = tmp_path / "pool.txt"
    pool.write_text(
        "# comment\n"
        "a@outlook.com----p1----rt1\n"
        "b@outlook.com----p2----rt2----cid2\n",
        encoding="utf-8",
    )
    name, ok, detail = connectivity.check_email_api(
        "outlook",
        {"outlook_pool_file": str(pool)},
        _noop,
        _noop,
    )
    assert name == "邮箱API"
    assert ok is True
    assert "Outlook 池可用" in detail
    assert "2" in detail


def test_check_email_api_outlook_missing_config_field():
    name, ok, detail = connectivity.check_email_api("outlook", {}, _noop, _noop)
    assert ok is False
    assert "未配置 outlook_pool_file" in detail


def test_check_email_api_outlook_pool_file_missing(tmp_path):
    name, ok, detail = connectivity.check_email_api(
        "outlook",
        {"outlook_pool_file": str(tmp_path / "nope.txt")},
        _noop,
        _noop,
    )
    assert ok is False
    assert "池文件不存在" in detail


def test_check_email_api_outlook_empty_pool(tmp_path):
    pool = tmp_path / "pool.txt"
    pool.write_text("# nothing here\n", encoding="utf-8")
    name, ok, detail = connectivity.check_email_api(
        "outlook",
        {"outlook_pool_file": str(pool)},
        _noop,
        _noop,
    )
    assert ok is False
    assert "池为空" in detail


def test_check_email_api_outlook_bad_first_line(tmp_path):
    pool = tmp_path / "pool.txt"
    pool.write_text("this is not a pool line\n", encoding="utf-8")
    name, ok, detail = connectivity.check_email_api(
        "outlook",
        {"outlook_pool_file": str(pool)},
        _noop,
        _noop,
    )
    assert ok is False
    assert "email----password----refresh_token----client_id" in detail


if __name__ == "__main__":
    import tempfile

    def _run(tmp):
        test_check_email_api_outlook_ok_with_valid_pool(tmp)
        test_check_email_api_outlook_missing_config_field()
        test_check_email_api_outlook_pool_file_missing(tmp)
        test_check_email_api_outlook_empty_pool(tmp)
        test_check_email_api_outlook_bad_first_line(tmp)

    with tempfile.TemporaryDirectory() as td:
        _run(Path(td))
    print("OK test_outlook_connectivity")