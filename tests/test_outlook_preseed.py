# -*- coding: utf-8 -*-
"""Regression test for the OTP pre-seed swallow bug (preseed-race inverted).

The bug: a freshly-arriving xAI confirmation code carries a Date header ~1-2s
EARLIER than `submitted_at` (xAI generates it at the click instant; the caller
captures submitted_at a beat later after debug I/O). The old `received >=
submitted_at` pre-seed filter treated that fresh code as "history" and swallowed
it -> OTP silently missed -> 180s timeout on every mailbox.

Fix: only swallow messages strictly older than submitted_at - margin.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_providers import outlook  # noqa: E402


class PreseedShouldSkipTests(unittest.TestCase):
    def test_fresh_code_a_second_before_submit_is_kept(self):
        """The exact failure: OTP Date ~2s before submitted_at must NOT be swallowed."""
        submitted = 1_000_000.0
        otp_ts = submitted - 2.0  # xAI generated it ~2s before we captured submitted_at
        self.assertFalse(
            outlook._preseed_should_skip(otp_ts, submitted, 60.0),
            "fresh OTP must stay a candidate, not be swallowed",
        )

    def test_fresh_code_at_boundary_is_kept(self):
        """OTP exactly at submitted_at - margin is still kept (>= semantics)."""
        self.assertFalse(outlook._preseed_should_skip(1000.0 - 60.0, 1000.0, 60.0))

    def test_future_code_is_kept(self):
        """Code arriving after submitted_at is a candidate."""
        self.assertFalse(outlook._preseed_should_skip(1000.0 + 5.0, 1000.0, 60.0))

    def test_old_history_is_swallowed(self):
        """A previous signup's code (minutes/hours old) is swallowed."""
        self.assertTrue(outlook._preseed_should_skip(1000.0 - 600.0, 1000.0, 60.0))

    def test_just_outside_margin_is_swallowed(self):
        """Code older than the margin is history."""
        self.assertTrue(outlook._preseed_should_skip(1000.0 - 61.0, 1000.0, 60.0))

    def test_no_margin_swallows_fresh_code(self):
        """With margin=0 the old buggy behavior returns (fresh 2s-old code swallowed)."""
        self.assertTrue(outlook._preseed_should_skip(1000.0 - 2.0, 1000.0, 0.0))

    def test_garbage_input_is_conservative_keep(self):
        """Bad timestamp should not swallow a possible code (leave for poll)."""
        self.assertFalse(outlook._preseed_should_skip(None, 1000.0, 60.0))
        self.assertFalse(outlook._preseed_should_skip("not-a-number", 1000.0, 60.0))


class WaitCodePreseedBehaviorTests(unittest.TestCase):
    """Behavioral check of wait_code's pre-seed guard (no real IMAP/network).

    The pre-seed fetch returns a mailbox message carrying an xAI code whose Date
    header is ~2s EARLIER than submitted_at (the preseed-race). With a positive
    margin the fresh code must survive pre-seed and be returned by the poll loop;
    with margin=0 the old buggy behavior swallows it -> TimeoutError.
    """

    MAILBOX = "base@outlook.com"

    def _fresh_code_msg(self, received_ts: float) -> dict:
        import email.utils

        return {
            "subject": "Your verification code",
            "body": "Your xAI code is ABC-123.",
            "from": "noreply@x.ai",
            "received": email.utils.formatdate(received_ts, usegmt=True),
        }

    def _session_and_side_effects(self):
        import contextlib
        from unittest.mock import patch

        outlook.remember_session(
            self.MAILBOX,
            {
                "mailbox_email": self.MAILBOX,
                "refresh_token": "fake-refresh",
                "client_id": "fake-client",
                "email": self.MAILBOX,
            },
        )
        stack = contextlib.ExitStack()
        stack.enter_context(
            patch.object(outlook, "refresh_access_token", return_value="fake-token")
        )
        stack.enter_context(patch.object(outlook, "mark_used", return_value=None))
        stack.enter_context(patch.object(outlook, "record_fail", return_value=0))
        stack.enter_context(patch.object(outlook, "_retire_mailbox", return_value=None))
        stack.enter_context(patch.object(outlook, "_purge_mailbox", return_value=True))
        return stack

    def test_fresh_code_within_margin_survives_preseed(self):
        """OTP Date 2s before submitted_at (within 60s margin) is returned, not swallowed."""
        import time

        submitted_at = time.time()
        msg = self._fresh_code_msg(submitted_at - 2.0)

        def _fake_fetch(*args, **kwargs):
            return [msg], ""

        with self._session_and_side_effects():
            with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                outlook, "fetch_messages_imap", side_effect=_fake_fetch
            ):
                code = outlook.wait_code(
                    self.MAILBOX,
                    config={
                        "outlook_use_alias": True,
                        "outlook_mark_used": False,
                        "outlook_preseed_margin_sec": 60,
                    },
                    timeout=30,
                    poll_interval=0.05,
                    submitted_at=submitted_at,
                )
        self.assertEqual(code, "ABC-123")

    def test_fresh_code_outside_small_margin_is_swallowed(self):
        """A 2s-earlier OTP with a tight 1s margin is outside the window -> swallowed
        -> OTP never surfaces -> TimeoutError (the preseed-race failure mode)."""
        from unittest.mock import patch

        class _FakeTime:
            def __init__(self):
                self.now = 1_000_000.0

            def time(self):
                return self.now

            def sleep(self, seconds):
                self.now += max(0.0, seconds)

        fake = _FakeTime()
        submitted_at = fake.now
        msg = self._fresh_code_msg(submitted_at - 2.0)

        def _fake_fetch(*args, **kwargs):
            return [msg], ""

        with self._session_and_side_effects():
            with patch.object(outlook, "time", fake):
                with patch.object(
                    outlook, "fetch_messages_imap", side_effect=_fake_fetch
                ):
                    with self.assertRaises(TimeoutError):
                        outlook.wait_code(
                            self.MAILBOX,
                            config={
                                "outlook_use_alias": True,
                                "outlook_mark_used": False,
                                "outlook_preseed_margin_sec": 1,
                            },
                            timeout=30,
                            poll_interval=3,
                            submitted_at=submitted_at,
                        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
