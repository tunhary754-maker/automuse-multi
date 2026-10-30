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
import re
import time
from pathlib import Path

from .accounts import AccountManager
from .audio import (
    NATIVE_BED_VOLUME,
    build_narration_timeline,
    extract_native_bed,
    mix_with_music,
    probe_duration,
    write_srt,
)
from .config import DEFAULT_CACHE_DIR, DEFAULT_OUTPUT_DIR, get_sync_key
from .elevenlabs import build_elevenlabs, estimate_speech_duration
from .lipsync import build_lipsync
from .muse_client import build_client
from .notify import send_telegram, telegram_configured
from .script import Scene, Script, load_script, validate
from .usage import UsageTracker
from .video import (
    apply_iris_out,
    assemble_final,
    concat_clips,
    extend_clip,
    make_poster,
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


def is_connection_error(exc: Exception) -> bool:
    """Acc có 'kết nối được' với Muse không?

    Lỗi tầng gateway (GatewayError: handshake/RPC/stream, thiếu
    __HATCH_GATEWAY_INIT__, thiếu authToken) = không kết nối được — khác
    với lỗi sinh clip (prompt bị từ chối, timeout sinh...). Dùng để báo
    lỗi NGAY khi acc chết, thay vì lặng lẽ chuyển acc khác.
    """
    from .gateway import GatewayError

    if isinstance(exc, GatewayError):
        return True
    msg = str(exc)
    return ("__HATCH_GATEWAY_INIT__" in msg or "authToken" in msg
            or "phiên đăng nhập" in msg)


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


_REFUSAL_HEADS = ("i ", "sorry", "as an ai", "i'm ", "i cannot", "i can't",
                  "tôi ", "xin lỗi", "không thể")


def _soften_visual(client, scene) -> str | None:
    """Nhờ Muse viết lại visual cho nhẹ nhàng khi cảnh bị từ chối video.

    Chỉ dùng cho lần thử lại trong cùng một cảnh — KHÔNG ghi đè visual
    gốc trong kịch bản (kịch bản là tài sản sáng tạo của user, và lời dẫn
    vẫn giữ nguyên sự thật). Trả None nếu không viết lại được (lỗi text
    call, trả về y nguyên, hoặc trả về câu từ chối/giải thích thay vì
    prompt) — caller khi đó xử lý lỗi gốc như bình thường.
    """
    from .prompting import ask_muse_text

    instruction = (
        "Rewrite this video generation prompt so it passes a content "
        "safety filter. Keep the same animals, setting, and general "
        "behavior, but remove any explicit sexual, violent, or graphic "
        "detail; describe only filmable, non-explicit behavior "
        "(courtship, closeness, grooming, playing, caring for young). "
        "Return ONLY the rewritten prompt: no explanation, no quotes, "
        "no code fence.\n"
        f"Scene title: {scene.title}\n"
        f"Original prompt: {scene.visual}"
    )
    try:
        out = ask_muse_text(client, instruction, timeout=120)
    except Exception:
        return None
    out = (out or "").strip()
    if out.startswith("```"):
        out = out[3:].strip()
        if "\n" in out:
            first, rest = out.split("\n", 1)
            if first.strip().isalpha():
                out = rest
        if out.endswith("```"):
            out = out[:-3]
    out = " ".join(out.split())
    if not out or out == scene.visual:
        return None
    low = out.lower()
    if low.startswith(_REFUSAL_HEADS) or "cannot generate" in low[:80]:
        return None
    return out


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
    from .gateway import NoVideoError

    tracker = tracker or UsageTracker()
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    width, height = ASPECT_SIZE.get(script.aspect, (1280, 720))
    max_retries = int(settings.get("generation", {}).get("max_retries_per_scene", 3))
    delay = float(settings.get("generation", {}).get("delay_between_jobs", 2))

    clips: list[Path] = []
    infos: list[dict] = []
    # Acc nào đã báo lỗi kết nối trong build này (báo 1 lần/acc, tránh spam).
    reported_connection: set[str] = set()
    for scene in script.scenes:
        target = (scene_durations or {}).get(scene.id, float(scene.duration))
        dest = clips_dir / f"{scene.id}.mp4"
        if resume and dest.exists() and dest.stat().st_size > 0:
            log(f"{scene.id} đã có clip, bỏ qua (resume)")
            clips.append(dest)
            infos.append({"id": scene.id, "account": "cached", "duration": target})
            continue

        # -- Failover + làm tiếp: acc nào lỗi thì lượt sau ưu tiên acc khác
        # (loại acc vừa lỗi khỏi lượt chọn của cảnh này); hết acc tạm thời
        # (toàn bộ đang cooldown sau lỗi liên tiếp, vd: hết quota thật)
        # thì CHỜ cooldown hết rồi làm tiếp, không dừng build — đúng yêu
        # cầu "hết quota acc này chuyển acc khác làm tiếp nhiệm vụ dở
        # dang". Chỉ dừng khi: không có acc nào, hết quota tuần thật
        # (không phải cooldown — chờ cũng không giúp), hoặc chờ quá giới
        # hạn. Clip đã sinh vẫn nằm trên đĩa, chạy lại với resume để làm
        # tiếp nếu build vẫn dừng.
        gen_cfg = settings.get("generation", {})
        max_wait_rounds = int(gen_cfg.get("max_wait_rounds", 3))
        max_wait_seconds = float(gen_cfg.get("max_wait_for_account_seconds", 1800))
        if mock and not tracker.has_budget("mock", settings):
            r = tracker.remaining("mock", settings)
            raise RuntimeError(
                f"Hết giới hạn tuần: đã dùng {r['videos']} video, "
                f"{r['minutes']} phút trong tuần {r['week']} — "
                "tăng limits.weekly_max_videos/minutes trong settings.yaml"
            )
        waited = 0.0
        wait_rounds = 0

        def _wait_for_cooldown(pool) -> bool:
            """Chờ acc hết cooldown nếu còn lượt chờ. Trả True nếu đã chờ."""
            nonlocal waited, wait_rounds
            cooling = [a for a in pool if a.is_on_cooldown()]
            if (not cooling or wait_rounds >= max_wait_rounds
                    or waited >= max_wait_seconds):
                return False
            wait = min(max(0.0, a.cooldown_until - time.time())
                       for a in cooling) + 2
            wait = max(1.0, min(wait, max_wait_seconds - waited))
            log(f"  ... {scene.id}: hết acc dùng được tạm thời, chờ "
                f"~{int(wait)}s rồi làm tiếp "
                f"(lượt chờ {wait_rounds + 1}/{max_wait_rounds})")
            time.sleep(wait)
            waited += wait
            wait_rounds += 1
            return True

        def _wait_for_new_account(pool) -> bool:
            """Hết acc thật: báo Telegram + chờ user thêm acc mới rồi làm tiếp.

            Chỉ kích hoạt khi đã cấu hình Telegram và wait_for_new_account_
            minutes > 0. Trả True nếu trong lúc chờ xuất hiện acc dùng được
            (acc mới user thêm, hoặc acc cũ hết cooldown) — caller thử lại
            cảnh này. Trả False nếu hết thời gian chờ (caller dừng build,
            clip đã sinh vẫn còn, chạy lại với resume để làm tiếp).
            """
            from .notify import send_telegram, telegram_configured

            wait_minutes = float(gen_cfg.get("wait_for_new_account_minutes", 240))
            poll_seconds = float(gen_cfg.get("new_account_poll_seconds", 60))
            if wait_minutes <= 0 or not telegram_configured():
                return False
            dead = _describe_ineligible(pool, tracker, settings)
            send_telegram(
                f"🎬 Build '{script.title}' dừng ở cảnh {scene.id}: {dead}\n"
                f"Thêm tài khoản Muse mới (tab Tài khoản Muse hoặc "
                f"`accounts add`) — build sẽ tự làm tiếp, không cần chạy lại."
            )
            log(f"  ... {scene.id}: đã báo Telegram, chờ acc mới tối đa "
                f"{wait_minutes:g} phút...")
            total_wait = wait_minutes * 60
            waited_new = 0.0
            added_names: set[str] = set()
            while waited_new < total_wait:
                slept = min(poll_seconds, total_wait - waited_new)
                time.sleep(slept)
                waited_new += slept
                added_names.update(manager.reload_new())
                candidates = manager.enabled_accounts()
                if account_names:
                    # Acc MỚI user thêm để cứu build này thì nhận luôn,
                    # kể cả khi build ban đầu giới hạn acc khác.
                    wanted = set(account_names) | added_names
                    candidates = [a for a in candidates if a.name in wanted]
                eligible_new = [
                    a for a in candidates
                    if not a.is_on_cooldown() and tracker.has_budget(a, settings)
                ]
                if eligible_new:
                    log(f"  ... {scene.id}: đã có acc dùng được "
                        f"({', '.join(a.name for a in eligible_new)}), làm tiếp.")
                    return True
                if added_names:
                    log(f"  ... đã thấy acc mới ({', '.join(sorted(added_names))}) "
                        f"nhưng chưa dùng được, chờ tiếp...")
            send_telegram(
                f"🎬 Build '{script.title}' vẫn chưa có acc mới sau "
                f"{wait_minutes:g} phút, đã dừng ở cảnh {scene.id}. "
                f"Chạy lại với resume để làm tiếp khi có acc."
            )
            return False

        last_error = ""
        done = False
        while not done:
            # Tính lại acc dùng được mỗi vòng (cooldown có thể vừa hết).
            pool: list = []
            eligible_names: list[str] | None = None
            if not mock:
                pool = manager.enabled_accounts()
                if account_names:
                    wanted = set(account_names)
                    pool = [a for a in pool if a.name in wanted]
                if not pool:
                    raise RuntimeError(
                        "Không có tài khoản nào dùng được"
                        + (f" (không có acc enabled tên {account_names})"
                           if account_names else "")
                        + ". Thêm acc bằng `accounts add`."
                    )
                eligible = [
                    a for a in pool
                    if not a.is_on_cooldown() and tracker.has_budget(a, settings)
                ]
                if not eligible:
                    if _wait_for_cooldown(pool):
                        continue
                    if _wait_for_new_account(pool):
                        continue
                    raise RuntimeError(_describe_ineligible(pool, tracker, settings))
                eligible_names = [a.name for a in eligible]

            failed_this_round: set[str] = set()
            # Prompt đang dùng cho cảnh này: ban đầu là visual gốc, nếu
            # model không trả video thì đổi sang bản làm nhẹ (1 lần).
            prompt = scene.visual
            softened = False
            for attempt in range(1, max_retries + 1):
                account = (manager.pick(eligible_names,
                                        exclude=failed_this_round)
                           if not mock else None)
                if account is None and not mock:
                    # Hết acc trong vòng này (tất cả vừa lỗi/cooldown) —
                    # ra vòng ngoài để chờ hoặc dừng rõ ràng, đừng lặng lẽ
                    # rơi sang mock client rồi sinh clip giả mạo clip thật.
                    last_error = "Không còn acc nào dùng được trong lượt này"
                    break
                client = build_client(account, settings, mock=mock)
                who = account.name if account else "mock"
                log(f"{scene.id} (lần {attempt}/{max_retries}) qua acc '{who}' — {scene.title}")
                try:
                    tmp = clips_dir / f"{scene.id}.tmp.mp4"
                    client.generate_video(
                        prompt=prompt,
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
                    if isinstance(e, NoVideoError) and not mock:
                        # Model trả lời xong nhưng không đưa video (thường
                        # do nội dung bị từ chối) — KHÔNG phải acc hỏng:
                        # không tính lỗi acc, không loại acc khỏi lượt,
                        # không báo "không kết nối được". Lần đầu thì nhờ
                        # Muse viết lại prompt cho nhẹ nhàng rồi thử lại;
                        # các lần sau vẫn dùng prompt (đã nhẹ) đó.
                        if not softened:
                            new_prompt = _soften_visual(client, scene)
                            if new_prompt:
                                prompt = new_prompt
                                softened = True
                                log(f"  ... {scene.id}: prompt đã làm nhẹ, "
                                    f"thử lại: {prompt[:160]}")
                        time.sleep(delay)
                        continue
                    if account:
                        manager.mark_failure(account)
                        failed_this_round.add(account.name)
                        # Acc không kết nối được thì BÁO LỖI LUÔN (log nổi
                        # bật + Telegram nếu có) — dù build vẫn làm tiếp
                        # bằng acc khác, user phải biết acc này chết để sửa
                        # cookie, không lặng lẽ bỏ qua. Báo 1 lần/acc/build.
                        if (is_connection_error(e)
                                and account.name not in reported_connection):
                            reported_connection.add(account.name)
                            warn = (
                                f"⚠️ Acc '{account.name}' KHÔNG KẾT NỐI ĐƯỢC: "
                                f"{last_error} — build '{script.title}' đang "
                                f"làm tiếp bằng acc khác."
                            )
                            log(warn)
                            if telegram_configured():
                                send_telegram(warn + " Kiểm tra lại cookie "
                                                   "của acc này khi rảnh.")
                    time.sleep(delay)
            if done:
                break
            # Hết lượt thử mà chưa xong: mock không có cooldown nên dừng
            # ngay; acc thật thì chờ cooldown, rồi chờ acc mới qua Telegram,
            # rồi mới dừng. Khi dừng hẳn mà vẫn còn acc đang nghỉ thì kèm
            # lý do cooldown vào lỗi (để không đổ oan cho hết quota tuần).
            if mock:
                raise RuntimeError(f"{scene.id} thất bại sau {max_retries} lần: {last_error}")
            if _wait_for_cooldown(pool):
                continue
            if _wait_for_new_account(pool):
                continue
            msg = f"{scene.id} thất bại sau {max_retries} lần: {last_error}"
            if softened:
                msg += " (đã thử prompt làm nhẹ)"
            if any(a.is_on_cooldown() for a in pool):
                msg += ". " + _describe_ineligible(pool, tracker, settings)
            raise RuntimeError(msg)
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
    native_bed: bool | None = None,
    api_key: str = "",
    tracker: UsageTracker | None = None,
    lipsync: bool = False,
    sync_api_key: str = "",
) -> dict:
    """Chạy toàn bộ pipeline, trả về manifest dict."""
    settings = settings or {}
    manager = manager or AccountManager()
    tracker = tracker or UsageTracker()
    script = load_script(script_path)
    errors = validate(script)
    if errors:
        raise ValueError("Kịch bản lỗi:\n- " + "\n- ".join(errors))
    # -- Nhân vật đồng nhất: khóa mô tả cố định vào visual trước khi sinh
    # clip (cả visual viết tay cũng được khóa), rồi báo cảnh báo nếu có
    # nhân vật chưa khai báo hoặc ảnh tham chiếu không thấy trên đĩa.
    from .characters import apply_character_lock, character_warnings

    apply_character_lock(script)
    _locked_count = sum(
        1 for s in script.scenes
        if (s.character or "").strip()
        and (s.character or "").strip() in (script.characters or {})
    )
    if _locked_count:
        log(f"Khóa mô tả nhân vật cho {_locked_count} cảnh")
    for _w in character_warnings(script, Path(script_path).resolve().parent):
        log(f"  ! {_w}")
    # -- Khớp môi: bật mà chưa có key (và không phải mock) thì dừng ngay,
    # đừng chạy hết pipeline rồi mới báo.
    sync_key = sync_api_key or get_sync_key()
    if lipsync and not mock and not sync_key:
        raise RuntimeError(
            "Chưa có key Sync Labs — chạy `lipsync set-key` để nhập key "
            "trước khi build với --lipsync."
        )

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

    # -- 2b. Khớp môi hậu kỳ (Sync Labs) ------------------------------
    # Chỉ cảnh nào có lipsync: true VÀ có file TTS mới được sync.
    # Lỗi ở bước này không bao giờ làm hỏng build: giữ nguyên clip gốc,
    # ghi "failed" vào manifest để biết cảnh nào chưa khớp môi.
    for info in infos:
        info["lipsync"] = "skipped"
    if lipsync:
        lip_client = build_lipsync(sync_key, settings, mock=mock)
        lip_dir = out_dir / "clips_lipsync"
        lip_dir.mkdir(parents=True, exist_ok=True)
        for i, scene in enumerate(script.scenes):
            if not scene.lipsync or scene.id not in tts_files:
                continue
            dest = lip_dir / f"{scene.id}.mp4"
            log(f"Khớp môi {scene.id} — {scene.title}")
            try:
                lip_client.sync(clips[i], tts_files[scene.id], dest)
                clips[i] = dest
                infos[i]["lipsync"] = "ok"
                log(f"  Khớp môi {scene.id} xong")
            except Exception as e:  # noqa: BLE001 - giữ clip gốc
                infos[i]["lipsync"] = "failed"
                log(f"  ! Khớp môi {scene.id} lỗi, giữ clip gốc: {e}")

    # -- 2c. Tiếng gốc của clip làm nền ------------------------------
    # Clip sinh ra vốn đã có tiếng của nó (khớp từng cảnh: cảnh rượt
    # đuổi tự nó to và gấp hơn cảnh bình minh). Pipeline cũ bỏ hết ở
    # bước chuẩn hóa (-an) rồi đắp nhạc/ambience ngoài vào nên nghe giả
    # (2026-10-26, tập "Vua của thảo nguyên" — user chê "trông giả vcl").
    # Giờ giữ tiếng gốc làm nền dưới lời dẫn. Nhạc ngoài (nếu có) vẫn
    # thắng: user đã chọn nhạc thì dùng nhạc, không đắp thêm tiếng gốc.
    # Cảnh đã khớp môi thì dùng khoảng lặng — tiếng trong clip chính là
    # lời thoại, trộn lại sẽ thành nói đúp.
    bed_path: Path | None = None
    audio_bed = "none"
    music_path = music or script.music or None
    if music_path:
        audio_bed = "music"
    elif tts_files:
        use_native = (
            native_bed if native_bed is not None
            else (script.kind == "documentary")
        )
        if use_native:
            candidate = audio_dir / "native_bg.wav"
            if resume and candidate.exists() and candidate.stat().st_size > 0:
                log("Đã có tiếng gốc (resume), dùng lại")
                bed_path = candidate
            else:
                sources = [
                    (None if infos[i]["lipsync"] == "ok" else clips[i],
                     scene_durations[scene.id])
                    for i, scene in enumerate(script.scenes)
                ]
                log("Trích tiếng gốc của từng clip làm nền...")
                bed_path = extract_native_bed(sources, candidate)
            if bed_path is not None:
                audio_bed = "native"
            else:
                log("  (clip không có tiếng gốc — bỏ qua nền)")

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
        final_audio = audio_dir / "final_audio.wav"
        if music_path:
            log("Trộn nhạc nền + chuẩn hóa âm lượng...")
            mix_with_music(narration_wav, music_path, final_audio)
        elif bed_path is not None:
            log("Trộn tiếng gốc của clip + chuẩn hóa âm lượng...")
            mix_with_music(
                narration_wav, bed_path, final_audio,
                music_volume=NATIVE_BED_VOLUME,
            )
        else:
            log("Chuẩn hóa âm lượng lời thoại...")
            mix_with_music(narration_wav, None, final_audio)
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

    # -- 6. Poster cho tab Kết quả (ảnh nhẹ thay cho thẻ <video>) ---------
    poster_path = None
    try:
        poster_path = make_poster(final_path)
    except Exception:  # noqa: BLE001 - không có poster vẫn có video
        log("  ! Không tạo được poster, tab Kết quả sẽ dùng video xem trước")

    manifest = {
        "title": script.title,
        "kind": script.kind,
        "aspect": script.aspect,
        "scenes": infos,
        "scene_durations": scene_durations,
        "audio_bed": audio_bed,
        "final": str(final_path),
        "poster": str(poster_path) if poster_path else None,
        "subtitles": str(srt_path) if srt_path else None,
        "mock": mock,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"XONG: {final_path}")
    return manifest
