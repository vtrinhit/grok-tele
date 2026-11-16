# GrokTele

Tạo video hàng loạt qua bot Telegram **@GrokKian_bot** (Grok Không Giới Hạn Theo Tuần), chạy nền 24/7 trên Windows.
Mỗi tài khoản Telegram là 1 luồng (~100 giây/video → ~35 video/giờ/tài khoản).

API **giống hệt MultiKit Grok**, nên ReelFB Studio dùng ngay provider `multikit` có sẵn, chỉ đổi URL.

## Script nhanh (nháy đúp)

| File | Việc |
|---|---|
| `01.setup.bat` | Cài lần đầu: tạo `.venv`, cài thư viện, tạo `.env` và mở để điền |
| `02.login.bat` | Thêm 1 tài khoản Telegram (hỏi tên, proxy, rồi số điện thoại + mã OTP) |
| `03.start.bat` | Chạy server có cửa sổ, tự khởi động lại nếu bị tắt |
| `04.start-hidden.vbs` | Chạy server ẩn, không có cửa sổ |
| `06.stop.bat` | Dừng server (cả bản chạy ẩn) |
| `05.status.bat` | Xem tài khoản, hạn dùng, thống kê job |
| `07.install-autostart.bat` | Tự chạy ẩn mỗi khi đăng nhập Windows (`07.install-autostart.bat remove` để gỡ) |
| `08.cancel-all.bat` | Huỷ mọi job đang chờ / đang chạy |

Bắt đầu nhanh: `01.setup.bat` → điền `.env` → `02.login.bat` → `03.start.bat`.

