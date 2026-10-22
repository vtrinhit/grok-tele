# GrokTele — Design

Ngày: 2026-10-22. Thay thế phần kiến trúc của `docs/grok-video-tool-spec.md` (bỏ xAI API, bỏ Docker/VPS/Redis/webhook).

## 1. Mục tiêu

App Windows chạy nền, tạo video hàng loạt qua bot Telegram **@GrokKian_bot** ("Grok Không Giới Hạn Theo Tuần")
bằng 1–N tài khoản Telegram (userbot Telethon). Expose **API giống hệt MultiKit Grok** để ReelFB Studio dùng lại
provider `multikit` có sẵn, chỉ đổi URL sang `http://127.0.0.1:8888`.

Thành công = ReelFB gửi job, nhận file mp4 ở `outputPath`, app tự chạy 24/7 không cần can thiệp,
tự xử lý bot bảo trì / FloodWait / mất mạng / tài khoản hết hạn.

Không làm: xAI API, Docker, Redis, webhook, SSE, dashboard web, video nhiều ảnh, nối dài video.

## 2. Hành vi bot (quan sát thật 2026-10-22)

1. Gửi **ảnh + caption = prompt** (không cần `/start`).
2. Bot gửi tin mới `✅ Đã nhận 1 bức ảnh kèm kịch bản...` với inline keyboard:
   `Dọc 9:16 | Ngang 16:9` / `6s | 10s | 15s` / `⚡ 720p | ✨ 1080p Full HD` / `🎬 Tạo Video (từ ảnh + prompt)`.
   Nút đang chọn có tiền tố `✓`. Mặc định theo `/settings` của tài khoản.
3. Bấm nút tùy chọn → bot **edit** chính tin đó (✓ dời sang nút mới).
4. Bấm Tạo Video → tin tùy chọn bị xoá; bot gửi `⏳ Đã nhận yêu cầu Tạo Video [1 Ảnh | Tỷ lệ 9:16 | 10s | 1080p]...`.
5. Bot gửi `🎬 Đang tạo video (9:16 | 1080p): 30%`, edit dần tới 99%, rồi xoá.
6. Bot gửi video mp4 (không reply_to, không caption), sau đó `✅ Nhiệm vụ đã hoàn thành...`.
7. Tổng ~2 phút / video 10s 1080p.
8. `/status` → `Trạng thái: Còn hạn sử dụng`, `Hết hạn lúc: 18:24 25/10/2026 (giờ Việt Nam)`.
9. Bot broadcast bất chợt: "bảo trì", "đóng bot", "reset mạng ... gửi lại", "hoạt động trở lại".

Hệ quả: video không gắn với job → **mỗi tài khoản chạy đúng 1 job tại một thời điểm**, và chỉ nhận tin có id lớn hơn
mốc id ghi lại trước khi gửi ảnh.

## 3. Kiến trúc

Một process `python -m groktele serve`: FastAPI (uvicorn) + 1 worker asyncio / tài khoản, chung event loop.

```
groktele/
  config.py      .env + accounts.yaml
  db.py          SQLite (jobs, accounts) — claim job nguyên tử
  chat.py        BotChat interface + TelethonChat (Telethon thật)
  bot_driver.py  run_job(): toàn bộ hiểu biết về @GrokKian_bot nằm ở đây
  worker.py      vòng lặp mỗi tài khoản, retry, pause, health check /status
  api.py         route kiểu MultiKit
  cli.py         login / serve / status / pause / resume / try
  app.py         ghép mọi thứ, startup/shutdown
```

`bot_driver` chỉ phụ thuộc `BotChat` (send_photo, send_text, last_message_id, next_event, get_message, click, download).
Test dùng `FakeChat` mô phỏng bot.

## 4. Map tham số

