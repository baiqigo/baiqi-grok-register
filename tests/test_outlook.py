# -*- coding: utf-8 -*-
"""Outlook provider seams S1-S3: make_alias, parse_pool_line/load_pool, create_mailbox.

All tests are pure-logic / temp-file based. No IMAP, no network, no real pool data.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from email_providers import outlook  # noqa: E402


def _reset_state():
    outlook._POOL = []
    outlook._CURSOR = 0
    outlook._USED.clear()
    outlook._DEAD.clear()
    outlook._SESSIONS.clear()


# ---------------------------------------------------------------- S1: make_alias
def test_make_alias_alias_mode_returns_plus_alias():
    _reset_state()
    email = outlook.make_alias("fiona@outlook.com", {"outlook_use_alias": True})
    local, sep, domain = email.partition("@")
    assert sep and domain == "outlook.com"
    root, plus, tag = local.partition("+")
    assert root == "fiona"
    assert plus == "+"
    assert tag.startswith("g") and len(tag) == 11  # 'g' + 10 hex chars


def test_make_alias_alias_mode_is_unique_per_call():
    _reset_state()
    cfg = {"outlook_use_alias": True}
    a = outlook.make_alias("fiona@outlook.com", cfg)
    b = outlook.make_alias("fiona@outlook.com", cfg)
    assert a != b


def test_make_alias_use_alias_false_returns_base():
    _reset_state()
    assert (
        outlook.make_alias("fiona@outlook.com", {"outlook_use_alias": False})
        == "fiona@outlook.com"
    )


def test_make_alias_defaults_to_alias_mode_when_config_empty():
    _reset_state()
    email = outlook.make_alias("fiona@outlook.com", {})
    assert "+" in email


def test_make_alias_invalid_base_raises():
    _reset_state()
    try:
        outlook.make_alias("not-an-email", {"outlook_use_alias": True})
    except ValueError:
        return
    raise AssertionError("expected ValueError for invalid base mailbox")


# ------------------------------------------------------- S2: parse_pool_line/load_pool
def test_parse_pool_line_4_segments():
    row = outlook.parse_pool_line("user@outlook.com----pw----rt123----cid456")
    assert row == {
        "email": "user@outlook.com",
        "password": "pw",
        "refresh_token": "rt123",
        "client_id": "cid456",
    }


def test_parse_pool_line_3_segments_defaults_client_id():
    row = outlook.parse_pool_line("user@outlook.com----pw----rt123")
    assert row["email"] == "user@outlook.com"
    assert row["refresh_token"] == "rt123"
    assert row["client_id"] == outlook.DEFAULT_CLIENT_ID


def test_parse_pool_line_colon_separator_accepted():
    row = outlook.parse_pool_line("user@outlook.com:pw:rt123:cid456")
    assert row["refresh_token"] == "rt123"
    assert row["client_id"] == "cid456"


def test_parse_pool_line_bad_lines_return_none():
    for bad in ("", "   ", "# comment", "no-separator", "a@b", "a@b----pw"):
        assert outlook.parse_pool_line(bad) is None, bad


def test_load_pool_mixed_lines(tmp_path):
    pool = tmp_path / "pool.txt"
    pool.write_text(
        "# header comment\n"
        "a@outlook.com----p1----r1\n"
        "bad line\n"
        "b@outlook.com----p2----r2----cid2\n",
        encoding="utf-8",
    )
    rows = outlook.load_pool(str(pool))
    assert [r["email"] for r in rows] == ["a@outlook.com", "b@outlook.com"]
    assert rows[0]["client_id"] == outlook.DEFAULT_CLIENT_ID
    assert rows[1]["client_id"] == "cid2"


def test_load_pool_missing_file_raises(tmp_path):
    try:
        outlook.load_pool(str(tmp_path / "nope.txt"))
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError for missing pool file")


# ------------------------------------------------------- S3: create_mailbox
def test_create_mailbox_picks_unused_base_alias_and_marks_used(tmp_path):
    _reset_state()
    pool = tmp_path / "pool.txt"
    pool.write_text(
        "aaa@outlook.com----p1----rt1----cid1\n"
        "bbb@outlook.com----p2----rt2\n",
        encoding="utf-8",
    )
    used = tmp_path / "used.txt"
    dead = tmp_path / "dead.txt"
    used.write_text("aaa+gdeadbeef@outlook.com\n", encoding="utf-8")
    config = {
        "outlook_pool_file": str(pool),
        "outlook_used_file": str(used),
        "outlook_dead_file": str(dead),
        "outlook_mark_used": True,
        "outlook_use_alias": True,
    }
    email, token = outlook.create_mailbox(config)

    # base aaa is skipped because a used alias carries its prefix; bbb is picked.
    assert email.split("@")[0].startswith("bbb+")
    assert email.endswith("@outlook.com")
    # non-sensitive bearer, matches nosms design (not a raw refresh token)
    assert token == f"outlook:{email}"

    # used file records the alias
    used_lines = used.read_text(encoding="utf-8").splitlines()
    assert email in used_lines

    # session remembered with refresh token + client id + mailbox base
    session = outlook.get_session(email)
    assert session is not None
    assert session["refresh_token"] == "rt2"
    assert session["client_id"] == outlook.DEFAULT_CLIENT_ID
    assert session["mailbox_email"] == "bbb@outlook.com"


def test_create_mailbox_non_alias_returns_base(tmp_path):
    _reset_state()
    pool = tmp_path / "pool.txt"
    pool.write_text("ccc@outlook.com----p----rt\n", encoding="utf-8")
    used = tmp_path / "used.txt"
    dead = tmp_path / "dead.txt"
    config = {
        "outlook_pool_file": str(pool),
        "outlook_used_file": str(used),
        "outlook_dead_file": str(dead),
        "outlook_mark_used": True,
        "outlook_use_alias": False,
    }
    email, _token = outlook.create_mailbox(config)
    assert email == "ccc@outlook.com"


def test_create_mailbox_pool_exhausted_raises(tmp_path):
    _reset_state()
    dead = tmp_path / "dead.txt"
    used = tmp_path / "used.txt"
    pool = tmp_path / "pool.txt"
    pool.write_text("zzz@outlook.com----p----rt\n", encoding="utf-8")
    dead.write_text("zzz@outlook.com\n", encoding="utf-8")
    config = {
        "outlook_pool_file": str(pool),
        "outlook_used_file": str(used),
        "outlook_dead_file": str(dead),
        "outlook_mark_used": True,
        "outlook_use_alias": True,
    }
    try:
        outlook.create_mailbox(config)
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError when all bases dead")


# ------------------------------------------------- S3b: 失败计数/永久禁用（黑名单）
def test_fail_file_default_path():
    assert outlook.fail_file_path({}).name == "emails_fail_outlook.txt"
    assert outlook.fail_file_path({"outlook_fail_file": "x.txt"}).name == "x.txt"


def test_fail_threshold_default_and_override():
    assert outlook._fail_threshold({}) == 3
    assert outlook._fail_threshold({"outlook_fail_threshold": "5"}) == 5
    assert outlook._fail_threshold({"outlook_fail_threshold": "0"}) == 1  # 下限 1


def test_record_fail_accumulates_across_calls(tmp_path):
    _reset_state()
    cfg = {"outlook_fail_file": str(tmp_path / "fail.txt")}
    assert outlook.record_fail(cfg, "base@outlook.com") == 1
    assert outlook.record_fail(cfg, "base@outlook.com") == 2
    assert outlook.record_fail(cfg, "other@outlook.com") == 1
    counts = outlook.load_fail_counts(cfg)
    assert counts["base@outlook.com"] == 2
    assert counts["other@outlook.com"] == 1


def test_record_fail_reaches_threshold_then_retire(tmp_path):
    # base 累计失败达阈值(3) → mark_dead 永久禁用（写入 dead 文件）。
    _reset_state()
    pool = tmp_path / "pool.txt"
    pool.write_text("waste@outlook.com----p----rt\n", encoding="utf-8")
    dead = tmp_path / "dead.txt"
    used = tmp_path / "used.txt"
    cfg = {
        "outlook_pool_file": str(pool),
        "outlook_used_file": str(used),
        "outlook_dead_file": str(dead),
        "outlook_fail_file": str(tmp_path / "fail.txt"),
        "outlook_fail_threshold": "3",
        "outlook_mark_used": True,
    }
    # 前 2 次超时只累计，不禁用
    for _ in range(2):
        outlook.record_fail(cfg, "waste@outlook.com")
    assert outlook.record_fail(cfg, "waste@outlook.com") == 3
    outlook._retire_mailbox(cfg, "waste@outlook.com", "repeated_otp_timeout")
    dead_lines = dead.read_text(encoding="utf-8").splitlines()
    assert "waste@outlook.com" in dead_lines
    # create_mailbox 不再选中它
    try:
        outlook.create_mailbox(cfg)
    except RuntimeError:
        pass  # 池被禁后为空，符合预期
    else:
        raise AssertionError("disabled base should not be re-picked")


if __name__ == "__main__":
    test_make_alias_alias_mode_returns_plus_alias()
    test_make_alias_alias_mode_is_unique_per_call()
    test_make_alias_use_alias_false_returns_base()
    test_make_alias_defaults_to_alias_mode_when_config_empty()
    test_make_alias_invalid_base_raises()
    test_parse_pool_line_4_segments()
    test_parse_pool_line_3_segments_defaults_client_id()
    test_parse_pool_line_colon_separator_accepted()
    test_parse_pool_line_bad_lines_return_none()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        test_load_pool_mixed_lines(td)
        test_load_pool_missing_file_raises(td)
        test_create_mailbox_picks_unused_base_alias_and_marks_used(td)
        test_create_mailbox_non_alias_returns_base(td)
        test_create_mailbox_pool_exhausted_raises(td)
        test_fail_file_default_path()
        test_fail_threshold_default_and_override()
        test_record_fail_accumulates_across_calls(td)
        test_record_fail_reaches_threshold_then_retire(td)
    print("OK test_outlook")