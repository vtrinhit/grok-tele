"""Ghép config + DB + worker + API thành 1 app, và chạy bằng uvicorn."""
from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import socket
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI

from groktele.api import create_api
from groktele.chat import BotChat, TelethonChat
from groktele.config import AccountConfig, Settings, parse_proxy
from groktele.db import DB
from groktele.worker import ChatFactory, Manager

log = logging.getLogger("groktele")


def setup_logging(settings: Settings, level: int = logging.INFO) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_groktele", False) for h in root.handlers):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = logging.handlers.RotatingFileHandler(settings.log_path, maxBytes=5 * 1024 * 1024, backupCount=5,
                                                encoding="utf-8")
    file.setFormatter(fmt)
    for h in (console, file):
        h._groktele = True  # type: ignore[attr-defined]
        root.addHandler(h)
    root.setLevel(level)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def telethon_factory(settings: Settings) -> ChatFactory:
    async def factory(account: AccountConfig) -> BotChat:
        return await TelethonChat.connect(
            session=account.session, api_id=settings.api_id, api_hash=settings.api_hash,
            bot=settings.bot, proxy=parse_proxy(account.proxy),
        )

    return factory


def cleanup_uploads(settings: Settings) -> int:
    if not settings.uploads_dir.exists():
        return 0
    cutoff = time.time() - settings.upload_ttl_days * 86400
    removed = 0
    for f in settings.uploads_dir.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def build_app(settings: Settings, *, db: Optional[DB] = None, chat_factory: Optional[ChatFactory] = None,
              delay: Optional[Callable[[], Awaitable[None]]] = None) -> FastAPI:
    db = db or DB(settings.db_path)
    manager = Manager(settings, db, chat_factory or telethon_factory(settings), delay)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        removed = cleanup_uploads(settings)
        if removed:
            log.info("Dọn %d ảnh upload cũ", removed)
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = create_api(settings, db, manager, lifespan=lifespan)
    app.state.manager = manager
    app.state.db = db
    return app


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def serve(settings: Settings) -> None:
    """Mã thoát: 2 = thiếu cấu hình, 3 = cổng đang bận. 03.start.bat dừng hẳn (không khởi động lại) khi mã >= 2."""
    import uvicorn

    setup_logging(settings)
    log.info("Thư mục dữ liệu: %s", settings.root)
    if not (settings.root / ".env").exists():
        log.error("Không thấy %s. Copy .env.example thành .env rồi điền TG_API_ID, TG_API_HASH",
                  settings.root / ".env")
        raise SystemExit(2)
    if not settings.api_id or not settings.api_hash:
        log.error("Thiếu TG_API_ID / TG_API_HASH trong %s (lấy tại https://my.telegram.org)", settings.root / ".env")
        raise SystemExit(2)
    if not _port_free(settings.host, settings.port):
        log.error("Cổng %s:%d đang bị chiếm (GrokTele đã chạy rồi? chạy 06.stop.bat). Hoặc đổi GROKTELE_PORT trong .env",
                  settings.host, settings.port)
        raise SystemExit(3)
    if not settings.accounts:
        log.warning("accounts.yaml chưa có tài khoản nào. Chạy 02.login.bat (hoặc python -m groktele login acc1)")
    pid_file = settings.data_dir / "groktele.pid"
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    atexit.register(lambda: pid_file.unlink(missing_ok=True))
    app = build_app(settings)
    log.info("GrokTele nghe tại http://%s:%d (bot @%s, %d tài khoản)", settings.host, settings.port,
             settings.bot, len(settings.accounts))
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning", access_log=False)
