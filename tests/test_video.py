"""Test make_poster (ảnh đại diện cho tab Kết quả).

Chạy: python -m unittest discover -s tests -v
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automuse.video import make_poster, probe  # noqa: E402


def _make_test_video(path: Path, duration: float = 2.0) -> Path:
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc2=size=320x180:rate=30:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


class TestMakePoster(unittest.TestCase):
    def test_poster_is_small_jpeg(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_test_video(Path(d) / "final.mp4")
            poster = make_poster(src)
            self.assertEqual(poster.name, "poster.jpg")
            data = poster.read_bytes()
            self.assertTrue(data.startswith(b"\xff\xd8"), "poster phải là JPEG")
            # Ảnh đại diện chỉ ~vài chục KB, không phải hàng MB như video
            self.assertLess(poster.stat().st_size, 100 * 1024)
            info = probe(str(poster))
            stream = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual(int(stream["width"]), 640)

    def test_short_video_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            src = _make_test_video(Path(d) / "final.mp4", duration=0.5)
            poster = make_poster(src)
            self.assertTrue(poster.exists())


if __name__ == "__main__":
    unittest.main()
