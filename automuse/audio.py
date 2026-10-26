"""Xử lý âm thanh: timeline lời thoại, nhạc nền, phụ đề SRT."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess:
    """Chạy ffmpeg, ghi log ra file tạm thay vì pipe.

    (Dùng capture_output với ffmpeg dài sẽ làm đầy pipe stderr -> ffmpeg
    bị treo chờ, đây là lỗi deadlock kinh điển của subprocess.)
    """
    fd, log_path = tempfile.mkstemp(prefix="automuse-ffmpeg-", suffix=".log")
    ok = False
    try:
        with os.fdopen(fd, "w") as log:
            result = subprocess.run(
                cmd, stdout=log, stderr=subprocess.STDOUT, text=True
            )
        if result.returncode != 0:
            tail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-3000:]
            raise subprocess.CalledProcessError(result.returncode, cmd, output=tail)
        ok = True
        return result
    finally:
        if ok:
            try:
                os.unlink(log_path)
            except OSError:
                pass
        else:
            print(f"[ffmpeg log giữ lại tại: {log_path}]")


def probe_duration(path: str | Path) -> float:
    """Độ dài (giây) của file audio/video bằng ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def build_narration_timeline(
    segments: list[tuple[Path, float]],
    total_duration: float,
    out_path: str | Path,
) -> Path:
    """Ghép các file lời thoại vào đúng thời điểm bắt đầu của từng cảnh.

    segments: [(file_wav, start_seconds), ...]
    Dùng adelay + amix rồi apad cho đủ tổng độ dài video.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not segments:
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"anullsrc=r=44100:cl=stereo:d={total_duration:.2f}",
            "-c:a", "pcm_s16le", str(out),
        ]
        run_ffmpeg(cmd)
        return out

    inputs: list[str] = []
    filters: list[str] = []
    for i, (wav, start) in enumerate(segments):
        inputs += ["-i", str(wav)]
        delay_ms = max(0, int(start * 1000))
        filters.append(f"[{i}:a]adelay={delay_ms}:all=1[a{i}]")
    mix_inputs = "".join(f"[a{i}]" for i in range(len(segments)))
    filters.append(
        f"{mix_inputs}amix=inputs={len(segments)}:normalize=0,"
        f"apad=whole_dur={total_duration:.2f}[aout]"
    )
    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", ";".join(filters),
           "-map", "[aout]", "-c:a", "pcm_s16le", str(out)]
    run_ffmpeg(cmd)
    return out


def mix_with_music(
    narration_wav: str | Path,
    music_path: str | Path | None,
    out_path: str | Path,
    music_volume: float = 0.15,
) -> Path:
    """Trộn nhạc nền nhỏ dưới lời thoại + loudnorm chuẩn hóa âm lượng."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not music_path:
        cmd = [
            "ffmpeg", "-y", "-i", str(narration_wav),
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:a", "pcm_s16le", str(out),
        ]
        run_ffmpeg(cmd)
        return out
    cmd = [
        "ffmpeg", "-y",
        "-i", str(narration_wav), "-i", str(music_path),
        "-filter_complex",
        f"[1:a]volume={music_volume},aloop=loop=-1:size=2e+09[m];"
        f"[0:a][m]amix=inputs=2:normalize=0,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11[aout]",
        "-map", "[aout]", "-c:a", "pcm_s16le", str(out),
    ]
    run_ffmpeg(cmd)
    return out


# ---------------------------------------------------------------- subtitles

def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(entries: list[tuple[float, float, str]], out_path: str | Path) -> Path:
    """entries: [(start, end, text), ...] -> file .srt UTF-8."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i, (start, end, text) in enumerate(entries, 1):
        text = " ".join((text or "").split())
        if not text:
            continue
        lines += [str(i), f"{_srt_time(start)} --> {_srt_time(end)}", text, ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
