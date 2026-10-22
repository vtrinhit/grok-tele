# Grok Video Batch Tool — Đặc tả & Định hướng phát triển

> Tài liệu bàn giao để phát triển ở session khác.
> Mục tiêu: tự động hóa việc tạo video hàng loạt qua Telegram bot "Grok Không Giới Hạn Theo Tuần", chạy 24/7, nhiều luồng, nhận job qua API từ tool khác.

---

## 1. Bối cảnh

### 1.1. Luồng thao tác tay hiện tại

| # | Người dùng | Bot |
|---|---|---|
| 1 | Gửi `/start` | Gửi lời chào + 4 inline button: `🎬 Tạo Video (1 Ảnh)`, `🎞️ Tạo Video Nhiều Ảnh`, `👤 Tài khoản & Lượt thử`, `💌 Yêu cầu gia hạn` |
| 2 | Bấm `Tạo Video (1 Ảnh)` | Gửi `🎬 HƯỚNG DẪN TẠO VIDEO TỪ 1 ẢNH` — yêu cầu gửi 1 ảnh + prompt |
| 3 | Gửi 1 ảnh, caption = prompt (vd: "Tạo video hoa nở") | Hiển thị bảng tùy chọn (inline buttons: thời lượng 10s…, độ phân giải 720p/1080p, tỷ lệ…, nút bắt đầu) |
| 4 | Bấm các tùy chọn → bấm nút bắt đầu | `⏳ Đã nhận yêu cầu Tạo Video [1 Ảnh \| Tỷ lệ 9:16 \| 10s \| 1080p]. Bot sẽ gửi video tại đây ngay khi hoàn tất!` |
| 5 | Chờ (~2–5 phút) | Gửi video (.mp4) |

### 1.2. Tin nhắn đặc biệt cần nhận diện
- `Đã nhận yêu cầu` → job đã được bot nhận.
- Tin nhắn chứa video → kết quả.
- `Bạn đã nhận video thử thành công! ... Nhập Tên Zalo của bạn` → hết lượt dùng thử / cần gia hạn → **tạm dừng tài khoản**.
- `Nếu quá trình tạo video báo lỗi, bạn hãy gửi lại ảnh và prompt` → bot có thể báo lỗi → cần retry.

