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
from urllib.parse import urljoin, urlparse

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


# -- kiểm tra kết quả trả về từ Muse ------------------------------------
# API web của Muse không công khai nên hình dạng JSON thật chưa được xác
# minh bằng lần chạy thật nào (mới chỉ chạy --mock). Các hàm dưới đây
# cố tình "rộng tay": bóc lớp data/result, chấp nhận nhiều tên khóa
# (video_url/url/result_url/output_url...), nhiều giá trị status
# (completed/done/finished/ready...), và khi gặp dạng lạ thì báo lỗi
# kèm nội dung thô (cắt ngắn) để lần chạy thật đầu tiên sửa cho khớp.

_URL_KEYS = ("video_url", "url", "result_url", "output_url", "media_url", "download_url")
_IMAGE_URL_KEYS = ("image_url", "url", "result_url", "output_url", "media_url")
_DONE_STATUSES = {"completed", "done", "success", "succeeded", "complete", "finished", "ready"}
_FAIL_STATUSES = {"failed", "error", "cancelled", "fail", "canceled"}


def _unwrap(data):
    """Bóc một lớp data/result nếu response bọc kết quả bên trong."""
    if isinstance(data, dict):
        for key in ("data", "result"):
            inner = data.get(key)
            if isinstance(inner, dict):
                return inner
    return data if isinstance(data, dict) else {}


def _find_url(data, keys=_URL_KEYS) -> str | None:
    """Tìm link media: tầng ngoài trước, rồi tới dict data/result bọc trong."""
    for d in (data, _unwrap(data)):
        if not isinstance(d, dict):
            continue
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
            if isinstance(v, dict):  # {"video": {"url": "..."}}
                u = v.get("url")
                if isinstance(u, str) and u.startswith("http"):
                    return u
    return None


def _status_of(data) -> str:
    for d in (data, _unwrap(data)):
        if isinstance(d, dict):
            s = d.get("status") or d.get("state")
            if s:
                return str(s).lower()
    return ""


def _progress_of(data) -> float | None:
    for d in (data, _unwrap(data)):
        if isinstance(d, dict):
            for k in ("progress", "percentage", "percent"):
                v = d.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
    return None


