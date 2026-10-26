"""Tests: check kết quả sau khi gửi prompt — submit/poll/download.
Dùng session giả lập (không gọi Muse thật).
Chạy: python -m unittest discover -s tests -v"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automuse.accounts import Account  # noqa: E402
from automuse.config import DEFAULT_SETTINGS  # noqa: E402
from automuse.muse_client import MuseClient, MuseError  # noqa: E402


def _settings(**kw):
    s = {"muse": dict(DEFAULT_SETTINGS["muse"])}
    s["muse"].update(kw)
    return s


def _resp(status=200, payload=None, text=""):
    r = Mock()
    r.status_code = status
    r.text = text if text else str(payload)
    r.raise_for_status = Mock()
    if payload is None:
        r.json = Mock(side_effect=ValueError("No JSON"))
    else:
        r.json = Mock(return_value=payload)
    return r


def _download_resp(content=b"fake-video-bytes"):
    r = Mock()
    r.status_code = 200
    r.raise_for_status = Mock()
    r.iter_content = Mock(return_value=[content])
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    return r


def _client(posts=None, gets=None, **kw):
    acc = Account(name="t", cookie="a=b")
    muse_kw = {"poll_interval": 0, "poll_timeout": 30}
    muse_kw.update(kw)
    client = MuseClient(acc, _settings(**muse_kw))
    client.session = Mock()
    if posts is not None:
        client.session.post = Mock(side_effect=list(posts))
    if gets is not None:
        client.session.get = Mock(side_effect=list(gets))
    return client


class TestAuthError(unittest.TestCase):
    def _resp(self, status, body):
        r = Mock()
        r.status_code = status
        r.text = body
        return r

    def test_401_means_expired_cookie(self):
        from automuse.muse_client import _auth_error

        err = _auth_error(401, "submit video", self._resp(401, '{"error":"Authentication required"}'))
        self.assertIn("401", str(err))
        self.assertIn("hết hạn", str(err))

    def test_403_means_forbidden_not_expired(self):
        from automuse.muse_client import _auth_error

        err = _auth_error(403, "submit video", self._resp(403, '{"error":"Forbidden"}'))
        self.assertIn("403", str(err))
        self.assertNotIn("hết hạn", str(err))
        self.assertIn("Forbidden", str(err))


class TestSubmit(unittest.TestCase):
    def test_job_id_top_level(self):
        c = _client(posts=[_resp(200, {"job_id": "j1"})])
        self.assertEqual(c._submit_video("p", "16:9", 5, None), ("j1", None))

    def test_job_id_nested(self):
        c = _client(posts=[_resp(200, {"data": {"id": "j2", "status": "processing"}})])
        self.assertEqual(c._submit_video("p", "16:9", 5, None), ("j2", None))

    def test_direct_video_url(self):
        c = _client(posts=[_resp(200, {"video_url": "https://cdn.example.com/v.mp4"})])
        self.assertEqual(
            c._submit_video("p", "16:9", 5, None),
            (None, "https://cdn.example.com/v.mp4"),
        )

    def test_direct_url_nested(self):
        c = _client(posts=[_resp(200, {"data": {"url": "https://cdn.example.com/v.mp4"}})])
        self.assertEqual(
            c._submit_video("p", "16:9", 5, None),
            (None, "https://cdn.example.com/v.mp4"),
        )

    def test_unknown_shape_reports_raw(self):
        c = _client(posts=[_resp(200, {"weird": True})])
        with self.assertRaises(MuseError) as cm:
            c._submit_video("p", "16:9", 5, None)
        self.assertIn("Response lạ", str(cm.exception))
        self.assertIn("weird", str(cm.exception))

    def test_non_json_body(self):
        c = _client(posts=[_resp(200, None, text="<html>error</html>")])
        with self.assertRaises(MuseError) as cm:
            c._submit_video("p", "16:9", 5, None)
        self.assertIn("không phải JSON", str(cm.exception))

    def test_401_expired(self):
        c = _client(posts=[_resp(401, {"error": "x"})])
        with self.assertRaises(MuseError) as cm:
            c._submit_video("p", "16:9", 5, None)
        self.assertIn("hết hạn", str(cm.exception))

    def test_429_rate_limit(self):
        c = _client(posts=[_resp(429, {"error": "slow down"})])
        with self.assertRaises(MuseError) as cm:
            c._submit_video("p", "16:9", 5, None)
        self.assertIn("rate-limit", str(cm.exception))


class TestPoll(unittest.TestCase):
    def test_processing_then_done_nested(self):
        c = _client(gets=[
            _resp(200, {"status": "processing", "progress": 40}),
            _resp(200, {"data": {"state": "finished", "result": {"video_url": "https://cdn.example.com/v.mp4"}}}),
        ])
        data = c._poll("j1")
        self.assertEqual(data["data"]["state"], "finished")

    def test_failed_status(self):
        c = _client(gets=[_resp(200, {"status": "failed", "error": "boom"})])
        with self.assertRaises(MuseError) as cm:
            c._poll("j1")
        self.assertIn("thất bại", str(cm.exception))

    def test_no_status_but_video_url(self):
        c = _client(gets=[_resp(200, {"video_url": "https://cdn.example.com/v.mp4"})])
        data = c._poll("j1")
        self.assertIn("video_url", data)

    def test_timeout_includes_last_response(self):
        c = _client(poll_timeout=1)
        c.session.get = Mock(return_value=_resp(200, {"status": "processing"}))
        with self.assertRaises(MuseError) as cm:
            c._poll("j1")
        self.assertIn("quá thời gian chờ", str(cm.exception))
        self.assertIn("processing", str(cm.exception))


class TestGenerateVideoEndToEnd(unittest.TestCase):
    def test_job_flow_downloads_nested_url(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "clip.mp4"
            c = _client(
                posts=[_resp(200, {"job_id": "j9"})],
                gets=[
                    _resp(200, {"status": "processing"}),
                    _resp(200, {"data": {"status": "completed", "video_url": "https://cdn.example.com/v.mp4"}}),
                    _download_resp(),
                ],
            )
            out = c.generate_video("p", "16:9", 5, out_path=dest)
            self.assertEqual(out, dest)
            self.assertEqual(dest.read_bytes(), b"fake-video-bytes")

    def test_direct_url_flow(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "clip.mp4"
            c = _client(
                posts=[_resp(200, {"url": "https://cdn.example.com/v.mp4"})],
                gets=[_download_resp()],
            )
            out = c.generate_video("p", "16:9", 5, out_path=dest)
            self.assertEqual(dest.read_bytes(), b"fake-video-bytes")

    def test_done_but_no_url(self):
        c = _client(
            posts=[_resp(200, {"job_id": "j9"})],
            gets=[_resp(200, {"status": "completed"})],
        )
        with self.assertRaises(MuseError) as cm:
            c.generate_video("p", "16:9", 5, out_path=Path("/tmp/x.mp4"))
        self.assertIn("không có link video", str(cm.exception))


class TestGenerateImage(unittest.TestCase):
    def test_direct_image_url(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "img.png"
            c = _client(
                posts=[_resp(200, {"image_url": "https://cdn.example.com/i.png"})],
                gets=[_download_resp(b"fake-image")],
            )
            out = c.generate_image("p", out_path=dest)
            self.assertEqual(dest.read_bytes(), b"fake-image")

    def test_job_then_nested_url(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "img.png"
            c = _client(
                posts=[_resp(200, {"job_id": "ji"})],
                gets=[
                    _resp(200, {"result": {"status": "done", "url": "https://cdn.example.com/i.png"}}),
                    _download_resp(b"fake-image"),
                ],
            )
            out = c.generate_image("p", out_path=dest)
            self.assertEqual(dest.read_bytes(), b"fake-image")


if __name__ == "__main__":
    unittest.main()
