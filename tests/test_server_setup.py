"""Test cơ chế setup port cho server: cli.py web đọc settings.yaml +
scripts/setup_server.py lưu port + tạo file khởi động.

Setup script được test qua subprocess với --root thư mục tạm (không động
vào repo thật, không tạo venv/cài deps thật nhờ --skip-venv/--skip-install).
"""

import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestWebPortResolution(unittest.TestCase):
    """cli.py web: cờ CLI thắng settings.yaml, settings thắng mặc định."""

    def _run_web(self, cli_host, cli_port, settings_web):
        import cli as cli_mod
        args = mock.Mock(host=cli_host, port=cli_port)
        with mock.patch.object(cli_mod, "load_settings",
                               return_value={"web": settings_web}), \
                mock.patch("automuse.web.server.serve") as serve:
            cli_mod.cmd_web(args)
        return serve.call_args[0]

    def test_cli_args_win(self):
        host, port = self._run_web("0.0.0.0", 9001, {"host": "127.0.0.1",
                                                    "port": 8080})
        self.assertEqual((host, port), ("0.0.0.0", 9001))

    def test_settings_beats_default(self):
        host, port = self._run_web(None, None, {"host": "127.0.0.1",
                                               "port": 8931})
        self.assertEqual((host, port), ("127.0.0.1", 8931))

    def test_default_when_nothing_set(self):
        host, port = self._run_web(None, None, {})
        self.assertEqual((host, port), ("127.0.0.1", 8080))

    def test_invalid_port_falls_back(self):
        host, port = self._run_web(None, None, {"port": 99999})
        self.assertEqual(port, 8080)


class TestSetupServerScript(unittest.TestCase):
    def _run_setup(self, root: Path, *extra):
        cmd = [sys.executable, str(REPO / "scripts" / "setup_server.py"),
               "--root", str(root), "--yes", "--skip-venv", "--skip-install",
               *extra]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    def test_saves_port_and_creates_start_scripts(self):
        root = Path(tempfile.mkdtemp())
        port = _free_port()
        proc = self._run_setup(root, "--port", str(port))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        import yaml
        settings = yaml.safe_load((root / "config" / "settings.yaml").read_text())
        self.assertEqual(settings["web"]["port"], port)
        self.assertEqual(settings["web"]["host"], "127.0.0.1")
        bat = (root / "start-server.bat").read_text()
        sh = (root / "start-server.sh").read_text()
        self.assertIn("cli.py", bat)
        self.assertIn("cli.py", sh)
        self.assertIn(".venv", bat)
        self.assertIn(".venv", sh)

    def test_busy_port_fails_in_yes_mode(self):
        root = Path(tempfile.mkdtemp())
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        try:
            proc = self._run_setup(root, "--port", str(busy))
        finally:
            sock.close()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("đang bận", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
