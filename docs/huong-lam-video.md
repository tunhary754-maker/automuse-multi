# Bốn hướng làm video — định nghĩa chuẩn

Mọi video tạo bằng automuse-multi thuộc đúng một trong bốn hướng dưới đây.
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

## 4. ASMR hoạt hình (kind: `asmr`)

- **Là gì:** hoạt hình 3D mèo dễ thương làm dịch vụ thư giãn, không thoại,
  tiếng động nhẹ nhàng là nhân vật chính; phong cách kênh hayang
  (@hayang04).
- **Mẫu:** `templates/asmr_script.yaml` (Tiệm spa của mèo Mimi, 60 giây,
  6 cảnh).
- **Tham chiếu thực tế (kênh hayang, phân tích 2026-10-27):** "I love
  making animation with asmr vibes. and also cute things." Các video
  đều là "ASMR Animation", mỗi video 1–6 triệu view: Cat's Lash Salon
  6,4 triệu, Cat's Japanese Ear Cleaning Shop 4,2 triệu, Cat's Barber
  Shop 4,1 triệu, Cat's Spa Treatment 3,9 triệu, Cat's Winter Spa 3,7
  triệu, Cat's Foot Care Shop 3,5 triệu, Cozy Fall Night Routine 2,1
  triệu, Cat's Fish-shaped Bun Stand 1,8 triệu, Cat's Dental Clinic
  1,7 triệu.
- **Công thức:** một nhân vật mèo dễ thương làm chủ một dịch vụ thư giãn
  (spa, salon tóc, tiệm cắt tóc, phòng khám răng, tiệm ngoáy tai, tiệm
  làm móng, quán bánh cá) → mỗi cảnh là một bước nhẹ nhàng trong quy
  trình, mỗi bước gắn đúng một âm thanh mềm gây nghiện (chải lông, rót
  nước, gõ nhẹ, xoa bóp, quấn khăn) → kết bằng cảnh thư giãn yên bình.
- **Nhân vật:** mèo là nhân vật chính (chủ tiệm hoặc khách), biểu cảm
  hiền, mắt nhắm lim dim, chuyển động chậm rãi; màu pastel, ánh sáng ấm,
  không gian cozy (spa, tiệm mùa đông, đêm thu).
- **Âm thanh — quan trọng nhất:** KHÔNG lời dẫn, KHÔNG nhạc nền, KHÔNG
  ambience đắp ngoài. Tiếng gốc của từng clip làm nền (đã khớp từng
  hành động: chải thì có tiếng chải, rót nước thì có tiếng nước).
  Pipeline ráp tập mặc định bỏ tiếng gốc nên phải ghép lại từ
  `clips/*.mp4` sau khi build. Mỗi cảnh ghi rõ `sfx` (âm thanh mềm của
  hành động đó) để prompt sinh clip có tiếng gốc khớp.
- **Hình ảnh:** cute soft 3D animation, pastel colors, cozy and relaxing,
  gentle slow movements, satisfying repetitive actions.
- **Góc máy:** extreme close-up, macro close-up, slow close-up, overhead
  close-up, soft focus close-up — cận cảnh là chính vì hành động gây
  tiếng nằm ở chi tiết nhỏ.
- **Lời thoại:** không có. Build với `--no-tts` (ASMR không lồng tiếng).
- **Cấu trúc:** mở đầu bằng không gian cozy → từng bước dịch vụ nhẹ
  nhàng, mỗi bước một âm thanh → kết bằng cảnh thư giãn, yên bình.
- **Thời lượng:** tập 1–2 phút, 6–12 cảnh, tỉ lệ 16:9.
- **Prompt mẫu:** `cute soft 3D animation, ASMR vibes, pastel colors,
  cozy and relaxing, gentle slow movements, satisfying repetitive
  actions`.

## Khung kỹ thuật chung (cả bốn hướng)

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
