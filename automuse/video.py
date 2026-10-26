"""Xử lý video bằng ffmpeg: chuẩn hóa clip, nối, lồng tiếng, iris-out."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from .audio import run_ffmpeg


def probe(path: str | Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=width,height,r_frame_rate,codec_type",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def video_size(path: str | Path) -> tuple[int, int, float]:
    info = probe(path)
    stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    num, den = stream["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    return int(stream["width"]), int(stream["height"]), fps


def make_poster(src: str | Path, dst: str | Path | None = None,
                at_seconds: float = 1.0, width: int = 640) -> Path:
    """Chụp 1 khung hình làm ảnh đại diện (poster) cho tab Kết quả.

    Tab Kết quả hiển thị ảnh thay cho thẻ <video>: mỗi thẻ chỉ tải ~50KB
    thay vì chạm vào file video hàng chục MB. Poster được tạo lúc build
    xong (và có thể tạo lại cho project cũ).
    """
    src = Path(src)
    dst = Path(dst) if dst else src.with_name("poster.jpg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = float(probe(src)["format"]["duration"])
    at = max(0.0, min(at_seconds, max(0.0, duration - 0.5)))
    cmd = [
        "ffmpeg", "-y", "-ss", f"{at:.2f}", "-i", str(src),
        "-vframes", "1", "-vf", f"scale={width}:-2",
        "-q:v", "4", str(dst),
    ]
    run_ffmpeg(cmd)
    return dst


def normalize_clip(src: str | Path, dst: str | Path, width: int, height: int, fps: int) -> Path:
    """Scale/pad về đúng khung hình + fps chung để nối clip không lỗi."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={fps},format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast",
        "-video_track_timescale", "90000", str(dst),
    ]
    run_ffmpeg(cmd)
    return dst


def extend_clip(src: str | Path, dst: str | Path, target_duration: float) -> Path:
    """Kéo dài clip bằng cách đóng băng khung hình cuối (lời thoại dài hơn clip)."""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    current = float(probe(src)["format"]["duration"])
    if current >= target_duration - 0.05:
        return src
    extra = target_duration - current
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"tpad=stop_mode=clone:stop_duration={extra:.2f}",
        "-t", f"{target_duration:.2f}",
        "-an", "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", str(dst),
    ]
    run_ffmpeg(cmd)
    return dst


def concat_clips(clips: list[Path | str], out_path: str | Path) -> Path:
    """Nối các clip đã chuẩn hóa (cùng codec/khung hình) bằng concat demuxer."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    list_file = out.parent / "concat_list.txt"
    list_file.write_text(
        "".join(f"file '{Path(c).resolve()}'\n" for c in clips), encoding="utf-8"
    )
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(out),
    ]
    run_ffmpeg(cmd)
    return out


def assemble_final(
    video_silent: str | Path,
    audio_wav: str | Path | None,
    out_path: str | Path,
    srt_path: str | Path | None = None,
    fade_out_seconds: float = 1.0,
    crf: int = 20,
    preset: str = "medium",
) -> Path:
    """Ghép audio + fade-out cuối video + nhúng phụ đề (mov_text track).

    Phụ đề được remux ở pass riêng: nhét track mov_text chung pass encode
    với -shortest làm ffmpeg treo (track phụ đề thưa, muxer chờ mãi).
    Fade-out tính theo độ dài cuối thực tế (ngắn hơn trong video/audio).
    """
    from .audio import probe_duration

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    video_duration = float(probe(video_silent)["format"]["duration"])
    final_duration = video_duration
    if audio_wav:
        final_duration = min(video_duration, probe_duration(audio_wav))
    fade_start = max(0.0, final_duration - fade_out_seconds)

    tmp_out = out.with_suffix(".no-sub.mp4") if srt_path else out
    cmd = ["ffmpeg", "-y", "-i", str(video_silent)]
    if audio_wav:
        cmd += ["-i", str(audio_wav)]
    cmd += [
        "-vf", f"fade=t=out:st={fade_start:.2f}:d={fade_out_seconds:.2f}",
        "-map", "0:v",
    ]
    if audio_wav:
        cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-shortest", str(tmp_out),
    ]
    run_ffmpeg(cmd)

    if srt_path:
        # Pass 2: chỉ copy stream + thêm phụ đề (nhanh, không encode lại)
        remux_cmd = [
            "ffmpeg", "-y", "-i", str(tmp_out), "-i", str(srt_path),
            "-map", "0", "-map", "1",
            "-c", "copy", "-c:s", "mov_text",
            "-metadata:s:s:0", "language=vie",
            "-movflags", "+faststart", str(out),
        ]
        run_ffmpeg(remux_cmd)
        tmp_out.unlink(missing_ok=True)
    return out


def apply_iris_out(src: str | Path, dst: str | Path, iris_seconds: float = 1.0) -> bool:
    """Thu hình tròn (iris-out) ở 1 giây cuối. Trả False nếu filter lỗi.

    Dùng geq tạo mặt nạ alpha hình tròn co dần, overlay lên nền đen.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = probe(src)
        duration = float(info["format"]["duration"])
        w, h, _ = video_size(src)
    except Exception:
        return False
    if duration <= iris_seconds + 0.5:
        return False
    iris_seconds = min(iris_seconds, duration - 0.5)
    head_end = duration - iris_seconds
    radius = math.hypot(w, h) / 2
    # geq: alpha=255 trong vòng tròn bán kính R*(1-T/D), 0 ngoài vòng tròn.
    geq = (
        "geq=lum='lum(X,Y)':"
        f"a='if(lt(hypot(X-{w}/2\\,Y-{h}/2)\\,{radius:.1f}*(1-T/{iris_seconds:.2f}))\\,255\\,0)'"
    )
    fc = (
        f"[0:v]split=2[head][tail];"
        f"[head]trim=end={head_end:.2f},setpts=PTS-STARTPTS[vhead];"
        f"[tail]trim=start={head_end:.2f},setpts=PTS-STARTPTS,"
        f"format=yuva420p,{geq}[vtail];"
        f"color=c=black:s={w}x{h}:d={iris_seconds:.2f},format=yuva420p[bg];"
        f"[bg][vtail]overlay=shortest=1,format=yuv420p[vtailbg];"
        f"[vhead][vtailbg]concat=n=2:v=1:a=0[vout]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "0:a?", "-map", "0:s?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-c:s", "copy",
        "-movflags", "+faststart", str(dst),
    ]
    try:
        run_ffmpeg(cmd)
        return dst.exists() and dst.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False