def _response_json(r, context: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise MuseError(
            f"{context}: HTTP {r.status_code}, body không phải JSON: {r.text[:300]}"
        ) from None
    if not isinstance(data, dict):
        raise MuseError(f"{context}: JSON không phải object: {str(data)[:300]}")
    return data


def _raise_for_status(r, context: str) -> None:
    try:
        r.raise_for_status()
    except requests.HTTPError:
        raise MuseError(f"{context}: HTTP {r.status_code}: {r.text[:300]}") from None


def _auth_error(status: int, action: str, r) -> MuseError:
    """401 ≠ 403 — đừng gộp chung thành 'cookie hết hạn':
    401 = session không được nhận (hết hạn/thiếu cookie),
    403 = session được nhận nhưng path này từ chối
    (sai endpoint, hoặc thiếu token/chữ ký riêng)."""
    body = (r.text or "")[:200]
    if status == 401:
        return MuseError(f"Cookie hết hạn (401) khi {action} — cần lấy cookie mới")
    return MuseError(
        f"Bị từ chối (403) khi {action} — session còn sống nhưng endpoint "
        f"không chấp nhận (sai đường dẫn hoặc thiếu token/chữ ký): {body}"
    )


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

    # -- auth / quota -------------------------------------------------
    def check_login(self) -> tuple[bool, str]:
        """Kiểm tra cookie có thực sự đăng nhập được vào Muse hay không.

        Đã xác minh bằng thử nghiệm thật (2026-10-26, cookie dạng
        hatch_native_auth_state / hatch_sess / hatch_gw):
        - GET {base}/ với cookie hợp lệ -> 200, trang app đầy đủ (~250KB),
          ở lại đúng host muse.ai.
        - Cookie hết hạn/giả -> bị redirect sang auth.muse.ai (400) hoặc 401.
        - /api/auth/session KHÔNG dùng được cho loại cookie này: nó trả 401
          ngay cả khi cookie còn sống, nên không được dùng làm phép thử nữa.
        Phép thử chính là GET trang chủ; /api/me là phép thử phụ.
        Lưu ý: đây chỉ kiểm tra session web còn sống, không đảm bảo API
        sinh video (endpoint chưa công khai) sẽ chấp nhận cookie.
        """
        base_host = urlparse(self.base_url).hostname or ""
        last_reason = ""
        for label, path in (("trang chủ", "/"), ("api/me", "/api/me")):
            try:
                r = self.session.get(self._url(path), timeout=20)
            except requests.RequestException as e:
                return False, f"Lỗi mạng/proxy khi kiểm tra {label}: {e}"
            final_host = urlparse(r.url).hostname or ""
            if (
                r.status_code == 200
                and final_host == base_host
                and len(r.text) > 10_000
            ):
                return True, f"Đăng nhập OK (session web còn sống, kiểm tra qua {label})"
            if r.status_code in (401, 403):
                last_reason = f"Cookie hết hạn hoặc bị từ chối (HTTP {r.status_code} ở {label})"
                break
            if final_host != base_host:
                last_reason = f"Chưa đăng nhập (bị chuyển sang {final_host})"
                break
            last_reason = (
                f"{label}: HTTP {r.status_code}, trang trả về không giống app đã đăng nhập"
            )
        return False, last_reason or "Không xác định được trạng thái đăng nhập"

    def check_quota(self) -> tuple[bool | None, str]:
        """Check token/quota còn lại của acc (best-effort).

        API web của Muse không công khai nên không có endpoint quota chắc
        chắn. Hàm này thử lần lượt các path ứng viên (cấu hình ở
        settings["muse"]["quota_check_paths"]) và đọc JSON trả về tìm các
        khóa số dư quen thuộc (credits/tokens/remaining/balance/quota...).
        Trả về (True, mô tả số dư) nếu đọc được; (False, lý do) nếu cookie
        hết hạn; (None, lý do) nếu không endpoint nào cho ra số dư — lúc
        đó giới hạn tuần cục bộ (UsageTracker) là cơ chế chính.
        """
        paths = self.settings.get("muse", {}).get(
            "quota_check_paths",
            ["/api/user/usage", "/api/usage", "/api/credits", "/api/quota"],
        )
        seen_keys: set[str] = set()
        for path in paths:
            try:
                r = self.session.get(self._url(path), timeout=15)
            except requests.RequestException:
                continue
            if r.status_code in (401, 403):
                return False, f"Cookie hết hạn hoặc bị từ chối (HTTP {r.status_code} ở {path})"
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data = data["data"]
            if not isinstance(data, dict):
                continue
            seen_keys.update(str(k) for k in data.keys())
            balance = self._extract_balance(data)
            if balance is not None:
                return True, f"Token/quota ({path}): {balance}"
        if seen_keys:
            return None, (
                "Endpoint quota trả JSON nhưng không có khóa số dư quen thuộc "
                f"(thấy: {', '.join(sorted(seen_keys)[:8])})"
            )
        return None, (
            "Không đọc được token/quota qua API "
            f"(đã thử: {', '.join(paths)}) — dùng giới hạn tuần cục bộ thay thế"
        )

    @staticmethod
    def _extract_balance(data: dict) -> str | None:
        """Tìm số dư trong JSON quota, ưu tiên khóa 'còn lại' rồi 'đã dùng'."""
        remaining_keys = (
            "remaining", "credits_remaining", "tokens_remaining",
            "balance", "token_balance", "credits", "tokens", "available",
            "quota_remaining", "weekly_remaining",
        )
        for key in remaining_keys:
            v = data.get(key)
            if isinstance(v, (int, float)):
                return f"còn {v} {key}"
            if isinstance(v, dict):
                for sub in ("value", "amount", "count"):
                    if isinstance(v.get(sub), (int, float)):
                        return f"còn {v[sub]} {key}.{sub}"
        used = data.get("used")
        limit = data.get("limit") or data.get("quota")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
            return f"đã dùng {used}/{limit}"
        return None

    # -- generation ---------------------------------------------------
    def generate_video(
        self,
        prompt: str,
        aspect: str = "16:9",
        duration: int = 5,
        reference_image: str | Path | None = None,
        out_path: str | Path | None = None,
    ) -> Path:
        job_id, direct_url = self._submit_video(prompt, aspect, duration, reference_image)
        if direct_url:
            # Một số API trả thẳng link video, không qua job
            dest = Path(out_path) if out_path else Path("video.mp4")
            return self._download(direct_url, dest)
        result = self._poll(job_id)
        url = _find_url(result)
        if not url:
            raise MuseError(f"Job {job_id} xong nhưng không có link video: {str(result)[:500]}")
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
            raise _auth_error(r.status_code, "sinh ảnh", r)
        _raise_for_status(r, "Sinh ảnh")
        data = _response_json(r, "Sinh ảnh")
        url = _find_url(data, _IMAGE_URL_KEYS)
        job_id = data.get("job_id") or _unwrap(data).get("job_id")
        if not url and job_id:
            result = self._poll(str(job_id))
            url = _find_url(result, _IMAGE_URL_KEYS)
        if not url:
            raise MuseError(f"Không có link ảnh: {str(data)[:500]}")
        dest = Path(out_path) if out_path else Path("image.png")
        return self._download(url, dest)

    def _submit_video(
        self, prompt: str, aspect: str, duration: int, reference_image
    ) -> tuple[str | None, str | None]:
        """Gửi prompt sinh video. Trả về (job_id, direct_url) — chỉ một trong hai có giá trị."""
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
            raise _auth_error(r.status_code, "submit video", r)
        if r.status_code == 429:
            raise MuseError("Bị rate-limit (429) — acc này nên nghỉ một lúc")
        _raise_for_status(r, "Submit video")
        data = _response_json(r, "Submit video")
        flat = _unwrap(data)
        job_id = data.get("job_id") or data.get("id") or flat.get("job_id") or flat.get("id")
        if job_id:
            return str(job_id), None
        url = _find_url(data)
        if url:
            return None, url
        raise MuseError(f"Response lạ khi submit: {str(data)[:500]}")

    def _poll(self, job_id: str) -> dict:
        deadline = time.time() + self.poll_timeout
        last_data: dict = {}
        last_progress: float | None = None
        while time.time() < deadline:
            r = self.session.get(self._url(self._job_status_path(job_id)), timeout=self.timeout)
            if r.status_code in (401, 403):
                raise _auth_error(r.status_code, "poll job", r)
            _raise_for_status(r, f"Poll job {job_id}")
            data = _response_json(r, f"Poll job {job_id}")
            last_data = data
            status = _status_of(data)
            if status in _DONE_STATUSES:
                return last_data
            if status in _FAIL_STATUSES:
                raise MuseError(f"Job {job_id} thất bại ({status}): {str(data)[:500]}")
            if not status and _find_url(
                data, ("video_url", "result_url", "output_url", "media_url", "download_url")
            ):
                # Không có status nhưng đã có link video rõ ràng -> coi như xong
                return last_data
            progress = _progress_of(data)
            if progress is not None and progress != last_progress:
                last_progress = progress
                print(f"  Job {job_id}: {progress:g}%", flush=True)
            time.sleep(self.poll_interval)
        raise MuseError(
            f"Job {job_id} quá thời gian chờ ({self.poll_timeout}s). "
            f"Phản hồi cuối: {str(last_data)[:500]}"
        )

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

    def check_quota(self) -> tuple[bool | None, str]:
        return None, "Mock mode — không kiểm tra quota thật"

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
    transport = settings.get("muse", {}).get("transport", "rest")
    if transport == "gateway":
        # Import muộn: gateway.py import muse_client (tránh vòng tròn).
        from .gateway import GatewayClient

        return GatewayClient(account, settings)
    return MuseClient(account, settings)
