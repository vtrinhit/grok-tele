"""Ổ đĩa đầy: không lặp vô hạn, không tốn lượt render, báo lỗi rõ cho ReelFB."""
from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path

import pytest

from fake_bot import FakeChat, Scenario
from test_api_worker import Factory, running, wait_status
from groktele import disk
from groktele.config import Settings


def test_is_disk_full_detection() -> None:
    assert disk.is_disk_full(OSError(errno.ENOSPC, "No space left on device"))
    assert disk.is_disk_full(sqlite3.OperationalError("database or disk is full"))
    assert disk.is_disk_full(disk.DiskFullError("x"))
    wrapped = RuntimeError("download failed")
    wrapped.__cause__ = OSError(errno.ENOSPC, "full")
    assert disk.is_disk_full(wrapped)
    assert not disk.is_disk_full(ConnectionError("reset"))
    assert not disk.is_disk_full(FileNotFoundError(2, "missing"))
    if os.name == "nt":
        assert disk.is_disk_full(OSError(None, "There is not enough space on the disk", None, 112))


def test_free_mb_and_check(tmp_path: Path) -> None:
    free = disk.free_mb(tmp_path / "chua" / "tao")  # thư mục chưa tồn tại → đo thư mục cha
    assert free is not None and free > 0
    disk.check_free(tmp_path, 1)
    with pytest.raises(disk.DiskFullError):
        disk.check_free(tmp_path, free + 10_000_000)


async def test_low_space_fails_job_before_sending_photo(settings: Settings, image: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    f = Factory()
    async with running(settings, f) as (client, _):
        jid = (await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p",
                                                     "outputPath": str(tmp_path / "o.mp4")})).json()["jobId"]
        # Ổ đầy sau khi đã nhận job (vd ReelFB vừa ghi file lớn)
        monkeypatch.setattr(disk, "free_mb", lambda p: 10.0)
        settings.min_free_data_mb = 0
        job = await wait_status(client, jid, ("succeeded", "failed"), timeout=8)
        assert job["status"] == "failed" and job["error"]["kind"] == "disk_full", job
        assert f.chats["acc1"].photos == []  # không tốn lượt render của bot


async def test_disk_full_during_download_fails_once(settings: Settings, image: Path, tmp_path: Path) -> None:
    async def full(self, msg, path, progress=None):  # noqa: ANN001
        raise OSError(errno.ENOSPC, "No space left on device")

    FakeChat.download, orig = full, FakeChat.download
    try:
        f = Factory({"acc1": [Scenario()] * 5})
        async with running(settings, f) as (client, _):
            jid = (await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})).json()["jobId"]
            job = await wait_status(client, jid, ("succeeded", "failed"), timeout=8)
            assert job["status"] == "failed" and job["error"]["kind"] == "disk_full", job
            assert job["attempts"] == 1
            assert len(f.chats["acc1"].photos) == 1  # trước đây: lặp vô hạn, bot render mãi
    finally:
        FakeChat.download = orig


async def test_api_rejects_with_507_when_full(settings: Settings, image: Path, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    async with running(settings, Factory()) as (client, _):
        ok = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p", "clientRef": "r1",
                                                   "outputPath": str(tmp_path / "r1.mp4")})
        done = await wait_status(client, ok.json()["jobId"], ("succeeded",))
        monkeypatch.setattr(disk, "free_mb", lambda p: 10.0)
        r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p",
                                                  "outputPath": str(tmp_path / "x.mp4")})
        assert r.status_code == 507
        err = r.json()["error"]
        assert err["code"] == "insufficient_storage" and err["details"]["reason"] == "disk_full"
        # job đã xong với cùng clientRef vẫn trả về bình thường (ReelFB dùng lại file có sẵn)
        again = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p", "clientRef": "r1",
                                                      "outputPath": str(tmp_path / "r1.mp4")})
        assert again.status_code == 202 and again.json()["jobId"] == done["jobId"]
        accounts = (await client.get("/grok/accounts")).json()
        assert accounts["disk"] and accounts["disk"][0]["low"] is True


async def test_worker_survives_database_full(settings: Settings, image: Path, monkeypatch) -> None:
    from groktele.db import DB

    calls = {"n": 0}
    real = DB.claim_job

    def flaky(self, account):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database or disk is full")
        return real(self, account)

    monkeypatch.setattr(DB, "claim_job", flaky)
    monkeypatch.setattr("groktele.worker.asyncio.sleep", _fast_sleep)
    async with running(settings, Factory()) as (client, _):
        jid = (await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})).json()["jobId"]
        job = await wait_status(client, jid, ("succeeded", "failed"), timeout=10)
        assert job["status"] == "succeeded" and calls["n"] >= 3


_real_sleep = __import__("asyncio").sleep


async def _fast_sleep(seconds: float, *a, **k):  # noqa: ANN001
    await _real_sleep(min(seconds, 0.02))
