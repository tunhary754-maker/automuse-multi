"""Web Studio — giao diện quản lý kiểu automuse.site, chạy local.

Chỉ dùng Python stdlib (http.server), không cần Flask/FastAPI.
Mặc định chỉ nghe trên 127.0.0.1 (máy của bạn), chạy bằng:

    python cli.py web [--port 8080]

API:
    GET  /api/status                       tổng quan hệ thống
    GET  /api/accounts                     liệt kê acc (cookie/proxy đã che)
    POST /api/accounts                     thêm acc {name, cookie, cookie_file, proxy, note}
    DELETE /api/accounts/<name>            xóa acc
    POST /api/accounts/<name>/check        kiểm tra proxy + cookie
    GET  /api/voices                      liệt kê giọng ElevenLabs
    POST /api/elevenlabs/key              nhập key ElevenLabs {key} (kiểm tra rồi mới lưu .env)
    POST /api/lipsync/key                 nhập key Sync Labs {key} (kiểm tra rồi mới lưu .env)
    POST /api/voices/default              đặt giọng mặc định {voice}
    POST /api/voices/settings             cài đặt giọng chi tiết {voice?, speed?, stability?, similarity_boost?, style?, model?, use_speaker_boost?}
    POST /api/voices/test                 nghe thử giọng {voice, text} -> file mp3
    GET  /api/scripts                     liệt kê kịch bản (templates/examples/scripts)
    GET  /api/scripts/content?path=...    đọc nội dung kịch bản
    POST /api/scripts                     lưu kịch bản {name, content}
    DELETE /api/scripts/<name>            xóa kịch bản (chỉ thư mục scripts/) + file kèm theo
    POST /api/build                       chạy build {script, mock, accounts, no_tts, no_iris, out}
    GET  /api/jobs                        liệt kê job
    GET  /api/jobs/<id>                   trạng thái + log + manifest của job
    GET  /api/outputs                     liệt kê video đã dựng xong ?page=&per_page=&q=
    DELETE /api/outputs/<project>         xóa 1 video đã dựng (cả thư mục output/)
    POST /api/results/download-zip        nén các video đã chọn thành 1 file zip {projects}
    GET  /files/<project>/<file>          stream/download file (hỗ trợ Range)
    GET  /files-cache/<name>.zip          tải file zip đã nén ở trên
    GET  /api/batches                     liệt kê các đợt sản xuất hàng loạt
    GET  /api/batches/<id>                chi tiết 1 đợt (chủ đề, kịch bản, tiến độ)
    POST /api/trending-topics             gợi ý chủ đề theo xu hướng tìm kiếm {count}
    POST /api/batch-create                tạo đợt hàng loạt {topics?, topic_count, episodes_per_topic, minutes, account?}
"""

from __future__ import annotations

import html
import json
import mimetypes
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..accounts import Account, AccountManager, load_cookies
from ..batches import (
    append_batch,
    batch_progress,
    create_batch,
    load_batches,
    prepare_restart,
    refresh_statuses,
    remove_script_from_batches,
    replace_batch,
    run_batch,
    save_batches,
    set_batch_flags,
)
from ..config import (
    DEFAULT_OUTPUT_DIR,
    REPO_ROOT,
    elevenlabs_api_key,
    get_sync_key,
    load_settings,
    save_env_key,
    save_user_settings,
)
from ..elevenlabs import ElevenLabsClient, ElevenLabsError, build_elevenlabs
from ..lipsync import SyncError, SyncLipSyncClient
from ..muse_client import build_client
from ..notify import (check_bot_token, save_telegram_config, send_telegram,
                      send_telegram_to, telegram_configured)
from ..pipeline import is_connection_error
from ..script import load_script, validate
from ..topics import (research_topic, review_script, suggest_topics,
                      suggest_trending_topics, write_script)
from ..usage import UsageTracker
from . import jobs as job_store

STATIC_DIR = Path(__file__).resolve().parent / "static"
SCRIPTS_DIR = REPO_ROOT / "scripts"
CACHE_DIR = REPO_ROOT / "cache"

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------- helpers

def _json(handler: BaseHTTPRequestHandler, obj, status: int = 200) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def _safe_rel_path(base: Path, rel: str) -> Path | None:
    """Chặn path traversal: rel phải nằm trong base."""
    candidate = (base / unquote(rel)).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate


def _elevenlabs_voices():
    """(client, voices) thật — raise ValueError nếu chưa có key hoặc API lỗi."""
    key = elevenlabs_api_key()
    if not key:
        raise ValueError("chưa có key ElevenLabs — nhập key trước")
    client = build_elevenlabs(key, load_settings())
    return client, client.list_voices()


def _find_voice(voices: list[dict], want: str) -> dict | None:
    want = (want or "").strip()
    return next(
        (v for v in voices
         if str(v.get("voice_id")) == want
         or str(v.get("name", "")).lower() == want.lower()),
        None,
    )


def _build_progress_map() -> dict:
    """Quét output/ 1 lần, map kịch bản -> tiến độ build.

    Key ("path", script_path) ưu tiên (manifest.script_path ghi từ
    _post_process_build cho build mới); key ("title", title) là fallback
    cho build cũ chưa có script_path. Giá trị: project, output_path,
    số clip đã có, có final.mp4 chưa, mtime (build mới nhất thắng).
    """
    out: dict = {}
    base = DEFAULT_OUTPUT_DIR
    if not base.exists():
        return out
    for project in base.iterdir():
        if not project.is_dir():
            continue
        manifest: dict = {}
        manifest_path = project / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                manifest = {}
        clips_dir = project / "clips"
        clips = 0
        if clips_dir.is_dir():
            clips = sum(1 for f in clips_dir.iterdir()
                        if f.suffix.lower() == ".mp4")
        info = {
            "project": project.name,
            "output_path": f"output/{project.name}",
            "clips": clips,
            "final": (project / "final.mp4").exists(),
            "mtime": project.stat().st_mtime,
        }
        key_path = manifest.get("script_path")
        if key_path:
            prev = out.get(("path", key_path))
            if prev is None or info["mtime"] > prev["mtime"]:
                out[("path", key_path)] = info
        title = manifest.get("title")
        if title:
            prev = out.get(("title", title))
            if prev is None or info["mtime"] > prev["mtime"]:
                out[("title", title)] = info
    return out


def _scripts_catalog() -> list[dict]:
    out: list[dict] = []
    pmap = _build_progress_map()
    for source, folder in (("template", REPO_ROOT / "templates"),
                           ("example", REPO_ROOT / "examples"),
                           ("scripts", SCRIPTS_DIR)):
        if not folder.exists():
            continue
        for p in sorted(folder.glob("*.yaml")) + sorted(folder.glob("*.yml")) + sorted(folder.glob("*.md")):
            try:
                script = load_script(p)
                scenes = len(script.scenes)
                kind = script.kind
                title = script.title
            except Exception:  # noqa: BLE001 - file lỗi thì vẫn liệt kê
                scenes, kind, title = 0, "?", ""
            rel = str(p.relative_to(REPO_ROOT))
            # Chủ đề: field "topic" trong YAML (kịch bản từ đợt hàng loạt
            # ghi sẵn); file lỗi hoặc không có topic -> "".
            topic = ""
            if p.suffix.lower() in (".yaml", ".yml"):
                try:
                    import yaml

                    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                    topic = str(data.get("topic", "") or "")
                except Exception:  # noqa: BLE001 - đọc topic lỗi thì bỏ qua
                    topic = ""
            # Tiến độ video: chỉ tính cho kịch bản người dùng (scripts/),
            # tránh template/example trùng title khớp nhầm.
            progress = None
            if source == "scripts" and scenes:
                info = pmap.get(("path", rel))
                if info is None and title:
                    info = pmap.get(("title", title))
                if info is not None:
                    pct = round(100 * info["clips"] / scenes) if scenes else 0
                    status = ("done" if info["final"]
                              else "building" if info["clips"] else "none")
                    progress = {
                        "status": status,
                        "clips": info["clips"],
                        "scenes": scenes,
                        "pct": min(100, pct),
                        "project": info["project"],
                        "output_path": info["output_path"],
                    }
            out.append({
                "name": p.stem,
                "file": p.name,
                "source": source,
                "path": rel,
                "kind": kind,
                "scenes": scenes,
                "topic": topic,
                "progress": progress,
            })
    return out


