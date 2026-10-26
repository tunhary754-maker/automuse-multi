# Ba hướng làm video — định nghĩa chuẩn

Mọi video tạo bằng automuse-multi thuộc đúng một trong ba hướng dưới đây.
Mỗi hướng có phong cách hình ảnh, cấu trúc kịch bản, lời thoại và prompt
riêng, không lẫn nhau. File này là chuẩn để đối chiếu khi viết kịch bản,
viết prompt và khi code thêm tính năng.

## 1. Phim truyện (kind: `movie`)

- **Là gì:** phim ngắn có cốt truyện, nhân vật và thoại đối đáp, phong cách
  điện ảnh live-action.
- **Mẫu:** `templates/movie_script.yaml` (Sài Gòn đêm mưa, An và Bình).
- **Tham chiếu thực tế (video phân tích 2026-10-26):** phim ngôn tình
  Trung Quốc lồng tiếng Việt, kênh DramaMax CEO, dạng phim bộ nhiều phần
  (P1–P4, khán giả hóng P5), video dài 2 giờ 20 phút là tập hợp nhiều tập
  ngắn nối lại, gắn nhãn nội dung tạo bằng AI.
- **Công thức kể:** mở đầu bằng lời tóm tắt ngôi thứ nhất của nữ chính
  (nàng là ai, đã đạt được gì, tham vọng tiếp theo là gì) để người xem
  mới vào phần giữa vẫn theo kịp; sau đó xen kẽ lời dẫn ngôi thứ nhất
  với thoại đối đáp trong các cảnh đời thường (thăm trang trại, cha cày
  đất, người hầu mang đào biếu, ăn thạch). Kịch tính nằm ở quan hệ và
  những thắng lợi nhỏ, không nằm ở hành động.
- **Ba sợi cảm xúc bện vào nhau:** gia đình (cha mẹ), tình cảm (người
  theo đuổi tặng quà lấy lòng), sự nghiệp (quán ăn ngày càng đắt khách).
- **Cốt lõi là ước mơ đổi đời:** người thường có một tài lẻ (nấu ăn)
  xuyên không về thời nghèo khó, dựng cơ đồ từ tay trắng; khán giả xem
  để thấy tài năng của nhân vật được đền đáp từng bước.
- **Giữ chân bằng kỳ vọng tập sau:** mỗi phần kết ở điểm chưa xong,
  khán giả phải hóng phần tiếp theo; không có luận điểm kết như phim
  tài liệu.
- **Bài học từ điểm yếu của video gốc:** một bình luận chê thuyết minh
  lộn xộn — lời dẫn ngôi thứ nhất và thoại nhân vật trộn vào nhau không
  phân biệt rõ giọng nên khó theo dõi. Giọng narrator và giọng từng nhân
  vật phải khác nhau rõ, ai nói phải rõ người đó.
- **Nhân vật khớp lời thoại (lip-sync):** model sinh video của Muse
  không nhận file audio đầu vào, nên môi nhân vật không tự khớp với
  giọng ElevenLabs. Tool có bước khớp môi hậu kỳ bằng Sync Labs: build
  với `--lipsync`, cảnh nào có `lipsync: true` (cận mặt đang nói) sẽ
  được đưa clip + audio lời thoại qua Sync Labs để vẽ lại miệng khớp
  từng âm; cảnh nào sync lỗi thì giữ nguyên clip gốc, không làm hỏng
  build. Cảnh thoại không khớp môi thì vẫn nên quay góc qua vai, sau
  lưng hoặc góc rộng. Key Sync Labs nhập bằng `lipsync set-key`.
- **Nhân vật đồng nhất:** bảng `characters:` trong kịch bản khóa một mô
  tả cố định (tiếng Anh) vào mọi cảnh có nhân vật đó, theo dạng
  `Featuring Tên (mô tả).`, để nhân vật giống nhau qua các cảnh. Ảnh
  tham chiếu (`image`, vd: `assets/characters/an.png`) được đính kèm
  inline base64 trong cùng tin nhắn chat.stream (ảnh trước, text sau);
  server có dùng ảnh làm mẫu nhân vật hay không thì đang test live 1 clip.
- **Hình ảnh:** cinematic film still, người thật, ánh sáng điện ảnh
  (moody lighting, neon, rain bokeh), màu phim trầm.
