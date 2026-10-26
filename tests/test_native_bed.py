"""Test tiếng gốc của clip làm nền (native bed).

Hồi quy cho tối 2026-10-26: tập "Vua của thảo nguyên" bị user chê
"trông giả vcl" vì pipeline bỏ tiếng gốc của clip (chuẩn hóa dùng -an)
rồi đắp nhạc/ambience ngoài vào. Giờ pipeline trích tiếng gốc từng clip
làm nền dưới lời dẫn, ở mức đã hiệu chuẩn trên bản tay tối đó.

Chạy: python -m unittest discover -s tests -v
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automuse.audio import (  # noqa: E402
    NATIVE_BED_VOLUME,
    clip_has_audio,
    extract_native_bed,
    mix_with_music,
    probe_duration,
)


def _mean_volume(path: Path, start: float, dur: float) -> float:
    cmd = ["ffmpeg", "-hide_banner", "-ss", f"{start}", "-t", f"{dur}",
           "-i", str(path), "-af", "volumedetect",
           "-vn", "-sn", "-dn", "-f", "null", "/dev/null"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            val = line.split("mean_volume:")[1].split("dB")[0].strip()
            return -999.0 if val == "-inf" else float(val)
    raise RuntimeError(f"không đo được volume: {path}")


def _make_clip(path: Path, duration: float = 2.0, with_audio: bool = False) -> Path:
    cmd = ["ffmpeg", "-y", "-f", "lavfi",
           "-i", f"testsrc2=size=320x180:rate=30:duration={duration}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def _make_wav(path: Path, duration: float = 2.0, freq: int | None = None) -> Path:
    src = (f"sine=frequency={freq}:duration={duration}" if freq
           else f"anullsrc=r=44100:cl=stereo:d={duration}")
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", src,
           "-c:a", "pcm_s16le", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def _doc_script(tmp: Path, kind: str) -> Path:
    p = tmp / f"{kind}.yaml"
    p.write_text(
        f"title: Test {kind}\nkind: {kind}\naspect: '16:9'\nscenes:\n"
        "  - id: s01\n    title: Mot canh\n    duration: 5\n"
        "    visual: savanna dawn\n    narration: Xin chao thao nguyen.\n"
        "    voice: narrator\n",
        encoding="utf-8")
    return p


class TestExtractNativeBed(unittest.TestCase):
    def test_audio_clip_then_silent_clip(self):
        """Nửa đầu có tiếng, nửa sau lặng — đúng thứ tự cảnh."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            loud = _make_clip(tmp / "loud.mp4", with_audio=True)
            quiet = _make_clip(tmp / "quiet.mp4", with_audio=False)
            self.assertTrue(clip_has_audio(loud))
            self.assertFalse(clip_has_audio(quiet))
            bed = extract_native_bed(
                [(loud, 2.0), (quiet, 2.0)], tmp / "native_bg.wav")
            self.assertIsNotNone(bed)
            assert bed is not None
            self.assertAlmostEqual(probe_duration(bed), 4.0, delta=0.15)
            self.assertGreater(_mean_volume(bed, 0.5, 1.0), -40)
            self.assertLess(_mean_volume(bed, 2.5, 1.0), -60)

    def test_none_source_is_silence(self):
        """Cảnh lipsync (clip=None) thành khoảng lặng, không nói đúp."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            loud = _make_clip(tmp / "loud.mp4", with_audio=True)
            bed = extract_native_bed(
                [(None, 2.0), (loud, 2.0)], tmp / "native_bg.wav")
            self.assertIsNotNone(bed)
            assert bed is not None
            self.assertAlmostEqual(probe_duration(bed), 4.0, delta=0.15)
            self.assertLess(_mean_volume(bed, 0.5, 1.0), -60)
            self.assertGreater(_mean_volume(bed, 2.5, 1.0), -40)

    def test_all_silent_returns_none(self):
        """Không clip nào có tiếng -> None, pipeline giữ hành vi cũ."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            quiet = _make_clip(tmp / "quiet.mp4", with_audio=False)
            bed = extract_native_bed([(quiet, 2.0)], tmp / "native_bg.wav")
            self.assertIsNone(bed)


class TestMixNativeBed(unittest.TestCase):
    def test_mix_at_calibrated_volume_keeps_duration(self):
        """Trộn ở mức hiệu chuẩn (1.0) — ra đúng độ dài lời thoại."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            narration = _make_wav(tmp / "narration.wav", freq=880)
            bed = _make_wav(tmp / "bed.wav", freq=220)
            out = mix_with_music(narration, bed, tmp / "final.wav",
                                 music_volume=NATIVE_BED_VOLUME)
            self.assertTrue(out.exists())
            self.assertAlmostEqual(probe_duration(out), 2.0, delta=0.15)
            # Cả hai nguồn đều nghe được trong bản trộn
            self.assertGreater(_mean_volume(out, 0.5, 1.0), -40)


class TestRunBuildNativeBed(unittest.TestCase):
    def test_mock_documentary_without_real_audio_reports_none(self):
        """Mock build phim tài liệu: clip mock câm -> audio_bed 'none',
        build vẫn xong như cũ (không vỡ vì tính năng mới)."""
        tmp = Path(tempfile.mkdtemp())
        script = _doc_script(tmp, "documentary")
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3}}
        from automuse.pipeline import run_build
        manifest = run_build(script, out_dir=tmp / "out", settings=settings,
                             mock=True)
        self.assertEqual(manifest["audio_bed"], "none")
        self.assertTrue((tmp / "out" / "final.mp4").exists())

    def test_mock_build_with_bed_wired_as_native(self):
        """Dây nối: có tiếng gốc -> manifest ghi 'native', final có audio."""
        tmp = Path(tempfile.mkdtemp())
        bed = _make_wav(tmp / "fake-bed.wav", duration=5.0, freq=220)
        script = _doc_script(tmp, "documentary")
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3}}
        from automuse.pipeline import run_build
        with mock.patch("automuse.pipeline.extract_native_bed",
                        return_value=bed):
            manifest = run_build(script, out_dir=tmp / "out",
                                 settings=settings, mock=True)
        self.assertEqual(manifest["audio_bed"], "native")
        final = tmp / "out" / "final.mp4"
        self.assertTrue(final.exists())
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(final)],
            capture_output=True, text=True)
        self.assertTrue(info.stdout.strip(), "final phải có track audio")


if __name__ == "__main__":
    unittest.main()
