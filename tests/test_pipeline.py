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
    def pick(self, names=None, exclude=None):
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
        không được sinh clip mock giả mạo clip thật. Đặt max_wait_rounds=0
        để test không chờ cooldown thật (hành vi chờ được test riêng)."""
        out = Path(tempfile.mkdtemp())
        mgr = _manager_with(Account(name="acc test", jobs_failed=2))
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3,
                                   "max_wait_rounds": 0}}
        with mock.patch("automuse.pipeline.build_client",
                        return_value=_FailingClient()):
            with self.assertRaises(RuntimeError) as ctx:
                generate_clips(_script(), out, settings, mgr, mock=False,
                               account_names=["acc test"], tracker=_tracker())
        self.assertIn("đang nghỉ sau", str(ctx.exception))
        self.assertEqual(list(out.rglob("*.mp4")), [])


class _WorkingClient:
    """Ghi file clip giả vào out_path (đủ để pipeline chuyển thành đích)."""

    def generate_video(self, **kwargs):
        out = Path(kwargs["out_path"])
        out.write_bytes(b"fake-clip")


class _FlakyClient:
    """Lỗi N lần đầu rồi mới ghi clip — mô phỏng acc hồi sau cooldown."""

    def __init__(self, fail_times: int = 1):
        self.calls = 0
        self.fail_times = fail_times

    def generate_video(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("gateway chập chờn")
        Path(kwargs["out_path"]).write_bytes(b"fake-clip")


def _manager_with_many(names: list[str], **kwargs) -> AccountManager:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.close()
    Path(tmp.name).write_text('{"accounts": []}', encoding="utf-8")
    mgr = AccountManager(Path(tmp.name))
    mgr.accounts = [Account(name=n, **kwargs) for n in names]
    mgr.save()
    return mgr


class TestFailoverContinue(unittest.TestCase):
    """Yêu cầu 2026-10-30: hết quota acc này thì chuyển acc khác làm tiếp
    nhiệm vụ dở dang, không dừng build."""

    def test_failover_to_second_account_same_scene(self):
        """acc1 lỗi -> lần thử sau của CÙNG cảnh dùng acc2 ngay (không
        quay lại acc1), cảnh xong với acc2."""
        out = Path(tempfile.mkdtemp())
        mgr = _manager_with_many(["acc1", "acc2"])
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3,
                                   "max_wait_rounds": 0}}

        def _client(account, settings, mock=False):
            return _FailingClient() if account.name == "acc1" else _WorkingClient()

        with mock.patch("automuse.pipeline.build_client",
                        side_effect=_client), \
                mock.patch("automuse.pipeline.extend_clip",
                           side_effect=lambda tmp, dest, target: tmp):
            clips, infos = generate_clips(_script(), out, settings, mgr,
                                          mock=False, tracker=_tracker())
        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].exists())
        self.assertEqual(infos[0]["account"], "acc2")
        acc1 = [a for a in mgr.accounts if a.name == "acc1"][0]
        self.assertEqual(acc1.jobs_failed, 1)

    def test_wait_for_cooldown_then_continue(self):
        """Acc duy nhất vào cooldown sau lỗi -> build CHỜ cooldown hết rồi
        làm tiếp cảnh đó (đồng hồ giả để không chờ 5 phút thật)."""
        out = Path(tempfile.mkdtemp())
        mgr = _manager_with_many(["acc1"], jobs_failed=2)
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3}}
        flaky = _FlakyClient(fail_times=1)
        fake_now = [1_000_000.0]
        slept: list[float] = []

        def _fake_sleep(secs):
            slept.append(secs)
            fake_now[0] += secs

        with mock.patch("automuse.pipeline.build_client",
                        return_value=flaky), \
                mock.patch("automuse.pipeline.extend_clip",
                           side_effect=lambda tmp, dest, target: tmp), \
                mock.patch("time.time", side_effect=lambda: fake_now[0]), \
                mock.patch("time.sleep", side_effect=_fake_sleep):
            clips, infos = generate_clips(_script(), out, settings, mgr,
                                          mock=False, tracker=_tracker())
        self.assertEqual(len(clips), 1)
        self.assertEqual(infos[0]["account"], "acc1")
        # Có một lần chờ ~302s (cooldown 300s + 2s đệm) rồi mới làm tiếp.
        self.assertTrue(any(s > 300 for s in slept),
                        f"không thấy lượt chờ cooldown trong {slept}")
        self.assertEqual(flaky.calls, 2)


class _GatewayFailingClient:
    """Lỗi tầng gateway (không kết nối được) — như acc1: login OK nhưng
    gateway thiếu authToken."""

    def generate_video(self, **kwargs):
        from automuse.gateway import GatewayError
        raise GatewayError("__HATCH_GATEWAY_INIT__ thiếu khóa authToken")


class TestConnectionErrorReport(unittest.TestCase):
    """Yêu cầu 2026-10-30: acc nào không kết nối được thì báo lỗi luôn
    (log + Telegram), dù build vẫn làm tiếp bằng acc khác."""

    def test_is_connection_error(self):
        from automuse.gateway import GatewayError
        from automuse.pipeline import is_connection_error
        self.assertTrue(is_connection_error(
            GatewayError("__HATCH_GATEWAY_INIT__ thiếu khóa authToken")))
        self.assertTrue(is_connection_error(
            RuntimeError("Không thấy __HATCH_GATEWAY_INIT__ trong trang chủ")))
        self.assertFalse(is_connection_error(RuntimeError("prompt bị từ chối")))

    def test_reports_once_per_account_then_failover(self):
        """2 cảnh: acc1 lỗi gateway ở cả 2 cảnh -> chỉ báo 1 lần (cảnh 1),
        cả 2 cảnh đều xong bằng acc2."""
        out = Path(tempfile.mkdtemp())
        mgr = _manager_with_many(["acc1", "acc2"])
        script = Script(title="Test", scenes=[
            Scene(id="s01", title="Một", duration=5, visual="v1"),
            Scene(id="s02", title="Hai", duration=5, visual="v2"),
        ])
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3,
                                   "max_wait_rounds": 0}}
        sent: list[str] = []

        def _client(account, settings, mock=False):
            return _GatewayFailingClient() if account.name == "acc1" else _WorkingClient()

        with mock.patch("automuse.pipeline.build_client",
                        side_effect=_client), \
                mock.patch("automuse.pipeline.extend_clip",
                           side_effect=lambda tmp, dest, target: tmp), \
                mock.patch("automuse.pipeline.telegram_configured",
                           return_value=True), \
                mock.patch("automuse.pipeline.send_telegram",
                           side_effect=lambda t: sent.append(t) or True):
            clips, infos = generate_clips(script, out, settings, mgr,
                                          mock=False, tracker=_tracker())
        self.assertEqual(len(clips), 2)
        self.assertTrue(all(i["account"] == "acc2" for i in infos))
        self.assertEqual(len(sent), 1)
        self.assertIn("acc1", sent[0])
        self.assertIn("KHÔNG KẾT NỐI ĐƯỢC", sent[0])


class TestWaitForNewAccount(unittest.TestCase):
    """Hết acc thật + có Telegram -> báo tin, chờ user thêm acc mới, rồi
    làm tiếp cảnh dở dang bằng acc mới (không cần chạy lại build)."""

    def test_telegram_notify_and_continue_with_new_account(self):
        out = Path(tempfile.mkdtemp())
        mgr = _manager_with_many(["acc1"], jobs_failed=2)
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3,
                                   "max_wait_rounds": 0,
                                   "wait_for_new_account_minutes": 1,
                                   "new_account_poll_seconds": 1}}
        sent: list[str] = []

        def _client(account, settings, mock=False):
            return _FailingClient() if account.name == "acc1" else _WorkingClient()

        orig_reload = mgr.reload_new

        def _reload_and_add():
            # Mô phỏng user thêm acc2 qua Web Studio trong lúc build chờ.
            if not mgr.get("acc2"):
                mgr.accounts.append(Account(name="acc2"))
                return ["acc2"]
            return orig_reload()

        with mock.patch("automuse.pipeline.build_client",
                        side_effect=_client), \
                mock.patch("automuse.pipeline.extend_clip",
                           side_effect=lambda tmp, dest, target: tmp), \
                mock.patch("automuse.notify.telegram_configured",
                           return_value=True), \
                mock.patch("automuse.notify.send_telegram",
                           side_effect=lambda t: sent.append(t) or True), \
                mock.patch.object(mgr, "reload_new",
                                  side_effect=_reload_and_add):
            clips, infos = generate_clips(_script(), out, settings, mgr,
                                          mock=False, tracker=_tracker())
        self.assertEqual(len(clips), 1)
        self.assertEqual(infos[0]["account"], "acc2")
        self.assertEqual(len(sent), 1)
        self.assertIn("dừng ở cảnh s01", sent[0])

    def test_no_telegram_config_raises_without_waiting(self):
        """Chưa cấu hình Telegram -> không chờ acc mới, dừng rõ ràng ngay."""
        out = Path(tempfile.mkdtemp())
        mgr = _manager_with_many(["acc1"], jobs_failed=2)
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3,
                                   "max_wait_rounds": 0}}
        with mock.patch("automuse.pipeline.build_client",
                        return_value=_FailingClient()), \
                mock.patch("automuse.notify.telegram_configured",
                           return_value=False), \
                mock.patch("automuse.notify.send_telegram") as sent:
            with self.assertRaises(RuntimeError):
                generate_clips(_script(), out, settings, mgr,
                               mock=False, tracker=_tracker())
        sent.assert_not_called()

    def test_reload_new_preserves_running_state(self):
        """reload_new chỉ thêm acc mới, không reset jobs_failed/cooldown
        của acc đang chạy."""
        import json
        mgr = _manager_with_many(["acc1"], jobs_failed=2)
        acc1 = mgr.get("acc1")
        acc1.cooldown_until = 9999999999.0
        data = json.loads(Path(mgr.path).read_text(encoding="utf-8"))
        data["accounts"].append({"name": "acc2"})
        Path(mgr.path).write_text(json.dumps(data), encoding="utf-8")
        added = mgr.reload_new()
        self.assertEqual(added, ["acc2"])
        self.assertEqual(mgr.get("acc1").jobs_failed, 2)
        self.assertEqual(mgr.get("acc1").cooldown_until, 9999999999.0)
        self.assertEqual(mgr.reload_new(), [])


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
