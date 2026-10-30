"""Proxy sai định dạng không được làm sập bảng tài khoản."""

import unittest

from automuse.accounts import ProxyConfig


class TestProxyParse(unittest.TestCase):
    def test_empty_is_invalid_but_safe(self):
        cfg = ProxyConfig.parse("")
        self.assertFalse(cfg.is_valid())
        self.assertEqual(cfg.masked(), "-")

    def test_valid_proxy(self):
        cfg = ProxyConfig.parse("http://user:pass@127.0.0.1:8080")
        self.assertTrue(cfg.is_valid())
        self.assertEqual(cfg.masked(), "http://user:***@127.0.0.1:8080")

    def test_host_port_without_scheme(self):
        cfg = ProxyConfig.parse("127.0.0.1:8080")
        self.assertTrue(cfg.is_valid())
        self.assertEqual(cfg.masked(), "http://127.0.0.1:8080")

    def test_non_numeric_port_is_invalid_not_exception(self):
        # Trước đây urlparse().port ném ValueError, làm sập /api/accounts.
        cfg = ProxyConfig.parse("host:abc")
        self.assertFalse(cfg.is_valid())
        self.assertEqual(cfg.masked(), "-")

    def test_out_of_range_port_is_invalid_not_exception(self):
        cfg = ProxyConfig.parse("host:99999")
        self.assertFalse(cfg.is_valid())
        self.assertEqual(cfg.masked(), "-")


if __name__ == "__main__":
    unittest.main()