- **Góc máy:** wide shot, close-up, over-the-shoulder, tracking shot.
- **Lời thoại:** thoại đối đáp theo từng nhân vật, mỗi nhân vật gắn một
  giọng ElevenLabs riêng trong bảng `voices`; có thể có narrator dẫn.
- **Cấu trúc:** mở đầu gặp gỡ → xung đột nhỏ → cao trào → kết; mỗi cảnh
  một bước ngoặt, lời thoại đẩy câu chuyện.
- **Thời lượng:** tập 1–3 phút, 6–12 cảnh, tỉ lệ 16:9.
- **Prompt mẫu:** `cinematic live-action drama film, realistic,
  dramatic lighting`.

## 2. Thế giới động vật (kind: `documentary`)

- **Là gì:** phim tài liệu thiên nhiên, lời dẫn ngôi thứ ba, một loài chủ
  chốt dẫn cả tập.
- **Mẫu:** `templates/documentary_script.yaml`,
  `examples/the-gioi-dong-vat.yaml`.
- **Công thức (rút từ video ARCTIC):** hook tương phản + số liệu (trên
  mặt -30°C, dưới 30cm tuyết gần 0°C, con vật nhỏ bằng nắm tay sinh con
  giữa tháng Một) → loài chủ chốt → mỗi cảnh một cơ chế sinh tồn + một
  con số → kết bằng một câu luận điểm đảo ngược trực giác.
- **Hình ảnh:** wildlife documentary cinematography, BBC Earth style;
  aerial, slow motion, close-up, tracking chậm.
- **Góc máy:** wide establishing, close-up, slow tracking, aerial,
  slow motion close-up.
- **Lời dẫn:** tiếng Việt, câu ngắn, mỗi câu một ý, nhịp chậm; giọng đọc
  stability khoảng 0.7, tốc độ khoảng 0.9.
- **Thời lượng:** tập 1–3 phút, 6–12 cảnh, tỉ lệ 16:9.
- **Prompt mẫu:** `wildlife documentary cinematography, BBC Earth style`.

## 3. Hoạt hình (kind: `animation`)

- **Là gì:** phim hoạt hình 3D, nhân vật biểu cảm thái quá, gag thị giác,
  câm hoặc thoại tối thiểu; phong cách Larva.
- **Mẫu:** series Urban Pests (Chuột Cống, Gián Tăng Động, Bồ Câu Ngáo
  Ngơ) — 30 giây, 6 cảnh, câm, iris-out.
- **Hình ảnh:** colorful 3D cartoon, exaggerated cartoon physics, nhân
  vật biểu cảm mạnh, màu tươi, nền đơn giản để nhân vật nổi.
- **Góc máy:** wide shot, close-up (slapstick timing), low angle,
  tracking shot, overhead shot.
- **Lời thoại:** không có, hoặc narrator bình luận ngắn; âm thanh là
  SFX (boing, pop, thud) và nhạc nền vui.
- **Cấu trúc:** mỗi cảnh một gag, gag sau leo thang từ gag trước; kết
  bằng cú twist hoặc iris-out.
- **Thời lượng:** chuẩn 30 giây (6 cảnh x 5 giây), tỉ lệ 16:9 hoặc 9:16.
- **Prompt mẫu:** `Larva-style silent slapstick animation, exaggerated
  cartoon physics`.

## Khung kỹ thuật chung (cả ba hướng)

- Một clip sinh ra dài 10 giây, 1280x720, dù kịch bản xin 8 giây.
- Giới hạn mặc định: 20 video và 120 phút mỗi tuần cho mỗi tài khoản.
- Quy trình: `init` lấy template → sửa kịch bản → `prompts` viết prompt
  (`--apply` ghi vào visual) → `build --mock` chạy thử → `build` chạy
  thật (lồng tiếng ElevenLabs, phụ đề SRT, iris-out).
- Prompt sinh video viết bằng tiếng Anh; kịch bản và lời thoại bằng
  tiếng Việt.

## Quyết định đang chờ xác nhận

- Hiện tại trong code, phong cách Larva slapstick đang gán cho `movie`
  (không khớp template phim truyện vốn là drama live-action). Đề xuất:
  chuyển phong cách Larva sang `animation`, `movie` dùng phong cách điện
  ảnh live-action như mục 1. Xác nhận rồi mới sửa code theo.
