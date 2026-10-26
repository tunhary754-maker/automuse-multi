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
    DELETE /api/scripts/<name>            xóa kịch bản (chỉ thư mục scripts/)
    POST /api/build                       chạy build {script, mock, accounts, no_tts, no_iris, out}
    GET  /api/jobs                        liệt kê job
    GET  /api/jobs/<id>                   trạng thái + log + manifest của job
    GET  /api/outputs                     liệt kê video đã dựng xong ?page=&per_page=&q=
    GET  /files/<project>/<file>          stream/download file (hỗ trợ Range)
"""

from __future__ import annotations

import html
import json
import mimetypes
import re
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..accounts import Account, AccountManager, load_cookies
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
from ..script import load_script, validate
from ..usage import UsageTracker
from . import jobs as job_store

STATIC_DIR = Path(__file__).resolve().parent / "static"
SCRIPTS_DIR = REPO_ROOT / "scripts"

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


def _scripts_catalog() -> list[dict]:
    out: list[dict] = []
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
            except Exception:  # noqa: BLE001 - file lỗi thì vẫn liệt kê
                scenes, kind = 0, "?"
            out.append({
                "name": p.stem,
                "file": p.name,
                "source": source,
                "path": str(p.relative_to(REPO_ROOT)),
                "kind": kind,
                "scenes": scenes,
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
            "final": f"/files/{project.name}/final.mp4" if final.exists() else None,
            "poster": f"/files/{project.name}/poster.jpg" if poster.exists() else None,
            "subtitles": f"/files/{project.name}/subtitles.srt" if (project / "subtitles.srt").exists() else None,
            "size": final.stat().st_size if final.exists() else 0,
            "mtime": final.stat().st_mtime if final.exists() else project.stat().st_mtime,
            "manifest": None,
        }
        if manifest.exists():
            try:
                info["manifest"] = json.loads(manifest.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        if not (info["final"] or info["manifest"]):
            continue
        if q:
            title = str((info["manifest"] or {}).get("title", ""))
            kind = str((info["manifest"] or {}).get("kind", ""))
            if q not in f"{project.name} {title} {kind}".lower():
                continue
        out.append(info)
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


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
        manifest = pipeline_mod.run_build(
            script_path=script_path,
            out_dir=out_dir,
            settings=settings,
            manager=manager,
            mock=bool(params.get("mock")),
            no_tts=bool(params.get("no_tts")),
            iris_out=not bool(params.get("no_iris")),
            resume=bool(params.get("resume")),
            account_names=params.get("accounts") or None,
            music=params.get("music") or None,
            api_key=elevenlabs_api_key(),
            lipsync=bool(params.get("lipsync")),
            sync_api_key=get_sync_key(),
        )
        job["manifest"] = manifest
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 - ghi lỗi vào job cho UI hiển thị
        job["status"] = "failed"
        job["error"] = str(e)
        job["log"].append(f"LỖI: {e}")
    finally:
        pipeline_mod.log = orig_log
        job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


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

        if path == "/api/status":
            ffmpeg = shutil.which("ffmpeg") is not None
            mgr = AccountManager()
            return _json(self, {
                "version": "0.2.0",
                "ffmpeg": ffmpeg,
                "accounts": len(mgr.accounts),
                "accounts_ready": len(mgr.enabled_accounts()),
                "elevenlabs_configured": bool(elevenlabs_api_key()),
                "lipsync_configured": bool(get_sync_key()),
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
            if not SAFE_NAME_RE.match(name):
                return _json(self, {"error": "tên không hợp lệ"}, 400)
            target = SCRIPTS_DIR / f"{name}.yaml"
            if target.exists():
                target.unlink()
                return _json(self, {"ok": True})
            return _json(self, {"error": "không thấy kịch bản này"}, 404)
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
