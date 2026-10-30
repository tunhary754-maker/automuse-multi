"""Tìm kiếm chủ đề + tự tạo kịch bản (automuse/topics).

Hai tính năng mới cho repo, dùng chung pattern với automuse/prompting.py
(đường Muse khi có client, fallback xác định khi không có client / gọi
Muse thất bại / trả về không parse được — không bao giờ crash):

* ``suggest_topics(direction, keyword, count, client)`` — gợi ý chủ đề
  video cho một hướng (documentary/movie/animation/asmr). Có client thì
  nhờ Muse brainstorm qua gateway chat; không có client hoặc Muse trả về
  lỗi thì rơi về danh sách gợi ý tĩnh theo hướng.
* ``write_script(topic, kind, scene_count, scene_duration, client)`` —
  viết kịch bản HOÀN CHỈNH từ một chủ đề. Có client thì nhờ Muse viết
  YAML đúng format của automuse/script.py; không có client hoặc YAML lỗi
  thì trả về khung xương template (``strict=True`` thì raise thay vì
  fallback).

Lưu ý về session: tin nhắn gửi qua gateway chat xuất hiện trong đoạn chat
chính của tài khoản (giống mọi lệnh ``chat.stream`` khác) — đây không phải
API headless. Chỉ gọi Muse khi người dùng đã đồng ý.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import yaml

from .prompting import ask_muse_text
from .script import Scene, Script, _from_yaml, validate

__all__ = [
    "TopicIdea",
    "TOPIC_IDEA_INSTRUCTION",
    "SCRIPT_WRITER_INSTRUCTION",
    "suggest_topics",
    "write_script",
    "parse_topic_reply",
]

_KINDS = ("documentary", "movie", "animation", "asmr")


@dataclass
class TopicIdea:
    """Một chủ đề video: tiêu đề tiếng Việt + một câu hook/tóm tắt."""

    title: str
    summary: str = ""


# ---------------------------------------------------------------------------
# Instruction gửi cho Muse (dùng .replace(), KHÔNG dùng .format() vì mẫu
# chứa ngoặc {} của JSON/YAML).
# ---------------------------------------------------------------------------

_TOPIC_FLAVOR = {
    "documentary": (
        "wildlife/nature documentary: each topic should carry one striking "
        "number or a sharp contrast (hot vs cold, big vs small, drought vs "
        "flood)"
    ),
    "movie": (
        "everyday Vietnamese drama: a relatable everyday situation with one "
        "twist or secret that changes it"
    ),
    "animation": (
        "silent slapstick animation: the Urban Pests-style trio (a chubby "
        "sewer rat, a hyperactive cockroach, a goofy cross-eyed pigeon) or "
        "a new cute cast, with exaggerated cartoon physics and no dialogue"
    ),
    "asmr": (
        "cute ASMR animation: one cute cat (or cute animal) running one "
        "relaxing service, one soft sound per step (brushing, pouring "
        "water, gentle tapping), no dialogue"
    ),
}

TOPIC_IDEA_INSTRUCTION = (
    "You brainstorm video topics for an AI video studio. "
    "The direction is {direction} ({flavor}). "
    "Keyword/filter (optional, empty means any topic): {keyword}. "
    "Suggest {count} distinct, filmable topics. Each topic needs a "
    "Vietnamese title and one hook line (summary) that makes a viewer "
    "want to watch. "
    "Reply with ONLY a JSON array, one object per topic in order, like: "
    '[{"title": "Tieu de tieng Viet", "summary": "Mot cau hook..."}]. '
    "No markdown fences, no extra commentary."
)

_SCRIPT_RULES_BY_KIND = {
    "documentary": (
        "- Documentary rules: one survival mechanism + one species + one "
        "number per scene. Open with a contrast + numbers hook, end with "
        "a thesis line that reverses intuition. Narration in Vietnamese, "
        "visual in English."
    ),
    "movie": (
        "- Movie rules: everyday Vietnamese drama. Add a characters: map "
        "with 1-2 characters (name -> description in English, e.g. An: "
        "young Vietnamese woman, shoulder-length black hair, yellow "
        "raincoat, red scarf). Give each speaking scene a character: name. "
        "Narration carries the story; keep spoken lines short."
    ),
    "animation": (
        "- Animation rules: silent slapstick, NO narration (narration: \"\" "
        "for every scene). Give each scene an sfx: line describing the "
        "cartoon sound (boing, splat, squeak). Exaggerated cartoon "
        "physics, no dialogue, no on-screen text."
    ),
    "asmr": (
        "- ASMR rules: NO narration (narration: \"\" for every scene). One "
        "cute cat running one relaxing service, one gentle step + one "
        "soft sound per scene; give each scene an sfx: line (gentle "
        "brushing, pouring warm water, soft tapping). Pastel colors, "
        "cozy and relaxing, no dialogue."
    ),
}

SCRIPT_WRITER_INSTRUCTION = (
    "You write a COMPLETE video script in YAML for an AI video studio. "
    "Topic: {topic}. Kind: {kind}. Number of scenes: {scene_count}. "
    "Scene duration: {scene_duration} seconds each. "
    "Reply with raw YAML only (optionally inside one ```yaml fence), in "
    "exactly this format:\n"
    "title: <Vietnamese title>\n"
    "kind: {kind}\n"
    "aspect: \"16:9\"\n"
    "fps: 30\n"
    "default_voice: narrator\n"
    "scenes:\n"
    "  - id: s01\n"
    "    title: <scene title>\n"
    "    duration: {scene_duration}\n"
    "    visual: <English visual description, one shot, concrete action>\n"
    "    narration: <Vietnamese narration>\n"
    "Rules:\n"
    "- Narration at most ~20 words per scene so it fits a 10-second frame.\n"
    "{kind_rules}\n"
    "No markdown commentary outside the YAML."
)


# Chế độ tự quyết (kịch bản dài): Muse tự chọn số cảnh và thời lượng từng
# cảnh theo câu chuyện, thay vì nhận số cố định. Dùng khi gọi write_script
# với scene_count=None (nút "Tạo tập mới tự động" trên Web Studio).
# {scene_guidance} = mục tiêu số cảnh (vd: "roughly 15 to 30 scenes" hoặc
# "roughly 48 scenes" khi người dùng nhập số phút). {research_block} = dữ
# liệu nghiên cứu chèn vào (rỗng nếu không có).
SCRIPT_WRITER_INSTRUCTION_AUTO = (
    "You write a COMPLETE, LONG video script in YAML for an AI video studio. "
    "Topic: {topic}. Kind: {kind}. "
    "Decide YOURSELF how many scenes the story needs to be complete and long: "
    "{scene_guidance}, one shot per scene, covering the topic "
    "from hook to thesis without rushing. "
    "Decide YOURSELF each scene's duration in seconds: usually 10, shorter or "
    "longer only where that beat genuinely needs it. "
    "{research_block}"
    "Reply with raw YAML only (optionally inside one ```yaml fence), in "
    "exactly this format:\n"
    "title: <Vietnamese title>\n"
    "kind: {kind}\n"
    "aspect: \"16:9\"\n"
    "fps: 30\n"
    "default_voice: narrator\n"
    "scenes:\n"
    "  - id: s01\n"
    "    title: <scene title>\n"
    "    duration: <seconds you choose for this scene>\n"
    "    visual: <English visual description, one shot, concrete action>\n"
    "    narration: <Vietnamese narration>\n"
    "Rules:\n"
    "- Narration must fit its scene's duration: about 20 words per 10 seconds, "
    "scaled up or down with the duration you choose.\n"
    "{kind_rules}\n"
    "No markdown commentary outside the YAML."
)


# Nghiên cứu trước khi viết kịch bản dài: kịch bản càng dài càng cần nhiều
# dữ liệu thật (cơ chế, số liệu, vòng đời), không thì Muse sẽ lặp ý hoặc
# viết chung chung. Hỏi Muse tổng hợp fact sheet rồi đưa vào instruction
# viết kịch bản (xem research_topic).
TOPIC_RESEARCH_INSTRUCTION = (
    "Research this wildlife documentary topic and return a structured fact "
    "sheet in plain text (no YAML, no markdown commentary outside the facts). "
    "Topic: {topic}. "
    "Cover, with concrete numbers wherever they exist: key survival "
    "mechanisms; body size, weight, speed, depth, distance, temperature; "
    "life cycle stages (birth, growth, migration, reproduction, death); "
    "diet and hunting; predators and threats; habitat and range; social "
    "behavior; one or two surprising or counterintuitive facts; "
    "conservation status if notable. "
    "Prefer specific numbers over general statements. If a number is "
    "uncertain, say so briefly instead of inventing precision."
)


def research_topic(topic: str, client, timeout: float = 180.0) -> str:
    """Nhờ Muse nghiên cứu chủ đề, trả về fact sheet (chuỗi rỗng nếu lỗi).

    Không bao giờ raise: nghiên cứu thất bại thì kịch bản vẫn viết được
    (ít dữ liệu hơn), chỉ ghi log cảnh báo qua print để server log thấy.
    """
    try:
        text = ask_muse_text(
            client,
            TOPIC_RESEARCH_INSTRUCTION.replace("{topic}", topic),
            timeout=timeout,
        )
        return (text or "").strip()
    except Exception as exc:  # noqa: BLE001 - nghiên cứu là bước tùy chọn
        print(f"Cảnh báo: nghiên cứu chủ đề '{topic}' thất bại: {exc}")
        return ""


def _normalize_kind(kind: str) -> str:
    """Hướng không hợp lệ -> documentary (hướng mặc định của repo)."""
    return kind if kind in _KINDS else "documentary"


# ---------------------------------------------------------------------------
# Danh sách gợi ý tĩnh (fallback khi không có client / Muse lỗi)
# ---------------------------------------------------------------------------

_FALLBACK_TOPICS: dict[str, list[TopicIdea]] = {
    "documentary": [
        TopicIdea("Vua của thảo nguyên",
                  "Sư tử cái săn mồi giữa mùa khô 40°C, cả đàn nhịn ăn nhiều ngày chờ một cú vồ."),
        TopicIdea("Kẻ sống sót trên băng",
                  "Gấu Bắc Cực bơi 100 cây số giữa biển băng, gấu con chỉ nặng 600 gam khi chào đời."),
        TopicIdea("Dưới lớp tuyết là một thế giới",
                  "Chuột lemmut sinh con giữa mùa đông −30°C, dưới tuyết ấm hơn mặt đất 30 độ."),
        TopicIdea("Thợ săn của đêm",
                  "Cú mèo tuyết nghe thấy chuột đồng dưới tuyết dày 30 phân trong bóng tối hoàn toàn."),
        TopicIdea("Hành trình của cá hồi",
                  "Cá hồi bơi ngược dòng 3.000 cây số để về nơi sinh ra, vượt thác cao 3 mét."),
        TopicIdea("Rừng mưa sau cơn bão",
                  "Ếch phi tiêu độc hồi sinh khu rừng sau bão, mỗi ngày ăn 50 con kiến."),
        TopicIdea("Sa mạc nở hoa",
                  "Sau 10 năm khô hạn, một cơn mưa khiến sa mạc nở hoa chỉ trong 7 ngày."),
        TopicIdea("Kẻ khổng lồ hiền lành",
                  "Voi châu Phi uống 200 lít nước mỗi ngày và nhớ đường về nguồn nước suốt 30 năm."),
    ],
    "movie": [
        TopicIdea("Quán nhỏ cuối phố",
                  "Cô chủ quán phở bán đến 2 giờ sáng, vị khách quen 11 giờ đêm nào cũng gọi đúng một tô."),
        TopicIdea("Chuyến xe cuối cùng",
                  "Nữ tài xế xe buýt đêm và hành khách duy nhất đi chuyến cuối suốt một tháng."),
        TopicIdea("Bức thư không gửi",
                  "Ông lão viết thư cho vợ đã mất mỗi tuần, rồi một ngày nhận được thư trả lời."),
        TopicIdea("Cây đàn của bố",
                  "Cô gái trẻ tìm thấy cây đàn cũ của bố trong gác xép và quyết định học lại từ đầu."),
        TopicIdea("Người giao hàng lúc nửa đêm",
                  "Anh shipper giao đơn cuối cùng trong đêm mưa và phát hiện địa chỉ đó là nhà mình ngày xưa."),
        TopicIdea("Tiệm sửa đồng hồ",
                  "Thợ sửa đồng hồ già nhận sửa chiếc đồng hồ ngừng chạy đúng ngày con trai ông ra đi."),
        TopicIdea("Bánh mì sáng chủ nhật",
                  "Hai chị em tranh nhau lò bánh của mẹ, rồi phát hiện công thức bánh giấu trong cuốn sổ cũ."),
        TopicIdea("Ghế đá công viên",
                  "Mỗi chiều, một bà cụ ngồi đúng chiếc ghế đá đó để đợi người bạn đã hứa 40 năm trước."),
    ],
    "animation": [
        TopicIdea("Chuột Cống và miếng phô mai",
                  "Chuột Cống, Gián Tăng Động và Bồ Câu Ngáo Ngơ tranh nhau một miếng phô mai trong hẻm tối."),
        TopicIdea("Gián Tăng Động đi chợ",
                  "Gián Tăng Động chạy khắp chợ đêm để gom đồ ăn, phá tung mọi sạp hàng trên đường."),
        TopicIdea("Bồ Câu Ngáo Ngơ học bay",
                  "Bồ Câu mắt lé tập bay từ mái nhà xuống, lần nào cũng đáp nhầm chỗ."),
        TopicIdea("Mưa trong hẻm",
                  "Ba nhân vật trú mưa dưới một chiếc lá, giành nhau chỗ khô ráo nhất."),
        TopicIdea("Chiếc bánh sinh nhật",
                  "Chuột Cống lén mang bánh sinh nhật về hang, Gián và Bồ Câu bám theo gây náo loạn."),
        TopicIdea("Đêm hội đom đóm",
                  "Bồ Câu đuổi theo đàn đom đóm, Chuột và Gián dùng ánh sáng đó để tìm phô mai."),
        TopicIdea("Chiếc nệm bay",
                  "Ba nhân vật ngủ trên nệm cũ ngoài hẻm, gió thổi bay cả nệm qua thành phố."),
        TopicIdea("Củ khoai tây khổng lồ",
                  "Chuột Cống đào được củ khoai to hơn cả người, cả ba hợp sức lăn nó về hang."),
    ],
    "asmr": [
        TopicIdea("Tiệm spa của mèo Mimi",
                  "Mèo Mimi chải lông, rót nước ấm và xoa bóp cho khách mèo trong tiệm spa pastel."),
        TopicIdea("Tiệm cắt tóc mèo",
                  "Mèo thợ cắt tỉa lông nhẹ nhàng, tiếng kéo cắt và lược chải đều đều gây buồn ngủ."),
        TopicIdea("Tiệm bánh cá của mèo",
                  "Mèo làm bánh cá nướng, tiếng bột nhào, nhân chảy và giấy gói sột soạt."),
        TopicIdea("Tiệm trà chiều của mèo",
                  "Mèo rót trà ấm, tiếng nước chảy và tách chạm nhau trong buổi chiều yên tĩnh."),
        TopicIdea("Tiệm làm móng mèo",
                  "Mèo dũa móng nhẹ nhàng, tiếng dũa và đệm lót êm ái cho khách mèo buồn ngủ."),
        TopicIdea("Tiệm giặt là mèo",
                  "Mèo giặt khăn ấm, tiếng nước xà phòng và bàn là hơi nước phì phò."),
        TopicIdea("Tiệm kem mèo",
                  "Mèo múc kem mát lạnh, tiếng muỗng cạo và ốc quế giòn tan trong ngày hè."),
        TopicIdea("Đêm đông của mèo Mimi",
                  "Mèo Mimi pha ca cao nóng, đắp chăn và nghe tiếng lửa tí tách bên lò sưởi."),
    ],
}


def _fallback_topics(direction: str, keyword: str, count: int) -> list[TopicIdea]:
    """Danh sách tĩnh theo hướng, lọc theo keyword nếu có."""
    ideas = list(_FALLBACK_TOPICS[_normalize_kind(direction)])
    kw = (keyword or "").strip().lower()
    if kw:
        filtered = [t for t in ideas
                    if kw in t.title.lower() or kw in t.summary.lower()]
        # Keyword lọc sạch hết -> trả toàn bộ danh sách hướng đó.
        if filtered:
            ideas = filtered
    return ideas[:max(1, count)]


# ---------------------------------------------------------------------------
# Parse câu trả lời gợi ý chủ đề của Muse
# ---------------------------------------------------------------------------

_JUNK_TITLES = {"...", "…", "-", "–", "—", "ok", "oke", "done", "xong"}

def _split_title_summary(text: str) -> tuple[str, str]:
    """Tách "Title — summary" / "Title - summary" / "Title: summary".

    Không có dấu tách -> toàn bộ là title, summary rỗng.
    """
    for sep in (" — ", " - ", ": "):
        if sep in text:
            title, summary = text.split(sep, 1)
            return title.strip(" #*-"), summary.strip()
    return text.strip(" #*-"), ""


def _meaningful(title: str, summary: str) -> bool:
    """Bỏ mục rỗng / vô nghĩa (chỉ dấu "...", "ok", 1 ký tự...)."""
    t = title.strip()
    if not t or t.lower() in _JUNK_TITLES:
        return False
    if len(t) < 2 and not summary.strip():
        return False
    return True


def parse_topic_reply(text: str) -> list[TopicIdea]:
    """Parse câu trả lời gợi ý chủ đề của Muse thành list TopicIdea.

    Chấp nhận, theo thứ tự: JSON array (có thể trong ```json fence, hoặc
    lẫn trong text), dòng đánh số ("1. Title — summary"), rồi đoạn tách
    bằng dòng trống. Không parse được gì -> ValueError.
    """
    if not text or not text.strip():
        raise ValueError("Câu trả lời của Muse rỗng")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    # 1) JSON array (lẫn trong text vẫn bắt được nhờ regex)
    m = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
        except ValueError:
            data = None
        if isinstance(data, list) and data:
            out: list[TopicIdea] = []
            for item in data:
                if isinstance(item, str) and item.strip():
                    title, summary = _split_title_summary(item.strip())
                elif isinstance(item, dict):
                    title = str(item.get("title") or item.get("name")
                                or item.get("topic") or "").strip()
                    summary = str(item.get("summary") or item.get("description")
                                  or item.get("hook") or "").strip()
                else:
                    continue
                if _meaningful(title, summary):
                    out.append(TopicIdea(title=title, summary=summary))
            if out:
                return out

    # 2) Dòng đánh số: "1. Title — summary" / "Chủ đề 1: Title"
    numbered: list[TopicIdea] = []
    current_title = ""
    current_parts: list[str] = []
    saw_heading = False

    def flush_numbered():
        # current_title đã tách khỏi summary ngay khi gặp dòng tiêu đề;
        # current_parts = summary trên dòng tiêu đề + các dòng tiếp nối.
        nonlocal current_title, current_parts
        if current_title:
            body = " ".join(p for p in current_parts if p).strip()
            numbered.append(TopicIdea(title=current_title, summary=body))
        elif current_parts:
            # Tiêu đề trống nhưng có dòng tiếp nối: dòng đầu làm title.
            title, summary = _split_title_summary(current_parts[0])
            rest = " ".join(current_parts[1:]).strip()
            summary = f"{summary} {rest}".strip() if summary else rest
            numbered.append(TopicIdea(title=title, summary=summary))
        current_title, current_parts = "", []

    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(
            r"^(?:chủ đề|chu de|topic|đề tài)\s*\d+\s*[-:.—)]\s*(.*)$"
            r"|^\d+\s*[.)]\s+(.*)$",
            line, flags=re.IGNORECASE,
        )
        if m:
            saw_heading = True
            flush_numbered()
            rest = (m.group(1) or m.group(2) or "").strip()
            title, summary = _split_title_summary(rest)
            current_title = title
            if summary:
                current_parts.append(summary)
            continue
        current_parts.append(line)
    flush_numbered()
    ideas = [t for t in numbered if _meaningful(t.title, t.summary)]
    if saw_heading and ideas:
        return ideas

    # 3) Đoạn tách bằng dòng trống: dòng đầu là title, còn lại là summary.
    blocks = [b.strip() for b in re.split(r"\n\s*\n", cleaned) if b.strip()]
    if len(blocks) > 1:
        out = []
        for block in blocks:
            lines = block.splitlines()
            title, summary = _split_title_summary(lines[0].strip())
            rest = " ".join(ln.strip() for ln in lines[1:] if ln.strip())
            summary = f"{summary} {rest}".strip() if summary else rest
            if _meaningful(title, summary):
                out.append(TopicIdea(title=title, summary=summary))
        if out:
            return out
    if blocks:
        title, summary = _split_title_summary(blocks[0].strip())
        if _meaningful(title, summary):
            return [TopicIdea(title=title, summary=summary)]
    raise ValueError("Không parse được chủ đề nào từ câu trả lời của Muse")


# ---------------------------------------------------------------------------
# Gợi ý chủ đề
# ---------------------------------------------------------------------------

def suggest_topics(direction: str, keyword: str = "", count: int = 10,
                   client=None, timeout: float = 180.0) -> list[TopicIdea]:
    """Gợi ý chủ đề video cho một hướng.

    Có ``client`` (GatewayClient) thì nhờ Muse brainstorm; không có client
    hoặc gọi Muse thất bại / trả về không parse được thì rơi về danh sách
    tĩnh theo hướng (lọc theo keyword nếu có). Không bao giờ crash.
    """
    direction = _normalize_kind(direction)
    if client is not None:
        instruction = (
            TOPIC_IDEA_INSTRUCTION
            .replace("{direction}", direction)
            .replace("{flavor}", _TOPIC_FLAVOR[direction])
            .replace("{keyword}", keyword.strip() or "bất kỳ")
            .replace("{count}", str(max(1, count)))
        )
        try:
            reply = ask_muse_text(client, instruction, timeout=timeout)
            ideas = parse_topic_reply(reply)
            if ideas:
                return ideas[:max(1, count)]
        except Exception:
            pass  # rơi về danh sách tĩnh bên dưới
    return _fallback_topics(direction, keyword, count)


# ---------------------------------------------------------------------------
# Chủ đề theo xu hướng tìm kiếm thật (cho sản xuất hàng loạt)
# ---------------------------------------------------------------------------
# Khác với suggest_topics (brainstorm tự do), đây là phân tích nhu cầu:
# Muse phải dựa vào tín hiệu nhu cầu khán giả đang tìm/xem (câu hỏi được
# tìm nhiều, dạng video triệu view, chủ đề đang hot theo mùa, loài vật
# đang được nhắc nhiều), xếp hạng theo độ hút, và nêu rõ lý do từng chủ
# đề — không bịa chủ đề vu vơ. Lưu ý trung thực: đây là phân tích từ kiến
# thức của Muse về hành vi khán giả, không phải số liệu live từ Google
# Trends (gateway không có feed đó); UI phải ghi rõ nguồn khi trend_based
# là True, và ghi rõ "list mẫu" khi rơi về fallback.
TREND_RESEARCH_INSTRUCTION = (
    "You are a YouTube wildlife-documentary trend analyst. Do NOT invent "
    "random topics. Analyze what audiences are ACTUALLY searching for and "
    "watching right now in wildlife / animal documentary content: "
    "frequently searched animal questions, million-view video formats, "
    "animals and stories trending this season, species that keep appearing "
    "in viral nature clips, and evergreen topics with durable search "
    "demand. Rank {count} topics by audience demand, strongest first. "
    "Each topic needs a Vietnamese title and a summary that states the "
    "DEMAND SIGNAL (why people search/watch this: the question they ask, "
    "the viral format it matches, or the seasonal reason), not just a "
    "description of the animal. "
    "Reply with ONLY a JSON array, one object per topic in order, like: "
    '[{"title": "Tieu de tieng Viet", "summary": "Ly do nhu cau: ..."}]. '
    "No markdown fences, no extra commentary."
)


def suggest_trending_topics(count: int, client=None,
                            timeout: float = 240.0) -> tuple[list[TopicIdea], bool]:
    """Gợi ý chủ đề theo xu hướng tìm kiếm, kèm cờ trend_based.

    Có ``client`` thì nhờ Muse phân tích xu hướng rồi parse; thành công
    thì trả (ideas, True). Không có client, Muse lỗi, hoặc parse không
    được gì thì rơi về suggest_topics tĩnh và trả (ideas, False) để UI
    ghi rõ đây là list mẫu chứ không phải xu hướng. Không bao giờ crash.
    """
    count = max(1, min(20, int(count or 1)))
    if client is not None:
        instruction = TREND_RESEARCH_INSTRUCTION.replace("{count}", str(count))
        try:
            reply = ask_muse_text(client, instruction, timeout=timeout)
            ideas = parse_topic_reply(reply)
            if ideas:
                return ideas[:count], True
        except Exception:
            pass  # rơi về list tĩnh bên dưới
    return suggest_topics("documentary", count=count), False


# ---------------------------------------------------------------------------
# Viết kịch bản hoàn chỉnh từ chủ đề
# ---------------------------------------------------------------------------

def _extract_yaml(text: str) -> str:
    """Lấy khối YAML từ reply của Muse (fence ```yaml hoặc toàn bộ)."""
    t = text.strip()
    m = re.search(r"```(?:yaml|yml)?\s*\n(.*?)```", t,
                  flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    t = re.sub(r"^```(?:yaml|yml)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _parse_script_yaml(text: str) -> tuple[str, dict]:
    """Parse YAML kịch bản từ reply của Muse.

    Chấp nhận fence ```yaml và commentary trước YAML (cắt từ dòng key cấp
    cao đầu tiên). Trả về (yaml_text_dùng_để_parse, data_dict).
    """
    base = _extract_yaml(text)
    candidates = [base]
    lines = base.splitlines()
    for i, line in enumerate(lines):
        if i > 0 and re.match(r"^(title|kind|scenes)\s*:", line):
            candidates.append("\n".join(lines[i:]))
    for cand in candidates:
        if not cand.strip():
            continue
        try:
            data = yaml.safe_load(cand)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("scenes"), list) \
                and data["scenes"]:
            return cand, data
    # Kèm đầu reply của Muse để chẩn đoán (YAML lỗi, reply bị cắt, hay
    # Muse trả văn xuôi) — không có nó thì lỗi parse không gỡ được.
    snippet = (text or "")[:300].replace("\n", " ")
    raise ValueError(
        "Không parse được YAML kịch bản từ câu trả lời của Muse "
        f"(reply dài {len(text or '')} ký tự, đầu reply: {snippet!r})"
    )


def _skeleton_script(topic: str, kind: str, scene_count: int,
                     scene_duration: int) -> Script:
    """Khung xương template khi không có client / Muse lỗi (luôn validate)."""
    n = max(1, int(scene_count))
    dur = int(scene_duration) if scene_duration and scene_duration > 0 else 10
    scenes: list[Scene] = []
    for i in range(n):
        narration = ""
        if kind in ("movie", "documentary"):
            # Placeholder có dấu [] để người dùng biết cần viết lại.
            narration = f"[Viết lời dẫn cho cảnh {i + 1} về {topic}.]"
        scenes.append(Scene(
            id=f"s{i + 1:02d}",
            title=f"Cảnh {i + 1}",
            duration=dur,
            visual=(f"Wide shot illustrating {topic}, scene {i + 1}, "
                    f"cinematic lighting, 16:9 aspect ratio."),
            narration=narration,
            voice="narrator",
        ))
    script = Script(
        title=topic,
        kind=kind,
        aspect="16:9",
        fps=30,
        default_voice="narrator",
        scenes=scenes,
    )
    errors = validate(script)
    if errors:
        # Không bao giờ trả về Script lỗi ở chế độ không strict.
        raise ValueError(f"Khung xương kịch bản không hợp lệ: {errors}")
    return script


def write_script(topic: str, kind: str = "documentary", scene_count: int | None = 10,
                 scene_duration: int = 10, client=None,
                 timeout: float = 180.0, strict: bool = False,
                 target_scenes: int | None = None,
                 research: str = "") -> Script:
    """Viết kịch bản HOÀN CHỈNH từ một chủ đề.

    Có ``client`` thì nhờ Muse viết YAML đúng format của automuse/script.py;
    không có client hoặc gọi Muse thất bại / YAML lỗi / script không hợp
    lệ thì ``strict=True`` raise ValueError, còn không thì trả về khung
    xương template (luôn validate).

    ``scene_count=None`` là chế độ tự quyết (kịch bản dài): Muse tự chọn
    số cảnh và thời lượng từng cảnh theo câu chuyện, thay vì nhận số cố
    định. ``target_scenes`` đặt mục tiêu số cảnh cho chế độ tự quyết (vd:
    người dùng nhập 8 phút -> ~48 cảnh); không đặt thì mục tiêu mặc định
    khoảng 15–30 cảnh. ``research`` là fact sheet từ research_topic, chèn
    vào instruction để kịch bản dài có đủ dữ liệu thật.
    """
    kind = _normalize_kind(kind)
    dur = int(scene_duration) if scene_duration and scene_duration > 0 else 10
    auto = scene_count is None
    if client is not None:
        if auto:
            if target_scenes and int(target_scenes) > 0:
                guidance = f"aim for roughly {int(target_scenes)} scenes"
            else:
                guidance = "aim for roughly 15 to 30 scenes"
            research_block = ""
            if (research or "").strip():
                research_block = (
                    "Use this researched source material for your facts and "
                    "numbers; do not contradict it, and do not pad with "
                    "generic filler when the material runs out — cover more "
                    "of the material instead:\n"
                    f"{research.strip()}\n"
                )
            instruction = (
                SCRIPT_WRITER_INSTRUCTION_AUTO
                .replace("{topic}", topic)
                .replace("{kind}", kind)
                .replace("{scene_guidance}", guidance)
                .replace("{research_block}", research_block)
                .replace("{kind_rules}", _SCRIPT_RULES_BY_KIND[kind])
            )
        else:
            instruction = (
                SCRIPT_WRITER_INSTRUCTION
                .replace("{topic}", topic)
                .replace("{kind}", kind)
                .replace("{scene_count}", str(max(1, int(scene_count))))
                .replace("{scene_duration}", str(dur))
                .replace("{kind_rules}", _SCRIPT_RULES_BY_KIND[kind])
            )
        try:
            reply = ask_muse_text(client, instruction, timeout=timeout)
            yaml_text, data = _parse_script_yaml(reply)
            script = _from_yaml(yaml_text)
            # Fix-up: id s01.. theo thứ tự, kind đúng yêu cầu, duration
            # mặc định khi Muse không ghi duration cho cảnh đó.
            raw_scenes = data.get("scenes", [])
            script.kind = kind
            if not script.title or script.title == "Untitled":
                script.title = topic
            for i, scene in enumerate(script.scenes):
                scene.id = f"s{i + 1:02d}"
                raw = raw_scenes[i] if i < len(raw_scenes) \
                    and isinstance(raw_scenes[i], dict) else {}
                if not raw.get("duration"):
                    scene.duration = dur
            errors = validate(script)
            if errors:
                raise ValueError(f"Kịch bản Muse viết không hợp lệ: {errors}")
            return script
        except Exception as exc:
            if strict:
                raise ValueError(
                    f"Không viết được kịch bản từ Muse: {exc}") from exc
    if strict:
        raise ValueError(
            "Không có client Muse để viết kịch bản (strict=True, "
            "không dùng khung xương template)")
    # Chế độ tự quyết mà không có client: khung xương dài mặc định 20 cảnh.
    return _skeleton_script(topic, kind, 20 if auto else scene_count, dur)


# ---------------------------------------------------------------------------
# Rà soát & sửa kịch bản (người dùng đọc thấy cần bổ sung / phần chưa tốt)
# ---------------------------------------------------------------------------
# Muse đọc lại toàn bộ kịch bản, sửa phần chưa tốt theo đúng luật phim
# tài liệu, đồng thời áp dụng hướng dẫn bổ sung của người dùng (vd: thêm
# chi tiết vào cảnh 3). Nguyên tắc SÁT BẢN GỐC: chỉ sửa tối thiểu những
# chỗ yếu, giữ nguyên cảnh tốt, không đổi chủ đề/cốt truyện/số cảnh trừ
# khi hướng dẫn của người dùng yêu cầu. Trả về YAML sửa + list thay đổi
# để người dùng xem rồi mới bấm Lưu (server KHÔNG tự lưu đè).
SCRIPT_REVIEW_INSTRUCTION = (
    "You are an expert wildlife-documentary script editor. Review the "
    "script below and return an IMPROVED version plus a change list. "
    "Check these documentary rules: scene 1 opens with a contrast + "
    "numbers hook; every scene has one survival mechanism + one number; "
    "narration fits its duration (about 20 words per 10 seconds); every "
    "visual is one concrete, filmable shot in English; the ending is a "
    "thesis line that reverses intuition; no duplicated scenes, numbers, "
    "or lines. "
    "User's extra instruction (may be empty, apply it FIRST, e.g. add a "
    "detail to a scene): {user_instruction}. "
    "IMPORTANT — stay faithful to the original script: make the MINIMAL "
    "changes needed. Fix only weak scenes and apply the user's "
    "instruction; keep good scenes unchanged. Do not change the topic, "
    "the story arc, or the number of scenes unless the user's instruction "
    "explicitly asks for it. Do not invent a different script. "
    "Reply in exactly this format:\n"
    "```yaml\n"
    "<the full improved script YAML, same format as the input>\n"
    "```\n"
    "CHANGES:\n"
    "- s03: <what changed and why>\n"
    "- s07: <what changed and why>\n"
    "(one line per changed scene; if nothing needed changing, write "
    "'- none: script already good' and return the original YAML unchanged.)\n"
    "Script to review:\n"
    "```yaml\n"
    "{script_yaml}\n"
    "```\n"
)


