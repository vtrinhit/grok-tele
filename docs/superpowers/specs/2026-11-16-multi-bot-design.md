# GrokTele — nhiều bot song song

## Mục tiêu

`@GrokKian2_bot` giống hệt `@GrokKian_bot` và dùng chung gói (mua 1 lần dùng ở cả 2). Tận dụng điều đó để mỗi
tài khoản Telegram có thêm 1 luồng tạo video: 1 tài khoản × 2 bot = 2 video cùng lúc.

Không làm: cân bằng tải theo bot, driver riêng cho bot có giao diện khác, tự phát hiện bot mới.

## Các hướng đã cân nhắc

1. **Luồng = tài khoản × bot, mỗi luồng 1 worker (chọn).** Worker, hàng đợi, cơ chế render dở / `/cancel` giữ
   nguyên vì chúng vốn gắn với 1 hội thoại bot. Ít thay đổi, một bot hỏng không ảnh hưởng bot kia.
2. 1 worker / tài khoản chạy N job nội bộ. Phải viết lại trạng thái worker (cooldown, orphan, current_job) thành
   theo bot — nhiều rủi ro hơn mà kết quả như hướng 1.
3. Khai báo cùng session 2 lần trong `accounts.yaml` với bot khác nhau. Không được: 1 session mở 2 kết nối bị
   Telegram huỷ (`AuthKeyDuplicated`).

## Thiết kế

- **Cấu hình**: `GROKTELE_BOTS` (danh sách, bot đầu là bot chính) > `GROKTELE_BOT` (cũ, 1 bot) > mặc định cả 2 bot.
  `accounts.yaml` có thể ghi `bots:` cho riêng tài khoản. `Settings.expand_lanes()` sinh danh sách luồng.
- **Tên luồng**: bot chính giữ tên tài khoản (`acc1`) để database, `accountId` và ReelFB cũ không đổi; bot khác là
  `acc1@GrokKian2_bot`. Mỗi luồng là 1 dòng trong bảng `accounts` (trạng thái, hạn, render dở riêng).
- **Kết nối**: `ClientPool` giữ 1 `TelegramClient` cho mỗi tài khoản, đếm số luồng đang dùng, chỉ ngắt khi luồng
  cuối trả lại. Luồng kết nối lại mà client chung đã chết lửng thì kết nối lại tại chỗ. Mỗi `TelethonChat` chỉ nghe
  sự kiện của bot mình. Chưa có hội thoại với bot → tự gửi `/start`.
- **Hàng đợi**: không đổi; luồng nào rảnh thì nhận. Job ghim `accountId` = tên tài khoản thì mọi bot của nó nhận
  được, = tên luồng thì đúng bot đó.
- **API / CLI**: `/grok/accounts` liệt kê từng luồng (thêm `bot`, `telegramAccount`, `summary.telegramAccounts`).
  `pause` / `resume` theo tên tài khoản áp dụng cho mọi bot. `try --bot`. `login` chào mọi bot.

## Giả định cần kiểm chứng trên bot thật

- Hai bot render song song cho cùng 1 tài khoản mà không chặn nhau.
- FloodWait tính theo tài khoản: 2 luồng gửi gấp đôi lượng tin; mỗi luồng tự nghỉ khi gặp.
