"""Sản xuất hàng loạt kịch bản theo chủ đề (Web Studio).

Một "đợt" (batch) = N chủ đề x M tập mỗi chủ đề, mỗi tập dài K phút
mục tiêu. Vòng chạy viết từng kịch bản (nghiên cứu 1 lần mỗi chủ đề,
dùng chung cho mọi tập của chủ đề đó), ghi tiến độ vào cache/batches.json
sau mỗi kịch bản để UI theo dõi và để server restart không mất trạng thái.
Kịch bản lỗi không làm chết cả đợt: ghi lỗi rồi làm tiếp tập sau.
Mỗi kịch bản xong gửi 1 tin Telegram (nếu đã cấu hình): "Đã hoàn thành
xong: <chủ đề> + <tên kịch bản>".

Chạy song song theo số acc: có bao nhiêu acc Muse thì bấy nhiêu kịch
bản chạy cùng lúc, MỖI ACC LÀM TRỌN 1 KỊCH BẢN (không xé nhỏ 1 kịch bản
ra nhiều acc rồi ghép clip lại — ghép như vậy khó khớp). Mỗi acc cùng
lúc chỉ gọi gateway 1 lần (1 lock/acc); acc chết giữa chừng thì phần
việc còn lại chuyển sang acc khác (failover, giống pipeline build).
Acc hỏng chỉ báo 1 lần/đợt.

Tạm dừng / dừng hẳn: UI bấm tạm dừng thì các kịch bản CHƯA BẮT ĐẦU sẽ
đứng chờ (kịch bản đang viết vẫn viết nốt 1 lần gọi gateway rồi mới
dừng — không cắt ngang cuộc gọi đang chạy); bấm tiếp tục thì chạy tiếp.
Bấm dừng hẳn thì không bắt đầu kịch bản mới nữa; đợt bị gián đoạn
(server restart, crash) hoặc đã dừng/lỗi có thể bấm "chạy tiếp" để làm
nốt phần còn lại (kịch bản đã xong được bỏ qua, không viết lại).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BATCHES_FILE = REPO_ROOT / "cache" / "batches.json"

# Khóa cho mọi thao tác đọc-sửa-ghi manifest (server 1 process, nhiều
# thread: worker batch + handler HTTP). Đọc file thuần thì an toàn nhờ
# ghi atomically (tmp + replace), nhưng đọc-sửa-ghi phải khóa mới không
# ghi đè mất thay đổi của nhau (vd: worker persist đè mất cờ tạm dừng
# mà UI vừa bấm).
_STATE_LOCK = threading.Lock()
# Khóa chọn tên file + lưu kịch bản (2 task khác chủ đề có thể slug
# trùng nhau — vd: "Cá voi xanh" vs "Cá Voi Xanh" — nên phải khóa).
_SAVE_LOCK = threading.Lock()


# ---------------------------------------------------------------- manifest

def load_batches() -> list[dict]:
    """Đọc toàn bộ đợt. File thiếu/hỏng -> list rỗng, không crash."""
    try:
        data = json.loads(BATCHES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_batches(batches: list[dict]) -> None:
    """Ghi atomically (tmp + replace). CHỈ gọi khi đã giữ _STATE_LOCK."""
    BATCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BATCHES_FILE.with_name(BATCHES_FILE.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(batches, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, BATCHES_FILE)


def save_batches(batches: list[dict]) -> None:
    """Ghi toàn bộ manifest (khóa). Dùng cho ghi đè đơn giản."""
    with _STATE_LOCK:
        _write_batches(batches)


def _update_batches(fn) -> None:
    """Đọc-sửa-ghi nguyên tử: fn(batches) sửa list tại chỗ rồi ghi."""
    with _STATE_LOCK:
        batches = load_batches()
        fn(batches)
        _write_batches(batches)


def append_batch(batch: dict) -> None:
    """Thêm 1 đợt mới (khóa, không làm mất tiến độ đợt đang chạy)."""
    _update_batches(lambda bs: bs.append(batch))


def replace_batch(batch: dict) -> None:
    """Ghi đè 1 đợt theo id (khóa). Dùng khi restart: chỉ thay đợt đó,
    không làm mất tiến độ đợt khác đang chạy."""

    def _replace(bs):
        for i, b in enumerate(bs):
            if b.get("id") == batch["id"]:
                bs[i] = batch
                break
        else:
            bs.append(batch)

    _update_batches(_replace)


def get_batch(batch_id: str) -> dict | None:
    for b in load_batches():
        if b.get("id") == batch_id:
            return b
    return None


def create_batch(topics: list[dict], episodes_per_topic: int,
                 minutes: int, account: str = "") -> dict:
    """Tạo dict đợt mới. topics: [{name, reason}]."""
    topics_out = []
    total = 0
    for t in topics:
        scripts = [{"index": i + 1, "file": "", "title": "",
                    "status": "pending", "scenes": 0, "error": ""}
                   for i in range(max(1, episodes_per_topic))]
        topics_out.append({"name": t.get("name", ""),
                           "reason": t.get("reason", ""),
                           "scripts": scripts})
        total += len(scripts)
    batch = {
        "id": f"batch-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "minutes": int(minutes or 0),
        "episodes_per_topic": int(episodes_per_topic),
        "account": account or "",
        "status": "running",
        "paused": False,
        "cancelled": False,
        "total": total,
        "done": 0,
        "failed": 0,
        "topics": topics_out,
    }
    return batch


def recount(batch: dict) -> None:
    """Tính lại done/failed/total từ trạng thái từng kịch bản."""
    done = failed = total = 0
    for t in batch.get("topics", []):
        for s in t.get("scripts", []):
            total += 1
            if s.get("status") == "done":
                done += 1
            elif s.get("status") == "failed":
                failed += 1
    batch["total"] = total
    batch["done"] = done
    batch["failed"] = failed


def remove_script_from_batches(rel_file: str) -> bool:
    """Xóa 1 kịch bản khỏi mọi đợt (khi người dùng xóa file). Trả True
    nếu có đợt nào bị thay đổi."""
    changed = [False]

    def _remove(bs):
        for b in bs:
            for t in b.get("topics", []):
                before = len(t.get("scripts", []))
                t["scripts"] = [s for s in t.get("scripts", [])
                                if s.get("file") != rel_file]
                if len(t["scripts"]) != before:
                    changed[0] = True
                    recount(b)

    _update_batches(_remove)
    return changed[0]


def set_batch_flags(batch_id: str, paused=None, cancelled=None) -> bool:
    """Đặt cờ tạm dừng/dừng hẳn cho 1 đợt (khóa). Trả False nếu không
    thấy đợt. Chỉ đổi cờ được truyền vào (None = giữ nguyên)."""
    found = [False]

    def _set(bs):
        for b in bs:
            if b.get("id") == batch_id:
                found[0] = True
                if paused is not None:
                    b["paused"] = bool(paused)
                if cancelled is not None:
                    b["cancelled"] = bool(cancelled)

    _update_batches(_set)
    return found[0]


def refresh_statuses(alive_ids: set[str]) -> None:
    """Đợt nào status 'running'/'paused' mà thread đã chết/mất (server
    restart, crash) thì đánh 'interrupted' để UI không hiển thị 'đang
    chạy' mãi. Đợt interrupted dùng được nút 'chạy tiếp'."""
    def _refresh(bs):
        for b in bs:
            if b.get("status") in ("running", "paused") \
                    and b.get("id") not in alive_ids:
                b["status"] = "interrupted"

    _update_batches(_refresh)


