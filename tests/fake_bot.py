"""FakeChat: mô phỏng @GrokKian_bot đúng như quan sát thật (2026-10-22), để test không cần Telegram."""
from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from groktele.chat import BotMessage, ChatEvent

ACCEPTED = ("⏳ Đã nhận yêu cầu Tạo Video [1 Ảnh | Tỷ lệ {aspect} | {duration}s | {res}]. Bot sẽ gửi video tại đây "
            "ngay khi hoàn tất!\n\n🤖 Bot là AI nên có thể mắc sai sót.\n🔄 Nếu quá trình tạo video báo lỗi, bạn "
            "hãy gửi lại ảnh và prompt để tạo lại nhé.")
OPTIONS = ("✅ Đã nhận 1 bức ảnh kèm kịch bản.\n📐 Tỷ lệ: {aspect} | ⏱ Thời lượng: {duration}s | 🎞 Chất lượng: {res}\n"
           "👇 Hãy chọn thông số rồi bấm Tạo Video bên dưới:")
STATUS_ACTIVE = ("👤 THÔNG TIN TÀI KHOẢN\n🆔 ID: 1790523934\n✅ Trạng thái: Còn hạn sử dụng\n📅 Hết hạn lúc: 18:24 "
                 "25/10/2026 (giờ Việt Nam)\n♾ Tạo Video AI (720p & 1080p) không giới hạn!")
STATUS_EXPIRED = "👤 THÔNG TIN TÀI KHOẢN\n🆔 ID: 1790523934\n❌ Trạng thái: Đã hết hạn sử dụng"
MAINTENANCE = "🛠 Hiện đang đóng bot để bảo trì, bot sẽ quay trở lại ngay đây thôi"
EXPIRED = ("🎉 Bạn đã nhận video thử thành công! Để tiếp tục sử dụng, vui lòng gia hạn. Nhập Tên Zalo của bạn để "
           "admin liên hệ.")
RENDER_ERROR = "❌ Có lỗi xảy ra khi tạo video. Bạn hãy gửi lại ảnh và prompt nhé."
DONE = "✅ Nhiệm vụ đã hoàn thành, nếu bạn muốn tạo thêm hãy gửi nhé!"
POLICY = ("⚠️ Nội dung yêu cầu vi phạm chính sách của Grok nên không thể tạo video.\n"
          "📝 Vui lòng chỉnh sửa prompt hoặc ảnh rồi thử lại nhé.")
CANCELLED = "✅ Đã làm mới trạng thái! Bạn hãy gửi ảnh và kịch bản mới nhé. 🎬"


# Popup thật (28/10) khi bấm Tạo Video lúc bot sắp khởi động lại.
RESTARTING = "🔄 Hệ thống đang chuẩn bị khởi động lại khi hết tác vụ, bạn vui lòng bấm lại sau khoảng 30 giây nhé!"


class CaptionTooLong(Exception):
    """Giống telethon MediaCaptionTooLongError."""


@dataclass
class Scenario:
    photo: str = "options"       # options | maintenance | expired | silent
    render: str = "video"        # video | error | policy | silent | maintenance
    ignore_clicks: bool = False  # bot không phản hồi khi bấm tuỳ chọn
    video_bytes: int = 20_000
    step: float = 0.01           # độ trễ giữa các tin của bot
    cancel_kills: bool = True    # /cancel có dừng render đang chạy không
    start_answer: str = ""       # popup khi bấm Tạo Video; RESTARTING = bot từ chối, không render


