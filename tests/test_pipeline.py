"""Test thông báo acc không dùng được + transport mặc định.

Hồi quy cho lỗi 2026-10-26: acc "acc test" (0 video, 0.0 phút) bị báo
"Hết giới hạn tuần" trong khi quota chưa dùng gì — thực ra acc đang
nghỉ cooldown 5 phút sau 3 lỗi liên tiếp (do transport mặc định "rest"
gọi endpoint đã biết sai -> 403). Thông báo mới phải tách 2 lý do này.
"""

import tempfile
import time
import unittest
from pathlib import Path

from automuse.accounts import Account
from automuse.config import DEFAULT_SETTINGS, load_settings
from automuse.pipeline import _describe_ineligible
from automuse.usage import UsageTracker


def _tracker():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.close()
    return UsageTracker(Path(tmp.name))


class TestDescribeIneligible(unittest.TestCase):
    def test_cooldown_not_quota(self):
        """Acc 0 video nhưng đang cooldown -> báo nghỉ sau lỗi, không đổ oan quota."""
        acc = Account(name="acc test", cooldown_until=time.time() + 240)
        msg = _describe_ineligible([acc], _tracker(), load_settings())
        self.assertIn("đang nghỉ sau", msg)
        self.assertIn("thử lại sau", msg)
        self.assertNotIn("hết giới hạn tuần", msg.lower())

    def test_quota_exhausted(self):
        """Acc hết quota thật -> báo hết giới hạn tuần kèm số đã dùng/giới hạn."""
        tracker = _tracker()
        tracker.record("acc1", "video", 10.0)
        settings = {"limits": {"weekly_max_videos": 1, "weekly_max_minutes": 0}}
        msg = _describe_ineligible([Account(name="acc1")], tracker, settings)
        self.assertIn("hết giới hạn tuần", msg)
        self.assertIn("1/1 video", msg)
        self.assertNotIn("đang nghỉ", msg)

    def test_mixed_pool(self):
        """Pool có cả acc cooldown lẫn acc hết quota -> báo đủ cả hai."""
        tracker = _tracker()
        tracker.record("acc1", "video", 10.0)
        settings = {"limits": {"weekly_max_videos": 1, "weekly_max_minutes": 0}}
        pool = [
            Account(name="acc test", cooldown_until=time.time() + 60),
            Account(name="acc1"),
        ]
        msg = _describe_ineligible(pool, tracker, settings)
        self.assertIn("đang nghỉ sau", msg)
        self.assertIn("hết giới hạn tuần", msg)


class TestDefaultTransport(unittest.TestCase):
    def test_default_is_gateway(self):
        """Gateway đã kiểm chứng xong (vòng lặp khép kín 2026-10-26)
        nên phải là transport mặc định, không còn "rest" (403)."""
        self.assertEqual(DEFAULT_SETTINGS["muse"]["transport"], "gateway")
        self.assertEqual(load_settings()["muse"]["transport"], "gateway")


if __name__ == "__main__":
    unittest.main()