def _parse_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    """Parse 'bytes=start-end' -> (start, end) inclusive, đã kẹp vào [0, size-1].

    Trả None nếu header sai định dạng / không thỏa được (caller trả 200 toàn file).
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    try:
        start_s, end_s = range_header[6:].split("-", 1)
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    start = max(0, start)
    end = min(end, size - 1)
    if start > end or size <= 0:
        return None
    return start, end


def _outputs_catalog(query: str | None = None) -> list[dict]:
    """Liệt kê project đã dựng, mới nhất trước. `query` lọc theo tên
    project / tiêu đề / kind (không phân biệt hoa thường)."""
    out: list[dict] = []
    base = DEFAULT_OUTPUT_DIR
    if not base.exists():
        return out
    q = (query or "").strip().lower()
    for project in base.iterdir():
        if not project.is_dir():
            continue
        final = project / "final.mp4"
        poster = project / "poster.jpg"
        manifest = project / "manifest.json"
        info: dict = {
            "project": project.name,
            "output_path": f"output/{project.name}",
            "final": f"/files/{project.name}/final.mp4" if final.exists() else None,
            "poster": f"/files/{project.name}/poster.jpg" if poster.exists() else None,
            "subtitles": f"/files/{project.name}/subtitles.srt" if (project / "subtitles.srt").exists() else None,
            "narration_txt": f"/files/{project.name}/loi-dan.txt" if (project / "loi-dan.txt").exists() else None,
            "size": final.stat().st_size if final.exists() else 0,
            "mtime": final.stat().st_mtime if final.exists() else project.stat().st_mtime,
            "manifest": None,
            "script_name": "",
        }
        if manifest.exists():
            try:
                info["manifest"] = json.loads(manifest.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        if info["manifest"]:
            sp = str(info["manifest"].get("script_path", "") or "")
            if sp:
                info["script_name"] = Path(sp).stem
        if not (info["final"] or info["manifest"]):
            continue
        if q:
            title = str((info["manifest"] or {}).get("title", ""))
            kind = str((info["manifest"] or {}).get("kind", ""))
            script_path = str((info["manifest"] or {}).get("script_path", ""))
            if q not in f"{project.name} {title} {kind} {script_path} {info['script_name']}".lower():
                continue
        out.append(info)
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


# ---------------------------------------------------------------- wildlife workflow
# Tool này chỉ làm video Thế giới động vật: kịch bản phim tài liệu, build
# không lồng tiếng, lời dẫn xuất txt riêng, tiếng gốc ghép lại vào final.

def _gateway_client_for(account_name: str):
    """Gateway client cho acc, hoặc (None, lỗi). Giống cli._muse_gateway_client."""
    from ..gateway import GatewayClient

    mgr = AccountManager()
    acc = mgr.get(account_name)
    if not acc:
        return None, f"Không có tài khoản '{account_name}'."
    client = build_client(acc, load_settings(), mock=False)
    if not isinstance(client, GatewayClient):
        return None, ("Cần transport gateway để nhờ Muse "
                       "(settings.yaml: muse.transport: gateway).")
    return client, ""


def _slugify_topic(topic: str) -> str:
    """Tên file an toàn từ chủ đề (bỏ dấu tiếng Việt). Giống cli._slugify_topic."""
    norm = unicodedata.normalize("NFD", topic)
    ascii_text = "".join(c for c in norm if unicodedata.category(c) != "Mn")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug or "kich-ban"


def _narration_txt(script) -> str:
    """Lời dẫn từng cảnh thành txt: id — tiêu đề (timecode) + câu dẫn."""
    t = 0
    out: list[str] = []
    for sc in script.scenes:
        d = sc.duration or 10
        out.append(f"{sc.id} — {sc.title} "
                   f"({t // 60:02d}:{t % 60:02d}–{(t + d) // 60:02d}:{(t + d) % 60:02d})")
        out.append(sc.narration or "")
        out.append("")
        t += d
    return "\n".join(out)


def _ffmpeg_volume(path: Path) -> tuple[float | None, float | None]:
    """Đo mean/max volume (dB) của file, trả (None, None) nếu đo lỗi."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", str(path), "-af", "volumedetect",
             "-vn", "-sn", "-dn", "-f", "null", "/dev/null"],
            capture_output=True, text=True, timeout=120,
        )
        mean = maxv = None
        for line in (r.stderr or "").splitlines():
            if "mean_volume:" in line:
                try:
                    mean = float(line.split("mean_volume:")[1].split("dB")[0].strip())
                except ValueError:
                    pass
            if "max_volume:" in line:
                try:
                    maxv = float(line.split("max_volume:")[1].split("dB")[0].strip())
                except ValueError:
                    pass
        return mean, maxv
    except (subprocess.SubprocessError, OSError, ValueError):
        return None, None


def _attach_native_audio(out_dir: Path, log) -> None:
    """Ghép tiếng gốc từ clips/*.mp4 vào final.mp4, chuẩn hóa nếu quá nhỏ.

    Pipeline ráp final mặc định bỏ tiếng gốc nên final ban đầu chỉ có hình.
    Tiếng gốc mỗi clip khác nhau rất nhiều: mean -20 đến -27 dB thì nghe
    rõ, dưới -30 dB thì nghe như không có tiếng → chuẩn hóa loudnorm
    về -16 LUFS trước khi giao (quy tắc trong skill wildlife-documentary).
    """
    final = out_dir / "final.mp4"
    clips_dir = out_dir / "clips"
    if not final.exists() or not clips_dir.is_dir():
        log("Bỏ qua ghép tiếng gốc: thiếu final.mp4 hoặc thư mục clips.")
        return
    clips = sorted(p for p in clips_dir.glob("s*.mp4")
                   if ".tmp." not in p.name and p.stat().st_size > 0)
    if not clips:
        log("Bỏ qua ghép tiếng gốc: không thấy clip nào trong clips/.")
        return
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    filt = "".join(f"[{i}:a]" for i in range(1, len(clips) + 1))
    filt += f"concat=n={len(clips)}:v=0:a=1[a]"
    tmp = out_dir / "final-audio.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(final)] + inputs +
        ["-filter_complex", filt, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(tmp)],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0 or not tmp.exists():
        log("Ghép tiếng gốc thất bại — giữ nguyên final không tiếng.")
        return
    tmp.replace(final)
    mean, maxv = _ffmpeg_volume(final)
    log(f"Tiếng gốc đã ghép: mean {mean} dB, max {maxv} dB.")
    if mean is not None and mean < -30:
        normed = out_dir / "final-norm.mp4"
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(final),
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(normed)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode == 0 and normed.exists():
            normed.replace(final)
            mean2, maxv2 = _ffmpeg_volume(final)
            log(f"Tiếng gốc quá nhỏ ({mean} dB) → đã chuẩn hóa: "
                f"mean {mean2} dB, max {maxv2} dB.")
        else:
            log("Chuẩn hóa âm lượng thất bại — giữ bản tiếng gốc chưa chuẩn hóa.")


