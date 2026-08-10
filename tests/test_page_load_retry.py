# -*- coding: utf-8 -*-
"""Unit tests for the signup-page load transient-error classifier (browser_session).

The IPv6 pool egress is intermittent: the browser fails page.goto to
accounts.x.ai with NS_ERROR_UNKNOWN_HOST / NS_ERROR_NET_RESET etc. These are
transient pool failures worth retrying the page open (pre-email), so the run
loop can ride out the flakiness instead of failing the whole account.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_session import is_transient_page_load_error  # noqa: E402


class IsTransientPageLoadErrorTests(unittest.TestCase):
    def test_dns_unknown_host_is_transient(self):
        """accounts.x.ai DNS blip via the pool -> retryable."""
        self.assertTrue(is_transient_page_load_error("Page.goto: NS_ERROR_UNKNOWN_HOST"))

    def test_net_reset_is_transient(self):
        self.assertTrue(is_transient_page_load_error("打开注册页失败: NS_ERROR_NET_RESET"))

    def test_connection_closed_is_transient(self):
        self.assertTrue(is_transient_page_load_error("ERR_CONNECTION_CLOSED"))

    def test_connection_reset_is_transient(self):
        self.assertTrue(is_transient_page_load_error("ERR_CONNECTION_RESET"))

    def test_empty_response_is_transient(self):
        self.assertTrue(is_transient_page_load_error("ERR_EMPTY_RESPONSE"))

    def test_socket_not_connected_is_transient(self):
        self.assertTrue(is_transient_page_load_error("ERR_SOCKET_NOT_CONNECTED"))

    def test_net_timeout_is_transient(self):
        self.assertTrue(is_transient_page_load_error("NS_ERROR_NET_TIMEOUT"))

    def test_name_not_resolved_is_transient(self):
        self.assertTrue(is_transient_page_load_error("ERR_NAME_NOT_RESOLVED"))

    def test_unrelated_error_is_not_transient(self):
        """Business logic errors (domain rejected, CF blocked) must fail fast."""
        self.assertFalse(is_transient_page_load_error("EmailDomainRejected: domain not allowed"))

    def test_empty_and_none_are_not_transient(self):
        self.assertFalse(is_transient_page_load_error(""))
        self.assertFalse(is_transient_page_load_error(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
