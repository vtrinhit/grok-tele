# Video nhiều ảnh (2–6 ảnh) — thiết kế ngắn

Ngày: 2026-11-21

## Vì sao
ReelFB chỉ gửi 1 ảnh mỗi clip. Công cụ khác cần tạo video từ nhiều ảnh, và bot @GrokKian_bot đã hỗ trợ sẵn.
Mục tiêu: công cụ khác gửi được 2–6 ảnh qua cùng API, **ReelFB không đổi gì**.

Không làm: giao diện riêng, nối dài video, chọn chất lượng cho video nhiều ảnh (bot không cho chọn).

## Bot thật (thăm dò 21/11, không bấm Tạo Video)
| Gửi | Bot trả lời | Bàn phím |
|-----|-------------|----------|
| 1 ảnh + caption | "Đã nhận 1 bức ảnh kèm kịch bản" | tỷ lệ / 3s 5s 6s 7s / 8s 10s 12s 15s / 720p 1080p / "Tạo Video (từ ảnh + prompt)" |
| album 2 ảnh + caption | "Đã nhận 2 bức ảnh kèm kịch bản ... Hãy chọn kiểu ghép 2 ảnh" | tỷ lệ / thời lượng / "🎯 Chuyển cảnh: Ảnh Đầu → Ảnh Cuối" / "🎞️ Ghép Nguyên Liệu (2 ảnh tham chiếu)" |
| album 3 ảnh + caption | "Đã nhận 3 bức ảnh kèm kịch bản" | tỷ lệ / thời lượng / "🎞️ Tạo Video từ 3 ảnh (Nguyên liệu / Xoay 2 mặt)" |
| album 4–6 ảnh + caption | "Đã nhận N bức ảnh kèm kịch bản (Chế độ >3 ảnh)": ảnh 1→N-1 làm nguyên liệu, **ảnh N làm khung hình cuối** | tỷ lệ / thời lượng / "🎬 Tạo Video N ảnh (N-1 Nguyên liệu + Ảnh N Last Frame)" |
| album 7 ảnh | bot **lặng lẽ chỉ lấy 6** | như 6 ảnh |
| album không caption | "📥 Đã nhận N bức ảnh! ... gửi thêm ảnh hoặc nhắn kịch bản" (không nút), nhắn prompt xong mới ra bảng tuỳ chọn | như trên |

Nhiều ảnh: **không có hàng 720p/1080p**, chữ bot ghi "Chất lượng: 720p".

## Cách làm (đã chọn: A)
- **A. Thêm trường tuỳ chọn vào `POST /grok/jobs`** — chọn. Không gửi trường mới (ReelFB) → đi đúng code cũ.
- B. Endpoint riêng `/grok/jobs/multi` — tách biệt hơn nhưng chép lại validate/dedupe/batch, dễ lệch. Bỏ.
- C. Nhét nhiều đường dẫn vào `imagePath` ngăn cách bằng ký tự — chắp vá. Bỏ.

## Thiết kế
- API nhận thêm `imagePaths: [đường dẫn tuyệt đối]` hoặc `imagesBase64: [base64]`, 1–6 ảnh, chỉ một nguồn ảnh
  cho mỗi job (trộn với `imagePath`/`imageBase64` → 400). Quá 6 → 400 (bot sẽ bỏ bớt ảnh mà không báo).
- Danh sách 1 ảnh = job 1 ảnh bình thường (đúng luồng cũ, chọn được 1080p).
- `multiImageMode` chỉ dùng khi đúng 2 ảnh: `reference` (mặc định, "Ghép Nguyên Liệu") hoặc `firstLast`
  ("Chuyển cảnh Ảnh Đầu → Ảnh Cuối"). Không đặt tên `mode` vì ReelFB đã gửi `"mode": "custom"`.
- 2+ ảnh: `resolution` luôn lưu và trả về `720p` (giống cách 480p đã được nâng lên 720p).
- DB: thêm cột `image_paths` (JSON, NULL với job 1 ảnh) và `image_mode`; `image_path` vẫn là ảnh đầu.
  `input` của job trả thêm `imagePaths`, `multiImageMode`.
- Driver: 1 ảnh giữ nguyên code cũ. 2+ ảnh: gửi album (`send_photos`, caption = prompt ở ảnh đầu), chờ bảng tuỳ
  chọn, **kiểm tra bot nhận đủ N ảnh** (sai → lỗi thử lại), chọn tỷ lệ + thời lượng, bỏ bước chất lượng, bấm nút
  bắt đầu theo số ảnh (2 ảnh: nút theo `multiImageMode`).
  Prompt dài hơn caption: gửi album không caption, **chờ "Đã nhận N bức ảnh!"** rồi mới nhắn prompt.
- Thời hạn upload album nhân theo số ảnh.

## Chưa kiểm chứng (kiểm bằng job thật sau khi xong)
Popup khi bấm nút bắt đầu nhiều ảnh, chữ "Đã nhận yêu cầu", tin % — giả định giống 1 ảnh.
