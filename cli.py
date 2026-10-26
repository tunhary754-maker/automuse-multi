#!/usr/bin/env python3
"""AutoMuse Multi — CLI.

Ví dụ:
  python cli.py accounts add --name acc1 --cookie "sessionid=...; ..." --proxy http://user:pass@host:port
  python cli.py accounts list
  python cli.py accounts check
  python cli.py voices
  python cli.py init --kind documentary --out scripts/dong-vat.yaml
  python cli.py build --script examples/the-gioi-dong-vat.yaml --mock
  python cli.py build --script scripts/phim.yaml --out output/phim1
"""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from automuse.accounts import Account, AccountManager, load_cookies  # noqa: E402
from automuse.config import (  # noqa: E402
    ACCOUNTS_FILE,
    REPO_ROOT,
    elevenlabs_api_key,
    load_settings,
)
from automuse.elevenlabs import build_elevenlabs  # noqa: E402
from automuse.muse_client import build_client  # noqa: E402
from automuse.pipeline import generate_clips, log, run_build  # noqa: E402
from automuse.script import load_script, validate  # noqa: E402


def _manager() -> AccountManager:
    return AccountManager()


# ---------------------------------------------------------------- accounts

def cmd_accounts_add(args) -> int:
    mgr = _manager()
    cookie = args.cookie or ""
    if args.cookie_file:
        cookie = ""  # dùng file, không lưu chuỗi thô
    if not cookie and not args.cookie_file:
        cookie = getpass.getpass("Dán cookie Muse (ẩn khi gõ, Enter để xong): ").strip()
    if not cookie and not args.cookie_file:
        print("Chưa nhập cookie.")
        return 1
    try:
        if args.cookie_file:
            load_cookies(args.cookie_file)
        else:
            if not load_cookies(cookie):
                print("Cookie rỗng/không parse được.")
                return 1
    except Exception as e:  # noqa: BLE001
        print(f"Cookie không hợp lệ: {e}")
        return 1
    acc = Account(
        name=args.name,
        cookie=cookie,
        cookie_file=args.cookie_file or "",
        proxy=args.proxy or "",
        note=args.note or "",
    )
    try:
        mgr.add(acc)
    except ValueError as e:
        print(f"Lỗi: {e}")
        return 1
    print(f"Đã thêm tài khoản '{args.name}' (proxy: {acc.proxy_config().masked()})")
    print(f"Lưu tại: {ACCOUNTS_FILE}")
    return 0


def cmd_accounts_list(_args) -> int:
    mgr = _manager()
    if not mgr.accounts:
        print("Chưa có tài khoản nào. Thêm bằng: accounts add --name acc1 --cookie ...")
        return 0
    print(f"{'Tên':<16} {'Proxy':<32} {'Jobs':<6} {'Trạng thái'}")
    for a in mgr.accounts:
        status = "tắt" if not a.enabled else ("cooldown" if a.is_on_cooldown() else "sẵn sàng")
        print(f"{a.name:<16} {a.proxy_config().masked():<32} {a.jobs_done:<6} {status}")
    return 0


def cmd_accounts_remove(args) -> int:
    mgr = _manager()
    if mgr.remove(args.name):
        print(f"Đã xóa '{args.name}'")
        return 0
    print(f"Không có tài khoản '{args.name}'")
    return 1


def cmd_accounts_check(args) -> int:
    mgr = _manager()
    targets = [mgr.get(args.name)] if args.name else mgr.accounts
    targets = [a for a in targets if a]
    if not targets:
        print("Không có tài khoản nào để kiểm tra.")
        return 1
    settings = load_settings()
    ok_all = True
    for acc in targets:
        print(f"\n== {acc.name} ==")
        proxy = acc.proxy_config()
        if proxy.is_valid():
            ok, msg = proxy.test()
            print(f"  Proxy {proxy.masked()}: {'OK' if ok else 'LỖI'} — {msg}")
            if not ok:
                ok_all = False
        client = build_client(acc, settings, mock=False)
        ok, msg = client.check_login()
        print(f"  Muse login: {'OK' if ok else 'LỖI'} — {msg}")
        if not ok:
            ok_all = False
    return 0 if ok_all else 1


