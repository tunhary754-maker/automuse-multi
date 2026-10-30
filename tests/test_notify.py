"""Test thông báo Telegram (Bot API) — urlopen được mock, không gọi thật."""

import io
import json
import unittest
from unittest import mock

from automuse.notify import (check_bot_token, send_telegram_to,
                             telegram_configured)


class _FakeResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _urlopen_ok(url, timeout=None):
    return _FakeResp({"ok": True, "result": {"username": "automuse_bot"}})


def _urlopen_fail(url, timeout=None):
    return _FakeResp({"ok": False, "description": "Unauthorized"})


class TestSendTelegram(unittest.TestCase):
    def test_send_ok(self):
        with mock.patch("automuse.notify.urllib.request.urlopen",
                        side_effect=_urlopen_ok):
            self.assertTrue(send_telegram_to("tok", "123", "hello"))

    def test_send_unauthorized_false(self):
        with mock.patch("automuse.notify.urllib.request.urlopen",
                        side_effect=_urlopen_fail):
            self.assertFalse(send_telegram_to("bad", "123", "hello"))

    def test_send_missing_args_false(self):
        self.assertFalse(send_telegram_to("", "123", "hello"))
        self.assertFalse(send_telegram_to("tok", "", "hello"))

    def test_check_token_ok(self):
        with mock.patch("automuse.notify.urllib.request.urlopen",
                        side_effect=_urlopen_ok):
            ok, name = check_bot_token("tok")
        self.assertTrue(ok)
        self.assertEqual(name, "@automuse_bot")

    def test_check_token_bad(self):
        with mock.patch("automuse.notify.urllib.request.urlopen",
                        side_effect=_urlopen_fail):
            ok, _ = check_bot_token("bad")
        self.assertFalse(ok)

    def test_configured_false_without_config(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("automuse.notify._read_env_value", return_value=""):
            self.assertFalse(telegram_configured())


if __name__ == "__main__":
    unittest.main()
