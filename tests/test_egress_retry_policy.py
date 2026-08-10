# -*- coding: utf-8 -*-
"""Unit tests for the IPv6 pool egress retry policy (browser_session).

IPv6 pool egress intermittently resets mid-flight; the browser reports it as
NS_ERROR_NET_RESET / ERR_CONNECTION_CLOSED during signup page load. These are
transient transport errors worth retrying with a short backoff; other errors
are not retried (they fail fast so the caller can swap proxy / surface the
real failure).
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_session import egress_retry_policy  # noqa: E402


class EgressRetryPolicyTests(unittest.TestCase):
    def test_net_reset_is_retried_with_backoff(self):
        """NS_ERROR_NET_RESET is a transient egress reset -> retry, short backoff."""
        should_retry, backoff = egress_retry_policy(
            Exception("NS_ERROR_NET_RESET"), attempt=1, base_retries=3, backoff_sec=1.5
        )
        self.assertTrue(should_retry)
        self.assertEqual(backoff, 1.5)

    def test_connection_closed_is_retried(self):
        """ERR_CONNECTION_CLOSED is a transient egress reset -> retry."""
        should_retry, _ = egress_retry_policy(
            Exception("ERR_CONNECTION_CLOSED"), attempt=1, base_retries=3, backoff_sec=1.0
        )
        self.assertTrue(should_retry)

    def test_connection_reset_variant_is_retried(self):
        """ERR_CONNECTION_RESET (Chromium spelling) also counts as egress reset."""
        should_retry, _ = egress_retry_policy(
            Exception("ERR_CONNECTION_RESET"), attempt=1, base_retries=3, backoff_sec=1.0
        )
        self.assertTrue(should_retry)

    def test_backoff_grows_with_attempt(self):
        """Backoff scales with the attempt number (short but not fixed)."""
        _, backoff_1 = egress_retry_policy(
            Exception("NS_ERROR_NET_RESET"), attempt=1, base_retries=4, backoff_sec=1.5
        )
        _, backoff_2 = egress_retry_policy(
            Exception("NS_ERROR_NET_RESET"), attempt=2, base_retries=4, backoff_sec=1.5
        )
        self.assertEqual(backoff_1, 1.5)
        self.assertEqual(backoff_2, 3.0)

    def test_retries_exhausted_stops(self):
        """Once attempt reaches base_retries, stop retrying."""
        should_retry, _ = egress_retry_policy(
            Exception("NS_ERROR_NET_RESET"), attempt=3, base_retries=3, backoff_sec=1.0
        )
        self.assertFalse(should_retry)

    def test_non_transport_error_is_not_retried(self):
        """A non-transport error (e.g. page/turnstile failure) fails fast."""
        should_retry, _ = egress_retry_policy(
            Exception("Detected headless browser, access denied"),
            attempt=1,
            base_retries=3,
            backoff_sec=1.0,
        )
        self.assertFalse(should_retry)

    def test_none_error_is_conservative_no_retry(self):
        """Unknown/empty error info must not loop retries blindly."""
        should_retry, _ = egress_retry_policy(None, attempt=1, base_retries=3, backoff_sec=1.0)
        self.assertFalse(should_retry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
