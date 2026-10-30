#!/usr/bin/env python3
"""Setup máy làm server Web Studio (Windows/Linux) + setup port.

Chạy 1 lần trên máy sẽ làm server (máy 4GB RAM chạy tốt — sinh video
xảy ra trên server của Meta qua gateway, máy này chỉ chạy server Python
nhỏ + ffmpeg ráp clip 720p):

  python scripts/setup_server.py

Làm gì:
  1. Kiểm tra Python >= 3.10 và ffmpeg/ffprobe có trong PATH.
  2. Tạo .venv (nếu chưa có) và cài requirements.txt (trừ --skip-install).
  3. Hỏi host + port (mặc định 127.0.0.1:8080), kiểm tra port còn trống
     (port < 1024 có thể cần quyền admin/root — chỉ cảnh báo).
  4. Lưu web.host/web.port vào config/settings.yaml (file này gitignored,
     không đẩy lên GitHub).
  5. Tạo start-server.bat (Windows) + start-server.sh (Linux) — chạy file
     đó là mở server với port đã setup, vì `cli.py web` tự đọc settings.yaml
     (cờ --host/--port vẫn thắng nếu bạn gõ thêm).

Chạy không tương tác (để test hoặc script tự động):
  python scripts/setup_server.py --port 8931 --host 127.0.0.1 --yes --skip-install

Đổi port sau này: chạy lại script này, hoặc sửa web.port trong
config/settings.yaml rồi khởi động lại server.
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parent.parent


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def port_free(host: str, port: int) -> bool:
    """Thử bind để biết port còn trống (đóng ngay sau khi thử)."""
    bind_host = "" if host == "0.0.0.0" else host
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((bind_host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def prompt_host(default: str, yes: bool) -> str:
    if yes:
        return default
    raw = input(f"Host để server lắng nghe [{default}] "
                f"(127.0.0.1 = chỉ máy này, 0.0.0.0 = cả mạng LAN): ").strip()
    return raw or default


def prompt_port(default: int, host: str, yes: bool,
                preset: int | None) -> int:
    """Hỏi port tới khi được số hợp lệ + còn trống (yes mode thì lỗi luôn
    nếu port preset bận, thay vì hỏi lại)."""
    candidate = preset if preset is not None else default
    if yes:
        if not 1 <= candidate <= 65535:
            raise SystemExit(f"Port {candidate} ngoài khoảng 1-65535.")
        if not port_free(host, candidate):
            raise SystemExit(f"Port {candidate} đang bận (đã có tiến trình "
                             f"dùng) — chọn port khác.")
        return candidate
    while True:
        raw = input(f"Port cho server [{candidate}]: ").strip()
        if raw:
            try:
                candidate = int(raw)
            except ValueError:
                print("  Port phải là số.")
                continue
        if not 1 <= candidate <= 65535:
            print("  Port phải trong khoảng 1-65535.")
            continue
        if candidate < 1024:
            print("  Lưu ý: port < 1024 có thể cần quyền admin/root.")
        if not port_free(host, candidate):
            print(f"  Port {candidate} đang bận — chọn port khác.")
            continue
        return candidate


def write_start_scripts(root: Path) -> tuple[Path, Path]:
    """Tạo file khởi động cho cả 2 hệ điều hành (port đọc từ settings.yaml,
    không hardcode port vào file chạy — 1 nguồn sự thật duy nhất)."""
    bat = root / "start-server.bat"
    bat.write_text(
        "@echo off\r\n"
        "REM Khoi dong The Gioi Dong Vat Studio (port doc tu config/settings.yaml).\r\n"
        "REM Tao boi scripts/setup_server.py — chay lai setup de doi port.\r\n"
        "\"%~dp0.venv\\Scripts\\python.exe\" \"%~dp0cli.py\" web\r\n"
        "pause\r\n",
        encoding="utf-8",
    )
    sh = root / "start-server.sh"
    sh.write_text(
        "#!/bin/sh\n"
        "# Khoi dong The Gioi Dong Vat Studio (port doc tu config/settings.yaml).\n"
        "# Tao boi scripts/setup_server.py — chay lai setup de doi port.\n"
        'DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'exec "$DIR/.venv/bin/python" "$DIR/cli.py" web\n',
        encoding="utf-8",
    )
    try:
        os.chmod(sh, 0o755)
    except OSError:
        pass
    return bat, sh


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Setup máy làm server Web Studio + setup port.")
    parser.add_argument("--root", default="",
                        help="Thư mục repo (mặc định: suy từ vị trí script; "
                             "chỉ dùng khi test)")
    parser.add_argument("--host", default=None, help="Host (mặc định hỏi)")
    parser.add_argument("--port", type=int, default=None,
                        help="Port (mặc định hỏi)")
    parser.add_argument("--yes", action="store_true",
                        help="Không hỏi gì, dùng giá trị cho sẵn/mặc định")
    parser.add_argument("--skip-venv", action="store_true",
                        help="Bỏ qua bước tạo .venv (để test)")
    parser.add_argument("--skip-install", action="store_true",
                        help="Bỏ qua cài requirements.txt (để test)")
    args = parser.parse_args(argv)
    # root = máy/repo cần setup (mặc định là repo chứa script này).
    # --root chỉ dùng khi test: import automuse vẫn lấy từ repo của script,
    # còn settings/file khởi động thì ghi vào root được chỉ định.
    script_repo = repo_root_from_script()
    root = Path(args.root).resolve() if args.root else script_repo

    if sys.version_info < MIN_PYTHON:
        print(f"Cần Python >= 3.10 (đang dùng {sys.version.split()[0]}).")
        return 1
    print(f"Repo: {root}")

    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        print(f"Thiếu trong PATH: {', '.join(missing)} — cài ffmpeg trước "
              f"(Windows: winget install ffmpeg; Linux: apt install ffmpeg).")
        return 1
    print("Đã có ffmpeg + ffprobe.")

    if not args.skip_venv:
        venv = venv_python(root)
        if not venv.exists():
            print("Đang tạo .venv ...")
            proc = subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")])
            if proc.returncode != 0:
                print("Tạo .venv thất bại.")
                return 1
            venv = venv_python(root)
        else:
            print("Đã có .venv, dùng lại.")
        if not args.skip_install:
            req = root / "requirements.txt"
            print("Đang cài requirements.txt (có thể mất vài phút) ...")
            proc = subprocess.run([str(venv), "-m", "pip", "install", "-q",
                                   "-r", str(req)])
            if proc.returncode != 0:
                print("Cài requirements thất bại — kiểm tra lại mạng/pip.")
                return 1
            print("Cài xong requirements.")
    else:
        print("Bỏ qua bước .venv (theo cờ --skip-venv).")

    host = prompt_host(args.host or DEFAULT_HOST, args.yes)
    port = prompt_port(DEFAULT_PORT, host, args.yes, args.port)

    sys.path.insert(0, str(script_repo))
    from automuse.config import save_user_settings
    save_user_settings({"web": {"host": host, "port": port}},
                       path=root / "config" / "settings.yaml")
    print(f"Đã lưu web.host={host}, web.port={port} vào config/settings.yaml.")

    bat, sh = write_start_scripts(root)
    print(f"Đã tạo {bat.name} (Windows) và {sh.name} (Linux).")
    print()
    print("XONG. Khởi động server:")
    if os.name == "nt":
        print(f"  {bat.name}   (nháy đúp hoặc chạy trong terminal)")
    else:
        print(f"  ./{sh.name}")
    if host == "0.0.0.0":
        print(f"  Rồi mở http://<IP-máy-này>:{port} từ máy khác trong mạng LAN.")
        print("  Lưu ý: Web Studio không có đăng nhập — đừng mở port ra internet.")
    else:
        print(f"  Rồi mở http://127.0.0.1:{port} trên chính máy đó.")
    print("  Tiếp theo: thêm tài khoản Muse ở tab Tài khoản Muse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
