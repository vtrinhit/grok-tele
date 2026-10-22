"""Lớp trừu tượng "chat với bot" + bản cài đặt Telethon.

`bot_driver` chỉ nói chuyện qua `BotChat`, nên test dùng `FakeChat` mô phỏng bot mà không cần Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

log = logging.getLogger("groktele.chat")


@dataclass
class BotMessage:
    id: int
    text: str = ""
    buttons: list[list[str]] = field(default_factory=list)
    has_video: bool = False
    video_duration_ms: Optional[int] = None
    raw: Any = None


@dataclass
class ChatEvent:
    kind: str  # "new" | "edit"
    msg: BotMessage


class BotChat(Protocol):
    async def last_message_id(self) -> int: ...
    async def send_photo(self, path: str, caption: str) -> int: ...
    async def send_text(self, text: str) -> int: ...
    async def next_event(self, timeout: float) -> ChatEvent: ...
    def drain_events(self) -> None: ...
    async def get_message(self, msg_id: int) -> Optional[BotMessage]: ...
    async def history_after(self, min_id: int) -> list[BotMessage]: ...
    async def click(self, msg_id: int, row: int, col: int) -> None: ...
    async def download(self, msg: BotMessage, path: Path,
                       progress: Optional[Callable[[float], None]] = None) -> int: ...
    async def close(self) -> None: ...


class AuthRequired(Exception):
    """Session không còn hợp lệ, cần chạy lại `login`."""


def _to_bot_message(m: Any) -> BotMessage:
    video = getattr(m, "video", None)
    doc = getattr(m, "document", None)
    mime = getattr(doc, "mime_type", "") or ""
    duration_ms = None
    for attr in getattr(doc, "attributes", None) or []:
        if hasattr(attr, "duration") and attr.duration:
            duration_ms = int(float(attr.duration) * 1000)
    return BotMessage(
        id=int(m.id),
        text=m.raw_text or m.message or "",
        buttons=[[b.text for b in row] for row in (m.buttons or [])],
        has_video=bool(video) or mime.startswith("video/"),
        video_duration_ms=duration_ms,
        raw=m,
    )


class TelethonChat:
    """Chat với bot qua 1 tài khoản Telegram (TelegramClient đã đăng nhập)."""

    def __init__(self, client: Any, bot_entity: Any):
        from telethon import events

        self.client = client
        self.bot = bot_entity
        self.bot_id = int(getattr(bot_entity, "id"))
        self._queue: asyncio.Queue[ChatEvent] = asyncio.Queue()
        self._handlers = [
            (self._on_new, events.NewMessage(chats=self.bot_id, incoming=True)),
            (self._on_edit, events.MessageEdited(chats=self.bot_id, incoming=True)),
        ]
        for handler, ev in self._handlers:
            client.add_event_handler(handler, ev)

    @classmethod
    async def connect(cls, *, session: str, api_id: int, api_hash: str, bot: str,
                      proxy: Optional[dict] = None) -> "TelethonChat":
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(session), api_id, api_hash,
            proxy=proxy,
            device_model="GrokTele",
            system_version="Windows",
            app_version="1.0",
            flood_sleep_threshold=30,  # FloodWait ngắn thì Telethon tự ngủ, dài hơn thì raise
            connection_retries=5,
            retry_delay=3,
            auto_reconnect=True,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise AuthRequired("Session không hợp lệ hoặc đã bị đăng xuất, chạy lại: python -m groktele login <tên>")
            entity = await client.get_entity(bot)
        except BaseException:
            # Không để lại kết nối treo khi thử lại nhiều lần.
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        return cls(client, entity)

    async def me(self) -> str:
        me = await self.client.get_me()
        name = " ".join(x for x in (me.first_name, me.last_name) if x)
        return f"{name} (@{me.username}, id {me.id})" if me.username else f"{name} (id {me.id})"

    async def _on_new(self, event: Any) -> None:
        self._queue.put_nowait(ChatEvent("new", _to_bot_message(event.message)))

    async def _on_edit(self, event: Any) -> None:
        self._queue.put_nowait(ChatEvent("edit", _to_bot_message(event.message)))

    def is_connected(self) -> bool:
        return bool(self.client.is_connected())

    async def last_message_id(self) -> int:
        msgs = await self.client.get_messages(self.bot, limit=1)
        return int(msgs[0].id) if msgs else 0

    async def send_photo(self, path: str, caption: str) -> int:
        m = await self.client.send_file(self.bot, path, caption=caption, force_document=False)
        return int(m.id)

    async def send_text(self, text: str) -> int:
        m = await self.client.send_message(self.bot, text)
        return int(m.id)

    async def next_event(self, timeout: float) -> ChatEvent:
        return await asyncio.wait_for(self._queue.get(), timeout=max(0.01, timeout))

    def drain_events(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    async def get_message(self, msg_id: int) -> Optional[BotMessage]:
        m = await self.client.get_messages(self.bot, ids=msg_id)
        return _to_bot_message(m) if m else None

    async def history_after(self, min_id: int) -> list[BotMessage]:
        """Tin của bot có id > min_id, cũ trước mới sau (đọc lịch sử, không phụ thuộc sự kiện đang chạy)."""
        msgs = await self.client.get_messages(self.bot, min_id=int(min_id), limit=100)
        return [_to_bot_message(m) for m in reversed(msgs) if not m.out]

    async def click(self, msg_id: int, row: int, col: int) -> None:
        from telethon.errors import BotResponseTimeoutError, MessageIdInvalidError

        m = await self.client.get_messages(self.bot, ids=msg_id)
        if not m:
            raise LookupError(f"Tin {msg_id} không còn (bot đã xoá)")
        try:
            # Bot trả lời callback bằng toast; chậm cũng không sao, ta kiểm tra kết quả qua tin đã sửa.
            await asyncio.wait_for(m.click(row, col), timeout=20)
        except (BotResponseTimeoutError, asyncio.TimeoutError):
            log.debug("callback %s/%s,%s không có trả lời, bỏ qua", msg_id, row, col)
        except MessageIdInvalidError as exc:
            raise LookupError(f"Tin {msg_id} không còn bấm được") from exc

    async def download(self, msg: BotMessage, path: Path,
                       progress: Optional[Callable[[float], None]] = None) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")

        def _cb(done: int, total: int) -> None:
            if progress and total:
                progress(done / total)

        doc = getattr(msg.raw, "document", None)
        if doc is not None:
            # Gói 512 KB (mặc định Telethon 128 KB) → ít vòng gọi hơn, tải nhanh hơn nhiều.
            await self.client.download_file(doc, str(tmp), part_size_kb=512, file_size=doc.size,
                                            progress_callback=_cb)
        else:
            await self.client.download_media(msg.raw, file=str(tmp), progress_callback=_cb)
        os.replace(tmp, path)
        return path.stat().st_size

    async def close(self) -> None:
        for handler, ev in self._handlers:
            self.client.remove_event_handler(handler, ev)
        try:
            await self.client.disconnect()
        except Exception:  # pragma: no cover - best effort
            pass
