# 🎬 AutoMuse Multi — Video dài multi-account + lồng tiếng ElevenLabs

Tạo video dài theo kịch bản (phim truyện, thế giới động vật, tài liệu...) bằng
cách sinh từng cảnh bằng Muse AI rồi dựng thành phim hoàn chỉnh, lồng tiếng
bằng ElevenLabs theo đúng lời thoại trong kịch bản.

Lấy cảm hứng từ [saitama9x/automuse-ai-video-generator](https://github.com/saitama9x/automuse-ai-video-generator)
(dùng cookie session muse.ai để batch video), nhưng viết lại thành repo Python
chạy CLI, thêm:

- 🔑 **Login nhiều acc Muse bằng cookies** — chuỗi cookie thô, file
  `cookies.txt` (Netscape) hoặc JSON export từ trình duyệt.
- 🌐 **Proxy riêng từng acc** — `http/https/socks5`, có lệnh kiểm tra proxy
  và kiểm tra cookie còn sống không.
- 🎞️ **Video dài theo kịch bản** — mỗi cảnh là 1 clip Muse, tự nối thành
  phim 1 phút, 10 phút hay dài hơn; có template **phim truyện** và
  **thế giới động vật**.
- 🎙️ **Lồng tiếng theo kịch bản** — ElevenLabs đọc lời thoại từng cảnh,
  nhiều giọng cho nhiều nhân vật, tự căn đúng mốc thời gian, kèm phụ đề SRT,
  nhạc nền, fade-out / iris-out cuối phim.
- 👄 **Khớp môi hậu kỳ (Sync Labs)** — cảnh thoại cận mặt có `lipsync: true`
  được vẽ lại miệng khớp với giọng đọc (`build --lipsync`); sync lỗi thì
  giữ clip gốc, không làm hỏng build.
- 🔁 **Chia tải qua các acc** — round-robin, acc lỗi tự đổi acc khác,
  cooldown sau 3 lỗi liên tiếp, `--resume` chạy tiếp từ chỗ dừng.

## Yêu cầu

- Python 3.10+, `pip install -r requirements.txt`
- `ffmpeg` + `ffprobe` (cài sẵn trong PATH)
- Cookie session muse.ai (xem bên dưới) — chỉ cần khi chạy thật
- API key ElevenLabs (xem bên dưới) — chỉ cần khi lồng tiếng thật

## Cài đặt

```bash
git clone <repo-cua-ban> automuse-multi
cd automuse-multi
pip install -r requirements.txt
cp config/settings.example.yaml config/settings.yaml
# Key ElevenLabs nhập sau bằng: python cli.py voices set-key
```

## 1. Thêm tài khoản Muse (cookie + proxy)

**Lấy cookie:** đăng nhập [muse.ai](https://muse.ai) trên trình duyệt →
F12 → Network → copy giá trị header `Cookie`. Hoặc dùng extension
"Get cookies.txt" / "Cookie-Editor" để export file.

```bash
# Thêm acc bằng chuỗi cookie + proxy riêng
python cli.py accounts add --name acc1 \
  --cookie "sessionid=abc123; csrftoken=xyz" \
  --proxy http://user:pass@proxy1.example.com:8080

# Hoặc bằng file cookies.txt export từ trình duyệt
python cli.py accounts add --name acc2 --cookie-file config/cookies-acc2.txt \
  --proxy socks5://user:pass@proxy2.example.com:1080

# Kiểm tra proxy + cookie còn sống không (+ token/quota + mức dùng tuần)
python cli.py accounts check
python cli.py accounts check --name acc1
python cli.py accounts list

# Xem mức dùng tuần này so với giới hạn (tuần lịch, thứ Hai 00:00 -> thứ Hai sau)
python cli.py accounts usage
python cli.py accounts usage --name acc1

# Đặt giới hạn tuần riêng cho một acc (0 = dùng giới hạn chung)
python cli.py accounts set-limit --name acc1 --videos 30 --minutes 180
```

Cookie và proxy lưu ở `config/accounts.json` (quyền 0600, đã gitignore —
không commit file này). Có thể sửa tay theo mẫu
`config/accounts.example.json`.

> ⚠️ Chỉ dùng tài khoản của chính bạn, chạy với tốc độ vừa phải
> (`delay_between_jobs` trong settings.yaml). Việc tự động hóa bằng cookie
> có thể vi phạm điều khoản của Muse — bạn tự chịu trách nhiệm với acc của mình.

### Check token/quota + giới hạn tuần

- **Check token/quota:** `accounts check` thử đọc số dư qua các endpoint
  `/api/user/usage`, `/api/credits`, `/api/quota`… rồi báo `OK` (kèm số dư),
  `LỖI` (cookie hết hạn) hoặc `KHÔNG RÕ` (API Muse không công khai nên không
  đọc được — lúc này giới hạn tuần cục bộ là cơ chế chính). Danh sách endpoint
  thử nằm ở `muse.quota_check_paths` trong settings.yaml.
- **Giới hạn tuần:** mỗi clip sinh thành công ghi một sự kiện vào
  `cache/usage.json`. Mặc định mỗi acc tối đa **20 video / 120 phút mỗi tuần**
  (tuần lịch, thứ Hai 00:00 → thứ Hai tuần sau). Hết giới hạn thì pipeline tự
  đổi sang acc khác còn quota; hết tất cả thì dừng build với thông báo rõ ràng.
  Đổi số chung trong `limits` ở `config/settings.yaml`, hoặc đặt riêng từng acc
  bằng `accounts set-limit` / `--weekly-max-videos` / `--weekly-max-minutes`
  khi `accounts add`. Mock build (`--mock`) không tính vào giới hạn.
  Xem mức dùng bằng `accounts usage`; Web Studio hiện cột **Tuần này** ở tab
  Tài khoản Muse.

## 2. Lồng tiếng ElevenLabs

Lấy key tại [elevenlabs.io](https://elevenlabs.io) rồi nhập bằng lệnh
(key được kiểm tra qua API rồi mới lưu vào `.env`, không cần sửa file tay):

```bash
python cli.py voices set-key   # nhập key (ẩn khi gõ), kiểm tra rồi lưu
python cli.py voices           # liệt kê giọng (lấy voice_id)
python cli.py voices test Rachel   # nghe thử: tạo output/voice-test/Rachel.mp3
python cli.py voices set-default Rachel  # đặt giọng mặc định (lưu settings.yaml)
```

**Setup giọng chi tiết** — chọn giọng bằng tên hoặc `voice_id` thẳng từ
ElevenLabs (giọng share từ Voice Library, giọng clone — không cần có trong
danh sách acc), chỉnh tốc độ và các tham số giọng:

```bash
python cli.py voices add 21m00Tcm4TlvDq8ikWAM  # kiểm tra 1 voice_id có tồn tại không
python cli.py voices set-default 21m00Tcm4TlvDq8ikWAM  # dùng giọng đó làm mặc định
python cli.py voices set-speed 1.2      # tốc độ đọc 0.5–2.0 (1.0 = bình thường)
python cli.py voices set-stability 0.7  # 0–1: càng cao giọng càng đều, rõ
python cli.py voices set-similarity 0.8 # 0–1: càng cao càng giống giọng gốc
python cli.py voices set-style 0.3      # 0–1: độ biểu cảm (model v2/v3)
python cli.py voices set-model eleven_v3
python cli.py voices show               # xem toàn bộ cài đặt hiện tại
```

Tốc độ được đổi bằng ffmpeg `atempo` sau khi tải audio nên áp dụng cho mọi
giọng/model; độ dài cảnh tự co theo đúng tốc độ đã chọn (đo từ file thật).
Các giá trị trên cũng lưu trong `config/settings.yaml` mục `elevenlabs`
(`default_voice`, `speed`, `stability`, `similarity_boost`, `style`,
`model`, `use_speaker_boost`).

Web Studio cũng có mục **Lồng tiếng** ở trang Tổng quan: nhập key, chọn
giọng, đặt mặc định, nghe thử ngay trên trình duyệt, nhập `voice_id`
custom và chỉnh tốc độ/ổn định/giống giọng/biểu cảm/model.

Trong kịch bản, bảng `voices:` map tên giọng (vd: `narrator`, `An`, `Bình`)
sang `voice_id`. Có thể dùng tên giọng thay cho ID — tool tự resolve.
Bảng `characters:` khóa mô tả nhân vật (tiếng Anh, cố định) vào mọi cảnh
có nhân vật đó để nhân vật giống nhau qua các cảnh — xem `characters show
--script <file>`; ảnh tham chiếu (`image`) được đính kèm inline base64
trong cùng tin nhắn chat.stream (ảnh trước, text sau), kèm câu dặn dùng
ảnh làm mẫu nhân vật.

## 2b. Tự viết prompt từ kịch bản

Chỉ cần paste kịch bản tiếng Việt, Muse sẽ viết prompt tạo video tiếng Anh
cho từng cảnh (đúng nhân vật, góc máy, phong cách theo thể loại):

```bash
python cli.py prompts --script scripts/dong-vat.yaml
cat kich-ban.txt | python cli.py prompts --kind documentary
python cli.py prompts --text "Cảnh 1 — Bình minh thảo nguyên
Bình minh lên trên thảo nguyên châu Phi, bầy sư tử trở về sau chuyến săn đêm."
```

Thêm `--account acc1` để nhờ Muse viết (tin nhắn sẽ hiện trong chat chính
của acc đó, như lúc tạo video); không có `--account` thì dùng mẫu có sẵn
(từ khóa chính vẫn đúng, câu văn chưa mượt bằng Muse viết). Thêm `--apply`
để ghi prompt vào `visual` và lưu file `<tên>.prompts.yaml`, rồi build luôn:

```bash
python cli.py prompts --script scripts/dong-vat.yaml --account acc1 --apply
python cli.py build --script scripts/dong-vat.prompts.yaml
```

## 2c. Khớp môi hậu kỳ — Sync Labs (lip-sync)

Muse sinh clip từ prompt chữ, không nhận file audio, nên môi nhân vật
không tự khớp với giọng ElevenLabs. Bước này chạy sau khi sinh clip:
cảnh nào có `lipsync: true` sẽ được đưa clip + file audio lời thoại qua
Sync Labs để vẽ lại miệng khớp từng âm.

```bash
python cli.py lipsync set-key   # nhập key Sync Labs (kiểm tra rồi mới lưu vào .env)
python cli.py build --script scripts/phim.yaml --lipsync
```

Trong kịch bản, chỉ đánh dấu cảnh thoại cận mặt (phim truyện):

```yaml
  - id: s02
    title: "Câu nói đầu tiên"
    narration: "Cô trú mưa ở đây à? Tôi có hai chiếc ô, cô lấy một chiếc đi."
    voice: Bình
    lipsync: true   # chỉ cảnh này được khớp môi
```

- **Model:** chỉnh trong `config/settings.yaml` mục `lipsync.model` —
  `lipsync-2` (mặc định, nhanh/rẻ), `lipsync-2-pro`, `sync-3` (tốt nhất,
  4K), `react-1` (biểu cảm, clip tối đa 15 giây). `sync_mode` mặc định
  là `cut_off` (cắt audio dài hơn video ở cuối clip).
- **An toàn:** cảnh nào sync lỗi thì giữ nguyên clip gốc, build vẫn chạy
  tiếp — lip-sync không bao giờ làm hỏng build. Trạng thái từng cảnh ghi
  trong `manifest.json` (`scenes[].lipsync`: `ok` / `skipped` / `failed`).
- **Chạy lẻ 1 cặp clip + audio** (không cần build cả phim):
  `python cli.py lipsync sync --video clips/s02.mp4 --audio audio/s02.mp3 --out clips/s02-lipsync.mp4`
  (thêm `--model sync-3`, `--mock` để chạy thử không cần key).
- **Chi phí:** Sync Labs tính phí theo giây ngoài gói tháng của họ — xem
  bảng giá tại sync.so/pricing. Web Studio cũng có ô nhập key Sync Labs
  ở trang Tổng quan và tick **Khớp môi (lip-sync)** ở tab Tạo video.
- Chưa có key mà build với `--lipsync` (không phải mock) thì dừng ngay
  với hướng dẫn chạy `lipsync set-key`.

## 2d. Tìm chủ đề + tự tạo kịch bản

Chưa có ý tưởng thì gợi ý chủ đề theo hướng, rồi nhờ Muse viết kịch bản
hoàn chỉnh từ chủ đề — xong chạy tiếp `prompts` rồi `build` như bình thường:

```bash
# Gợi ý 10 chủ đề phim tài liệu (thêm --keyword để lọc/gợi ý theo từ khóa)
python cli.py topics --kind documentary
python cli.py topics --kind documentary --keyword "gấu" --count 5

# Nhờ Muse viết kịch bản 10 cảnh từ chủ đề (tin nhắn sẽ hiện trong chat
# chính của acc đó, như lúc tạo video); không có --account thì lưu khung
# mẫu để tự điền lời dẫn
python cli.py write-script --topic "Gấu Bắc Cực" --kind documentary --scenes 10 --account acc1
python cli.py write-script --topic "Gấu Bắc Cực" --kind documentary --scenes 10
python cli.py prompts --script scripts/gau-bac-cuc.yaml --account acc1 --apply
python cli.py build --script scripts/gau-bac-cuc.prompts.yaml
```

Luật viết kịch bản theo từng hướng đã nhúng sẵn: động vật thì mỗi cảnh
1 cơ chế + 1 con số, mở đầu tương phản + số liệu, kết bằng câu luận điểm;
phim truyện thì có bảng nhân vật; hoạt hình thì câm + sfx từng cảnh; ASMR
thì không lời dẫn, chỉ có sfx từng bước nhẹ nhàng.

## 3. Viết kịch bản

Tạo từ template:

```bash
python cli.py init --kind documentary --out scripts/dong-vat.yaml
python cli.py init --kind movie --out scripts/phim.yaml
```

Hoặc xem ví dụ có sẵn: `examples/the-gioi-dong-vat.yaml`,
`examples/phim-truyen.yaml`.

**Định dạng YAML** (đủ nhất):

```yaml
title: "Thế giới động vật — Vua của thảo nguyên"
kind: documentary
aspect: "16:9"          # 16:9 hoặc 9:16
default_voice: narrator
voices:
  narrator: "21m00Tcm4TlvDq8ikWAM"
music: "assets/nhac-nen.mp3"   # tùy chọn
scenes:
  - id: s01
    title: "Bình minh thảo nguyên"
    duration: 8
    visual: "Cinematic aerial shot of African savanna at golden dawn, ..."
    narration: "Bình minh lên trên thảo nguyên châu Phi..."
    voice: narrator
```

**Định dạng Markdown** cũng được (dễ viết hơn):

```markdown
# Vua của thảo nguyên
kind: documentary
aspect: 16:9

## Cảnh 1 — Bình minh (8s)
Visual: Cinematic aerial shot of African savanna at golden dawn...
Lời thoại: Bình minh lên trên thảo nguyên châu Phi...
Giọng: narrator
```

Lưu ý:

- `visual` là prompt **tiếng Anh** đưa cho Muse sinh clip.
- `narration` là lời thoại **tiếng Việt** đưa cho ElevenLabs đọc.
- Nếu lời thoại dài hơn `duration`, clip tự kéo dài bằng cách đóng băng
  khung hình cuối để lời đọc không bị cắt.

## 4. Chạy

```bash
# Chạy thử toàn bộ pipeline, không cần acc/key (clip mẫu + tiếng im lặng)
python cli.py build --script examples/the-gioi-dong-vat.yaml --mock
```

### Web Studio (giao diện quản lý kiểu automuse.site)

Không thích gõ lệnh thì mở giao diện web:

```bash
python cli.py web            # mở http://127.0.0.1:8080
python cli.py web --port 8080
```

Trên web có đủ: **Tổng quan** (số acc, số video, việc cần làm) →
**Tài khoản Muse** (thêm/xóa/kiểm tra cookie + proxy) →
**Kịch bản** (xem/sửa/lưu kịch bản, mẫu phim truyện + tài liệu) →
**Tạo video** (chọn kịch bản, tick acc dùng, chạy queue, xem log trực tiếp) →
**Kết quả** (xem trước video, tải `final.mp4` và phụ đề `.srt`).

Server chỉ nghe trên máy của bạn (`127.0.0.1`) nên cookie và key không
rời khỏi máy. Muốn mở từ máy khác trong mạng LAN thì chạy
`python cli.py web --host 0.0.0.0` (chỉ dùng trong mạng tin cậy).

### Chạy bằng CLI

```bash
# Chạy thật (dùng các acc đã thêm + ElevenLabs key trong .env)
python cli.py build --script scripts/dong-vat.yaml --out output/dong-vat-tap1

# Chỉ dùng acc1 và acc2, chạy tiếp từ chỗ dừng
python cli.py build --script scripts/phim.yaml --accounts acc1,acc2 --resume

# Chỉ sinh clip (không lồng tiếng) / chỉ lồng tiếng
python cli.py generate --script scripts/phim.yaml
python cli.py dub --script scripts/phim.yaml

# Tắt iris-out cuối phim, hoặc tắt hẳn lồng tiếng
python cli.py build --script scripts/phim.yaml --no-iris
python cli.py build --script scripts/phim.yaml --no-tts
```

Kết quả trong `output/<ten-kich-ban>/`:

```
final.mp4        # phim hoàn chỉnh (video + lời thoại + nhạc nền + phụ đề)
subtitles.srt    # phụ đề theo từng cảnh
video_silent.mp4 # bản video chưa có tiếng
clips/s01.mp4 ...# clip từng cảnh
audio/           # file TTS từng cảnh + narration.wav
manifest.json    # cảnh nào do acc nào sinh, độ dài, thời gian tạo
```

## Cấu trúc repo

```
automuse-multi/
├── cli.py                  # CLI chính
├── automuse/
│   ├── accounts.py         # multi-account: cookie + proxy + round-robin
│   ├── muse_client.py      # client Muse (thật + mock)
│   ├── script.py           # đọc kịch bản YAML/Markdown
│   ├── elevenlabs.py       # TTS ElevenLabs (+ mock)
│   ├── lipsync.py          # khớp môi hậu kỳ Sync Labs (+ mock)
│   ├── audio.py            # timeline lời thoại, nhạc nền, SRT
│   ├── video.py            # ffmpeg: chuẩn hóa, nối, iris-out
│   ├── pipeline.py         # pipeline build full
│   └── config.py           # settings.yaml + .env
├── config/
│   ├── settings.example.yaml
│   └── accounts.example.json
├── templates/              # mẫu kịch bản movie / documentary
├── examples/               # 2 kịch bản ví dụ chạy được ngay
└── tests/
```

## Tùy chỉnh API Muse

API web của Muse không công khai. Các endpoint mặc định nằm trong
`config/settings.yaml` (`muse.base_url`, `generate_video_path`,
`job_status_path`...). Nếu Muse đổi API, bạn chỉ cần mở DevTools trên
muse.ai → tab Network → bấm Generate → copy đúng path/method rồi sửa
`settings.yaml` là chạy tiếp, không cần đụng code.

Chế độ `--mock` luôn chạy được để kiểm tra pipeline mà không cần acc thật.

## Troubleshooting

| Lỗi | Cách xử lý |
|---|---|
| `Cookie hết hạn (401/403)` | Lấy lại cookie mới, `accounts add` lại (hoặc sửa `config/accounts.json`) |
| `Bị rate-limit (429)` | Để acc nghỉ — tool tự cooldown sau 3 lỗi; giảm `delay_between_jobs`? Không — **tăng** delay lên |
| Proxy lỗi | `python cli.py accounts check --name <tên>` để xem proxy hay cookie lỗi |
| `ffmpeg: command not found` | Cài ffmpeg và thêm vào PATH |
| Lời thoại dài hơn clip | Bình thường — pipeline tự kéo dài clip bằng khung hình cuối |
| ElevenLabs 401 | Kiểm tra `ELEVENLABS_API_KEY` trong `.env` |

## License

MIT — dùng cho mục đích cá nhân, tự chịu trách nhiệm với tài khoản và
nội dung bạn tạo ra.
