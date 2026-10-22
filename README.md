# GrokTele

Tạo video hàng loạt qua bot Telegram **@GrokKian_bot** (Grok Không Giới Hạn Theo Tuần), chạy nền 24/7 trên Windows.
Mỗi tài khoản Telegram là 1 luồng (~2 phút/video → ~25 video/giờ/tài khoản).

API **giống hệt MultiKit Grok**, nên ReelFB Studio dùng ngay provider `multikit` có sẵn, chỉ đổi URL.

## Cài đặt lần đầu

```bat
cd D:\MMO\GrokTele
python -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
copy .env.example .env
```

Mở `.env`, điền `TG_API_ID` và `TG_API_HASH` (lấy tại https://my.telegram.org → API development tools).

## Thêm tài khoản Telegram (mỗi tài khoản làm 1 lần)

```bat
.venv\Scripts\python -m groktele login acc1
```

Nhập số điện thoại (+84…), mã Telegram gửi về app, mật khẩu 2FA nếu có. Session được lưu vào `accounts.yaml`
(file này = toàn quyền tài khoản, không chia sẻ). Proxy riêng cho tài khoản: thêm `--proxy socks5://user:pass@host:port`.

Đã có session string Telethon từ tool khác: `python -m groktele import-session acc2 <chuỗi>`.

Tài khoản phải còn hạn gói của bot (`/status` trong bot báo "Còn hạn sử dụng").

## Chạy thử 1 video (không cần server)

```bat
.venv\Scripts\python -m groktele try acc1 D:\anh\hoa.jpg "Camera zoom chậm vào bó hoa" --resolution 1080p
```

## Chạy server

Nháy đúp `run.bat` (tự khởi động lại nếu bị tắt), hoặc:

```bat
.venv\Scripts\python -m groktele serve
```

Nghe tại `http://127.0.0.1:17891`. Tài liệu API tương tác: `http://127.0.0.1:17891/docs`.

Tự chạy khi bật máy: Task Scheduler → Create Task → Trigger "At log on" → Action "Start a program" =
`D:\MMO\GrokTele\run.bat`, Start in = `D:\MMO\GrokTele`.

## Nối ReelFB Studio

ReelFB → Settings → Video → provider **MultiKit**:
- URL: `http://127.0.0.1:17891`
- API key: gõ bất kỳ (GrokTele không kiểm tra key trừ khi đặt `GROKTELE_API_KEY` trong `.env`)
- Tỷ lệ: chỉ `9:16` hoặc `16:9`. Thời lượng: 6 / 10 / 15. Chất lượng: `720p` hoặc `1080p` (`480p` tự nâng lên 720p).

## Lệnh hằng ngày

| Lệnh | Việc |
|---|---|
| `python -m groktele status` | Tài khoản, hạn dùng, số job, tỉ lệ thành công 24h |
| `python -m groktele resume acc1` | Chạy lại tài khoản sau khi gia hạn (hoặc `POST /grok/accounts/acc1/resume`) |
| `python -m groktele pause acc1` | Tạm dừng tài khoản |

Log: `data\groktele.log`. Dữ liệu job: `data\groktele.db`.

## API

| Method | Path | Việc |
|---|---|---|
| GET | `/health` | App đang chạy |
| POST | `/grok/jobs` | Tạo job: `imagePath` hoặc `imageBase64`, `prompt`, `aspectRatio`, `duration`, `resolution`, `outputPath`/`outputDir`, `priority`, `clientRef`, `accountId` |
| POST | `/grok/jobs/batch` | `{ defaults, jobs: [...] }`, lỗi 1 phần tử thì không tạo gì |
| GET | `/grok/jobs/{id}` | `status` queued/running/succeeded/failed/cancelled, `stage`, `progress`, `result.filePath` |
| GET | `/grok/jobs?status=&batchId=&clientRef=&limit=&cursor=` | Danh sách |
| DELETE | `/grok/jobs/{id}` | Huỷ |
| POST | `/grok/jobs/{id}/retry` | Chạy lại job failed/cancelled |
| GET | `/grok/accounts` | Trạng thái tài khoản, `summary.usable` |
| POST | `/grok/accounts/{name}/pause` · `/resume` | Tạm dừng / chạy lại |
| GET | `/grok/stats` | Thống kê |

`POST` trả `409 no_account_available` khi không tài khoản nào chạy được (chưa login, tất cả hết hạn / tạm dừng / session chết).

## Tự xử lý lỗi

| Tình huống | GrokTele làm gì |
|---|---|
| Timeout, bot báo lỗi render | Thử lại, tối đa 3 lượt rồi `failed` |
| Bot bảo trì / reset mạng | Trả job về hàng đợi (không mất lượt), tài khoản nghỉ 5 phút rồi hỏi `/status` |
| FloodWait của Telegram | Nghỉ đúng số giây Telegram yêu cầu |
| Mất mạng | Tự kết nối lại |
| Hết hạn gói | Tài khoản `expired`, job chuyển sang tài khoản khác. Gia hạn xong chạy `resume` |
| Session bị đăng xuất | Tài khoản `dead`. Chạy lại `login` rồi khởi động lại server |
| Tắt app giữa chừng | Mở lại, job dở được chạy lại |

## Khi bot đổi giao diện

Mọi hiểu biết về bot nằm trong `groktele/bot_driver.py` (từ khoá tin nhắn, tên nút). Test mô phỏng bot ở
`tests/fake_bot.py`. Chạy test: `.venv\Scripts\python -m pytest`.
