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
from unittest import mock

from automuse.accounts import Account, AccountManager
from automuse.config import DEFAULT_SETTINGS, load_settings
from automuse.pipeline import _describe_ineligible, generate_clips
from automuse.script import Scene, Script
from automuse.usage import UsageTracker


def _tracker():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.close()
    return UsageTracker(Path(tmp.name))


def _script():
    return Script(title="Test", scenes=[
        Scene(id="s01", title="Bình minh thảo nguyên", duration=5,
              visual="savanna dawn"),
    ])


def _manager_with(account: Account, cls=AccountManager) -> AccountManager:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.close()
    Path(tmp.name).write_text('{"accounts": []}', encoding="utf-8")
    mgr = cls(Path(tmp.name))
    mgr.accounts = [account]
    mgr.save()
    return mgr


class _FailingClient:
    """Giả lập lỗi gateway init (như acc test: cookie chết -> không blob)."""

    def generate_video(self, **kwargs):
        raise RuntimeError(
            "Không thấy __HATCH_GATEWAY_INIT__ trong trang chủ muse.ai")


class _NoPickManager(AccountManager):
    def pick(self, names=None):
        return None


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


class TestNoSilentMockFallback(unittest.TestCase):
    """Build thật không được lặng lẽ rơi sang mock client khi hết acc
    chọn được — phải dừng với lỗi rõ ràng, không sinh clip giả."""

    def test_pick_none_raises_instead_of_mock(self):
        out = Path(tempfile.mkdtemp())
        mgr = _manager_with(Account(name="acc test"), cls=_NoPickManager)
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3}}
        with self.assertRaises(RuntimeError) as ctx:
            generate_clips(_script(), out, settings, mgr, mock=False,
                           account_names=["acc test"], tracker=_tracker())
        self.assertIn("Không còn acc nào dùng được", str(ctx.exception))
        self.assertEqual(list(out.rglob("*.mp4")), [])

    def test_cooldown_mid_scene_raises_instead_of_mock(self):
        """Tái hiện đúng log của user: acc đã lỗi 2 lần, lần lỗi thứ 3
        kích hoạt cooldown 5 phút -> lần thử sau phải báo đang nghỉ,
        không được sinh clip mock giả mạo clip thật."""
        out = Path(tempfile.mkdtemp())
        mgr = _manager_with(Account(name="acc test", jobs_failed=2))
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3}}
        with mock.patch("automuse.pipeline.build_client",
                        return_value=_FailingClient()):
            with self.assertRaises(RuntimeError) as ctx:
                generate_clips(_script(), out, settings, mgr, mock=False,
                               account_names=["acc test"], tracker=_tracker())
        self.assertIn("đang nghỉ sau", str(ctx.exception))
        self.assertEqual(list(out.rglob("*.mp4")), [])


if __name__ == "__main__":
    unittest.main()