# ---------------------------------------------------------------- voices

def cmd_voices(args) -> int:
    settings = load_settings()
    key = elevenlabs_api_key()
    client = build_elevenlabs(key, settings, mock=args.mock or not key)
    try:
        voices = client.list_voices()
    except Exception as e:  # noqa: BLE001
        print(f"Lỗi: {e}")
        return 1
    print(f"{'Voice ID':<28} {'Tên'}")
    for v in voices:
        print(f"{str(v.get('voice_id')):<28} {v.get('name')}")
    if not key and not args.mock:
        print("\n(Chế độ mock — chưa có ELEVENLABS_API_KEY. "
              "Đặt key vào .env hoặc biến môi trường để xem giọng thật.)")
    return 0


# ---------------------------------------------------------------- init

def cmd_init(args) -> int:
    kind = args.kind
    src = REPO_ROOT / "templates" / f"{kind}_script.yaml"
    if not src.exists():
        print(f"Không có template cho kind '{kind}' (movie | documentary)")
        return 1
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not args.force:
        print(f"{dest} đã tồn tại, thêm --force để ghi đè.")
        return 1
    shutil.copy(src, dest)
    print(f"Đã tạo kịch bản mẫu: {dest}")
    print("Mở file ra sửa visual/narration rồi chạy: "
          f"python cli.py build --script {dest} --mock")
    return 0


# ---------------------------------------------------------------- generate/dub/build

def cmd_generate(args) -> int:
    settings = load_settings()
    mgr = _manager()
    script = load_script(args.script)
    errors = validate(script)
    if errors:
        print("Kịch bản lỗi:\n- " + "\n- ".join(errors))
        return 1
    if not args.mock and not mgr.enabled_accounts():
        print("Chưa có tài khoản nào. Thêm acc hoặc chạy với --mock để thử.")
        return 1
    out_dir = Path(args.out) if args.out else Path("output") / Path(args.script).stem
    names = args.accounts.split(",") if args.accounts else None
    try:
        clips, _infos = generate_clips(
            script, out_dir, settings, mgr, mock=args.mock,
            account_names=names, resume=args.resume,
        )
    except RuntimeError as e:
        print(f"Lỗi: {e}")
        return 1
    print(f"\nĐã sinh {len(clips)} clip tại {out_dir / 'clips'}")
    return 0


def cmd_dub(args) -> int:
    settings = load_settings()
    key = elevenlabs_api_key()
    script = load_script(args.script)
    errors = validate(script)
    if errors:
        print("Kịch bản lỗi:\n- " + "\n- ".join(errors))
        return 1
    client = build_elevenlabs(key, settings, mock=args.mock or not key)
    out_dir = Path(args.out) if args.out else Path("output") / Path(args.script).stem / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)
    from automuse.audio import build_narration_timeline, probe_duration  # noqa: E402
    from automuse.elevenlabs import estimate_speech_duration  # noqa: E402

    segments, starts, total = [], [], 0.0
    for scene in script.scenes:
        starts.append(total)
        if not scene.tts_text:
            total += scene.duration
            continue
        voice_name = scene.voice or script.default_voice
        voice_id = script.voices.get(voice_name) or voice_name
        if not args.mock and key:
            voice_id = client.resolve_voice(voice_id)
        dest = out_dir / f"{scene.id}.{'wav' if (args.mock or not key) else 'mp3'}"
        log(f"TTS {scene.id} giọng '{voice_name}'")
        dest = client.tts(scene.tts_text, voice_id, out_path=dest)
        try:
            dur = probe_duration(dest)
        except Exception:  # noqa: BLE001
            dur = estimate_speech_duration(scene.tts_text)
        segments.append((dest, starts[-1]))
        total += max(scene.duration, dur + 0.5)
    narration = out_dir / "narration.wav"
    build_narration_timeline(segments, total, narration)
    print(f"\nXong. File lời thoại tổng: {narration} (~{total:.1f}s)")
    return 0