def _parse_changes(reply: str) -> list[str]:
    """Lấy list thay đổi sau dòng 'CHANGES:' (mỗi dòng bắt đầu '- ')."""
    out = []
    in_changes = False
    for line in reply.splitlines():
        s = line.strip()
        if s.upper().startswith("CHANGES:"):
            in_changes = True
            continue
        if in_changes:
            if s.startswith("- ") and len(s) > 2:
                out.append(s[2:].strip())
            elif s.startswith("```"):
                break
    return out


def review_script(yaml_text: str, client, user_instruction: str = "",
                  timeout: float = 240.0) -> tuple[str, list[str], Script]:
    """Muse đọc lại kịch bản, sửa phần chưa tốt, giữ sát bản gốc.

    Trả (fixed_yaml_text, changes, fixed_script). Không có client ->
    ValueError (rà soát bắt buộc cần Muse, không có fallback template).
    YAML sửa xong không parse được hoặc validate lỗi -> ValueError
    (editor giữ nguyên bản cũ, không lưu đè).
    """
    if client is None:
        raise ValueError("Cần tài khoản Muse để rà soát kịch bản")
    instruction = (SCRIPT_REVIEW_INSTRUCTION
                   .replace("{user_instruction}",
                            (user_instruction or "").strip() or "(none)")
                   .replace("{script_yaml}", yaml_text.strip()))
    try:
        reply = ask_muse_text(client, instruction, timeout=timeout)
    except Exception as e:
        raise ValueError(f"Không rà soát được kịch bản: {e}") from e
    try:
        yaml_part, _ = _parse_script_yaml(reply)
        script = _from_yaml(yaml_part)
    except Exception as e:
        raise ValueError(
            f"Muse trả về bản sửa không đọc được, giữ nguyên bản cũ: {e}"
        ) from e
    errors = validate(script)
    if errors:
        raise ValueError(
            f"Kịch bản sau khi sửa không hợp lệ, giữ nguyên bản cũ: {errors}")
    return yaml_part, _parse_changes(reply), script
