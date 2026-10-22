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
