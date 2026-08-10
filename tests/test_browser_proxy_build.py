# -*- coding: utf-8 -*-
"""Unit tests for _build_camoufox_proxy IPv6-literal handling (browser_session).

The mihomo IPv6 pool egress is reached on this host via the IPv6 loopback
(`http://[::1]:17890`); the old build dropped the IPv6 brackets and emitted an
invalid `http://::1:17890` proxy URL. Regression-lock the bracket-preserving fix.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_session import _build_camoufox_proxy  # noqa: E402


class BuildCamoufoxProxyTests(unittest.TestCase):
    def test_ipv4_http(self):
        """Plain IPv4 proxy keeps working unchanged."""
        self.assertEqual(
            _build_camoufox_proxy("http://127.0.0.1:17890"),
            {"server": "http://127.0.0.1:17890"},
        )

    def test_ipv6_literal_keeps_brackets(self):
        """IPv6 loopback proxy must keep its brackets (`http://[::1]:17890`)."""
        self.assertEqual(
            _build_camoufox_proxy("http://[::1]:17890"),
            {"server": "http://[::1]:17890"},
        )

    def test_ipv6_with_credentials(self):
        """IPv6 + user:pass splits into server/username/password."""
        got = _build_camoufox_proxy("http://u:p@[::1]:17890")
        self.assertEqual(got["server"], "http://[::1]:17890")
        self.assertEqual(got["username"], "u")
        self.assertEqual(got["password"], "p")

    def test_empty_string_returns_empty_dict(self):
        """Empty proxy string -> no proxy settings."""
        self.assertEqual(_build_camoufox_proxy(""), {})

    def test_none_host_scheme_falls_back_to_raw(self):
        """Non-URL proxy string is passed through untouched."""
        self.assertEqual(_build_camoufox_proxy("socks5://127.0.0.1:1080"), {"server": "socks5://127.0.0.1:1080"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
