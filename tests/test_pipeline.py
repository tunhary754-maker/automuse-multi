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


def _lipsync_script(tmp: Path) -> Path:
    """Kịch bản 2 cảnh: s01 thoại cận mặt (lipsync), s02 lời dẫn (không)."""
    p = tmp / "phim.yaml"
    p.write_text(
        "title: Test lip-sync\nkind: movie\naspect: '16:9'\nscenes:\n"
        "  - id: s01\n    title: Nói\n    duration: 5\n"
        "    visual: close-up talking\n    narration: Chào bạn, tôi là Bình.\n"
        "    voice: narrator\n    lipsync: true\n"
        "  - id: s02\n    title: Nghe\n    duration: 5\n"
        "    visual: wide street\n    narration: Sài Gòn đêm mưa.\n"
        "    voice: narrator\n",
        encoding="utf-8")
    return p


class _FailingLipSync:
    def sync(self, video_path, audio_path, out_path):
        from automuse.lipsync import SyncError
        raise SyncError("Sync Labs lỗi giả lập")


class TestRunBuildLipSync(unittest.TestCase):
    def test_mock_build_records_lipsync_status(self):
        """Mock build với 1 cảnh lipsync: manifest ghi ok/skipped,
        file clips_lipsync/s01.mp4 được tạo."""
        tmp = Path(tempfile.mkdtemp())
        script = _lipsync_script(tmp)
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3}}
        from automuse.pipeline import run_build
        manifest = run_build(script, out_dir=tmp / "out", settings=settings,
                             mock=True, lipsync=True)
        scenes = {s["id"]: s for s in manifest["scenes"]}
        self.assertEqual(scenes["s01"]["lipsync"], "ok")
        self.assertEqual(scenes["s02"]["lipsync"], "skipped")
        self.assertTrue((tmp / "out" / "clips_lipsync" / "s01.mp4").exists())
        self.assertTrue((tmp / "out" / "final.mp4").exists())

    def test_failing_sync_keeps_original_clip(self):
        """Client lip-sync lỗi -> giữ clip gốc, build vẫn xong,
        manifest ghi failed."""
        tmp = Path(tempfile.mkdtemp())
        script = _lipsync_script(tmp)
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3}}
        from automuse.pipeline import run_build
        with mock.patch("automuse.pipeline.build_lipsync",
                        return_value=_FailingLipSync()):
            manifest = run_build(script, out_dir=tmp / "out",
                                 settings=settings, mock=True, lipsync=True)
        scenes = {s["id"]: s for s in manifest["scenes"]}
        self.assertEqual(scenes["s01"]["lipsync"], "failed")
        self.assertTrue((tmp / "out" / "final.mp4").exists())
        self.assertTrue((tmp / "out" / "clips" / "s01.mp4").exists())
        self.assertFalse((tmp / "out" / "clips_lipsync" / "s01.mp4").exists())

    def test_lipsync_without_key_fails_fast(self):
        """Bật --lipsync, không mock, chưa có key -> dừng ngay với
        hướng dẫn chạy `lipsync set-key`."""
        tmp = Path(tempfile.mkdtemp())
        script = _lipsync_script(tmp)
        from automuse.pipeline import run_build
        with mock.patch("automuse.pipeline.get_sync_key", return_value=""):
            with self.assertRaises(RuntimeError) as ctx:
                run_build(script, out_dir=tmp / "out", settings={},
                          mock=False, lipsync=True, sync_api_key="")
        self.assertIn("lipsync set-key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