class FakeChat:
    def __init__(self, scenarios: Optional[list[Scenario]] = None, status: str = "active", name: str = "acc1"):
        self.scenarios = list(scenarios or [])
        self.default = Scenario()
        self.status = status  # active | expired | silent
        self.name = name
        self.next_id = 1000
        self.messages: dict[int, BotMessage] = {}
        self.queue: asyncio.Queue[ChatEvent] = asyncio.Queue()
        self.photos: list[tuple[str, str]] = []
        self.texts: list[str] = []
        self.clicks: list[str] = []
        self.closed = False
        self.sel = {"aspect": "9:16", "duration": "10", "res": "720p"}
        self._tasks: list[asyncio.Task] = []
        self._current: Optional[Scenario] = None
        self.keep_rendering = False
        self._prompt = ""

    # ------------------------------------------------------------ helpers
    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def bot_says(self, text: str = "", buttons: Optional[list[list[str]]] = None, video: bool = False,
                 prompt: Optional[str] = None) -> BotMessage:
        m = BotMessage(id=self._id(), text=text, buttons=buttons or [], has_video=video,
                       video_duration_ms=10_000 if video else None, ts=time.time())
        if video:
            m.raw = self._prompt if prompt is None else prompt
        self.messages[m.id] = m
        self.queue.put_nowait(ChatEvent("new", copy.deepcopy(m)))
        return m

    def _edit(self, m: BotMessage) -> None:
        m.ts = time.time()
        self.messages[m.id] = m
        self.queue.put_nowait(ChatEvent("edit", copy.deepcopy(m)))

    def _keyboard(self) -> list[list[str]]:
        def mark(label: str, on: bool) -> str:
            return f"✓ {label}" if on else label

        s = self.sel
        return [
            [mark("📐 Dọc 9:16", s["aspect"] == "9:16"), mark("📐 Ngang 16:9", s["aspect"] == "16:9")],
            [mark(f"{d}s", s["duration"] == d) for d in ("6", "10", "15")],
            [mark("⚡ 720p", s["res"] == "720p"), mark("✨ 1080p Full HD", s["res"] == "1080p")],
            ["🎬 Tạo Video (từ ảnh + prompt)"],
        ]

    def _options_text(self) -> str:
        return OPTIONS.format(aspect=self.sel["aspect"], duration=self.sel["duration"], res=self.sel["res"])

    def _later(self, coro_fn: Callable[[], object]) -> None:
        self._tasks.append(asyncio.ensure_future(coro_fn()))  # type: ignore[arg-type]

    # ------------------------------------------------------------ BotChat
    async def last_message_id(self) -> int:
        return self.next_id

    async def send_photo(self, path: str, caption: str) -> int:
        assert Path(path).exists(), path
        if len(caption) > 1024:
            raise CaptionTooLong("The caption is too long (caused by SendMediaRequest)")
        self.photos.append((path, caption))
        self._prompt = caption
        my_id = self._id()
        sc = self.scenarios.pop(0) if self.scenarios else self.default
        self._current = sc
        if not caption:
            self._awaiting_prompt = True  # bot chờ tin nhắn kịch bản
            return my_id
        self._reply_photo(sc)
        return my_id

    def _reply_photo(self, sc: Scenario) -> None:
        async def reply() -> None:
            await asyncio.sleep(sc.step)
            if sc.photo == "options":
                # Bot thật: gửi chữ trước, ~0.5s sau mới sửa tin để gắn nút.
                m = self.bot_says(self._options_text())
                await asyncio.sleep(sc.step * 3)
                self._edit(BotMessage(id=m.id, text=m.text, buttons=self._keyboard()))
            elif sc.photo == "maintenance":
                self.bot_says(MAINTENANCE)
            elif sc.photo == "expired":
                self.bot_says(EXPIRED)

        self._later(reply)

    async def send_text(self, text: str) -> int:
        self.texts.append(text)
        my_id = self._id()
        if text == "/cancel":
            rt = getattr(self, "_render_task", None)
            if rt is not None and not rt.done() and getattr(self, "_render_sc", self.default).cancel_kills:
                rt.cancel()
                for mid, m in list(self.messages.items()):
                    if "Đang tạo video" in (m.text or ""):
                        del self.messages[mid]
            self._awaiting_prompt = False

            async def ack() -> None:
                await asyncio.sleep(0.005)
                self.bot_says(CANCELLED)

            self._later(ack)
            return my_id
        if getattr(self, "_awaiting_prompt", False) and not text.startswith("/"):
            assert len(text) <= 4096
            self._awaiting_prompt = False
            self._prompt = text
            self._reply_photo(self._current or self.default)
            return my_id
        if text == "/status" and self.status != "silent":
            async def reply() -> None:
                await asyncio.sleep(0.005)
                self.bot_says(STATUS_ACTIVE if self.status == "active" else STATUS_EXPIRED)

            self._later(reply)
        return my_id

    async def next_event(self, timeout: float) -> ChatEvent:
        return await asyncio.wait_for(self.queue.get(), timeout=max(0.01, timeout))

    def drain_events(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()

    async def get_message(self, msg_id: int) -> Optional[BotMessage]:
        m = self.messages.get(msg_id)
        return copy.deepcopy(m) if m else None

    async def history_after(self, min_id: int) -> list[BotMessage]:
        return [copy.deepcopy(self.messages[i]) for i in sorted(self.messages) if i > min_id]

    async def click(self, msg_id: int, row: int, col: int) -> Optional[str]:
        m = self.messages.get(msg_id)
        if m is None:
            raise LookupError("message deleted")
        label = m.buttons[row][col]
        self.clicks.append(label)
        sc = self._current or self.default
        if "Tạo Video" in label:
            if sc.start_answer == RESTARTING:
                return RESTARTING  # bảng tuỳ chọn giữ nguyên, không render
            del self.messages[msg_id]
            self._render_task = asyncio.ensure_future(self._render(sc))
            self._render_sc = sc
            self._tasks.append(self._render_task)
            return sc.start_answer or "Đã nhận yêu cầu tạo Video!"
        if sc.ignore_clicks:
            return None
        if row == 0:
            self.sel["aspect"] = "9:16" if col == 0 else "16:9"
        elif row == 1:
            self.sel["duration"] = ("6", "10", "15")[col]
        elif row == 2:
            self.sel["res"] = "720p" if col == 0 else "1080p"
        self._edit(BotMessage(id=msg_id, text=self._options_text(), buttons=self._keyboard()))
        return None

    async def _render(self, sc: Scenario) -> None:
        s = dict(self.sel)
        prompt = self._prompt  # video mang đúng prompt của lần render này, kể cả khi về muộn
        await asyncio.sleep(sc.step)
        self.bot_says(ACCEPTED.format(aspect=s["aspect"], duration=s["duration"], res=s["res"]))
        if sc.render == "silent":
            return
        if sc.render == "maintenance":
            await asyncio.sleep(sc.step)
            self.bot_says(MAINTENANCE)
            return
        prog = self.bot_says(f"🎬 Đang tạo video ({s['aspect']} | {s['res']}): 0%")
        if sc.render == "policy":  # bot thật: dừng ở ~20%, xoá tin % rồi báo vi phạm
            await asyncio.sleep(sc.step)
            self.messages.pop(prog.id, None)
            self.bot_says(POLICY)
            return
        for pct in (30, 60, 99):
            await asyncio.sleep(sc.step)
            prog.text = f"🎬 Đang tạo video ({s['aspect']} | {s['res']}): {pct}%"
            self._edit(copy.deepcopy(prog))
        self.messages.pop(prog.id, None)
        await asyncio.sleep(sc.step)
        if sc.render == "error":
            self.bot_says(RENDER_ERROR)
            return
        self._video_bytes = sc.video_bytes
        self.bot_says(video=True, prompt=prompt)
        await asyncio.sleep(sc.step)
        self.bot_says(DONE)

    async def download(self, msg: BotMessage, path: Path, progress=None) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        size = getattr(self, "_video_bytes", 20_000)
        payload = f"VIDEO:{msg.raw}|".encode("utf-8")
        path.write_bytes(payload + b"0" * max(0, size - len(payload)))
        if progress:
            progress(1.0)
        return path.stat().st_size

    async def close(self) -> None:
        self.closed = True
        if self.keep_rendering:
            return  # bot thật vẫn render dù client ngắt kết nối
        for t in self._tasks:
            t.cancel()
