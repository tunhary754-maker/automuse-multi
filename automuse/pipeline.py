"""Pipeline video dài: kịch bản -> clip từng cảnh -> lồng tiếng -> file cuối.

Luồng chạy:
  1. Đọc kịch bản (YAML/Markdown), validate.
  2. ElevenLabs đọc lời thoại từng cảnh (cache theo hash).
  3. Muse sinh clip từng cảnh, chia đều qua các acc (round-robin);
     acc lỗi thì tự đổi acc khác, tối đa max_retries_per_scene lần.
  4. Chuẩn hóa + nối clip, căn lời thoại đúng mốc thời gian từng cảnh.
  5. Ghép audio (+ nhạc nền), phụ đề SRT, fade-out / iris-out -> final.mp4
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from .accounts import AccountManager
from .audio import build_narration_timeline, mix_with_music, probe_duration, write_srt
from .config import DEFAULT_CACHE_DIR, DEFAULT_OUTPUT_DIR
from .elevenlabs import build_elevenlabs, estimate_speech_duration
from .muse_client import build_client
from .script import Scene, Script, load_script, validate
from .usage import UsageTracker
from .video import (
    apply_iris_out,
    assemble_final,
    concat_clips,
    extend_clip,
    normalize_clip,
)

ASPECT_SIZE = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _scene_target_duration(scene: Scene, tts_duration: float) -> float:
    """Clip phải đủ dài để chứa hết lời thoại (+0.5s thở)."""
    if tts_duration <= 0:
        return float(scene.duration)
    return max(float(scene.duration), tts_duration + 0.5)


def _describe_ineligible(pool, tracker, settings) -> str:
    """Lý do từng acc không dùng được — tách cooldown (lỗi liên tiếp)
    khỏi hết quota tuần, để thông báo không đổ oan cho giới hạn tuần."""
    cooling, exhausted = [], []
    for a in pool:
        r = tracker.remaining(a, settings)
        if a.is_on_cooldown():
            wait = max(0, int(a.cooldown_until - time.time()))
            cooling.append(f"{a.name} (đang nghỉ sau lỗi, thử lại sau ~{wait}s)")
        else:
            exhausted.append(
                f"{a.name} (đã dùng {r['videos']}/{r['max_videos'] or '∞'} video, "
                f"{r['minutes']}/{r['max_minutes'] or '∞'} phút)"
            )
    parts = []
    if cooling:
        parts.append("acc đang nghỉ sau 3 lỗi liên tiếp — " + ", ".join(cooling))
    if exhausted:
        parts.append(
            "acc hết giới hạn tuần — " + ", ".join(exhausted)
            + ". Tăng giới hạn trong settings.yaml hoặc `accounts set-limit`, "
            "hoặc đợi sang tuần mới."
        )
    return "Không còn acc nào dùng được: " + "; ".join(parts) + "."


def generate_clips(
    script: Script,
    out_dir: Path,
    settings: dict,
    manager: AccountManager,
    mock: bool = False,
    account_names: list[str] | None = None,
    scene_durations: dict[str, float] | None = None,
    resume: bool = False,
    tracker: UsageTracker | None = None,
) -> tuple[list[Path], list[dict]]:
    """Sinh clip cho từng cảnh. Trả về (danh sách clip, thông tin từng cảnh)."""
    tracker = tracker or UsageTracker()
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    width, height = ASPECT_SIZE.get(script.aspect, (1280, 720))
    max_retries = int(settings.get("generation", {}).get("max_retries_per_scene", 3))
    delay = float(settings.get("generation", {}).get("delay_between_jobs", 2))

    clips: list[Path] = []
    infos: list[dict] = []
    for scene in script.scenes:
        target = (scene_durations or {}).get(scene.id, float(scene.duration))
        dest = clips_dir / f"{scene.id}.mp4"
        if resume and dest.exists() and dest.stat().st_size > 0:
            log(f"{scene.id} đã có clip, bỏ qua (resume)")
            clips.append(dest)
            infos.append({"id": scene.id, "account": "cached", "duration": target})
            continue

        # -- giới hạn tuần: chỉ dùng acc còn quota, hết thì dừng rõ ràng --
        eligible_names: list[str] | None = None
        if mock:
            if not tracker.has_budget("mock", settings):
                r = tracker.remaining("mock", settings)
                raise RuntimeError(
                    f"Hết giới hạn tuần: đã dùng {r['videos']} video, "
                    f"{r['minutes']} phút trong tuần {r['week']} — "
                    "tăng limits.weekly_max_videos/minutes trong settings.yaml"
                )
        else:
            pool = manager.enabled_accounts()
            if account_names:
                wanted = set(account_names)
                pool = [a for a in pool if a.name in wanted]
            if pool:
                eligible = [
                    a for a in pool
                    if not a.is_on_cooldown() and tracker.has_budget(a, settings)
                ]
                if not eligible:
                    raise RuntimeError(_describe_ineligible(pool, tracker, settings))
                eligible_names = [a.name for a in eligible]

        last_error = ""
        done = False
        for attempt in range(1, max_retries + 1):
            account = manager.pick(eligible_names or account_names) if not mock else None
            if account is None and not mock:
                # pick() trả None khi không còn acc nào chọn được — vd: acc
                # duy nhất vừa vào cooldown sau lần lỗi trước. Đừng lặng lẽ
                # rơi sang mock client rồi sinh clip giả mạo clip thật.
                if not pool:
                    raise RuntimeError(
                        "Không có tài khoản nào dùng được"
                        + (f" (không có acc enabled tên {account_names})"
                           if account_names else "")
                        + ". Thêm acc bằng `accounts add`."
                    )
                raise RuntimeError(_describe_ineligible(pool, tracker, settings))
            client = build_client(account, settings, mock=mock)
            who = account.name if account else "mock"
            log(f"{scene.id} (lần {attempt}/{max_retries}) qua acc '{who}' — {scene.title}")
            try:
                tmp = clips_dir / f"{scene.id}.tmp.mp4"
                client.generate_video(
                    prompt=scene.visual,
                    aspect=script.aspect,
                    duration=max(1, math.ceil(target)),
                    reference_image=scene.reference_image or None,
                    out_path=tmp,
                )
                # Kéo dài clip nếu lời thoại dài hơn clip sinh ra
                extended = extend_clip(tmp, dest, target)
                if extended == tmp:
                    tmp.replace(dest)  # clip đã đủ dài, chuyển file tạm thành đích
                elif tmp.exists():
                    tmp.unlink(missing_ok=True)
                if account:
                    manager.mark_success(account)
                    tracker.record(account.name, "video", target, project=script.title)
                clips.append(dest)
                infos.append({"id": scene.id, "account": who, "duration": target})
                done = True
                break
            except Exception as e:  # noqa: BLE001 - retry acc khác
                last_error = str(e)
                log(f"  ! Lỗi: {last_error}")
                if account:
                    manager.mark_failure(account)
                time.sleep(delay)
        if not done:
            raise RuntimeError(f"{scene.id} thất bại sau {max_retries} lần: {last_error}")
        time.sleep(delay)
    return clips, infos


def run_build(
    script_path: str | Path,
    out_dir: str | Path | None = None,
    settings: dict | None = None,
    manager: AccountManager | None = None,
    mock: bool = False,
    no_tts: bool = False,
    iris_out: bool = True,
    resume: bool = False,
    account_names: list[str] | None = None,
    music: str | None = None,
    api_key: str = "",
    tracker: UsageTracker | None = None,
) -> dict:
    """Chạy toàn bộ pipeline, trả về manifest dict."""
    settings = settings or {}
    manager = manager or AccountManager()
    tracker = tracker or UsageTracker()
    script = load_script(script_path)
    errors = validate(script)
    if errors:
        raise ValueError("Kịch bản lỗi:\n- " + "\n- ".join(errors))

    out_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_DIR / Path(script_path).stem
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    width, height = ASPECT_SIZE.get(script.aspect, (1280, 720))
    fps = int(settings.get("video", {}).get("fps", 30))
    log(f"Dự án: {script.title} — {len(script.scenes)} cảnh, {script.aspect}, "
        f"tổng ~{script.total_duration}s")

    # -- 1. Lồng tiếng -------------------------------------------------
    tts_files: dict[str, Path] = {}
    tts_durations: dict[str, float] = {}
    if not no_tts:
        eleven = build_elevenlabs(
            api_key, settings, mock=mock, cache_dir=DEFAULT_CACHE_DIR / "tts"
        )
        for scene in script.scenes:
            if not scene.tts_text:
                continue
            voice_name = scene.voice or script.default_voice
            voice_id = script.voices.get(voice_name) or voice_name
            if not mock:
                try:
                    voice_id = eleven.resolve_voice(voice_id)
                except Exception as e:  # noqa: BLE001
                    log(f"  ! Không resolve được giọng '{voice_name}': {e}")
            dest = audio_dir / f"{scene.id}.{'wav' if mock else 'mp3'}"
            if resume and dest.exists() and dest.stat().st_size > 0:
                log(f"{scene.id} đã có audio, bỏ qua (resume)")
            else:
                log(f"TTS {scene.id} giọng '{voice_name}' ({len(scene.tts_text)} ký tự)")
                dest = eleven.tts(scene.tts_text, voice_id, out_path=dest)
            tts_files[scene.id] = dest
            try:
                tts_durations[scene.id] = probe_duration(dest)
            except Exception:  # noqa: BLE001
                tts_durations[scene.id] = estimate_speech_duration(scene.tts_text)
    else:
        log("Bỏ qua lồng tiếng (--no-tts)")

    scene_durations = {
        s.id: _scene_target_duration(s, tts_durations.get(s.id, 0.0))
        for s in script.scenes
    }

    # -- 2. Sinh clip -------------------------------------------------
    clips, infos = generate_clips(
        script, out_dir, settings, manager, mock=mock,
        account_names=account_names, scene_durations=scene_durations, resume=resume,
        tracker=tracker,
    )

    # -- 3. Chuẩn hóa + nối -------------------------------------------
    norm_dir = out_dir / "clips_norm"
    norm_dir.mkdir(parents=True, exist_ok=True)
    norm_clips = []
    for clip, scene in zip(clips, script.scenes):
        norm = norm_dir / f"{scene.id}.mp4"
        if resume and norm.exists() and norm.stat().st_size > 0:
            norm_clips.append(norm)
            continue
        log(f"Chuẩn hóa {scene.id} -> {width}x{height}@{fps}")
        norm_clips.append(normalize_clip(clip, norm, width, height, fps))

    silent_video = out_dir / "video_silent.mp4"
    log("Nối các cảnh thành video...")
    concat_clips(norm_clips, silent_video)

    # -- 4. Audio timeline + nhạc nền ----------------------------------
    final_audio: Path | None = None
    srt_path: Path | None = None
    if tts_files:
        starts: list[float] = []
        cursor = 0.0
        for scene in script.scenes:
            starts.append(cursor)
            cursor += scene_durations[scene.id]
        total = cursor
        segments = [(tts_files[s.id], starts[i])
                    for i, s in enumerate(script.scenes) if s.id in tts_files]
        narration_wav = audio_dir / "narration.wav"
        log("Ghép timeline lời thoại...")
        build_narration_timeline(segments, total, narration_wav)
        music_path = music or script.music or None
        final_audio = audio_dir / "final_audio.wav"
        log("Trộn nhạc nền + chuẩn hóa âm lượng...")
        mix_with_music(narration_wav, music_path, final_audio)
        srt_entries = [
            (starts[i], starts[i] + tts_durations.get(s.id, scene_durations[s.id]),
             s.narration)
            for i, s in enumerate(script.scenes) if s.tts_text
        ]
        srt_path = out_dir / "subtitles.srt"
        write_srt(srt_entries, srt_path)
        log(f"Phụ đề: {srt_path}")

    # -- 5. Ghép thành phẩm -------------------------------------------
    final_path = out_dir / "final.mp4"
    log("Ghép audio + fade-out -> final.mp4 ...")
    assemble_final(
        silent_video, final_audio, final_path, srt_path=srt_path,
        fade_out_seconds=float(settings.get("video", {}).get("fade_out_seconds", 1.0)),
        crf=int(settings.get("video", {}).get("crf", 20)),
        preset=str(settings.get("video", {}).get("preset", "medium")),
    )

    if iris_out:
        iris_seconds = float(settings.get("video", {}).get("iris_out_seconds", 1.0))
        iris_path = out_dir / "final_iris.mp4"
        log("Áp iris-out ở 1 giây cuối...")
        if apply_iris_out(final_path, iris_path, iris_seconds):
            iris_path.replace(final_path)
            log("Iris-out OK")
        else:
            log("  ! Iris-out không áp được, giữ bản fade-out")

    manifest = {
        "title": script.title,
        "kind": script.kind,
        "aspect": script.aspect,
        "scenes": infos,
        "scene_durations": scene_durations,
        "final": str(final_path),
        "subtitles": str(srt_path) if srt_path else None,
        "mock": mock,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"XONG: {final_path}")
    return manifest
