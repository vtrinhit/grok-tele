"""Dòng lệnh: python -m groktele <lệnh>."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from groktele import __version__
from groktele.config import AccountConfig, Settings, load_settings, parse_proxy, save_account


def _need_api(settings: Settings) -> None:
    if not settings.api_id or not settings.api_hash:
        print("Thiếu TG_API_ID / TG_API_HASH trong file .env (lấy tại https://my.telegram.org).")
        print(f"Copy {settings.root / '.env.example'} thành .env rồi điền vào.")
        raise SystemExit(2)


async def _login(settings: Settings, name: str, proxy: str | None, phone: str | None) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    _need_api(settings)
    client = TelegramClient(StringSession(), settings.api_id, settings.api_hash, proxy=parse_proxy(proxy),
                            device_model="GrokTele", system_version="Windows", app_version="1.0")
    print(f"Đăng nhập tài khoản '{name}'. Telegram sẽ gửi mã vào app Telegram của bạn.")
    await client.start(
        phone=(lambda: phone) if phone else (lambda: input("Số điện thoại (dạng +84...): ").strip()),
        code_callback=lambda: input("Mã xác nhận Telegram: ").strip(),
        password=lambda: input("Mật khẩu 2FA (nếu có): "),
    )
    me = await client.get_me()
    session = client.session.save()
    try:
        await client.send_message(settings.bot, "/status")
    except Exception as exc:
        print(f"Cảnh báo: không nhắn được cho @{settings.bot}: {exc}")
    await client.disconnect()
    save_account(settings.accounts_file, AccountConfig(name=name, session=session, proxy=proxy, enabled=True))
    print(f"Đã lưu '{name}' = {me.first_name or ''} (id {me.id}) vào {settings.accounts_file}")
    print("Nếu server đang chạy, khởi động lại để nạp tài khoản mới.")


def _import_session(settings: Settings, name: str, session: str, proxy: str | None) -> None:
    save_account(settings.accounts_file, AccountConfig(name=name, session=session.strip(), proxy=proxy))
    print(f"Đã lưu '{name}' vào {settings.accounts_file}. Khởi động lại server để nạp.")


def _status(settings: Settings) -> None:
    from groktele.db import DB

    if not settings.db_path.exists():
        print("Chưa có dữ liệu (server chưa chạy lần nào).")
        return
    db = DB(settings.db_path)
    print(f"GrokTele {__version__} — {settings.db_path}")
    print("\nTài khoản:")
    accounts = db.list_accounts()
    if not accounts:
        print("  (chưa có)")
    for a in accounts:
        cd = ""
        if a["status"] == "cooldown" and a["cooldown_until"]:
            cd = f" còn {max(0, int(a['cooldown_until'] - time.time()))}s"
        print(f"  {a['name']:<12} {a['status']:<10}{cd}  xong {a['jobs_done']:>5}  hạn {a['expires_at'] or '?'}"
              f"  {a['telegram_user'] or ''}")
        if a["reason"] and a["status"] != "active":
            print(f"               lý do: {a['reason']}")
    s = db.stats()
    print("\nJob:", ", ".join(f"{k}={v}" for k, v in s["jobs"].items()))
    l24 = s["last24h"]
    rate = f"{l24['successRate'] * 100:.1f}%" if l24["successRate"] is not None else "-"
    avg = f"{l24['avgJobMs'] / 1000:.0f}s" if l24["avgJobMs"] else "-"
    print(f"24h: xong {l24['succeeded']}, lỗi {l24['failed']}, tỉ lệ {rate}, trung bình {avg}/job")


def _set_account(settings: Settings, name: str, status: str) -> None:
    from groktele.db import DB

    db = DB(settings.db_path)
    if not db.get_account(name):
        print(f"Không có tài khoản '{name}' (server đã chạy với tài khoản này chưa?)")
        raise SystemExit(1)
    db.set_account(name, status=status, reason="CLI", cooldown_until=None)
    print(f"{name} -> {status}")


async def _try(settings: Settings, name: str, image: str, prompt: str, aspect: str, duration: int,
               resolution: str, out: str | None) -> None:
    from groktele.bot_driver import JobParams, check_status, human_delay, run_job
    from groktele.chat import TelethonChat
    from groktele.config import RESOLUTION_MAP

    _need_api(settings)
    acc = next((a for a in settings.accounts if a.name == name), None)
    if not acc:
        print(f"Không có '{name}' trong accounts.yaml")
        raise SystemExit(1)
    out_path = Path(out) if out else settings.outputs_dir / f"try_{int(time.time())}.mp4"
    print("Lưu ý: tắt server trước khi chạy lệnh này (1 session không nên chạy 2 nơi cùng lúc).")
    chat = await TelethonChat.connect(session=acc.session, api_id=settings.api_id, api_hash=settings.api_hash,
                                      bot=settings.bot, proxy=parse_proxy(acc.proxy))
    try:
        print("Tài khoản:", await chat.me())
        info = await check_status(chat)
        print(f"/status: active={info.active} hết hạn={info.expires_at}")
        t0 = time.time()
        last = [""]

        def on_stage(stage: str, p: float) -> None:
            key = f"{stage} {int(p * 20)}"  # in mỗi 5%
            if key != last[0]:
                last[0] = key
                print(f"  {time.time() - t0:6.1f}s  {stage:<9} {p * 100:5.1f}%")

        result = await run_job(
            chat,
            JobParams(image_path=str(Path(image).resolve()), prompt=prompt, aspect=aspect, duration=duration,
                      resolution=RESOLUTION_MAP.get(resolution, resolution), output_path=str(out_path.resolve())),
            ui_timeout=settings.ui_timeout, video_timeout=settings.video_timeout, on_stage=on_stage,
            delay=lambda: human_delay(*settings.action_delay),
        )
        print(f"XONG sau {time.time() - t0:.0f}s: {result.path} ({result.bytes // 1024} KB)")
    finally:
        await chat.close()


def main(argv: list[str] | None = None) -> None:
    # Console Windows mặc định cp1252 → in tiếng Việt bị lỗi.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    parser = argparse.ArgumentParser(prog="python -m groktele",
                                     description="GrokTele — tạo video hàng loạt qua @GrokKian_bot")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="Đăng nhập 1 tài khoản Telegram (1 lần), lưu session vào accounts.yaml")
    p.add_argument("name")
    p.add_argument("--proxy", help="socks5://user:pass@host:port")
    p.add_argument("--phone")

    p = sub.add_parser("import-session", help="Thêm tài khoản từ session string Telethon có sẵn")
    p.add_argument("name")
    p.add_argument("session")
    p.add_argument("--proxy")

    p = sub.add_parser("serve", help="Chạy API + worker")
    p.add_argument("--port", type=int)

    sub.add_parser("status", help="Xem tài khoản và thống kê job")

    for cmd, desc in (("pause", "Tạm dừng 1 tài khoản"), ("resume", "Chạy lại tài khoản (sau khi gia hạn)")):
        p = sub.add_parser(cmd, help=desc)
        p.add_argument("name")

    p = sub.add_parser("try", help="Chạy thử 1 video trực tiếp (không qua server)")
    p.add_argument("name")
    p.add_argument("image")
    p.add_argument("prompt")
    p.add_argument("--aspect", default="9:16", choices=["9:16", "16:9"])
    p.add_argument("--duration", type=int, default=10, choices=[6, 10, 15])
    p.add_argument("--resolution", default="720p", choices=["480p", "720p", "1080p"])
    p.add_argument("--out")

    args = parser.parse_args(argv)
    settings = load_settings()

    if args.cmd == "login":
        asyncio.run(_login(settings, args.name, args.proxy, args.phone))
    elif args.cmd == "import-session":
        _import_session(settings, args.name, args.session, args.proxy)
    elif args.cmd == "serve":
        from groktele.app import serve

        if args.port:
            settings.port = args.port
        serve(settings)
    elif args.cmd == "status":
        _status(settings)
    elif args.cmd == "pause":
        _set_account(settings, args.name, "paused")
    elif args.cmd == "resume":
        _set_account(settings, args.name, "connecting")
    elif args.cmd == "try":
        asyncio.run(_try(settings, args.name, args.image, args.prompt, args.aspect, args.duration,
                         args.resolution, args.out))
