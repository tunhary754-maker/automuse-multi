"""Client gọi Muse AI để sinh video/ảnh bằng session cookie.

Hai chế độ:
  * MuseClient (thật): requests.Session gắn cookie + proxy riêng của acc,
    gọi API web của Muse (endpoint cấu hình trong config/settings.yaml),
    poll job rồi tải file về.
  * MockMuseClient: không cần acc thật, sinh clip mẫu bằng ffmpeg
    (testsrc2) để chạy thử toàn bộ pipeline end-to-end.

LƯU Ý: API web của Muse không công khai. Các path mặc định trong
settings.yaml là dạng chuẩn thường gặp; nếu Muse đổi API, chỉ cần sửa
settings.yaml cho khớp với request thật quan sát được ở DevTools
(muse.ai -> Network) khi bấm Generate trên web.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

from .accounts import Account
from .audio import run_ffmpeg

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://muse.ai/",
    "Origin": "https://muse.ai",
}


class MuseError(Exception):
    pass


class MuseClient:
    """Client thật: cookie + proxy của từng tài khoản."""

    def __init__(self, account: Account, settings: dict):
        self.account = account
        self.settings = settings
        muse_cfg = settings.get("muse", {})
        self.base_url = muse_cfg.get("base_url", "https://muse.ai").rstrip("/")
        self.timeout = int(muse_cfg.get("timeout", 120))
        self.poll_interval = int(muse_cfg.get("poll_interval", 5))
        self.poll_timeout = int(muse_cfg.get("poll_timeout", 900))

        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.cookies.update(account.cookies())
        proxy = account.proxy_config()
        if proxy.is_valid():
            self.session.proxies.update(proxy.to_requests())

    # -- helpers ------------------------------------------------------
    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _job_status_path(self, job_id: str) -> str:
        tpl = self.settings["muse"].get("job_status_path", "/api/media/jobs/{job_id}")
        return tpl.format(job_id=job_id)

    # -- auth ---------------------------------------------------------
    def check_login(self) -> tuple[bool, str]:
        """GET endpoint session: 200 + có user/id là coi như cookie còn sống."""
        path = self.settings["muse"].get("login_check_path", "/api/auth/session")
        try:
            r = self.session.get(self._url(path), timeout=self.timeout)
        except requests.RequestException as e:
            return False, f"Lỗi mạng/proxy: {e}"
        if r.status_code in (401, 403):
            return False, f"Cookie hết hạn hoặc bị từ chối (HTTP {r.status_code})"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        try:
            data = r.json()
        except ValueError:
            return True, "HTTP 200 (không phải JSON, coi như đăng nhập OK)"
        user = data.get("user") or data.get("email") or data.get("id") or data
        return True, f"Đăng nhập OK: {user}"

    # -- generation ---------------------------------------------------
    def generate_video(
        self,
        prompt: str,
        aspect: str = "16:9",
        duration: int = 5,
        reference_image: str | Path | None = None,
        out_path: str | Path | None = None,
    ) -> Path:
        job_id = self._submit_video(prompt, aspect, duration, reference_image)
        result = self._poll(job_id)
        url = result.get("video_url") or result.get("url") or result.get("result_url")
        if not url:
            raise MuseError(f"Job {job_id} xong nhưng không có link video: {result}")
        dest = Path(out_path) if out_path else Path(f"{job_id}.mp4")
        return self._download(url, dest)

    def generate_image(
        self,
        prompt: str,
        aspect: str = "16:9",
        out_path: str | Path | None = None,
    ) -> Path:
        path = self.settings["muse"].get(
            "generate_image_path", "/api/media/generate/image"
        )
        r = self.session.post(
            self._url(path),
            json={"prompt": prompt, "aspect_ratio": aspect},
            timeout=self.timeout,
        )
        if r.status_code in (401, 403):
            raise MuseError("Cookie hết hạn (401/403) khi sinh ảnh")
        r.raise_for_status()
        data = r.json()
        url = data.get("image_url") or data.get("url")
        if not url and "job_id" in data:
            result = self._poll(str(data["job_id"]))
            url = result.get("image_url") or result.get("url")
        if not url:
            raise MuseError(f"Không có link ảnh: {data}")
        dest = Path(out_path) if out_path else Path("image.png")
        return self._download(url, dest)

    def _submit_video(
        self, prompt: str, aspect: str, duration: int, reference_image
    ) -> str:
        path = self.settings["muse"].get(
            "generate_video_path", "/api/media/generate/video"
        )
        payload: dict = {
            "prompt": prompt,
            "aspect_ratio": aspect,
            "duration": duration,
        }
        if reference_image:
            payload["reference_image"] = str(reference_image)
        r = self.session.post(self._url(path), json=payload, timeout=self.timeout)
        if r.status_code in (401, 403):
            raise MuseError("Cookie hết hạn (401/403) khi submit video")
        if r.status_code == 429:
            raise MuseError("Bị rate-limit (429) — acc này nên nghỉ một lúc")
        r.raise_for_status()
        data = r.json()
        job_id = data.get("job_id") or data.get("id")
        if job_id:
            return str(job_id)
        # Một số API trả thẳng link video, không qua job
        if data.get("video_url") or data.get("url"):
            raise MuseError("API trả thẳng video, chưa hỗ trợ dạng này")
        raise MuseError(f"Response lạ khi submit: {data}")

    def _poll(self, job_id: str) -> dict:
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            r = self.session.get(self._url(self._job_status_path(job_id)), timeout=self.timeout)
            if r.status_code in (401, 403):
                raise MuseError("Cookie hết hạn (401/403) khi poll job")
            r.raise_for_status()
            data = r.json()
            status = str(data.get("status") or data.get("state") or "").lower()
            if status in ("completed", "done", "success", "succeeded"):
                return data
            if status in ("failed", "error", "cancelled"):
                raise MuseError(f"Job {job_id} thất bại: {data}")
            time.sleep(self.poll_interval)
        raise MuseError(f"Job {job_id} quá thời gian chờ ({self.poll_timeout}s)")

    def _download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(url, stream=True, timeout=self.timeout) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        return dest


# ---------------------------------------------------------------- mock

class MockMuseClient:
    """Sinh clip mẫu bằng ffmpeg (testsrc2) — chạy thử pipeline không cần acc."""

    def __init__(self, account: Account | None = None, settings: dict | None = None):
        self.account = account
        self.settings = settings or {}
        self.name = (account.name if account else "mock")

    def check_login(self) -> tuple[bool, str]:
        return True, "Mock mode — không cần đăng nhập thật"

    def generate_video(
        self,
        prompt: str,
        aspect: str = "16:9",
        duration: int = 5,
        reference_image=None,
        out_path=None,
    ) -> Path:
        dest = Path(out_path) if out_path else Path("mock.mp4")
        dest.parent.mkdir(parents=True, exist_ok=True)
        w, h = (1280, 720) if aspect == "16:9" else (720, 1280)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
            str(dest),
        ]
        run_ffmpeg(cmd)
        return dest

    def generate_image(self, prompt: str, aspect: str = "16:9", out_path=None) -> Path:
        dest = Path(out_path) if out_path else Path("mock.png")
        dest.parent.mkdir(parents=True, exist_ok=True)
        w, h = (1280, 720) if aspect == "16:9" else (720, 1280)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration=1",
            "-frames:v", "1", str(dest),
        ]
        run_ffmpeg(cmd)
        return dest


def build_client(account: Account | None, settings: dict, mock: bool = False):
    if mock or account is None:
        return MockMuseClient(account, settings)
    return MuseClient(account, settings)