### 1.3. Thông tin còn thiếu (cần bổ sung trước khi code)
- [ ] Username chính xác của bot (`@...`).
- [ ] Ảnh chụp bảng tùy chọn ở bước 3: tên chính xác các nút (thời lượng, độ phân giải, tỷ lệ, nút "bắt đầu").
- [ ] Bot **gửi tin mới** hay **edit tin cũ** sau mỗi lần bấm tùy chọn?
- [ ] Video trả về có `reply_to` tin nhắn gốc không?
- [ ] Khi 1 video đang render, bot có nhận request thứ 2 trên cùng tài khoản không?
- [ ] Gói gia hạn tính theo tài khoản Telegram? Giới hạn số video/ngày thực tế?
- [ ] `api_id` / `api_hash` (lấy tại https://my.telegram.org).

---

## 2. Quyết định kỹ thuật

### 2.1. Dùng userbot (MTProto), KHÔNG dùng Bot API
Bot Telegram không thể chat với bot khác → tool phải đăng nhập bằng **tài khoản người dùng** qua **Telethon** (Python) hoặc Pyrogram, giả lập đúng thao tác tay.

| Thao tác | Telethon |
|---|---|
| `/start` | `client.send_message(bot, "/start")` |
| Bấm inline button | `msg.click(text=...)` / `msg.click(i, j)` |
| Gửi ảnh + prompt | `client.send_file(bot, path, caption=prompt)` |
| Chờ video | event `NewMessage` có `msg.video` → `msg.download_media()` |
| Theo dõi edit | event `MessageEdited` / `conv.get_edit()` |

### 2.2. KHÔNG dùng AI agent (Hermes, OpenClaw, Grok agent…) để điều khiển luồng

| Tiêu chí | Userbot script | AI agent |
|---|---|---|
| Tốc độ / bước | ms | vài giây |
| Chi phí | ~0 | token mỗi bước, mỗi video |
| Ổn định 24/7 | Cao, dễ debug | Thấp, dễ bấm nhầm / lạc luồng |
| Khi bot đổi UI | Sửa vài dòng | Tự thích nghi một phần |

→ Chỉ (tùy chọn) dùng LLM để **phân loại tin nhắn lạ** không khớp kịch bản.

### 2.3. Phương án thay thế cần đánh giá song song
Bot nhiều khả năng bọc lại **Grok Imagine (xAI)**. Kiểm tra **xAI API — video generation**: nếu khả dụng và giá chấp nhận được thì gọi API trực tiếp → song song tự do, không cần nhiều tài khoản Telegram, không rủi ro bị khóa.

---

## 3. Kiến trúc

```
[Tool nguồn] --POST /jobs--> [API Service (FastAPI)] --> [Queue / DB (SQLite → Redis)]
                                                              |
                         +----------------+-------------------+----------------+
                      Worker acc1      Worker acc2         Worker acc3   ...  (1 worker = 1 tài khoản Telegram)
                         |                |                   |
                         +-------- Telethon userbot ----------+
                                          |
                                     Bot Grok (Telegram)
                                          |
                    video .mp4 --> Storage (disk/S3) --> webhook callback về Tool nguồn
```

### 3.1. Thành phần
- **API Service**: nhận job, trả `job_id`, tra trạng thái.
- **Queue/DB**: lưu job + trạng thái + số lần retry + account đang xử lý.
- **Account Manager**: danh sách tài khoản, session file, proxy, trạng thái (`active` / `paused` / `expired` / `flood_wait`).
- **Worker**: mỗi tài khoản 1 worker, xử lý **tuần tự** 1 job/lần (trừ khi xác nhận bot hỗ trợ nhiều job song song + có `reply_to` để map).
- **Storage**: lưu video, trả URL/đường dẫn.
- **Notifier**: webhook về tool nguồn + cảnh báo admin (Telegram/Zalo) khi tài khoản hết hạn, lỗi liên tục.

### 3.2. Song song & năng suất
- Song song = **số tài khoản Telegram** (mỗi tài khoản có thể cần gói gia hạn riêng).
- Ước tính: ~5 phút/video → ~12 video/giờ → ~280 video/ngày/tài khoản.

---

## 4. Đặc tả API

### `POST /jobs`
```json
{
  "image_url": "https://... (hoặc image_base64)",
  "prompt": "Tạo video hoa nở",
  "mode": "single_image",
  "duration": "10s",
  "resolution": "1080p",
  "aspect_ratio": "9:16",
  "callback_url": "https://tool-nguon/webhook",
  "external_id": "id-ben-tool-nguon"
}
```
Response: `{ "job_id": "uuid", "status": "pending" }`

### `GET /jobs/{job_id}`
```json
{
  "job_id": "uuid",
  "status": "pending | running | done | failed",
  "account": "acc1",
  "attempts": 1,
  "video_url": "https://.../xxx.mp4",
  "error": null,
  "created_at": "...", "finished_at": "..."
}
```

### Webhook callback (POST tới `callback_url`)
Gửi cùng payload như `GET /jobs/{id}` khi job `done` hoặc `failed` (sau khi hết retry).

### (Tùy chọn) `POST /jobs/batch`, `GET /accounts`, `POST /accounts/{id}/pause|resume`

---

## 5. State machine của Worker

```
IDLE
 └─ lấy job ─> SEND_START ─> WAIT_MENU ─> CLICK_SINGLE_IMAGE ─> WAIT_GUIDE
     ─> SEND_PHOTO_PROMPT ─> WAIT_OPTIONS ─> CLICK_OPTIONS ─> CLICK_START
     ─> WAIT_ACCEPTED ─> WAIT_VIDEO ─> DOWNLOAD ─> DONE ─> IDLE

Bất kỳ bước nào:
  timeout / tin nhắn lỗi       -> RETRY (tối đa N lần, gửi lại từ /start)
  tin nhắn hết hạn / Zalo      -> ACCOUNT_PAUSED (trả job về queue, báo admin)
  FloodWaitError(x)            -> sleep x giây, trả job về queue
```

Timeout gợi ý: các bước UI 60s; chờ video 15 phút.

---

## 6. Code phác thảo (Telethon)

```python
from telethon import TelegramClient
import asyncio, random

BOT = "@ten_bot_grok"

async def wait_msg(conv, cond, timeout=60):
    while True:
        m = await conv.get_response(timeout=timeout)
        if cond(m):
            return m

def has(text):
    return lambda m: text in (m.raw_text or "")

async def click_kw(msg, kw):
    for row in msg.buttons or []:
        for b in row:
            if kw.lower() in b.text.lower():
                await asyncio.sleep(random.uniform(0.8, 2.0))
                return await b.click()
    raise RuntimeError(f"Không thấy nút chứa '{kw}'")

async def run_job(client, image_path, prompt, duration="10s", res="1080p"):
    async with client.conversation(BOT, timeout=900) as conv:
        await conv.send_message("/start")
        menu = await wait_msg(conv, lambda m: m.buttons)
        await click_kw(menu, "1 Ảnh")

        await wait_msg(conv, has("HƯỚNG DẪN"))
        await conv.send_file(image_path, caption=prompt)

        opts = await wait_msg(conv, lambda m: m.buttons)
        await click_kw(opts, duration)
        opts = await conv.get_edit()          # nếu bot edit message; nếu gửi tin mới thì dùng wait_msg
        await click_kw(opts, res)
        # TODO: bấm nút "bắt đầu" (cần tên chính xác)

        await wait_msg(conv, has("Đã nhận yêu cầu"))
        video = await wait_msg(conv, lambda m: m.video, timeout=900)
        return await video.download_media(file=f"out/{video.id}.mp4")

async def main():
    client = TelegramClient("sessions/acc1", API_ID, API_HASH)
    await client.start()
    print(await run_job(client, "flowers.jpg", "Tạo video hoa nở"))
```

---

## 7. Vận hành 24/7

- Chạy trên VPS Linux, Docker Compose (`restart: always`): `api`, `worker`, `redis`.
- Session Telethon lưu volume riêng; mỗi tài khoản một proxy/IP nếu chạy nhiều.
- Delay ngẫu nhiên giữa các thao tác; xử lý `FloodWaitError`.
- Tự reconnect khi mất kết nối; health check endpoint `/health`.
- Log có cấu trúc theo `job_id` + `account`; dashboard đơn giản (số job/giờ, tỉ lệ lỗi theo tài khoản).
- Khớp nút theo **từ khóa** (không khớp nguyên chuỗi có emoji) để chịu được thay đổi nhỏ của bot.
- Dọn video cũ theo TTL.

---

## 8. Rủi ro

1. **Phụ thuộc bot bên thứ ba**: bot đổi nút/luồng → tool gãy; chủ bot có thể chặn tài khoản dùng tự động.
2. **Telegram khóa tài khoản** nếu hành vi bất thường → dùng tài khoản lâu năm, giới hạn tốc độ, proxy riêng.
3. **Map sai video ↔ job** nếu gửi nhiều job song song trên 1 tài khoản → mặc định tuần tự.
4. Chi phí gói gia hạn nhân theo số tài khoản → so sánh với xAI API.

---

## 9. Lộ trình

| Phase | Nội dung | Kết quả |
|---|---|---|
| 0 | Thu thập thông tin mục 1.3, thử bằng tay các câu hỏi về song song/reply | Danh sách nút + hành vi bot |
| 1 | PoC Telethon 1 tài khoản, chạy trọn 1 job từ CLI | Tải được video |
| 2 | FastAPI + SQLite queue + webhook, 1 worker | Tool nguồn gọi API được |
| 3 | Multi-account worker pool, Account Manager, proxy, retry, cảnh báo | Chạy song song N tài khoản |
| 4 | Docker, VPS, monitoring, dọn dẹp | Chạy 24/7 |
| song song | Đánh giá xAI video API | Quyết định có chuyển hướng không |

### Stack đề xuất
Python 3.11+, Telethon, FastAPI, Uvicorn, SQLite (→ Redis/arq hoặc RQ khi scale), httpx, Docker Compose.
