"""Đọc kịch bản video dài: YAML hoặc Markdown tiếng Việt.

Mỗi kịch bản gồm nhiều cảnh (scene). Mỗi cảnh có:
  - visual: mô tả hình ảnh (prompt tiếng Anh đưa cho Muse sinh video)
  - narration: lời thoại / lời dẫn (đưa cho ElevenLabs đọc)
  - voice: tên giọng trong bảng voices (vd: narrator, An, Bình)
  - duration: độ dài clip mong muốn (giây)
  - character: nhân vật xuất hiện (phim truyện)
  - sfx: hiệu ứng âm thanh gợi ý

Định dạng Markdown (dễ viết nhất cho người Việt):

    # Tên phim
    kind: documentary
    aspect: 16:9

    ## Cảnh 1 — Bình minh trên thảo nguyên (8s)
    Visual: Golden savanna at dawn, a lion pride walking...
    Lời thoại: Bình minh lên trên thảo nguyên...
    Giọng: narrator
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Scene:
    id: str
    title: str = ""
    duration: int = 5
    visual: str = ""
    narration: str = ""
    voice: str = ""
    character: str = ""
    sfx: str = ""
    reference_image: str = ""
    # Khớp môi hậu kỳ (Sync Labs): chỉ dùng cho cảnh thoại cận mặt trong
    # phim truyện; build phải có --lipsync thì cờ này mới có tác dụng.
    lipsync: bool = False

    @property
    def tts_text(self) -> str:
        return self.narration.strip()


@dataclass
class Script:
    title: str = "Untitled"
    kind: str = "movie"  # movie | documentary | animation | asmr
    aspect: str = "16:9"
    fps: int = 30
    default_voice: str = "narrator"
    voices: dict[str, str] = field(default_factory=dict)  # tên -> voice_id ElevenLabs
    # Bảng nhân vật đồng nhất: tên -> {"description": mô tả cố định
    # (tiếng Anh), "image": đường dẫn ảnh tham chiếu (tùy chọn)}.
    # Mô tả được khóa vào mọi cảnh có nhân vật đó (xem automuse/characters.py).
    characters: dict[str, dict] = field(default_factory=dict)
    music: str = ""  # đường dẫn nhạc nền (tùy chọn)
    scenes: list[Scene] = field(default_factory=list)

    @property
    def total_duration(self) -> int:
        return sum(s.duration for s in self.scenes)


def load_script(path: str | Path) -> Script:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Không thấy file kịch bản: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        return _from_yaml(text)
    if p.suffix.lower() == ".md":
        return _from_markdown(text)
    raise ValueError("Kịch bản phải là .yaml/.yml hoặc .md")


def _from_yaml(text: str) -> Script:
    data = yaml.safe_load(text) or {}
    scenes = []
    for i, s in enumerate(data.get("scenes", [])):
        kwargs = _scene_kwargs(s)
        kwargs.setdefault("id", f"s{i+1:02d}")
        scenes.append(Scene(**kwargs))
    return Script(
        title=data.get("title", "Untitled"),
        kind=data.get("kind", "movie"),
        aspect=str(data.get("aspect", "16:9")),
        fps=int(data.get("fps", 30)),
        default_voice=data.get("default_voice", "narrator"),
        voices=data.get("voices", {}) or {},
        characters=_normalize_characters(data.get("characters", {}) or {}),
        music=data.get("music", "") or "",
        scenes=scenes,
    )


def _normalize_characters(raw: dict) -> dict[str, dict]:
    """Chuẩn hóa bảng characters: entry chuỗi -> {"description": chuỗi},
    entry không phải dict/chuỗi -> bỏ qua."""
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        if isinstance(entry, dict):
            out[str(name)] = {
                str(k): v for k, v in entry.items()
                if k in ("description", "image")
            }
        elif isinstance(entry, str) and entry.strip():
            out[str(name)] = {"description": entry.strip()}
    return out


def _scene_kwargs(raw: dict) -> dict:
    allowed = {"id", "title", "duration", "visual", "narration", "voice",
               "character", "sfx", "reference_image", "lipsync"}
    out = {k: v for k, v in raw.items() if k in allowed}
    if "duration" in out:
        out["duration"] = int(out["duration"])
    if "lipsync" in out:
        # YAML đã parse bool sẵn; Markdown đưa chuỗi "true"/"false".
        value = out["lipsync"]
        out["lipsync"] = (
            value if isinstance(value, bool)
            else str(value).strip().lower() in ("true", "1", "yes", "có")
        )
    return out


# ---------------------------------------------------------------- markdown

_KEY_ALIASES = {
    "visual": "visual", "hình ảnh": "visual", "hinh anh": "visual",
    "prompt": "visual",
    "lời thoại": "narration", "loi thoai": "narration", "narration": "narration",
    "lời dẫn": "narration", "loi dan": "narration", "thoại": "narration",
    "giọng": "voice", "giong": "voice", "voice": "voice",
    "nhân vật": "character", "nhan vat": "character", "character": "character",
    "sfx": "sfx", "âm thanh": "sfx", "am thanh": "sfx",
    "thời lượng": "duration", "thoi luong": "duration", "duration": "duration",
    "ảnh mẫu": "reference_image", "anh mau": "reference_image",
    "reference": "reference_image",
    "lipsync": "lipsync", "khớp môi": "lipsync", "khop moi": "lipsync",
}

_META_KEYS = {
    "kind": "kind", "thể loại": "kind", "the loai": "kind",
    "aspect": "aspect", "tỉ lệ": "aspect", "ti le": "aspect",
    "fps": "fps",
    "voice": "default_voice", "giọng mặc định": "default_voice",
    "music": "music", "nhạc nền": "music", "nhac nen": "music",
}

_DURATION_RE = re.compile(r"\((\d+)\s*s(?:ec)?\)", re.IGNORECASE)


def _from_markdown(text: str) -> Script:
    lines = text.splitlines()
    title = "Untitled"
    meta: dict[str, str] = {}
    voices: dict[str, str] = {}
    characters: dict[str, dict] = {}
    scenes_raw: list[dict] = []
    current: dict | None = None
    current_key: str | None = None

    def flush():
        nonlocal current, current_key
        if current is not None:
            scenes_raw.append(current)
        current, current_key = None, None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# ") and title == "Untitled":
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            dur = 5
            m = _DURATION_RE.search(heading)
            if m:
                dur = int(m.group(1))
                heading = _DURATION_RE.sub("", heading).strip(" —-")
            current = {"title": heading, "duration": dur}
            current_key = None
            continue
        if line.startswith("### "):
            continue
        m = re.match(r"^([^:]{1,30}):\s*(.*)$", line)
        if m:
            raw_key = m.group(1).strip()
            key = raw_key.lower()
            value = m.group(2).strip()
            if current is None:
                if key in _META_KEYS:
                    meta[_META_KEYS[key]] = value
                elif key.startswith("giọng ") or key.startswith("voice "):
                    name = key.split(None, 1)[1]
                    voices[name] = value
                elif key.startswith("nhân vật ") or key.startswith("character "):
                    # Giữ nguyên chữ hoa/thường của tên (khác voices).
                    # Tiền tố "Nhân vật"/"Character" có 2 từ nên cắt theo
                    # độ dài tiền tố, không split() từ đầu tiên.
                    prefix = "nhân vật " if key.startswith("nhân vật ") else "character "
                    name = raw_key[len(prefix):].strip()
                    characters[name] = {"description": value}
                elif key in ("voices",):
                    pass
                continue
            field_name = _KEY_ALIASES.get(key)
            if field_name:
                if field_name == "duration":
                    try:
                        current["duration"] = int(re.search(r"\d+", value).group())
                    except (AttributeError, ValueError):
                        pass
                else:
                    current[field_name] = value
                current_key = field_name
            else:
                current_key = None
            continue
        # Dòng tiếp nối (lời thoại nhiều dòng)
        if current is not None and current_key in ("narration", "visual"):
            current[current_key] = (current.get(current_key, "") + " " + line).strip()

    flush()

    scenes = []
    for i, raw in enumerate(scenes_raw):
        raw.setdefault("id", f"s{i+1:02d}")
        scenes.append(Scene(**_scene_kwargs(raw)))
    return Script(
        title=title,
        kind=meta.get("kind", "movie"),
        aspect=meta.get("aspect", "16:9"),
        fps=int(meta.get("fps", 30)),
        default_voice=meta.get("default_voice", "narrator"),
        voices=voices,
        characters=_normalize_characters(characters),
        music=meta.get("music", ""),
        scenes=scenes,
    )


def validate(script: Script) -> list[str]:
    """Trả về danh sách lỗi (rỗng = hợp lệ)."""
    errors = []
    if not script.scenes:
        errors.append("Kịch bản không có cảnh nào")
    for s in script.scenes:
        if not s.visual:
            errors.append(f"{s.id}: thiếu Visual (prompt sinh video)")
        if s.duration <= 0:
            errors.append(f"{s.id}: duration phải > 0")
    return errors