Source đặt ở thư mục nào cũng được (kể cả có dấu cách, tiếng Việt). Mọi dữ liệu (`.env`, `accounts.yaml`, `data\`)
nằm ngay trong thư mục source. Cần Python 3.11+ (`python` hoặc launcher `py` đều được).

## Cài lên VPS Windows

1. Cài Python 3.11+ (tick "Add python.exe to PATH") và Git.
2. `git clone https://github.com/vtrinhit/grok-tele.git` vào thư mục bất kỳ, chạy `01.setup.bat`, điền `.env`.
3. Thêm tài khoản: chạy `02.login.bat` trên VPS (khuyên dùng), hoặc copy `accounts.yaml` từ máy cũ sang.
   **Một session không được chạy ở 2 máy cùng lúc** (Telegram sẽ huỷ session → tài khoản `dead`).
   Nếu copy `accounts.yaml`, tắt hẳn GrokTele ở máy cũ trước (`06.stop.bat`, gỡ autostart).
4. `07.install-autostart.bat` để tự chạy ẩn khi VPS khởi động và đăng nhập. Muốn chạy cả khi chưa đăng nhập RDP,
   bật auto-logon cho user Windows, hoặc tạo task "Run whether user is logged on or not" trong Task Scheduler.
5. ReelFB phải chạy **trên cùng VPS**, vì API trao đổi bằng đường dẫn file (`imagePath`, `outputPath`).
   Hai bên không cần chung ổ đĩa hay thư mục, chỉ cần đường dẫn tuyệt đối hợp lệ trên VPS.
   API chỉ nghe `127.0.0.1`, không cần mở firewall.

## Cài đặt lần đầu (thủ công)

```bat
cd <thư mục source>
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
.venv\Scripts\python -m groktele try acc1 C:\anh\hoa.jpg "Camera zoom chậm vào bó hoa" --resolution 1080p
```

Tắt server trước khi chạy `try` (cùng một session không nên chạy 2 nơi cùng lúc).

## Chạy server

Nháy đúp `03.start.bat` (tự khởi động lại nếu bị tắt), hoặc:

```bat
.venv\Scripts\python -m groktele serve
```

Nghe tại `http://127.0.0.1:8888` (đổi bằng `GROKTELE_PORT` trong `.env`). Tài liệu API: `http://127.0.0.1:8888/docs`.

Cổng đang bận hoặc thiếu `.env` thì server báo lỗi rõ rồi dừng hẳn (không lặp khởi động lại).

Tự chạy khi bật máy: nháy đúp `07.install-autostart.bat`.

## Nối ReelFB Studio

ReelFB → Settings → Video → provider **MultiKit**:
- URL: `http://127.0.0.1:8888`
- API key: gõ bất kỳ (GrokTele không kiểm tra key trừ khi đặt `GROKTELE_API_KEY` trong `.env`)
- Tỷ lệ: chỉ `9:16` hoặc `16:9`. Thời lượng: 6 / 10 / 15. Chất lượng: `720p` hoặc `1080p` (`480p` tự nâng lên 720p).
- Bot giữ tỷ lệ theo ảnh gốc: ảnh vuông cho video vuông dù chọn 9:16. Muốn video dọc thì dùng ảnh dọc.
- Lô lớn: ReelFB chờ mỗi clip tối đa "Timeout mỗi clip" (`video_multikit_timeout`), tính cả thời gian xếp hàng.
  Đặt **7200** trong ReelFB Settings. Số record song song bước 3 nên khoảng 2 × số tài khoản Telegram
  (mỗi record gửi 2 clip cùng lúc, mỗi tài khoản làm ~35 clip/giờ).
- Để trống "Tài khoản" (account) của MultiKit trong ReelFB, hoặc ghi đúng tên trong `accounts.yaml`.

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
| Bot báo lỗi render | Thử lại, tối đa 3 lượt rồi `failed` |
| Quá 10 phút chưa có video (`GROKTELE_VIDEO_TIMEOUT`) | Gửi `/cancel` cho bot rồi thử lại ngay. Nếu bot vẫn gửi video muộn thì dùng luôn video đó |
| Bot bảo trì / reset mạng | Trả job về hàng đợi (không mất lượt), tài khoản nghỉ 5 phút rồi hỏi `/status` |
| FloodWait của Telegram | Nghỉ đúng số giây Telegram yêu cầu |
| Mất mạng | Tự kết nối lại |
| Hết hạn gói | Tài khoản `expired`, job chuyển sang tài khoản khác. Gia hạn xong chạy `resume` |
| Session bị đăng xuất | Tài khoản `dead`. Chạy lại `login` rồi khởi động lại server |
| Tắt app giữa chừng | Mở lại, job dở được chạy lại (hoặc nhận luôn video bot gửi muộn) |
| ReelFB huỷ job khi bot đang render | Gửi `/cancel`. Bot xác nhận và 20s không còn dấu hiệu render (`GROKTELE_CANCEL_QUIET`) thì nhận job mới; nếu bot vẫn render thì chờ video đó (tối đa 10 phút) và bỏ đi, để video không bị gán nhầm job |
| ReelFB bỏ job (vd GrokTele chết, ReelFB báo lỗi clip rồi đi tiếp) | Job 5 phút không ai hỏi trạng thái (`GROKTELE_ABANDON_AFTER`) thì tự huỷ, đang render thì `/cancel`. Huỷ ngay toàn bộ: `08.cancel-all.bat` |
| Ổ đĩa sắp đầy | Ổ chứa file kết quả còn dưới 1 GB (`GROKTELE_MIN_FREE_MB`): job fail ngay với `disk_full`, không gửi ảnh cho bot; job mới bị từ chối với mã `507`. Ổ đầy giữa lúc tải: fail ngay, xoá file dở. Ổ chứa `data` còn dưới 100 MB: tạm dừng nhận job. Dung lượng trống hiện trong `05.status.bat` |
| Bấm chuột vào cửa sổ `03.start.bat` | Không còn làm server đứng: app tự tắt QuickEdit của cửa sổ khi khởi động |
| Gói của bot còn dưới 24 giờ | Cảnh báo trong log, `05.status.bat` và `GET /grok/accounts` (`expiresInHours`) |
| Không còn tài khoản dùng được | Job đang chờ fail ngay với `no_account_available` (ReelFB không phải chờ hết timeout) |

Tỷ lệ `1:1`, `2:3`, `3:4`, `4:5` được gửi bằng nút Dọc 9:16; `3:2`, `4:3`, `21:9` bằng nút Ngang 16:9.
`accountId` không có trong GrokTele bị bỏ qua (tự xoay tài khoản).

## Khi bot đổi giao diện

Mọi hiểu biết về bot nằm trong `groktele/bot_driver.py` (từ khoá tin nhắn, tên nút). Test mô phỏng bot ở
`tests/fake_bot.py`. Chạy test: `.venv\Scripts\python -m pytest`.