def cmd_build(args) -> int:
    settings = load_settings()
    mgr = _manager()
    if not args.mock and not mgr.enabled_accounts():
        print("Chưa có tài khoản nào. Thêm acc (accounts add) hoặc chạy với --mock để thử.")
        return 1
    names = args.accounts.split(",") if args.accounts else None
    try:
        manifest = run_build(
            script_path=args.script,
            out_dir=args.out,
            settings=settings,
            manager=mgr,
            mock=args.mock,
            no_tts=args.no_tts,
            iris_out=not args.no_iris,
            resume=args.resume,
            account_names=names,
            music=args.music,
            api_key=elevenlabs_api_key(),
        )
    except (ValueError, RuntimeError) as e:
        print(f"Lỗi: {e}")
        return 1
    print("\n— Manifest —")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- web

def cmd_web(args) -> int:
    from automuse.web.server import serve

    print("Mở trình duyệt tại "
          f"http://{args.host}:{args.port}  (chỉ truy cập được từ máy này)")
    serve(args.host, args.port)
    return 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="automuse",
        description="AutoMuse Multi — video dài multi-account Muse + lồng tiếng ElevenLabs",
    )
    sub = p.add_subparsers(dest="command", required=True)

    acc = sub.add_parser("accounts", help="Quản lý tài khoản Muse (cookie + proxy)")
    acc_sub = acc.add_subparsers(dest="action", required=True)
    a_add = acc_sub.add_parser("add", help="Thêm tài khoản")
    a_add.add_argument("--name", required=True)
    a_add.add_argument("--cookie", default="", help="Chuỗi header Cookie thô")
    a_add.add_argument("--cookie-file", default="", help="File cookies.txt/.json")
    a_add.add_argument("--proxy", default="", help="vd: http://user:pass@host:port")
    a_add.add_argument("--note", default="")
    a_add.set_defaults(func=cmd_accounts_add)
    a_list = acc_sub.add_parser("list", help="Liệt kê tài khoản")
    a_list.set_defaults(func=cmd_accounts_list)
    a_rm = acc_sub.add_parser("remove", help="Xóa tài khoản")
    a_rm.add_argument("--name", required=True)
    a_rm.set_defaults(func=cmd_accounts_remove)
    a_check = acc_sub.add_parser("check", help="Kiểm tra proxy + cookie còn sống không")
    a_check.add_argument("--name", default="")
    a_check.set_defaults(func=cmd_accounts_check)

    v = sub.add_parser("voices", help="Liệt kê giọng ElevenLabs")
    v.add_argument("--mock", action="store_true")
    v.set_defaults(func=cmd_voices)

    i = sub.add_parser("init", help="Tạo kịch bản mẫu (movie/documentary)")
    i.add_argument("--kind", choices=["movie", "documentary"], default="documentary")
    i.add_argument("--out", required=True)
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    g = sub.add_parser("generate", help="Chỉ sinh clip từng cảnh (không lồng tiếng)")
    g.add_argument("--script", required=True)
    g.add_argument("--out", default="")
    g.add_argument("--mock", action="store_true")
    g.add_argument("--accounts", default="")
    g.add_argument("--resume", action="store_true")
    g.set_defaults(func=cmd_generate)

    d = sub.add_parser("dub", help="Chỉ lồng tiếng theo kịch bản (ElevenLabs)")
    d.add_argument("--script", required=True)
    d.add_argument("--out", default="")
    d.add_argument("--mock", action="store_true")
    d.set_defaults(func=cmd_dub)

    b = sub.add_parser("build", help="Chạy full pipeline: clip + lồng tiếng + dựng final.mp4")
    b.add_argument("--script", required=True)
    b.add_argument("--out", default="")
    b.add_argument("--mock", action="store_true")
    b.add_argument("--accounts", default="", help="Chỉ dùng các acc này, cách nhau bởi dấu phẩy")
    b.add_argument("--music", default="", help="File nhạc nền (mp3/wav)")
    b.add_argument("--no-tts", action="store_true")
    b.add_argument("--no-iris", action="store_true")
    b.add_argument("--resume", action="store_true")
    b.set_defaults(func=cmd_build)

    w = sub.add_parser("web", help="Mở Web Studio — giao diện quản lý kiểu automuse.site")
    w.add_argument("--host", default="127.0.0.1", help="Mặc định chỉ máy này truy cập được")
    w.add_argument("--port", type=int, default=8080)
    w.set_defaults(func=cmd_web)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