def _save_script_yaml(script, out_path: str, extra: dict | None = None) -> Path:
    """Lưu Script thành YAML (cùng format cli.write-script) vào out_path.

    ``extra``: field phụ chèn ngay sau title (vd: {"topic": ...} cho
    kịch bản từ đợt hàng loạt) — load_script bỏ qua field lạ nên an toàn.
    """
    import yaml

    data = {
        "title": script.title,
    }
    if extra:
        data.update(extra)
    data.update({
        "kind": script.kind,
        "aspect": script.aspect,
        **({"characters": script.characters} if script.characters else {}),
        **({"voices": script.voices} if script.voices else {}),
        "default_voice": script.default_voice,
        "fps": script.fps,
        **({"music": script.music} if script.music else {}),
        "scenes": [
            {
                "id": s.id,
                "title": s.title,
                "duration": s.duration,
                "visual": s.visual,
                "narration": s.narration,
                **({"voice": s.voice} if s.voice else {}),
                **({"character": s.character} if s.character else {}),
                **({"reference_image": s.reference_image} if s.reference_image else {}),
                **({"sfx": s.sfx} if s.sfx else {}),
                **({"lipsync": True} if s.lipsync else {}),
            }
            for s in script.scenes
        ],
    })
    dest = REPO_ROOT / out_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return dest


def _post_process_build(manifest: dict, script_path: str, log) -> dict:
    """Sau build phim tài liệu: xuất lời dẫn txt + ghép tiếng gốc.

    Chỉ chạy cho build thật (không mock). Cập nhật manifest.json trên đĩa
    và dict manifest trả về cho UI.
    """
    final_path = manifest.get("final") or ""
    out_dir = Path(final_path).parent if final_path else None
    if not out_dir or not out_dir.is_dir():
        return manifest
    try:
        script = load_script(script_path)
    except Exception as e:  # noqa: BLE001
        log(f"Không xuất được lời dẫn txt: {e}")
        script = None
    if script is not None:
        txt_path = out_dir / "loi-dan.txt"
        txt_path.write_text(_narration_txt(script), encoding="utf-8")
        manifest["narration_txt"] = str(txt_path)
        log(f"Đã xuất lời dẫn: {txt_path}")
    _attach_native_audio(out_dir, log)
    manifest["audio_bed"] = "native"
    # Ghi lại kịch bản gốc để tab Kịch bản/Kết quả nối video với kịch bản
    # (tính % tiến độ, tìm kiếm theo tên kịch bản).
    manifest["script_path"] = script_path
    try:
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        log(f"Không ghi lại được manifest.json: {e}")
    return manifest


# ---------------------------------------------------------------- build jobs

BUILD_LOCK = threading.Lock()