def batch_progress(batch: dict) -> dict:
    recount(batch)
    total = batch.get("total") or 0
    done = batch.get("done") or 0
    return {"total": total, "done": done, "failed": batch.get("failed") or 0,
            "pct": round(100 * done / total) if total else 0}


def prepare_restart(batch: dict) -> None:
    """Chuẩn bị chạy tiếp 1 đợt đã dừng/lỗi/gián đoạn: tập nào chưa xong
    (pending/writing/failed/cancelled) về lại pending để viết lại; tập
    đã xong giữ nguyên (không viết lại)."""
    for t in batch.get("topics", []):
        for s in t.get("scripts", []):
            if s.get("status") != "done":
                s["status"] = "pending"
                s["error"] = ""
    batch["status"] = "running"
    batch["paused"] = False
    batch["cancelled"] = False
    recount(batch)


# ---------------------------------------------------------------- runner

def run_batch(batch: dict, deps: dict) -> dict:
    """Chạy 1 đợt (có thể chạy tiếp đợt cũ: tập đã xong được bỏ qua).

    deps gồm các callable:
    - account_names: list[str] (acc sẵn sàng, theo thứ tự ưu tiên)
    - get_client(name) -> (client | None, err: str)
    - research(topic, client) -> str (không raise; lỗi -> "")
    - write(topic, client, target_scenes, research, timeout) -> Script
      (có .title, .scenes với .duration)
    - save_script(script, out_path, topic) -> Path
    - slugify(topic) -> str
    - notify(text) -> bool (không raise)
    - is_connection_error(exc) -> bool
    - log(msg) -> None
    - scripts_dir: Path (thư mục scripts/)

    Song song: tối đa min(số acc, số kịch bản) task cùng lúc, mỗi task
    giữ lock của đúng 1 acc trong suốt lần gọi gateway của nó (1 acc =
    1 kịch bản trọn vẹn, không xé nhỏ). Acc chết thì task chuyển sang
    acc khác (failover); acc hỏng báo 1 lần/đợt.
    """
    log = deps["log"]
    notify = deps["notify"]
    is_conn = deps["is_connection_error"]
    names: list[str] = list(deps.get("account_names") or [])
    if batch.get("account") and batch["account"] in names:
        names = [batch["account"]] + [n for n in names if n != batch["account"]]
    target_scenes = batch["minutes"] * 6 if batch.get("minutes") else None
    timeout = 180.0
    if target_scenes:
        timeout = max(180.0, target_scenes * 10.0)
    locks = {name: threading.Lock() for name in names}
    broken: set[str] = set()
    reported_conn: set[str] = set()

    def warn_connection(name: str, err: str) -> None:
        """Báo acc hỏng 1 lần/đợt (log + Telegram nếu có)."""
        with _STATE_LOCK:
            if name in reported_conn:
                return
            reported_conn.add(name)
            broken.add(name)
        msg = (f"⚠️ Acc '{name}' KHÔNG KẾT NỐI ĐƯỢC: {err} — "
               f"đợt {batch['id']} đang thử acc khác.")
        log(msg)
        notify(msg)

    def refresh_flags() -> None:
        """Đồng bộ cờ paused/cancelled từ đĩa vào batch đang chạy."""
        for b in load_batches():
            if b.get("id") == batch["id"]:
                batch["paused"] = b.get("paused", False)
                batch["cancelled"] = b.get("cancelled", False)
                return

    def persist() -> None:
        """Ghi batch đang chạy ra đĩa, KHÔNG đè mất cờ paused/cancelled
        mà UI vừa bấm (đĩa thắng cho 2 cờ đó)."""
        def _merge(bs):
            for i, b in enumerate(bs):
                if b.get("id") == batch["id"]:
                    paused = b.get("paused", False)
                    cancelled = b.get("cancelled", False)
                    bs[i] = batch
                    batch["paused"] = paused
                    batch["cancelled"] = cancelled
                    break
            else:
                bs.append(batch)

        _update_batches(_merge)

    def wait_for_pause_or_cancel() -> bool:
        """True nếu đã dừng hẳn. Tạm dừng thì chờ (poll 2s) đến khi
        tiếp tục hoặc dừng hẳn. Không giữ lock nào khi chờ."""
        while True:
            refresh_flags()
            if batch.get("cancelled"):
                return True
            if not batch.get("paused"):
                return False
            time.sleep(2.0)

    def run_with_account(fn):
        """Chạy fn(client) với 1 acc bất kỳ đang rảnh, failover sang acc
        khác khi acc lỗi (lỗi kết nối hoặc lỗi khác đều thử acc tiếp
        theo, giống bản tuần tự). Trả kết quả fn, hoặc None nếu dừng hẳn
        / hết acc thử được. fn chỉ chạy khi đang giữ lock của acc đó."""
        attempted: set[str] = set()
        while True:
            if wait_for_pause_or_cancel():
                return None
            name = None
            for n in names:
                if n in broken or n in attempted:
                    continue
                if locks[n].acquire(blocking=False):
                    name = n
                    break
            if name is None:
                if all(n in broken or n in attempted for n in names):
                    return None
                time.sleep(0.5)
                continue
            try:
                client, err = deps["get_client"](name)
                if client is None:
                    attempted.add(name)
                    if err and is_conn(RuntimeError(err)):
                        warn_connection(name, err)
                    continue
                try:
                    return fn(client)
                except Exception as e:  # noqa: BLE001 - thử acc tiếp theo
                    attempted.add(name)
                    if is_conn(e):
                        warn_connection(name, str(e))
                    continue
            finally:
                locks[name].release()

    def do_research(topic_name: str) -> str:
        """Nghiên cứu 1 chủ đề (không raise; lỗi -> '')."""
        if wait_for_pause_or_cancel():
            return ""
        try:
            result = run_with_account(
                lambda client: deps["research"](topic_name, client) or "")
        except Exception:  # noqa: BLE001 - nghiên cứu lỗi vẫn viết tiếp
            return ""
        return result if isinstance(result, str) else ""

    def save_unique(script, topic_name: str, index: int) -> str:
        """Chọn tên file duy nhất rồi lưu (khóa để 2 task slug trùng
        nhau không ghi đè nhau). Trả out_path dạng scripts/..."""
        base = f"{deps['slugify'](topic_name)}-tap{index:02d}"
        with _SAVE_LOCK:
            candidate = base
            k = 2
            while (deps["scripts_dir"] / f"{candidate}.yaml").exists():
                candidate = f"{base}-{k}"
                k += 1
            out_path = f"scripts/{candidate}.yaml"
            deps["save_script"](script, out_path, topic_name)
        return out_path

    def do_episode(topic_name: str, research_future, entry: dict) -> None:
        # Tập đã xong từ lần chạy trước (file còn) thì bỏ qua.
        if entry.get("status") == "done" and entry.get("file"):
            stem = Path(entry["file"]).name
            if (deps["scripts_dir"] / stem).exists():
                return
            entry["status"] = "pending"
        try:
            research_text = research_future.result()
            if not isinstance(research_text, str):
                research_text = ""
        except Exception:  # noqa: BLE001 - nghiên cứu lỗi vẫn viết tiếp
            research_text = ""
        if wait_for_pause_or_cancel():
            entry["status"] = "cancelled"
            persist()
            return
        entry["status"] = "writing"
        persist()

        def _write(client):
            return deps["write"](topic_name, client, target_scenes,
                                 research_text, timeout)

        script = run_with_account(_write)
        if script is None:
            refresh_flags()
            if batch.get("cancelled"):
                entry["status"] = "cancelled"
            else:
                entry["status"] = "failed"
                entry["error"] = "không viết được kịch bản (đã thử mọi acc)"
            log(f"❌ [{topic_name}] tập {entry['index']}: {entry['error']}")
            recount(batch)
            persist()
            return
        try:
            out_path = save_unique(script, topic_name, entry["index"])
        except Exception as e:  # noqa: BLE001 - lưu lỗi cũng tính failed
            entry["status"] = "failed"
            entry["error"] = f"không lưu được kịch bản: {e}"
            log(f"❌ [{topic_name}] tập {entry['index']}: {entry['error']}")
            recount(batch)
            persist()
            return
        entry["file"] = out_path
        entry["title"] = script.title
        entry["scenes"] = len(script.scenes)
        entry["status"] = "done"
        entry["error"] = ""
        recount(batch)
        persist()
        log(f"✅ [{topic_name}] tập {entry['index']}: {script.title} "
            f"({len(script.scenes)} cảnh)")
        notify(f"✅ Đã hoàn thành xong: {topic_name} + {script.title} "
               f"({batch['done']}/{batch['total']} kịch bản)")

    if not names:
        batch["status"] = "failed"
        log("Đợt không có tài khoản Muse nào để chạy.")
        persist()
        return batch
    episodes = [(t["name"], entry)
                for t in batch["topics"] for entry in t["scripts"]]
    if not episodes:
        batch["status"] = "done"
        persist()
        return batch
    max_workers = max(1, min(len(names), len(episodes)))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        # Nghiên cứu trước: MỌI research task đứng trước mọi episode task
        # trong hàng đợi FIFO (nếu không, research của chủ đề B có thể
        # phải chờ viết xong cả kịch bản của chủ đề A). Khóa theo chỉ số
        # chủ đề (không theo tên — 2 chủ đề có thể trùng tên).
        research_futures = [ex.submit(do_research, t["name"])
                            for t in batch["topics"]]
        # Viết kịch bản song song: mỗi task = 1 kịch bản trọn vẹn, 1 acc.
        # Task episode chặn ở research_future của đúng chủ đề nó (không
        # giữ lock khi chờ nên không deadlock: research task luôn đứng
        # trước episode task trong hàng đợi FIFO).
        futures = []
        for ti, t in enumerate(batch["topics"]):
            for entry in t["scripts"]:
                futures.append(ex.submit(do_episode, t["name"],
                                         research_futures[ti], entry))
        for f in futures:
            try:
                f.result()
            except Exception:  # noqa: BLE001 - task tự xử lý lỗi rồi
                pass
    recount(batch)
    refresh_flags()
    if batch.get("cancelled"):
        batch["status"] = "cancelled"
    elif batch["failed"] and batch["done"]:
        batch["status"] = "partial"
    elif batch["failed"]:
        batch["status"] = "failed"
    else:
        batch["status"] = "done"
    persist()
    log(f"Đợt {batch['id']} xong: {batch['done']}/{batch['total']} kịch bản, "
        f"{batch['failed']} lỗi.")
    return batch
