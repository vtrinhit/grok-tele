from __future__ import annotations

from pathlib import Path

import pytest

from groktele.config import AccountConfig, load_accounts, load_settings, parse_proxy, save_account
from groktele.db import DB


def _job(db: DB, **kw) -> dict:
    base = dict(image_path="C:/a.png", prompt="p", aspect="9:16", duration=10, resolution="720p",
                output_path="C:/o.mp4")
    base.update(kw)
    return db.create_job(**base)


def test_claim_order_priority_and_pin() -> None:
    db = DB(":memory:")
    a = _job(db)
    b = _job(db, priority=5)
    c = _job(db, pinned_account="acc2")
    assert db.claim_job("acc1")["id"] == b["id"]
    assert db.claim_job("acc1")["id"] == a["id"]
    assert db.claim_job("acc1") is None  # c ghim cho acc2
    got = db.claim_job("acc2")
    assert got["id"] == c["id"] and got["attempts"] == 1 and got["status"] == "running"


def test_requeue_refund_and_fail() -> None:
    db = DB(":memory:")
    j = _job(db)
    db.claim_job("acc1")
    db.requeue(j["id"], refund_attempt=True, kind="maintenance", message="m")
    assert db.get_job(j["id"])["attempts"] == 0
    db.claim_job("acc1")
    db.requeue(j["id"], refund_attempt=False)
    assert db.get_job(j["id"])["attempts"] == 1
    db.claim_job("acc1")
    assert db.finish_failed(j["id"], kind="timeout", message="x")
    assert db.get_job(j["id"])["status"] == "failed"
    assert db.retry(j["id"]) and db.get_job(j["id"])["attempts"] == 0


def test_client_ref_dedupe_and_cancel() -> None:
    db = DB(":memory:")
    j = _job(db, client_ref="r1")
    assert db.find_active_by_client_ref("r1")["id"] == j["id"]
    assert db.cancel(j["id"])
    assert db.find_active_by_client_ref("r1") is None
    assert not db.cancel(j["id"])


def test_success_only_when_running_and_stats() -> None:
    db = DB(":memory:")
    j = _job(db)
    db.ensure_account("acc1")
    db.claim_job("acc1")
    db.cancel(j["id"])
    assert not db.finish_success(j["id"], result_bytes=10, duration_ms=1)
    k = _job(db)
    db.claim_job("acc1")
    assert db.finish_success(k["id"], result_bytes=10, duration_ms=1)
    assert db.get_account("acc1")["jobs_done"] == 1
    s = db.stats()
    assert s["jobs"]["succeeded"] == 1 and s["last24h"]["successRate"] == 1.0


def test_reset_running(tmp_path: Path) -> None:
    db = DB(tmp_path / "x.db")
    j = _job(db)
    db.claim_job("acc1")
    assert db.reset_running() == 1
    assert db.get_job(j["id"])["status"] == "queued"


def test_parse_proxy() -> None:
    assert parse_proxy(None) is None
    p = parse_proxy("socks5://u:p%40ss@1.2.3.4:1080")
    assert p == {"proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080, "rdns": True, "username": "u",
                 "password": "p@ss"}
    assert parse_proxy("1.2.3.4:1080")["proxy_type"] == "socks5"
    with pytest.raises(ValueError):
        parse_proxy("ftp://1.2.3.4:21")


def test_save_and_load_accounts_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "accounts.yaml"
    save_account(f, AccountConfig("acc1", "S1"))
    save_account(f, AccountConfig("acc2", "S2", proxy="socks5://h:1"))
    save_account(f, AccountConfig("acc1", "S1b"))
    accs = {a.name: a for a in load_accounts(f)}
    assert accs["acc1"].session == "S1b" and accs["acc2"].proxy == "socks5://h:1"
    for k in ("TG_API_ID", "TG_API_HASH", "GROKTELE_PORT"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text("TG_API_ID=42\nTG_API_HASH=abc  # comment\nGROKTELE_PORT=18000\n", encoding="utf-8")
    s = load_settings(tmp_path)
    assert (s.api_id, s.api_hash, s.port, len(s.accounts)) == (42, "abc", 18000, 2)


def test_env_with_bom_and_ansi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("TG_API_ID", "TG_API_HASH"):
        monkeypatch.delenv(k, raising=False)
    # Notepad trên Windows Server hay lưu UTF-8 có BOM; comment tiếng Việt kiểu ANSI không được làm crash.
    (tmp_path / ".env").write_bytes(b"\xef\xbb\xbfTG_API_ID=77\r\n# ch\xfa th\xedch\r\nTG_API_HASH=hh\r\n")
    (tmp_path / "accounts.yaml").write_bytes(b"\xef\xbb\xbf- name: acc1\r\n  session: S\r\n")
    s = load_settings(tmp_path)
    assert (s.api_id, s.api_hash, [a.name for a in s.accounts]) == (77, "hh", ["acc1"])


def test_serve_exits_3_when_port_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from groktele import app as app_mod

    (tmp_path / ".env").write_text("TG_API_ID=1\nTG_API_HASH=x\n", encoding="utf-8")
    s = load_settings(tmp_path)
    s.api_id, s.api_hash = 1, "x"
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        s.port = busy.getsockname()[1]
        with pytest.raises(SystemExit) as ei:
            app_mod.serve(s)
    assert ei.value.code == 3


def test_serve_exits_2_without_env(tmp_path: Path) -> None:
    from groktele import app as app_mod
    from groktele.config import Settings

    with pytest.raises(SystemExit) as ei:
        app_mod.serve(Settings(root=tmp_path))
    assert ei.value.code == 2


def test_default_port_is_8888() -> None:
    from groktele.config import Settings

    assert Settings().port == 8888


async def test_connection_reset_noise_is_suppressed(caplog: pytest.LogCaptureFixture) -> None:
    import asyncio
    import logging

    from groktele.app import quiet_connection_resets

    loop = asyncio.get_running_loop()
    old = loop.get_exception_handler()
    try:
        quiet_connection_resets(loop)
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            loop.call_exception_handler({"message": "x", "exception": ConnectionResetError(10054, "reset")})
            loop.call_exception_handler({"message": "real problem", "exception": ValueError("boom")})
        assert "reset" not in caplog.text and "real problem" in caplog.text
    finally:
        loop.set_exception_handler(old)


def test_disable_quick_edit_never_raises() -> None:
    from groktele.app import disable_quick_edit

    assert disable_quick_edit() in (True, False)  # pytest không có console thật → False, không lỗi
