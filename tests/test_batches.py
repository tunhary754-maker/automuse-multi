"""Test sản xuất hàng loạt theo chủ đề (automuse/batches.py) + gợi ý xu
hướng (topics.suggest_trending_topics). Không gọi gateway thật."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import automuse.batches as batches
import automuse.topics as topics


class _FakeScene:
    def __init__(self, duration=10):
        self.duration = duration


class _FakeScript:
    def __init__(self, title, nscenes=3):
        self.title = title
        self.scenes = [_FakeScene() for _ in range(nscenes)]


class TestBatchManifest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = batches.BATCHES_FILE
        batches.BATCHES_FILE = Path(self._tmp.name) / "batches.json"

    def tearDown(self):
        batches.BATCHES_FILE = self._orig
        self._tmp.cleanup()

    def test_create_batch_counts(self):
        b = batches.create_batch(
            [{"name": "Ca voi xanh", "reason": "hot"},
             {"name": "Voi chau Phi", "reason": ""}],
            episodes_per_topic=2, minutes=20)
        self.assertEqual(b["total"], 4)
        self.assertEqual(len(b["topics"]), 2)
        self.assertEqual(len(b["topics"][0]["scripts"]), 2)
        self.assertTrue(all(s["status"] == "pending"
                            for t in b["topics"] for s in t["scripts"]))

    def test_recount_and_progress(self):
        b = batches.create_batch([{"name": "X", "reason": ""}], 3, 8)
        b["topics"][0]["scripts"][0]["status"] = "done"
        b["topics"][0]["scripts"][1]["status"] = "failed"
        p = batches.batch_progress(b)
        self.assertEqual((p["done"], p["failed"], p["total"]), (1, 1, 3))
        self.assertEqual(p["pct"], 33)

    def test_save_load_roundtrip_and_remove(self):
        b = batches.create_batch([{"name": "X", "reason": ""}], 1, 8)
        b["topics"][0]["scripts"][0]["file"] = "scripts/x-tap01.yaml"
        b["topics"][0]["scripts"][0]["status"] = "done"
        batches.save_batches([b])
        self.assertEqual(batches.load_batches()[0]["id"], b["id"])
        self.assertTrue(batches.remove_script_from_batches(
            "scripts/x-tap01.yaml"))
        self.assertEqual(batches.load_batches()[0]["topics"][0]["scripts"], [])
        self.assertFalse(batches.remove_script_from_batches(
            "scripts/khong-co.yaml"))


def _make_deps(scripts_dir, fail_on_write=None, bad_accounts=(),
               write_barrier=None, write_started=None, write_release=None):
    """deps giả cho run_batch. fail_on_write: số thứ tự lần write bị lỗi.
    write_barrier: Barrier(N) mà mỗi lần write phải chờ (chứng minh song
    song). write_started/write_release: Event để test dừng giữa chừng."""
    calls = {"research": [], "writes": 0, "notified": [], "saved": [],
             "write_accounts": [], "concurrent": 0, "max_concurrent": 0}
    lock = threading.Lock()

    def get_client(name):
        if name in bad_accounts:
            return None, "GatewayError: handshake failed"
        return name, ""  # client chính là tên acc (để test ghi nhận acc)

    def research(topic, client):
        with lock:
            calls["research"].append(topic)
        return f"fact sheet cho {topic}"

    def write(topic, client, target_scenes, research_text, timeout):
        with lock:
            calls["writes"] += 1
            n = calls["writes"]
            calls["write_accounts"].append(client)
            calls["concurrent"] += 1
            calls["max_concurrent"] = max(calls["max_concurrent"],
                                          calls["concurrent"])
        try:
            if write_started is not None and n == 1:
                write_started.set()
                if write_release is not None:
                    write_release.wait(timeout=10)
            if write_barrier is not None:
                write_barrier.wait(timeout=10)
            if fail_on_write is not None and n == fail_on_write:
                raise RuntimeError("mo phong loi viet")
        finally:
            with lock:
                calls["concurrent"] -= 1
        return _FakeScript(f"{topic} Tap {n}", nscenes=3)

    def save_script(script, out_path, topic):
        p = scripts_dir / Path(out_path).name
        p.write_text(f"title: {script.title}\ntopic: {topic}\n",
                     encoding="utf-8")
        with lock:
            calls["saved"].append((out_path, topic))
        return p

    deps = {
        "account_names": ["acc2", "acc1"] if bad_accounts else ["acc2"],
        "get_client": get_client,
        "research": research,
        "write": write,
        "save_script": save_script,
        "slugify": lambda t: "ca-voi-xanh" if "voi" in t.lower() else "chu-de",
        "notify": lambda text: calls["notified"].append(text) or True,
        "is_connection_error": lambda e: "GatewayError" in str(e),
        "log": lambda msg: None,
        "scripts_dir": scripts_dir,
    }
    return deps, calls


class TestRunBatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = batches.BATCHES_FILE
        batches.BATCHES_FILE = Path(self._tmp.name) / "batches.json"
        self.scripts_dir = Path(self._tmp.name) / "scripts"
        self.scripts_dir.mkdir()

    def tearDown(self):
        batches.BATCHES_FILE = self._orig
        self._tmp.cleanup()

    def test_happy_path_research_once_per_topic_and_telegram(self):
        b = batches.create_batch(
            [{"name": "Ca voi xanh", "reason": ""}], 2, 8)
        deps, calls = _make_deps(self.scripts_dir)
        out = batches.run_batch(b, deps)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["done"], 2)
        # Nghiên cứu 1 lần cho cả chủ đề, không phải mỗi tập.
        self.assertEqual(calls["research"], ["Ca voi xanh"])
        # Mỗi kịch bản xong gửi đúng 1 tin Telegram đúng format.
        done_msgs = [m for m in calls["notified"]
                     if m.startswith("✅ Đã hoàn thành xong:")]
        self.assertEqual(len(done_msgs), 2)
        self.assertIn("Ca voi xanh +", done_msgs[0])
        # File lưu kèm đúng topic, tên duy nhất theo tập.
        self.assertEqual(len(calls["saved"]), 2)
        self.assertTrue(all(t == "Ca voi xanh" for _, t in calls["saved"]))
        self.assertEqual(calls["saved"][0][0], "scripts/ca-voi-xanh-tap01.yaml")
        self.assertEqual(calls["saved"][1][0], "scripts/ca-voi-xanh-tap02.yaml")

    def test_filename_unique_when_exists(self):
        (self.scripts_dir / "ca-voi-xanh-tap01.yaml").write_text("cu")
        b = batches.create_batch([{"name": "Ca voi xanh", "reason": ""}], 1, 8)
        deps, calls = _make_deps(self.scripts_dir)
        batches.run_batch(b, deps)
        self.assertEqual(calls["saved"][0][0], "scripts/ca-voi-xanh-tap01-2.yaml")

    def test_failed_script_does_not_abort_batch(self):
        b = batches.create_batch([{"name": "Ca voi xanh", "reason": ""}], 3, 8)
        deps, calls = _make_deps(self.scripts_dir, fail_on_write=2)
        out = batches.run_batch(b, deps)
        self.assertEqual(out["status"], "partial")
        self.assertEqual((out["done"], out["failed"]), (2, 1))
        self.assertEqual(len([m for m in calls["notified"]
                              if m.startswith("✅ Đã hoàn thành xong:")]), 2)

    def test_connection_error_reported_once_per_account(self):
        b = batches.create_batch([{"name": "Ca voi xanh", "reason": ""}], 2, 8)
        deps, calls = _make_deps(self.scripts_dir, bad_accounts=("acc1",))
        # acc1 hỏng, acc2 làm được: đợt vẫn xong, chỉ báo acc1 1 lần.
        deps["account_names"] = ["acc1", "acc2"]
        out = batches.run_batch(b, deps)
        self.assertEqual(out["status"], "done")
        warnings = [m for m in calls["notified"]
                    if "KHÔNG KẾT NỐI ĐƯỢC" in m]
        self.assertEqual(len(warnings), 1)
        self.assertIn("acc1", warnings[0])


class TestTrending(unittest.TestCase):
    def test_trend_success_parses_reasons(self):
        reply = ('[{"title": "Ca voi xanh", "summary": "Ly do nhu cau: '
                 'cau hoi duoc tim nhieu"}, {"title": "Voi chau Phi", '
                 '"summary": "Ly do nhu cau: dang hot theo mua"}]')
        with mock.patch.object(topics, "ask_muse_text",
                               return_value=reply):
            ideas, trend_based = topics.suggest_trending_topics(
                2, client=object())
        self.assertTrue(trend_based)
        self.assertEqual([i.title for i in ideas],
                         ["Ca voi xanh", "Voi chau Phi"])
        self.assertIn("tim nhieu", ideas[0].summary)

    def test_trend_failure_falls_back(self):
        with mock.patch.object(topics, "ask_muse_text",
                               side_effect=RuntimeError("hong")):
            ideas, trend_based = topics.suggest_trending_topics(
                2, client=object())
        self.assertFalse(trend_based)
        self.assertTrue(len(ideas) >= 1)


class TestParallelAndControl(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = batches.BATCHES_FILE
        batches.BATCHES_FILE = Path(self._tmp.name) / "batches.json"
        self.scripts_dir = Path(self._tmp.name) / "scripts"
        self.scripts_dir.mkdir()

    def tearDown(self):
        batches.BATCHES_FILE = self._orig
        self._tmp.cleanup()

    def test_three_accounts_run_three_scripts_in_parallel(self):
        # 3 acc -> 3 kịch bản cùng lúc (barrier: cả 3 write phải gặp nhau
        # mới qua được; nếu chạy tuần tự thì barrier timeout -> lỗi).
        barrier = threading.Barrier(3)
        b = batches.create_batch([{"name": "Ca voi xanh", "reason": ""}],
                                 3, 8)
        batches.save_batches([b])
        deps, calls = _make_deps(self.scripts_dir, write_barrier=barrier)
        deps["account_names"] = ["acc1", "acc2", "acc3"]
        out = batches.run_batch(b, deps)
        self.assertEqual(out["status"], "done")
        self.assertEqual(calls["max_concurrent"], 3)
        # Mỗi kịch bản trọn vẹn 1 acc, không xé nhỏ: 3 write dùng 3 acc
        # khác nhau (mỗi write = 1 kịch bản, ghi nhận đúng 1 acc).
        self.assertEqual(len(calls["write_accounts"]), 3)
        self.assertEqual(set(calls["write_accounts"]),
                         {"acc1", "acc2", "acc3"})

    def test_pause_before_start_then_resume(self):
        b = batches.create_batch([{"name": "Ca voi xanh", "reason": ""}],
                                 2, 8)
        batches.save_batches([b])
        self.assertTrue(batches.set_batch_flags(b["id"], paused=True))
        deps, calls = _make_deps(self.scripts_dir)
        t = threading.Thread(target=batches.run_batch, args=(b, deps))
        t.start()
        time.sleep(0.6)
        # Đang tạm dừng: chưa kịch bản nào xong, thread vẫn sống.
        self.assertTrue(t.is_alive())
        self.assertEqual(batches.get_batch(b["id"])["done"], 0)
        self.assertTrue(batches.set_batch_flags(b["id"], paused=False))
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(batches.get_batch(b["id"])["status"], "done")
        self.assertEqual(batches.get_batch(b["id"])["done"], 2)

    def test_cancel_before_start(self):
        b = batches.create_batch([{"name": "Ca voi xanh", "reason": ""}],
                                 2, 8)
        batches.save_batches([b])
        batches.set_batch_flags(b["id"], cancelled=True)
        deps, _ = _make_deps(self.scripts_dir)
        out = batches.run_batch(b, deps)
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(out["done"], 0)

    def test_cancel_mid_run_stops_new_scripts(self):
        # 1 acc -> tuần tự: tập 1 đang viết thì bấm dừng hẳn; tập 1 viết
        # nốt (được lưu), tập 2 không bắt đầu nữa.
        started = threading.Event()
        release = threading.Event()
        b = batches.create_batch([{"name": "Ca voi xanh", "reason": ""}],
                                 2, 8)
        batches.save_batches([b])
        deps, _ = _make_deps(self.scripts_dir, write_started=started,
                             write_release=release)
        t = threading.Thread(target=batches.run_batch, args=(b, deps))
        t.start()
        self.assertTrue(started.wait(timeout=5))
        batches.set_batch_flags(b["id"], cancelled=True)
        release.set()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        done = batches.get_batch(b["id"])
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(done["done"], 1)
        statuses = [s["status"] for tp in done["topics"] for s in tp["scripts"]]
        self.assertEqual(statuses, ["done", "cancelled"])

    def test_restart_skips_done_scripts(self):
        b = batches.create_batch([{"name": "Ca voi xanh", "reason": ""}],
                                 2, 8)
        batches.save_batches([b])
        deps, calls = _make_deps(self.scripts_dir, fail_on_write=2)
        out = batches.run_batch(b, deps)
        self.assertEqual(out["status"], "partial")
        # Chạy tiếp: tập 1 đã xong không viết lại, chỉ tập 2 làm lại.
        batches.prepare_restart(b)
        deps2, calls2 = _make_deps(self.scripts_dir)
        out2 = batches.run_batch(b, deps2)
        self.assertEqual(out2["status"], "done")
        self.assertEqual(out2["done"], 2)
        tap01_saves = [p for p, _ in calls["saved"] + calls2["saved"]
                       if p.endswith("tap01.yaml")]
        self.assertEqual(len(tap01_saves), 1)

    def test_set_batch_flags(self):
        b = batches.create_batch([{"name": "X", "reason": ""}], 1, 8)
        batches.save_batches([b])
        self.assertTrue(batches.set_batch_flags(b["id"], paused=True))
        self.assertTrue(batches.get_batch(b["id"])["paused"])
        self.assertTrue(batches.set_batch_flags(b["id"], cancelled=True,
                                                paused=False))
        got = batches.get_batch(b["id"])
        self.assertTrue(got["cancelled"])
        self.assertFalse(got["paused"])
        self.assertFalse(batches.set_batch_flags("batch-khong-co",
                                                 paused=True))


if __name__ == "__main__":
    unittest.main()
