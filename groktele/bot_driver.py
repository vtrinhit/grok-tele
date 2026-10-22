"""Toàn bộ hiểu biết về bot @GrokKian_bot nằm ở file này. Bot đổi giao diện → chỉ sửa ở đây.

Luồng (quan sát thật 2026-10-22):
  1. gửi ảnh + caption = prompt
  2. bot gửi tin "✅ Đã nhận 1 bức ảnh kèm kịch bản" + nút: Dọc 9:16 | Ngang 16:9 / 6s | 10s | 15s /
     ⚡ 720p | ✨ 1080p Full HD / 🎬 Tạo Video (từ ảnh + prompt). Nút đang chọn có "✓"; bấm → bot sửa tin đó.
  3. bấm Tạo Video → tin tuỳ chọn bị xoá, bot gửi "⏳ Đã nhận yêu cầu Tạo Video [...]"
  4. "🎬 Đang tạo video (9:16 | 1080p): 30%" được sửa tới 99% rồi xoá
  5. bot gửi video (không reply, không caption) rồi "✅ Nhiệm vụ đã hoàn thành..."
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from groktele.chat import BotChat, BotMessage

log = logging.getLogger("groktele.driver")

MIN_VIDEO_BYTES = 5_000
CAPTION_LIMIT = 1000  # Telegram: caption ảnh tối đa 1024 ký tự (UTF-16), chừa biên an toàn
TEXT_LIMIT = 4000     # Telegram: tin nhắn tối đa 4096 ký tự


# ------------------------------------------------------------------ errors
class DriverError(Exception):
    kind = "unknown"

    def __init__(self, message: str, kind: Optional[str] = None):
        super().__init__(message)
        if kind:
            self.kind = kind


class RetryableError(DriverError):
    """Lỗi của lần chạy này (timeout, bot báo lỗi). Tính 1 lượt, retry tới max_attempts."""


class BotMaintenance(DriverError):
    """Bot bảo trì / reset mạng. Job quay lại hàng đợi, không tính lượt; tài khoản nghỉ một lúc."""

    kind = "maintenance"


class AccountExpired(DriverError):
    """Tài khoản hết hạn gói hoặc hết lượt thử."""

    kind = "quota_exceeded"


# --------------------------------------------------------------- text utils
def norm(text: str) -> str:
    """Chữ thường, NFC, bỏ emoji/ký hiệu, gộp khoảng trắng. Giữ chữ Việt, số, ':', '%', '(', ')', '|'."""
    text = unicodedata.normalize("NFC", text or "").lower()
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch.isalnum() or ch in ":%()|+/.,-" or ch.isspace():
            kept.append(ch)
        elif cat.startswith("M"):
            kept.append(ch)
        else:
            kept.append(" ")
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def is_selected(button_text: str) -> bool:
    return "✓" in button_text or "✔" in button_text or "☑" in button_text or "✅" in button_text


_PROGRESS_RE = re.compile(r"đang tạo video.*?(\d{1,3})\s*%")

# Thứ tự kiểm tra quan trọng: "hoạt động trở lại" chứa "bảo trì" nên phải xét trước.
_BACK_ONLINE = ("hoạt động trở lại", "hết bảo trì", "bảo trì xong", "đã bảo trì xong")
_MAINTENANCE = ("bảo trì", "đóng bot", "tạm đóng", "tạm dừng hoạt động")
_RESET = ("reset mạng", "gửi lại giúp", "tác vụ chưa hoàn thành")
_EXPIRED = ("zalo", "hết lượt", "đã hết hạn", "hết hạn sử dụng", "video thử thành công", "vui lòng gia hạn",
            "chưa gia hạn", "không còn lượt")
_ERROR = ("lỗi", "thất bại", "không thể tạo", "không tạo được", "failed", "error", "vi phạm", "kiểm duyệt")


def classify(text: str) -> Optional[str]:
    """Phân loại 1 tin của bot.

    Trả về: back_online | maintenance | reset | expired | accepted | options | progress | done | status | error | None
    """
    t = norm(text)
    if not t:
        return None
    if any(k in t for k in _BACK_ONLINE):
        return "back_online"
    if "đã nhận yêu cầu" in t:
        return "accepted"  # tin này có chữ "báo lỗi" trong lời dặn, phải xét trước "error"
    if "đã nhận 1 bức ảnh" in t or "bấm tạo video" in t:
        return "options"
    if _PROGRESS_RE.search(t):
        return "progress"
    if "nhiệm vụ đã hoàn thành" in t:
        return "done"
    if "làm mới trạng thái" in t or "đã hủy" in t or "đã huỷ" in t:
        return "cancelled"  # trả lời /cancel: "✅ Đã làm mới trạng thái! Bạn hãy gửi ảnh và kịch bản mới nhé."
    if "thông tin tài khoản" in t or "trạng thái:" in t:
        return "status"
    if any(k in t for k in _EXPIRED):
        return "expired"
    if any(k in t for k in _RESET):
        return "reset"
    if any(k in t for k in _MAINTENANCE):
        return "maintenance"
    if any(k in t for k in _ERROR):
        return "error"
    return None


def progress_percent(text: str) -> Optional[int]:
    m = _PROGRESS_RE.search(norm(text))
    return min(100, int(m.group(1))) if m else None


def _match(kind: str, value: str, text: str) -> bool:
    t = norm(text)
    if kind == "aspect":
        return value in t
    if kind == "duration":
        return re.search(rf"(?<!\d){int(value)}\s*s\b", t) is not None
    if kind == "resolution":
        return value.lower() in t
    if kind == "start":
        return "tạo video" in t and ("từ ảnh" in t or "prompt" in t)
    raise ValueError(kind)


def find_button(msg: BotMessage, kind: str, value: str = "") -> Optional[tuple[int, int, str]]:
    for r, row in enumerate(msg.buttons):
        for c, label in enumerate(row):
            if _match(kind, value, label):
                return r, c, label
    return None


# ------------------------------------------------------------------ driver
@dataclass
class JobParams:
    image_path: str
    prompt: str
    aspect: str = "9:16"
    duration: int = 10
    resolution: str = "720p"
    output_path: str = ""


@dataclass
class JobResult:
    path: Path
    bytes: int
    duration_ms: Optional[int]


@dataclass
class JobState:
    """Driver ghi lại để worker biết job có để lại một render dở trên bot hay không.

    render_baseline: id tin cuối trước khi gửi ảnh, có giá trị khi đã bấm "Tạo Video" (bot có thể đang render).
    render_ended: bot đã trả video hoặc báo lỗi cho render đó → không còn gì về muộn.
    """

    render_baseline: Optional[int] = None
    render_ended: bool = False
    # Gọi ngay trước khi bấm "Tạo Video" để worker ghi dấu render dở vào DB (sống sót cả khi app bị kill).
    on_render_start: Optional[Callable[[int], None]] = None

    @property
    def orphaned(self) -> bool:
        return self.render_baseline is not None and not self.render_ended


@dataclass
class StatusInfo:
    active: Optional[bool]
    expires_at: Optional[str]
    text: str


StageCallback = Callable[[str, float], None]
Delay = Callable[[], Awaitable[None]]


async def human_delay(lo: float = 0.6, hi: float = 1.5) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def _no_delay() -> None:
    return None


class _Waiter:
    """Đọc sự kiện từ chat, bỏ qua tin cũ hơn mốc, phát hiện tín hiệu chung (bảo trì, hết hạn...)."""

    def __init__(self, chat: BotChat, baseline: int, state: Optional[JobState] = None):
        self.chat = chat
        self.baseline = baseline
        self.accepted = False
        self.state = state

    async def wait(self, want: Callable[[BotMessage, Optional[str]], bool], timeout: float, what: str,
                   on_message: Optional[Callable[[BotMessage, Optional[str]], None]] = None) -> BotMessage:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RetryableError(f"Quá {int(timeout)}s không thấy {what}", kind="timeout")
            try:
                ev = await self.chat.next_event(remaining)
            except asyncio.TimeoutError:
                continue
            msg = ev.msg
            if msg.id <= self.baseline:
                continue  # tin của job trước (video về muộn, tin % cũ...)
            cls = classify(msg.text)
            if on_message:
                on_message(msg, cls)
            if want(msg, cls):
                return msg
            if cls == "expired":
                raise AccountExpired(f"Bot báo hết hạn: {msg.text[:200]}")
            if cls == "maintenance":
                raise BotMaintenance(f"Bot đang bảo trì: {msg.text[:200]}")
            if cls == "reset":
                raise BotMaintenance(f"Bot reset, cần gửi lại: {msg.text[:200]}", kind="reset")
            if cls == "error" and self.accepted and ev.kind == "new":
                if self.state is not None:
                    self.state.render_ended = True  # bot đã kết thúc render này bằng lỗi
                raise RetryableError(f"Bot báo lỗi: {msg.text[:300]}", kind="unknown")


async def _ensure_option(chat: BotChat, options_id: int, kind: str, value: str, delay: Delay) -> None:
    for attempt in range(3):
        msg, btn = None, None
        for _ in range(10):  # nút có thể được gắn trễ một chút
            msg = await chat.get_message(options_id)
            if msg is None:
                raise RetryableError("Bảng tuỳ chọn biến mất trước khi bấm Tạo Video")
            btn = find_button(msg, kind, value)
            if btn is not None:
                break
            await asyncio.sleep(0.5)
        if btn is None:
            raise RetryableError(f"Không thấy nút {kind}={value} trong bảng tuỳ chọn: {msg.buttons if msg else None}")
        row, col, label = btn
        if is_selected(label):
            return
        await delay()
        await chat.click(options_id, row, col)
        for _ in range(15):  # bot sửa tin sau ~0.5–2s
            await asyncio.sleep(0.4)
            msg = await chat.get_message(options_id)
            if msg is None:
                raise RetryableError("Bảng tuỳ chọn biến mất trước khi bấm Tạo Video")
            btn = find_button(msg, kind, value)
            if btn and is_selected(btn[2]):
                return
        log.warning("Nút %s=%s chưa được chọn sau lần bấm %d, bấm lại", kind, value, attempt + 1)
    raise RetryableError(f"Bấm nút {kind}={value} 3 lần mà bot không chọn")


async def run_job(chat: BotChat, p: JobParams, *, ui_timeout: float = 60.0, video_timeout: float = 900.0,
                  on_stage: Optional[StageCallback] = None, delay: Optional[Delay] = None,
                  state: Optional[JobState] = None) -> JobResult:
    """Chạy trọn 1 job trên 1 tài khoản. Raise DriverError (hoặc lỗi Telethon) khi thất bại.

    `state` cho worker biết job có để lại render dở không (khi bị huỷ / timeout / mất mạng sau khi bấm Tạo Video).
    """
    stage = on_stage or (lambda s, p_: None)
    delay = delay or _no_delay
    output = Path(p.output_path)
    state = state if state is not None else JobState()

    chat.drain_events()
    baseline = await chat.last_message_id()
    waiter = _Waiter(chat, baseline, state)

    stage("upload", 0.0)
    if len(p.prompt) <= CAPTION_LIMIT:
        await chat.send_photo(p.image_path, p.prompt)
    else:
        # Caption ảnh tối đa 1024 ký tự. Bot cũng nhận kiểu "gửi ảnh, rồi nhắn kịch bản" (đúng hướng dẫn của bot).
        await chat.send_photo(p.image_path, "")
        await delay()
        await chat.send_text(p.prompt[:TEXT_LIMIT])

    # Bot gửi chữ "Đã nhận 1 bức ảnh..." trước rồi mới sửa tin để gắn nút → chờ tới khi có nút Tạo Video.
    options = await waiter.wait(lambda m, c: find_button(m, "start") is not None, ui_timeout, "bảng tuỳ chọn")
    stage("options", 0.05)
    await _ensure_option(chat, options.id, "aspect", p.aspect, delay)
    await _ensure_option(chat, options.id, "duration", str(p.duration), delay)
    await _ensure_option(chat, options.id, "resolution", p.resolution, delay)

    current = await chat.get_message(options.id)
    start = find_button(current, "start") if current else None
    if not start:
        raise RetryableError("Không thấy nút Tạo Video")
    await delay()
    # Từ đây bot có thể đang render: nếu job dừng giữa chừng, video sẽ về muộn.
    state.render_baseline = baseline
    if state.on_render_start is not None:
        state.on_render_start(baseline)
    await chat.click(options.id, start[0], start[1])

    await waiter.wait(lambda m, c: c == "accepted", ui_timeout, "xác nhận 'Đã nhận yêu cầu'")
    waiter.accepted = True
    stage("generate", 0.1)

    def _progress(msg: BotMessage, cls: Optional[str]) -> None:
        pct = progress_percent(msg.text) if cls == "progress" else None
        if pct is not None:
            stage("generate", 0.1 + 0.8 * pct / 100)

    video = await waiter.wait(lambda m, c: m.has_video, video_timeout, "video", on_message=_progress)
    stage("download", 0.92)

    def _dl(frac: float) -> None:
        stage("download", 0.92 + 0.08 * frac)

    size = await chat.download(video, output, _dl)
    state.render_ended = True
    if size < MIN_VIDEO_BYTES:
        raise RetryableError(f"File video quá nhỏ ({size} bytes)")
    return JobResult(path=output, bytes=size, duration_ms=video.video_duration_ms)


async def find_orphan_outcome(chat: BotChat, baseline: int, cancel_at: Optional[float] = None,
                              cancel_quiet: float = 45.0, now: Optional[float] = None) -> tuple[str, Optional[BotMessage]]:
    """Render dở bắt đầu sau tin `baseline` đã có kết quả chưa (đọc lịch sử chat).

    Trả về ("video", msg) khi video đã về, ("ended", None) khi bot báo lỗi / reset / hoàn thành mà không có video,
    ("pending", None) khi còn đang render.

    `cancel_at`: thời điểm bot xác nhận /cancel. Nếu sau đó `cancel_quiet` giây không có dấu hiệu render
    (tin % mới hoặc được sửa, video) thì coi như render đã bị huỷ.
    """
    import time as _time

    accepted = False
    alive_after_cancel = False
    for msg in await chat.history_after(baseline):
        if msg.has_video:
            return "video", msg
        cls = classify(msg.text)
        if cls == "accepted":
            accepted = True
        elif cls == "reset" or (cls in ("done", "error") and accepted):
            # "done"/lỗi chỉ tính khi sau "Đã nhận yêu cầu" của chính render này (tránh tin "done" của job trước).
            return "ended", None
        if cancel_at is not None and cls == "progress" and msg.ts > cancel_at + 3:
            alive_after_cancel = True  # bot vẫn cập nhật % sau /cancel → render chưa bị huỷ
    if cancel_at is not None and not alive_after_cancel:
        if (now if now is not None else _time.time()) - cancel_at >= cancel_quiet:
            return "ended", None
    return "pending", None


async def send_cancel(chat: BotChat, timeout: float = 15.0) -> bool:
    """Gửi /cancel ("làm mới trạng thái" của bot). True khi bot xác nhận."""
    baseline = await chat.last_message_id()
    await chat.send_text("/cancel")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        for msg in await chat.history_after(baseline):
            if classify(msg.text) == "cancelled":
                return True
    return False


_EXPIRES_RE = re.compile(r"hết hạn lúc:\s*([0-9:]+\s+[0-9/]+)")


def hours_left(expires_at: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """'18:24 25/10/2026' (giờ Việt Nam, UTC+7) → số giờ còn lại; None nếu không đọc được."""
    import time as _time
    from datetime import datetime, timedelta, timezone

    if not expires_at:
        return None
    try:
        dt = datetime.strptime(expires_at.strip(), "%H:%M %d/%m/%Y").replace(tzinfo=timezone(timedelta(hours=7)))
    except ValueError:
        return None
    return round((dt.timestamp() - (now if now is not None else _time.time())) / 3600, 1)


async def check_status(chat: BotChat, timeout: float = 30.0) -> StatusInfo:
    """Gửi /status, đọc hạn dùng. active=None khi bot không trả lời (bảo trì, mạng)."""
    chat.drain_events()
    baseline = await chat.last_message_id()
    await chat.send_text("/status")
    waiter = _Waiter(chat, baseline)
    try:
        msg = await waiter.wait(lambda m, c: c in ("status", "expired"), timeout, "trả lời /status")
    except (RetryableError, BotMaintenance) as exc:
        return StatusInfo(active=None, expires_at=None, text=str(exc))
    except AccountExpired as exc:
        return StatusInfo(active=False, expires_at=None, text=str(exc))
    t = norm(msg.text)
    m = _EXPIRES_RE.search(t)
    expires = m.group(1) if m else None
    if "còn hạn" in t or "không giới hạn" in t:
        return StatusInfo(active=True, expires_at=expires, text=msg.text)
    return StatusInfo(active=False, expires_at=expires, text=msg.text)
