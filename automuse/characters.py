"""Nhân vật đồng nhất (character consistency): bảng characters + khóa mô tả.

Cách hoạt động: mỗi kịch bản có thể khai báo bảng ``characters`` ở cấp
script — mỗi nhân vật một mô tả cố định bằng tiếng Anh (và đường dẫn ảnh
tham chiếu tùy chọn). Trước khi sinh clip, mô tả đó được khóa vào mọi
cảnh có nhân vật đó, theo đúng một cách viết duy nhất, để nhân vật giống
nhau qua các cảnh:

    Featuring Bình (young Vietnamese man, short black hair, ...).

Ảnh tham chiếu (``image``) hiện CHƯA đính kèm được qua gateway
(chat.stream chưa hỗ trợ file_ref — một agent khác đang nghiên cứu);
mô tả khóa là lớp đồng nhất đang chạy được, ảnh chỉ để dành cho bước
đính kèm sau này.
"""

from __future__ import annotations

from pathlib import Path

from .script import Script

# Thư mục mặc định để ảnh nhân vật (tương đối so với repo).
DEFAULT_CHARACTERS_DIR = Path("assets") / "characters"


def character_entry(characters: dict | None, name: str) -> dict:
    """Entry của nhân vật trong bảng characters ({} nếu không có)."""
    entry = (characters or {}).get(name or "")
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str) and entry.strip():
        return {"description": entry.strip()}
    return {}


def character_description(characters: dict | None, name: str) -> str:
    """Mô tả cố định (tiếng Anh) của nhân vật, "" nếu không có."""
    return str(character_entry(characters, name).get("description") or "").strip()


def lock_fragment(characters: dict | None, name: str) -> str:
    """Câu khóa mô tả cho một nhân vật, "" nếu tên trống."""
    name = (name or "").strip()
    if not name:
        return ""
    desc = character_description(characters, name)
    if desc:
        return f"Featuring {name} ({desc})."
    return f"Featuring {name}."


def apply_character_lock(script: Script) -> Script:
    """Khóa mô tả nhân vật vào visual của mọi cảnh có nhân vật trong
    bảng characters.

    Chỉ ghi thêm khi visual chưa có mô tả đó (idempotent: gọi lần hai
    không ghi trùng). Cảnh có nhân vật chưa khai báo trong bảng thì để
    nguyên visual. Trả về chính script (sửa tại chỗ).
    """
    for scene in script.scenes:
        name = (scene.character or "").strip()
        if not name or name not in (script.characters or {}):
            continue  # chỉ khóa nhân vật có trong bảng characters
        desc = character_description(script.characters, name)
        visual = scene.visual or ""
        if desc and desc in visual:
            continue  # đã khóa rồi (mô tả đã nằm trong visual)
        if f"Featuring {name}" in visual:
            continue  # đã có câu Featuring cho nhân vật này rồi
        fragment = lock_fragment(script.characters, name)
        if not fragment:
            continue
        scene.visual = f"{visual.strip()} {fragment}".strip() if visual.strip() else fragment
    return script


def resolve_image_path(image: str, script_dir: Path | None = None) -> Path:
    """Đường dẫn ảnh tham chiếu: tuyệt đối giữ nguyên, tương đối tính
    từ thư mục chứa kịch bản (mặc định: thư mục làm việc hiện tại)."""
    p = Path(str(image or "").strip()).expanduser()
    if p.is_absolute() or script_dir is None:
        return p
    return Path(script_dir) / p


def character_warnings(script: Script, script_dir: Path | None = None) -> list[str]:
    """Cảnh báo (không lỗi): nhân vật chưa có trong bảng, ảnh không thấy
    trên đĩa. Trả về danh sách chuỗi (rỗng = không có gì)."""
    warnings: list[str] = []
    for scene in script.scenes:
        name = (scene.character or "").strip()
        if name and name not in (script.characters or {}):
            warnings.append(
                f"{scene.id}: nhân vật '{name}' chưa có trong bảng "
                "characters — mô tả sẽ không được khóa, nhân vật có thể "
                "không giống nhau qua các cảnh"
            )
    for name, entry in (script.characters or {}).items():
        image = entry.get("image") if isinstance(entry, dict) else ""
        if image and not resolve_image_path(image, script_dir).exists():
            warnings.append(
                f"Nhân vật '{name}': không thấy file ảnh tham chiếu "
                f"'{image}' (file không tồn tại — cảnh đó sẽ sinh bằng mô tả chữ, không có ảnh đính kèm)"
            )
    return warnings