| Input (MultiKit) | Bot |
|---|---|
| `aspectRatio` 9:16 / 16:9 | nút Dọc 9:16 / Ngang 16:9; giá trị khác → 400 invalid_input |
| `duration` 6 / 10 / 15 | nút 6s / 10s / 15s |
| `resolution` 480p, 720p / 1080p | nút 720p / 1080p |
| `mode` | bỏ qua |

Mặc định (config): 9:16, 10, 720p.

## 5. Vòng đời job & lỗi

Status: `queued → running → succeeded | failed | cancelled`. `stage`: upload, options, generate, download. `progress` 0–1.
Claim: job `queued` có priority cao nhất, cũ nhất; UPDATE nguyên tử. `clientRef` trùng (job chưa failed/cancelled) → trả job cũ.

| Tình huống | Job | Tài khoản |
|---|---|---|
| Timeout UI (60s), timeout video (15 phút), bot báo lỗi render | retry, tối đa 3 attempts → `failed` (`timeout`/`unknown`) | giữ |
| Bot bảo trì / reset mạng | về `queued`, không tính attempt | `cooldown` 5 phút |
| FloodWait x giây | về `queued`, không tính attempt | `cooldown` x giây |
| Mất kết nối | về `queued`, không tính attempt | reconnect, `cooldown` 30s |
| Hết hạn gói (Zalo / hết lượt / `/status` không còn hạn) | về `queued` | `expired` tới khi resume |
| Session chết / bị khoá | về `queued` | `dead` |
| DELETE job | huỷ task đang chạy, `cancelled` | giữ |

Restart app: job `running` → `queued`. Không còn tài khoản usable (mọi tài khoản expired/dead/disabled hoặc không có) →
`POST` trả `409 conflict` với `details.reason=no_account_available`.
Health check: khi khởi động và mỗi 30 phút rảnh, gửi `/status`, lưu `expiresAt`.

## 6. API (127.0.0.1:8888)

Giống MultiKit: `GET /health`, `POST /grok/jobs`, `POST /grok/jobs/batch`, `GET /grok/jobs/{id}`, `GET /grok/jobs`,
`DELETE /grok/jobs/{id}`, `POST /grok/jobs/{id}/retry`, `GET /grok/accounts`, `GET /grok/stats`.
Thêm: `POST /grok/accounts/{name}/pause|resume`.
Key tùy chọn (`GROKTELE_API_KEY`): nếu đặt thì yêu cầu `Authorization: Bearer` hoặc `X-API-Key`, trừ `/health`.
Lỗi: `{ "error": { "code", "message", "details" } }`.
Ảnh: `imagePath` hoặc `imageBase64` (lưu vào `data/uploads`). Output: `outputPath` > `outputDir` > `data/outputs/<jobId>.mp4`.

## 7. Cấu hình & vận hành

- `.env`: `TG_API_ID`, `TG_API_HASH`, tùy chọn `GROKTELE_PORT`, `GROKTELE_API_KEY`, `GROKTELE_BOT`, mặc định tham số.
- `accounts.yaml`: `- {name, session, proxy?, enabled}`; `login` tự thêm/cập nhật. File chứa secret → gitignore.
- `run.bat` chạy server; README hướng dẫn tự khởi động bằng Task Scheduler.
- Log: console + `data/groktele.log` (xoay vòng 5×5MB), mỗi dòng có job id + tài khoản.
- Dọn `data/uploads` quá 7 ngày khi khởi động.
- Delay ngẫu nhiên 0.6–1.5s giữa các thao tác.

## 8. Test

- Unit: map tham số, nhận diện tin nhắn, chọn nút.
- Driver với `FakeChat` (luồng thành công, bấm lại tùy chọn, bảo trì, hết hạn, timeout, bỏ qua tin cũ).
- Worker + DB: retry, requeue không tính attempt, cancel, 409.
- API qua `httpx.ASGITransport` + client `multikit.py` thật của ReelFB chạy được với app.
- E2E thật: `python -m groktele try <acc> <ảnh> "<prompt>"` sau khi login.