def _run_build_job(job_id: str, params: dict) -> None:
    """Chạy pipeline trong thread riêng, ghi log vào job store."""
    import automuse.pipeline as pipeline_mod

    job = job_store.get(job_id)
    job["status"] = "running"
    job["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    orig_log = pipeline_mod.log

    def job_log(msg: str) -> None:
        job["log"].append(msg)
        orig_log(msg)

    pipeline_mod.log = job_log
    try:
        settings = load_settings()
        manager = AccountManager()
        script_path = params["script"]
        out_dir = params.get("out") or None
        # Tool chỉ làm phim tài liệu Thế giới động vật: luôn không lồng
        # tiếng (lời dẫn xuất txt riêng), không nhạc nền, không lip-sync.
        manifest = pipeline_mod.run_build(
            script_path=script_path,
            out_dir=out_dir,
            settings=settings,
            manager=manager,
            mock=bool(params.get("mock")),
            no_tts=True,
            iris_out=not bool(params.get("no_iris")),
            resume=bool(params.get("resume")),
            account_names=params.get("accounts") or None,
            music=None,
            api_key=None,
            lipsync=False,
            sync_api_key=None,
        )
        if not manifest.get("mock"):
            manifest = _post_process_build(manifest, script_path, job_log)
        job["manifest"] = manifest
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 - ghi lỗi vào job cho UI hiển thị
        job["status"] = "failed"
        job["error"] = str(e)
        job["log"].append(f"LỖI: {e}")
    finally:
        pipeline_mod.log = orig_log
        job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- batch jobs

_BATCH_THREADS: dict[str, threading.Thread] = {}


def _refresh_batch_statuses() -> None:
    """Đợt nào status 'running'/'paused' mà thread đã chết/mất (server
    restart, crash) thì đánh 'interrupted' để UI không hiển thị 'đang
    chạy' mãi. Đợt interrupted dùng được nút 'chạy tiếp'."""
    alive = {bid for bid, t in _BATCH_THREADS.items() if t.is_alive()}
    refresh_statuses(alive)


def _run_batch_job(batch_id: str) -> None:
    """Chạy 1 đợt hàng loạt trong thread nền (dùng automuse/batches)."""
    from ..pipeline import is_connection_error

    batches = load_batches()
    batch = next((b for b in batches if b.get("id") == batch_id), None)
    if batch is None:
        return
    if batch.get("log") is None:
        batch["log"] = []

    def log(msg: str) -> None:
        batch["log"].append(msg)
        batch["log"] = batch["log"][-200:]

    def notify(text: str) -> bool:
        try:
            if telegram_configured():
                return send_telegram(text)
        except Exception:  # noqa: BLE001 - thông báo lỗi không dừng đợt
            pass
        return False

    mgr = AccountManager()
    names = [a.name for a in mgr.enabled_accounts()]
    deps = {
        "account_names": names,
        "get_client": lambda name: _gateway_client_for(name),
        "research": lambda topic, client: research_topic(topic, client),
        "write": lambda topic, client, target_scenes, research, timeout: (
            write_script(topic, kind="documentary", scene_count=None,
                         client=client, strict=True,
                         target_scenes=target_scenes, research=research,
                         timeout=timeout)),
        "save_script": lambda script, out_path, topic: (
            _save_script_yaml(script, out_path, extra={"topic": topic})),
        "slugify": _slugify_topic,
        "notify": notify,
        "is_connection_error": is_connection_error,
        "log": log,
        "scripts_dir": SCRIPTS_DIR,
    }
    try:
        run_batch(batch, deps)
    except Exception as e:  # noqa: BLE001 - crash vòng chạy vẫn ghi trạng thái
        batch["status"] = "failed"
        log(f"LỖI đợt: {e}")
        batches = load_batches()
        for i, b in enumerate(batches):
            if b.get("id") == batch_id:
                batches[i] = batch
                break
        else:
            batches.append(batch)
        save_batches(batches)
    finally:
        _BATCH_THREADS.pop(batch_id, None)


def _build_results_zip(projects: list[str], out_dir: Path) -> tuple[Path, int, int]:
    """Nén các project output đã chọn thành 1 zip.

    Mỗi project góp final.mp4 + loi-dan.txt + subtitles.srt (file nào
    có thì lấy). Trả (zip_path, số project, số file). Raise ValueError
    nếu tên không hợp lệ / không thấy project / không có file nào.
    """
    import zipfile

    if not projects:
        raise ValueError("chưa chọn video nào")
    if len(projects) > 100:
        raise ValueError("tối đa 100 video mỗi lần tải")
    dirs = []
    for name in projects:
        name = str(name)
        if not SAFE_NAME_RE.match(name):
            raise ValueError(f"tên project không hợp lệ: {name}")
        target = _safe_rel_path(DEFAULT_OUTPUT_DIR, name)
        if not target or not target.is_dir():
            raise ValueError(f"không thấy video: {name}")
        dirs.append((name, target))
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = (f"studio-download-{time.strftime('%Y%m%d-%H%M%S')}-"
                f"{uuid.uuid4().hex[:6]}.zip")
    zip_path = out_dir / zip_name
    added = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, d in dirs:
            for fname in ("final.mp4", "loi-dan.txt", "subtitles.srt"):
                f = d / fname
                if f.is_file():
                    zf.write(f, arcname=f"{name}/{fname}")
                    added += 1
    if not added:
        try:
            zip_path.unlink()
        except OSError:
            pass
        raise ValueError("các video đã chọn không có file nào để tải")
    return zip_path, len(dirs), added


# ---------------------------------------------------------------- handler

class StudioHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------
    def log_message(self, *args):  # noqa: D102 - tắt log mặc định cho gọn
        pass

    def _send_file(self, path: Path) -> None:
        # Stream từng chunk 1MB thay vì đọc nguyên file vào RAM: mở tab
        # Kết quả với 1000 video cũng không làm server phình RAM, và
        # Range (tua video) chỉ đọc đúng đoạn cần thiết.
        ctype, _ = mimetypes.guess_type(str(path))
        size = path.stat().st_size
        span = _parse_range(self.headers.get("Range"), size)
        f = open(path, "rb")
        try:
            if span is not None:
                start, end = span
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype or "application/octet-stream")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                f.seek(start)
                self._copy_range(f, length)
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype or "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                self._copy_range(f, size)
        finally:
            f.close()

    def _copy_range(self, f, length: int, chunk: int = 1024 * 1024) -> None:
        """Chép đúng `length` byte từ file ra socket theo từng chunk —
        không đọc nguyên file vào RAM."""
        remaining = length
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            self.wfile.write(data)
            remaining -= len(data)

    # -- routing ----------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            return self._send_file(STATIC_DIR / "index.html")
        if path in ("/app.js", "/style.css"):
            target = STATIC_DIR / path.lstrip("/")
            if target.exists():
                return self._send_file(target)
            return _json(self, {"error": "not found"}, 404)

        if path == "/api/notify/status":
            from ..notify import telegram_configured
            return _json(self, {"ok": True, "configured": telegram_configured()})

        if path == "/api/status":
            from ..notify import telegram_configured
            ffmpeg = shutil.which("ffmpeg") is not None
            mgr = AccountManager()
            return _json(self, {
                "version": "0.2.0",
                "ffmpeg": ffmpeg,
                "accounts": len(mgr.accounts),
                "accounts_ready": len(mgr.enabled_accounts()),
                "elevenlabs_configured": bool(elevenlabs_api_key()),
                "lipsync_configured": bool(get_sync_key()),
                "telegram_configured": telegram_configured(),
                "elevenlabs_default_voice": load_settings().get("elevenlabs", {}).get("default_voice", "narrator"),
                "elevenlabs_settings": {
                    "default_voice": load_settings().get("elevenlabs", {}).get("default_voice", "narrator"),
                    "model": load_settings().get("elevenlabs", {}).get("model", "eleven_multilingual_v2"),
                    "speed": float(load_settings().get("elevenlabs", {}).get("speed", 1.0)),
                    "stability": float(load_settings().get("elevenlabs", {}).get("stability", 0.5)),
                    "similarity_boost": float(load_settings().get("elevenlabs", {}).get("similarity_boost", 0.75)),
                    "style": float(load_settings().get("elevenlabs", {}).get("style", 0.0)),
                    "use_speaker_boost": bool(load_settings().get("elevenlabs", {}).get("use_speaker_boost", True)),
                },
                "outputs": len(_outputs_catalog()),
            })

        if path == "/api/accounts":
            mgr = AccountManager()
            settings = load_settings()
            tracker = UsageTracker()
            out = []
            for a in mgr.accounts:
                d = a.to_public_dict()
                d["usage"] = tracker.remaining(a, settings)
                out.append(d)
            return _json(self, out)

        if path == "/api/voices":
            mock = qs.get("mock", ["0"])[0] == "1" or not elevenlabs_api_key()
            try:
                client = build_elevenlabs(elevenlabs_api_key(), load_settings(), mock=mock)
                return _json(self, client.list_voices())
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e)}, 400)

        if path == "/api/scripts":
            return _json(self, _scripts_catalog())

        if path == "/api/scripts/content":
            rel = qs.get("path", [""])[0]
            target = _safe_rel_path(REPO_ROOT, rel)
            if not target or not target.exists():
                return _json(self, {"error": "không thấy file kịch bản"}, 404)
            return _json(self, {"path": rel, "content": target.read_text(encoding="utf-8")})

        if path == "/api/jobs":
            return _json(self, job_store.list_all())

        if path.startswith("/api/jobs/"):
            job = job_store.get(path[len("/api/jobs/"):])
            if not job:
                return _json(self, {"error": "không thấy job"}, 404)
            return _json(self, job)

        if path == "/api/batches":
            _refresh_batch_statuses()
            batches = load_batches()
            out = []
            for b in reversed(batches):
                p = batch_progress(b)
                out.append({**b, "progress": p})
            return _json(self, out)

        if path.startswith("/api/batches/"):
            _refresh_batch_statuses()
            batch_id = unquote(path[len("/api/batches/"):])
            batch = next((b for b in load_batches()
                          if b.get("id") == batch_id), None)
            if not batch:
                return _json(self, {"error": "không thấy đợt này"}, 404)
            return _json(self, {**batch, "progress": batch_progress(batch)})

        if path == "/api/outputs":
            # Phân trang: 1000 video cũng chỉ trả 1 trang/lần (mặc định 20).
            try:
                page = max(1, int(qs.get("page", ["1"])[0]))
            except ValueError:
                page = 1
            try:
                per_page = min(100, max(1, int(qs.get("per_page", ["20"])[0])))
            except ValueError:
                per_page = 20
            q = qs.get("q", [""])[0]
            all_items = _outputs_catalog(q or None)
            total = len(all_items)
            pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, pages)
            start = (page - 1) * per_page
            return _json(self, {
                "items": all_items[start:start + per_page],
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": pages,
            })

        if path.startswith("/files/"):
            rel = path[len("/files/"):]
            target = _safe_rel_path(DEFAULT_OUTPUT_DIR, rel)
            if not target or not target.is_file():
                return _json(self, {"error": "không thấy file"}, 404)
            return self._send_file(target)

        if path.startswith("/files-cache/"):
            # Chỉ phục vụ file zip đã nén từ /api/results/download-zip.
            name = unquote(path[len("/files-cache/"):])
            if not SAFE_NAME_RE.match(name) or not name.endswith(".zip"):
                return _json(self, {"error": "tên file không hợp lệ"}, 400)
            target = _safe_rel_path(CACHE_DIR, name)
            if not target or not target.is_file():
                return _json(self, {"error": "không thấy file"}, 404)
            return self._send_file(target)

        return _json(self, {"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/accounts":
            body = _read_json(self)
            name = (body.get("name") or "").strip()
            if not name:
                return _json(self, {"error": "thiếu tên tài khoản"}, 400)
            try:
                if body.get("cookie_file"):
                    load_cookies(body["cookie_file"])
                elif not load_cookies(body.get("cookie") or ""):
                    return _json(self, {"error": "cookie rỗng/không parse được"}, 400)
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": f"cookie không hợp lệ: {e}"}, 400)
            mgr = AccountManager()
            try:
                mgr.add(Account(
                    name=name,
                    cookie=body.get("cookie") or "",
                    cookie_file=body.get("cookie_file") or "",
                    proxy=body.get("proxy") or "",
                    note=body.get("note") or "",
                ))
            except ValueError as e:
                return _json(self, {"error": str(e)}, 400)
            return _json(self, {"ok": True, "name": name})

        if path.startswith("/api/accounts/") and path.endswith("/check"):
            name = unquote(path[len("/api/accounts/"):-len("/check")])
            mgr = AccountManager()
            acc = mgr.get(name)
            if not acc:
                return _json(self, {"error": "không có tài khoản này"}, 404)
            result: dict = {"name": name}
            proxy = acc.proxy_config()
            if proxy.is_valid():
                ok, msg = proxy.test()
                result["proxy"] = {"ok": ok, "message": msg, "masked": proxy.masked()}
            client = build_client(acc, load_settings(), mock=False)
            ok, msg = client.check_login()
            result["login"] = {"ok": ok, "message": msg}
            qok, qmsg = client.check_quota()
            result["quota"] = {"ok": qok, "message": qmsg}
            result["usage"] = UsageTracker().remaining(acc, load_settings())
            return _json(self, result)

        if path == "/api/scripts":
            body = _read_json(self)
            name = (body.get("name") or "").strip()
            content = body.get("content") or ""
            if not name or not SAFE_NAME_RE.match(name):
                return _json(self, {"error": "tên file không hợp lệ (chữ, số, -, _, .)"}, 400)
            if not content.strip():
                return _json(self, {"error": "nội dung kịch bản rỗng"}, 400)
            SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            dest = SCRIPTS_DIR / f"{name}.yaml"
            dest.write_text(content, encoding="utf-8")
            return _json(self, {"ok": True, "path": f"scripts/{name}.yaml"})

        if path == "/api/scripts/review":
            # Muse đọc lại kịch bản, sửa phần chưa tốt (giữ sát bản gốc)
            # và áp dụng hướng dẫn bổ sung của người dùng. CHỈ TRẢ bản
            # sửa để người dùng xem rồi bấm Lưu — không tự lưu đè.
            import tempfile

            body = _read_json(self)
            content = str(body.get("content") or "")
            instruction = str(body.get("instruction") or "")
            if not content.strip():
                return _json(self, {"error": "chưa có nội dung kịch bản"}, 400)
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                                 delete=False,
                                                 encoding="utf-8") as f:
                    f.write(content)
                    tmp_path = f.name
                orig = load_script(tmp_path)
                orig_errors = validate(orig)
                if orig_errors:
                    return _json(self, {"error": "kịch bản gốc lỗi: "
                                                 + "; ".join(orig_errors)}, 400)
                try:
                    import yaml

                    orig_topic = str((yaml.safe_load(content) or {})
                                     .get("topic", "") or "")
                except Exception:  # noqa: BLE001 - topic đọc lỗi thì bỏ qua
                    orig_topic = ""
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": f"kịch bản gốc không đọc được: {e}"},
                             400)
            finally:
                if tmp_path:
                    try:
                        Path(tmp_path).unlink()
                    except OSError:
                        pass
            mgr = AccountManager()
            ready = mgr.enabled_accounts()
            if not ready:
                return _json(self, {"error": "cần tài khoản Muse sẵn sàng để rà soát — "
                                             "thêm acc ở tab Tài khoản Muse trước"},
                             400)
            # Thử lần lượt từng acc: client chỉ lỗi khi gọi thật (vd: acc1
            # thiếu authToken dù check login vẫn OK) nên acc lỗi thì chuyển
            # sang acc khác, giống auto-create — không dừng ở acc đầu.
            fixed = changes = fixed_script = None
            last_err = ""
            for acc in ready:
                c, err = _gateway_client_for(acc.name)
                if c is None:
                    last_err = err or ""
                    continue
                try:
                    fixed, changes, fixed_script = review_script(
                        content, c, instruction)
                    break
                except Exception as e:  # noqa: BLE001 - thử acc tiếp theo
                    last_err = str(e)
                    if is_connection_error(e) and telegram_configured():
                        send_telegram(f"⚠️ Acc '{acc.name}' KHÔNG KẾT NỐI "
                                      f"ĐƯỢC: {last_err} — rà soát đang thử "
                                      f"acc khác.")
            if fixed is None:
                return _json(self, {"error": last_err or "không rà soát được"},
                             400)
            # Giữ lại topic để kịch bản không rơi khỏi nhóm chủ đề.
            if orig_topic and not any(line.startswith("topic:")
                                      for line in fixed.splitlines()):
                lines = fixed.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("title:"):
                        lines.insert(i + 1, "topic: " + json.dumps(
                            orig_topic, ensure_ascii=False))
                        break
                fixed = "\n".join(lines)
                if content.endswith("\n"):
                    fixed += "\n"
            return _json(self, {
                "ok": True,
                "fixed_content": fixed,
                "changes": changes,
                "scenes_before": len(orig.scenes),
                "scenes_after": len(fixed_script.scenes),
                "title": fixed_script.title,
            })

        if path == "/api/elevenlabs/key":
            body = _read_json(self)
            key = (body.get("key") or "").strip()
            if not key:
                return _json(self, {"error": "thiếu key"}, 400)
            try:
                voices = ElevenLabsClient(key).list_voices()
            except ElevenLabsError as e:
                return _json(self, {"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e)}, 400)
            save_env_key("ELEVENLABS_API_KEY", key)
            return _json(self, {"ok": True, "voices": len(voices)})

        if path == "/api/lipsync/key":
            body = _read_json(self)
            key = (body.get("key") or "").strip()
            if not key:
                return _json(self, {"error": "thiếu key"}, 400)
            try:
                ok = SyncLipSyncClient(key).check_key()
            except SyncError as e:
                return _json(self, {"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e)}, 400)
            if not ok:
                return _json(self, {"error": "key không đúng (401)"}, 400)
            save_env_key("SYNC_API_KEY", key)
            return _json(self, {"ok": True})

        if path == "/api/notify/save":
            # Nhập Telegram bot token + chat ID: kiểm tra token qua getMe,
            # kiểm tra chat bằng tin thử, rồi mới lưu vào .env.
            body = _read_json(self)
            token = (body.get("token") or "").strip()
            chat_id = (body.get("chat_id") or "").strip()
            if not token or not chat_id:
                return _json(self, {"error": "thiếu token hoặc chat ID"}, 400)
            ok, info = check_bot_token(token)
            if not ok:
                return _json(self, {"error": f"token không dùng được: {info}"}, 400)
            if not send_telegram_to(token, chat_id,
                                    "✅ automuse-multi đã kết nối Telegram — "
                                    "khi build hết acc, tin nhắn sẽ tới đây."):
                return _json(self, {"error": "không gửi được tin thử tới chat ID này "
                                             "(bot chưa được chat trước, hoặc chat ID sai)"},
                             400)
            save_telegram_config(token, chat_id)
            return _json(self, {"ok": True, "bot": info})

        if path == "/api/notify/test":
            if not telegram_configured():
                return _json(self, {"error": "chưa cấu hình Telegram — nhập token + chat ID trước"}, 400)
            if not send_telegram("✅ Tin thử từ automuse-multi — Telegram đang hoạt động."):
                return _json(self, {"error": "gửi thất bại — kiểm tra lại token/chat ID"}, 400)
            return _json(self, {"ok": True})

        if path == "/api/voices/default":
            body = _read_json(self)
            want = (body.get("voice") or "").strip()
            if not want:
                return _json(self, {"error": "thiếu tên giọng"}, 400)
            try:
                client, voices = _elevenlabs_voices()
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e)}, 400)
            match = _find_voice(voices, want)
            if match:
                name = str(match.get("name") or want)
                save_user_settings({"elevenlabs": {"default_voice": name}})
                return _json(self, {"ok": True, "name": name,
                                    "voice_id": str(match.get("voice_id"))})
            # Không có trong danh sách acc — thử lấy thẳng theo voice_id.
            try:
                voice = client.get_voice(want)
            except Exception:  # noqa: BLE001
                return _json(self, {"error": f"không thấy giọng '{want}'"}, 404)
            save_user_settings({"elevenlabs": {"default_voice": str(voice.get("voice_id"))}})
            return _json(self, {"ok": True, "name": str(voice.get("name") or want),
                                "voice_id": str(voice.get("voice_id"))})

        if path == "/api/voices/settings":
            # Cài đặt giọng chi tiết: tốc độ, stability, similarity,
            # style, model, speaker boost, giọng mặc định (tên/voice_id).
            body = _read_json(self)
            patch: dict = {}
            for key, lo, hi in (
                ("speed", 0.5, 2.0),
                ("stability", 0.0, 1.0),
                ("similarity_boost", 0.0, 1.0),
                ("style", 0.0, 1.0),
            ):
                if key in body and body[key] is not None:
                    try:
                        value = float(body[key])
                    except (TypeError, ValueError):
                        return _json(self, {"error": f"{key} phải là số"}, 400)
                    if not lo <= value <= hi:
                        return _json(self, {"error": f"{key} phải trong khoảng {lo}–{hi}"}, 400)
                    patch[key] = value
            if body.get("model"):
                patch["model"] = str(body["model"]).strip()
            if "use_speaker_boost" in body:
                patch["use_speaker_boost"] = bool(body["use_speaker_boost"])
            if body.get("voice"):
                want = str(body["voice"]).strip()
                resolved = None
                try:
                    _client, voices = _elevenlabs_voices()
                    match = _find_voice(voices, want)
                    if match:
                        resolved = str(match.get("name") or want)
                    else:
                        voice = _client.get_voice(want)
                        resolved = str(voice.get("voice_id"))
                except Exception:  # noqa: BLE001 - chưa có key thì lưu thô
                    resolved = want
                patch["default_voice"] = resolved
            if not patch:
                return _json(self, {"error": "không có gì để lưu"}, 400)
            save_user_settings({"elevenlabs": patch})
            return _json(self, {"ok": True, "settings": patch})

        if path == "/api/voices/test":
            body = _read_json(self)
            want = (body.get("voice") or "").strip()
            text = (body.get("text") or "").strip() or \
                "Xin chào, đây là giọng đọc thử cho bộ phim tài liệu Thế giới động vật."
            if not want:
                return _json(self, {"error": "thiếu tên giọng"}, 400)
            try:
                client, voices = _elevenlabs_voices()
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e)}, 400)
            match = _find_voice(voices, want)
            if not match:
                try:
                    v = client.get_voice(want)
                    match = {"voice_id": v.get("voice_id"), "name": v.get("name") or want}
                except Exception:  # noqa: BLE001
                    match = None
            if not match:
                return _json(self, {"error": f"không thấy giọng '{want}'"}, 404)
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(match.get("name") or want))[:40] or "voice"
            out_dir = DEFAULT_OUTPUT_DIR / "voice-test"
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"{safe}.mp3"
            try:
                client.tts(text, str(match.get("voice_id")), out_path=dest)
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e)}, 400)
            return _json(self, {"ok": True, "file": f"/files/voice-test/{dest.name}"})

        if path == "/api/topics":
            body = _read_json(self)
            keyword = (body.get("keyword") or "").strip()
            try:
                count = max(1, min(20, int(body.get("count") or 10)))
            except (TypeError, ValueError):
                count = 10
            client = None
            account = (body.get("account") or "").strip()
            if account:
                client, err = _gateway_client_for(account)
                if err:
                    return _json(self, {"error": err}, 400)
            try:
                ideas = suggest_topics("documentary", keyword=keyword,
                                       count=count, client=client)
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e)}, 400)
            return _json(self, {"items": [
                {"title": i.title, "summary": i.summary} for i in ideas
            ]})

        if path == "/api/write-script":
            body = _read_json(self)
            topic = (body.get("topic") or "").strip()
            if not topic:
                return _json(self, {"error": "thiếu chủ đề"}, 400)
            try:
                scenes = max(1, min(20, int(body.get("scenes") or 10)))
            except (TypeError, ValueError):
                scenes = 10
            try:
                duration = max(5, min(30, int(body.get("duration") or 10)))
            except (TypeError, ValueError):
                duration = 10
            client = None
            account = (body.get("account") or "").strip()
            if account:
                client, err = _gateway_client_for(account)
                if err:
                    return _json(self, {"error": err}, 400)
            out = (body.get("out") or "").strip()
            if out:
                target = _safe_rel_path(REPO_ROOT, out)
                if not target:
                    return _json(self, {"error": "đường dẫn out không hợp lệ"}, 400)
                out_path = str(target)
            else:
                out_path = f"scripts/{_slugify_topic(topic)}.yaml"
            try:
                script = write_script(topic, kind="documentary",
                                      scene_count=scenes,
                                      scene_duration=duration, client=client)
            except Exception as e:  # noqa: BLE001
                return _json(self, {"error": str(e)}, 400)
            _save_script_yaml(script, out_path)
            return _json(self, {
                "ok": True,
                "path": out_path,
                "title": script.title,
                "scenes": len(script.scenes),
            })

        if path == "/api/auto-create":
            # Một nút bấm: tự chọn chủ đề + tự viết kịch bản DÀI (Muse tự
            # quyết số cảnh và thời lượng từng cảnh theo câu chuyện).
            # Người dùng nhập số phút mục tiêu; càng dài càng cần nhiều dữ
            # liệu nên trước khi viết, Muse nghiên cứu chủ đề trước (fact
            # sheet) rồi mới viết kịch bản từ dữ liệu đó.
            # Cần tài khoản Muse (đường Muse viết kịch bản thật, không dùng
            # khung xương template). Thử lần lượt từng acc sẵn sàng: acc nào
            # gateway hỏng (vd: thiếu authToken dù check login vẫn OK) thì
            # tự chuyển sang acc khác, thay vì báo lỗi ngay.
            import random

            body = _read_json(self)
            try:
                minutes = int(body.get("minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0
            minutes = max(0, min(60, minutes))
            # Mỗi cảnh ~10s -> số cảnh mục tiêu = phút × 6. Không nhập phút
            # thì để Muse tự quyết theo mặc định (khoảng 15–30 cảnh).
            target_scenes = minutes * 6 if minutes else None

            mgr = AccountManager()
            ready = mgr.enabled_accounts()
            if not ready:
                return _json(self, {"error": "chưa có tài khoản Muse sẵn sàng — "
                                             "thêm acc ở tab Tài khoản Muse trước"},
                             400)
            first_client = None
            for acc in ready:
                c, _ = _gateway_client_for(acc.name)
                if c is not None:
                    first_client = c
                    break
            try:
                ideas = suggest_topics("documentary", client=first_client)
            except Exception:  # noqa: BLE001 - Muse gợi ý lỗi thì dùng list mẫu
                ideas = []
            if not ideas:
                ideas = suggest_topics("documentary")
            if not ideas:
                return _json(self, {"error": "không gợi ý được chủ đề nào"}, 400)
            # Tránh trùng chủ đề đã có kịch bản rồi (so theo slug file).
            existing = set()
            if SCRIPTS_DIR.is_dir():
                existing = {p.stem for p in SCRIPTS_DIR.glob("*.yaml")}
                existing |= {p.stem for p in SCRIPTS_DIR.glob("*.yml")}
            fresh = [i for i in ideas
                     if _slugify_topic(i.title) not in existing]
            topic = (random.choice(fresh) if fresh else random.choice(ideas)).title
            script = None
            used_account = None
            last_err = ""
            # Acc nào không kết nối được thì báo lỗi luôn (Telegram nếu có
            # + kèm vào kết quả), dù vẫn thử acc khác làm tiếp.
            account_warnings: list[dict] = []
            for acc in ready:
                client, err = _gateway_client_for(acc.name)
                if err:
                    last_err = err
                    if is_connection_error(RuntimeError(err)):
                        account_warnings.append({"account": acc.name,
                                                 "error": err})
                        if telegram_configured():
                            send_telegram(f"⚠️ Acc '{acc.name}' KHÔNG KẾT NỐI "
                                          f"ĐƯỢC: {err} — auto-create đang thử "
                                          f"acc khác.")
                    continue
                # Nghiên cứu trước: kịch bản dài cần nhiều dữ liệu thật.
                # research_topic không raise (lỗi -> chuỗi rỗng, vẫn viết).
                research = research_topic(topic, client)
                # Kịch bản càng dài Muse viết càng lâu: tăng timeout theo
                # số cảnh mục tiêu (mặc định 180s chỉ đủ cho ~10-20 cảnh).
                script_timeout = 180.0
                if target_scenes:
                    script_timeout = max(180.0, target_scenes * 10.0)
                try:
                    script = write_script(topic, kind="documentary",
                                          scene_count=None, client=client,
                                          strict=True,
                                          target_scenes=target_scenes,
                                          research=research,
                                          timeout=script_timeout)
                    used_account = acc.name
                    break
                except Exception as e:  # noqa: BLE001 - thử acc tiếp theo
                    last_err = str(e)
                    if is_connection_error(e):
                        account_warnings.append({"account": acc.name,
                                                 "error": last_err})
                        if telegram_configured():
                            send_telegram(f"⚠️ Acc '{acc.name}' KHÔNG KẾT NỐI "
                                          f"ĐƯỢC: {last_err} — auto-create "
                                          f"đang thử acc khác.")
            if script is None:
                return _json(self, {"error": f"không viết được kịch bản "
                                             f"(đã thử {len(ready)} acc): {last_err}"},
                             400)
            out_path = f"scripts/{_slugify_topic(topic)}.yaml"
            _save_script_yaml(script, out_path)
            total = sum(s.duration or 10 for s in script.scenes)
            return _json(self, {
                "ok": True,
                "path": out_path,
                "title": script.title,
                "topic": topic,
                "account": used_account,
                "scenes": len(script.scenes),
                "total_duration": total,
                "minutes_target": minutes or None,
                "account_warnings": account_warnings,
            })

        if path == "/api/trending-topics":
            # Gợi ý chủ đề theo xu hướng tìm kiếm thật (Muse phân tích nhu
            # cầu), không phải brainstorm vu vơ. Không có acc/gateway lỗi
            # thì rơi về list mẫu và báo rõ trend_based=False.
            body = _read_json(self)
            try:
                count = int(body.get("count") or 5)
            except (TypeError, ValueError):
                count = 5
            count = max(1, min(20, count))
            mgr = AccountManager()
            # Thử lần lượt từng acc cho đến khi có phân tích xu hướng thật:
            # client chỉ lỗi khi gọi thật (acc hỏng vẫn trả về object) nên
            # không dừng ở acc đầu tiên.
            ideas, trend_based = [], False
            for acc in mgr.enabled_accounts():
                c, _ = _gateway_client_for(acc.name)
                if c is None:
                    continue
                ideas, trend_based = suggest_trending_topics(count, c)
                if trend_based:
                    break
            if not ideas:
                ideas, trend_based = suggest_trending_topics(count, None)
            return _json(self, {
                "ok": True,
                "trend_based": trend_based,
                "topics": [{"title": i.title, "reason": i.summary}
                           for i in ideas],
            })

        if path == "/api/batch-create":
            # Sản xuất hàng loạt: N chủ đề x M tập mỗi chủ đề, mỗi tập K
            # phút mục tiêu. Chủ đề do người dùng nhập (mỗi dòng 1 chủ
            # đề) hoặc để trống để Muse gợi ý theo xu hướng. Viết kịch
            # bản chạy nền trong thread riêng, tiến độ lưu cache/batches.json.
            body = _read_json(self)
            try:
                episodes = int(body.get("episodes_per_topic") or 0)
            except (TypeError, ValueError):
                episodes = 0
            episodes = max(1, min(20, episodes or 10))
            try:
                minutes = int(body.get("minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0
            minutes = max(3, min(60, minutes or 8))
            try:
                topic_count = int(body.get("topic_count") or 0)
            except (TypeError, ValueError):
                topic_count = 0
            topic_count = max(1, min(20, topic_count or 2))
            topics: list[dict] = []
            seen: set[str] = set()
            for t in body.get("topics") or []:
                if isinstance(t, dict):
                    name = str(t.get("name", "") or "").strip()
                    reason = str(t.get("reason", "") or "")
                else:
                    name, reason = str(t).strip(), ""
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                topics.append({"name": name, "reason": reason})
            topics_source = "user" if topics else ""
            mgr = AccountManager()
            ready = mgr.enabled_accounts()
            if not ready:
                return _json(self, {"error": "chưa có tài khoản Muse sẵn sàng — "
                                             "thêm acc ở tab Tài khoản Muse trước"},
                             400)
            if not topics:
                # Thử lần lượt từng acc cho đến khi có phân tích xu hướng
                # thật (client chỉ lỗi khi gọi thật, acc hỏng vẫn trả về
                # object nên không dừng ở acc đầu tiên).
                ideas, trend_based = [], False
                for acc in ready:
                    c, _ = _gateway_client_for(acc.name)
                    if c is None:
                        continue
                    ideas, trend_based = suggest_trending_topics(topic_count, c)
                    if trend_based:
                        break
                if not ideas:
                    ideas, trend_based = suggest_trending_topics(topic_count, None)
                topics = [{"name": i.title, "reason": i.summary}
                          for i in ideas]
                topics_source = "trend" if trend_based else "fallback"
                if not topics:
                    return _json(self, {"error": "không gợi ý được chủ đề nào"},
                                 400)
            total = len(topics) * episodes
            if total > 200:
                return _json(self, {"error": f"đợt quá lớn ({total} kịch bản, "
                                             f"tối đa 200) — giảm số chủ đề "
                                             f"hoặc số tập"},
                             400)
            account = str(body.get("account") or "")
            batch = create_batch(topics, episodes, minutes, account)
            append_batch(batch)
            thread = threading.Thread(target=_run_batch_job,
                                      args=(batch["id"],), daemon=True)
            _BATCH_THREADS[batch["id"]] = thread
            thread.start()
            return _json(self, {
                "ok": True,
                "batch_id": batch["id"],
                "topics": topics,
                "topics_source": topics_source,
                "total_scripts": total,
                "minutes": minutes,
                "episodes_per_topic": episodes,
            })

        if path.startswith("/api/batches/") and path.endswith(
                ("/pause", "/resume", "/cancel", "/restart")):
            # Điều khiển đợt đang chạy: tạm dừng (kịch bản chưa bắt đầu sẽ
            # chờ, kịch bản đang viết viết nốt rồi dừng), tiếp tục, dừng
            # hẳn (không bắt đầu kịch bản mới nữa), chạy tiếp đợt đã
            # dừng/lỗi/gián đoạn (chỉ làm nốt phần chưa xong).
            action = path.rsplit("/", 1)[-1]
            batch_id = unquote(path[len("/api/batches/"):-len(action) - 1])
            _refresh_batch_statuses()
            batch = next((b for b in load_batches()
                          if b.get("id") == batch_id), None)
            if not batch:
                return _json(self, {"error": "không thấy đợt này"}, 404)
            status = batch.get("status")
            if action == "pause":
                if status != "running":
                    return _json(self, {"error": "chỉ tạm dừng được đợt đang chạy"},
                                 400)
                set_batch_flags(batch_id, paused=True)
                return _json(self, {"ok": True, "status": "paused"})
            if action == "resume":
                if status != "paused":
                    return _json(self, {"error": "chỉ tiếp tục được đợt đang tạm dừng"},
                                 400)
                set_batch_flags(batch_id, paused=False)
                return _json(self, {"ok": True, "status": "running"})
            if action == "cancel":
                if status not in ("running", "paused"):
                    return _json(self, {"error": "đợt này đã kết thúc, không dừng được"},
                                 400)
                set_batch_flags(batch_id, cancelled=True, paused=False)
                return _json(self, {"ok": True, "status": "cancelled"})
            # restart: chỉ khi không còn thread nào đang chạy đợt này.
            t = _BATCH_THREADS.get(batch_id)
            if t is not None and t.is_alive():
                return _json(self, {"error": "đợt này đang chạy rồi"}, 400)
            if status not in ("interrupted", "failed", "partial", "cancelled"):
                return _json(self, {"error": "chỉ chạy tiếp được đợt đã dừng/lỗi/gián đoạn"},
                             400)
            prepare_restart(batch)
            replace_batch(batch)
            thread = threading.Thread(target=_run_batch_job,
                                      args=(batch_id,), daemon=True)
            _BATCH_THREADS[batch_id] = thread
            thread.start()
            return _json(self, {"ok": True, "status": "running"})

        if path == "/api/results/download-zip":
            # Nén các video đã chọn (final.mp4 + lời dẫn txt + phụ đề nếu
            # có) thành 1 file zip trong cache/ rồi trả link tải.
            body = _read_json(self)
            projects = body.get("projects") or []
            # Dọn zip cũ hơn 7 ngày để cache không phình mãi.
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                now = time.time()
                for old in CACHE_DIR.glob("studio-download-*.zip"):
                    try:
                        if now - old.stat().st_mtime > 7 * 86400:
                            old.unlink()
                    except OSError:
                        pass
            except OSError:
                pass
            try:
                zip_path, n_projects, n_files = _build_results_zip(
                    projects, CACHE_DIR)
            except ValueError as e:
                return _json(self, {"error": str(e)}, 400)
            return _json(self, {
                "ok": True,
                "download_url": f"/files-cache/{zip_path.name}",
                "projects": n_projects,
                "files": n_files,
                "size": zip_path.stat().st_size,
            })

        if path == "/api/build":
            body = _read_json(self)
            script = body.get("script") or ""
            target = _safe_rel_path(REPO_ROOT, script)
            if not target or not target.exists():
                return _json(self, {"error": "chưa chọn kịch bản hợp lệ"}, 400)
            errors = validate(load_script(target))
            if errors:
                return _json(self, {"error": "kịch bản lỗi: " + "; ".join(errors)}, 400)
            if not BUILD_LOCK.acquire(blocking=False):
                return _json(self, {"error": "đang có một job chạy, đợi xong rồi chạy tiếp"}, 409)
            job_id = uuid.uuid4().hex[:8]
            job = job_store.create(job_id, {
                "script": script,
                "mock": bool(body.get("mock")),
                "accounts": body.get("accounts") or [],
            })
            thread = threading.Thread(
                target=_build_wrapper, args=(job_id, body), daemon=True
            )
            thread.start()
            return _json(self, {"ok": True, "job_id": job_id, "job": job})

        return _json(self, {"error": "not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/accounts/"):
            name = unquote(path[len("/api/accounts/"):])
            mgr = AccountManager()
            if mgr.remove(name):
                return _json(self, {"ok": True})
            return _json(self, {"error": "không có tài khoản này"}, 404)
        if path.startswith("/api/scripts/"):
            name = unquote(path[len("/api/scripts/"):])
            for suffix in (".yaml", ".yml", ".md"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
                    break
            if not SAFE_NAME_RE.match(name):
                return _json(self, {"error": "tên không hợp lệ"}, 400)
            target = None
            for suffix in (".yaml", ".yml"):
                cand = SCRIPTS_DIR / f"{name}{suffix}"
                if cand.exists():
                    target = cand
                    break
            if target is None:
                return _json(self, {"error": "không thấy kịch bản này"}, 404)
            target.unlink()
            # Xóa file kèm theo cùng tên (prompts đã viết, lời dẫn đã xuất)
            # và gỡ kịch bản khỏi các đợt hàng loạt để khỏi hiển thị rác.
            for sibling in (SCRIPTS_DIR / f"{name}.prompts.yaml",
                            SCRIPTS_DIR / f"{name}-loi-dan.txt"):
                try:
                    if sibling.exists():
                        sibling.unlink()
                except OSError:
                    pass
            remove_script_from_batches(f"scripts/{name}.yaml")
            remove_script_from_batches(f"scripts/{name}.yml")
            return _json(self, {"ok": True})
        if path.startswith("/api/outputs/"):
            name = unquote(path[len("/api/outputs/"):])
            if not SAFE_NAME_RE.match(name):
                return _json(self, {"error": "tên không hợp lệ"}, 400)
            target = _safe_rel_path(DEFAULT_OUTPUT_DIR, name)
            if not target or not target.is_dir():
                return _json(self, {"error": "không thấy video này"}, 404)
            shutil.rmtree(target)
            return _json(self, {"ok": True})
        return _json(self, {"error": "not found"}, 404)


def _build_wrapper(job_id: str, params: dict) -> None:
    try:
        _run_build_job(job_id, params)
    finally:
        BUILD_LOCK.release()


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), StudioHandler)
    print(f"AutoMuse Studio đang chạy tại http://{host}:{port}")
    print("Nhấn Ctrl+C để dừng.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        server.shutdown()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AutoMuse Studio — web UI quản lý")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve(args.host, args.port)
