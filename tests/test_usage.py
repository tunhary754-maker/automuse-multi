"""Tests: giới hạn tuần (UsageTracker), check_quota, chặn build khi hết quota.
Chạy: python -m unittest discover -s tests -v"""

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automuse.accounts import Account  # noqa: E402
from automuse.config import DEFAULT_SETTINGS  # noqa: E402
from automuse.muse_client import MuseClient  # noqa: E402
from automuse.usage import UsageTracker, week_label, week_start  # noqa: E402

SETTINGS = {"limits": {"weekly_max_videos": 2, "weekly_max_minutes": 1}}


def _settings(**kw):
    s = {"limits": dict(SETTINGS["limits"])}
    s["limits"].update(kw)
    return s


class TestUsageTracker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(Path(self.tmp.name) / "usage.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_weekly(self):
        self.tracker.record("acc1", "video", 30, project="P1")
        self.tracker.record("acc1", "video", 30, project="P1")
        self.tracker.record("acc1", "image", 0)
        self.tracker.record("acc2", "video", 30)
        w = self.tracker.weekly("acc1")
        self.assertEqual(w["videos"], 2)
        self.assertEqual(w["images"], 1)
        self.assertEqual(w["minutes"], 1.0)
        self.assertEqual(self.tracker.weekly("acc2")["videos"], 1)

    def test_last_week_not_counted(self):
        last_week = (week_start() - timedelta(days=1)).timestamp()
        self.tracker.record("acc1", "video", 30, ts=last_week)
        self.assertEqual(self.tracker.weekly("acc1")["videos"], 0)

    def test_remaining_and_budget(self):
        self.tracker.record("acc1", "video", 30)
        r = self.tracker.remaining("acc1", _settings())
        self.assertEqual(r["videos_left"], 1)
        self.assertEqual(r["minutes_left"], 0.5)
        self.assertTrue(self.tracker.has_budget("acc1", _settings()))
        self.tracker.record("acc1", "video", 30)
        self.assertFalse(self.tracker.has_budget("acc1", _settings()))
        self.assertEqual(self.tracker.remaining("acc1", _settings())["videos_left"], 0)

    def test_unlimited(self):
        r = self.tracker.remaining("acc1", _settings(weekly_max_videos=0, weekly_max_minutes=0))
        self.assertIsNone(r["videos_left"])
        self.assertIsNone(r["minutes_left"])
        self.assertTrue(self.tracker.has_budget("acc1", _settings(weekly_max_videos=0, weekly_max_minutes=0)))

    def test_per_account_override(self):
        acc = Account(name="acc1", cookie="a=b", weekly_max_videos=5)
        max_v, max_m = self.tracker.limits_for(acc, _settings())
        self.assertEqual((max_v, max_m), (5, 1))
        acc2 = Account(name="acc2", cookie="a=b")
        self.assertEqual(self.tracker.limits_for(acc2, _settings()), (2, 1))

    def test_week_label(self):
        self.assertRegex(week_label(), r"\d{2}/\d{2} – \d{2}/\d{2}/\d{4}")
        self.assertEqual(week_start().weekday(), 0)  # thứ Hai


def _fake_resp(status=200, payload=None):
    r = Mock()
    r.status_code = status
    r.url = "https://muse.ai/api/usage"
    r.text = "{}" if payload is None else str(payload)
    r.json = Mock(return_value=payload if payload is not None else {})
    return r


class TestCheckQuota(unittest.TestCase):
    def _client_with(self, responses):
        acc = Account(name="acc1", cookie="a=b")
        settings = dict(DEFAULT_SETTINGS)
        settings["muse"] = dict(DEFAULT_SETTINGS["muse"])
        settings["muse"]["quota_check_paths"] = ["/api/usage", "/api/credits"]
        client = MuseClient(acc, settings)
        it = iter(responses)
        client.session.get = Mock(side_effect=lambda *a, **k: next(it))
        return client

    def test_balance_found(self):
        client = self._client_with([_fake_resp(200, {"credits_remaining": 42})])
        ok, msg = client.check_quota()
        self.assertTrue(ok)
        self.assertIn("42", msg)

    def test_used_and_limit(self):
        client = self._client_with([_fake_resp(200, {"used": 7, "limit": 10})])
        ok, msg = client.check_quota()
        self.assertTrue(ok)
        self.assertIn("7/10", msg)

    def test_all_404_unknown(self):
        client = self._client_with([_fake_resp(404), _fake_resp(404)])
        ok, msg = client.check_quota()
        self.assertIsNone(ok)
        self.assertIn("Không đọc được", msg)

    def test_401_expired(self):
        client = self._client_with([_fake_resp(401)])
        ok, msg = client.check_quota()
        self.assertFalse(ok)
        self.assertIn("hết hạn", msg)


class TestWeeklyLimitEnforcement(unittest.TestCase):
    def test_mock_build_blocked_when_over_limit(self):
        from automuse.pipeline import run_build

        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "one.yaml"
            script.write_text(
                "title: Test 1 cảnh\nkind: movie\naspect: '16:9'\n"
                "scenes:\n  - title: Cảnh 1\n    duration: 5\n"
                '    visual: "A cat"\n    narration: "Meo."\n',
                encoding="utf-8",
            )
            tracker = UsageTracker(Path(d) / "usage.json")
            tracker.record("mock", "video", 5)  # đã dùng hết 1 clip tuần này
            settings = _settings(weekly_max_videos=1, weekly_max_minutes=120)
            with self.assertRaises(RuntimeError) as cm:
                run_build(
                    script, out_dir=Path(d) / "out", settings=settings,
                    mock=True, no_tts=True, tracker=tracker,
                )
            self.assertIn("Hết giới hạn tuần", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
