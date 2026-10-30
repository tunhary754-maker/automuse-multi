"""Tự sửa lỗi "không thấy video" (s19 Cáo Fennec, 2026-10-30).

Hồi quy cho log của user: cảnh s19 qua accmua1 — chat.stream xong,
message_done đã qua, nhưng không có delta.presentation nào (36 record
toàn text/status), quét chat.history 120s cũng không thấy. Lỗi đó bị
gói thành GatewayError nên pipeline báo nhầm "KHÔNG KẾT NỐI ĐƯỢC",
đánh lỗi acc, rồi thử y hệt 3 lần và fail cảnh.

Hành vi mới:
- "Không thấy video" là NoVideoError, KHÔNG phải lỗi kết nối
  (is_connection_error False, không báo Telegram, không tính lỗi acc).
- Gặp NoVideoError: nhờ Muse viết lại prompt cho nhẹ nhàng rồi thử
  lại trong cùng cảnh (tối đa 1 lần làm nhẹ), visual gốc trong kịch
  bản KHÔNG bị ghi đè.
- Làm nhẹ không được mà vẫn không có video: cảnh fail sau 3 lần nhưng
  acc không bị tính lỗi (jobs_failed vẫn 0).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automuse.accounts import Account, AccountManager
from automuse.gateway import GatewayError, GatewayClient, NoVideoError
from automuse.pipeline import generate_clips, is_connection_error
from automuse.script import Scene, Script
from automuse.usage import UsageTracker


def _tracker():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.close()
    return UsageTracker(Path(tmp.name))


def _manager(name="acc1"):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.close()
    Path(tmp.name).write_text('{"accounts": []}', encoding="utf-8")
    mgr = AccountManager(Path(tmp.name))
    mgr.accounts = [Account(name=name)]
    mgr.save()
    return mgr


def _settings():
    return {"generation": {"delay_between_jobs": 0,
                           "max_retries_per_scene": 3,
                           "max_wait_rounds": 0}}


def _script(visual):
    return Script(title="Test", scenes=[
        Scene(id="s01", title="Động dục", duration=5, visual=visual),
    ])


class _RefuseOriginalClient:
    """Từ chối prompt gốc (không có video), nhận prompt đã làm nhẹ."""

    def __init__(self, original):
        self.original = original
        self.prompts: list[str] = []

    def generate_video(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        if kwargs["prompt"] == self.original:
            raise NoVideoError(
                "Không thấy video trong chat.history sau 120s (đã qua "
                "delta.message_done mà không có delta.presentation)")
        Path(kwargs["out_path"]).write_bytes(b"fake-clip")


class _AlwaysRefuseClient:
    """Không bao giờ trả video, bất kể prompt nào."""

    def __init__(self):
        self.calls = 0

    def generate_video(self, **kwargs):
        self.calls += 1
        raise NoVideoError(
            "Không thấy video trong chat.history sau 120s (đã qua "
            "delta.message_done mà không có delta.presentation)")


class TestNoVideoClassification(unittest.TestCase):
    def test_no_video_is_not_connection_error(self):
        self.assertFalse(is_connection_error(NoVideoError("không thấy video")))
        self.assertTrue(is_connection_error(
            GatewayError("__HATCH_GATEWAY_INIT__ thiếu khóa authToken")))

    def test_gateway_history_timeout_raises_no_video(self):
        """retrieve_video_via_history hết timeout mà không có
        delta.presentation -> NoVideoError (không phải GatewayError)."""
        client = GatewayClient(Account(name="t"), {})
        client.rpc = lambda *args, **kwargs: {}
        dest = Path(tempfile.mkdtemp()) / "clip.mp4"
        with self.assertRaises(NoVideoError):
            client.retrieve_video_via_history(0, dest, timeout=0)


class TestAutoSoften(unittest.TestCase):
    def test_soften_and_retry_succeeds(self):
        """Lần 1 bị từ chối -> prompt làm nhẹ -> lần 2 xong, acc không
        bị tính lỗi, visual gốc trong kịch bản giữ nguyên."""
        original = "mating explicit"
        softened = "two fennec foxes nuzzling at dawn"
        client = _RefuseOriginalClient(original)
        script = _script(original)
        out = Path(tempfile.mkdtemp())
        mgr = _manager()
        with mock.patch("automuse.pipeline.build_client",
                        return_value=client), \
                mock.patch("automuse.pipeline.extend_clip",
                           side_effect=lambda tmp, dest, target: tmp), \
                mock.patch("automuse.pipeline.telegram_configured",
                           return_value=False), \
                mock.patch("automuse.prompting.ask_muse_text",
                           return_value=softened):
            clips, infos = generate_clips(script, out, _settings(), mgr,
                                          mock=False, tracker=_tracker())
        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].exists())
        self.assertEqual(client.prompts, [original, softened])
        self.assertEqual(script.scenes[0].visual, original)
        acc = mgr.accounts[0]
        self.assertEqual(acc.jobs_failed, 0)

    def test_soften_unavailable_still_fails_without_blaming_account(self):
        """Viết lại không được (trả về y nguyên) mà vẫn không có video:
        cảnh fail sau 3 lần, nhưng acc không bị tính lỗi nào."""
        client = _AlwaysRefuseClient()
        out = Path(tempfile.mkdtemp())
        mgr = _manager()
        with mock.patch("automuse.pipeline.build_client",
                        return_value=client), \
                mock.patch("automuse.pipeline.extend_clip",
                           side_effect=lambda tmp, dest, target: tmp), \
                mock.patch("automuse.pipeline.telegram_configured",
                           return_value=False), \
                mock.patch("automuse.prompting.ask_muse_text",
                           return_value="mating explicit"):
            with self.assertRaises(RuntimeError) as ctx:
                generate_clips(_script("mating explicit"), out, _settings(),
                               mgr, mock=False, tracker=_tracker())
        self.assertIn("thất bại sau 3 lần", str(ctx.exception))
        self.assertEqual(client.calls, 3)
        self.assertEqual(mgr.accounts[0].jobs_failed, 0)


if __name__ == "__main__":
    unittest.main()
