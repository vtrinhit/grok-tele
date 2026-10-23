"""Job bị client bỏ rơi (vd ReelFB bỏ cuộc lúc GrokTele chết): tự huỷ thay vì render cho không ai."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fake_bot import Scenario
from test_api_worker import Factory, running, wait_status
from groktele.config import Settings
from groktele.db import DB


async def _status(db: DB, job_id: str, want: tuple[str, ...], timeout: float = 10) -> dict:
    for _ in range(int(timeout / 0.05)):
        job = db.get_job(job_id)
        if job["status"] in want:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(db.get_job(job_id))


async def test_leftover_job_without_client_is_cancelled(settings: Settings, image: Path, tmp_path: Path) -> None:
    # Job còn sót từ lần chạy trước (GrokTele chết khi đang chạy), ReelFB đã bỏ, không ai hỏi nữa.
    db = DB(settings.db_path)
    job = db.create_job(image_path=str(image), prompt="OLD", aspect="9:16", duration=10, resolution="720p",
                        output_path=str(tmp_path / "old.mp4"))
    db.ensure_account("acc1")
    db.claim_job("acc1")  # đang 'running' lúc chết
    db.close()
    settings.abandon_after = 1.0
    settings.ui_timeout = 5
    f = Factory({"acc1": [Scenario(step=0.6)]})
    async with running(settings, f) as (_, db2):
        done = await _status(db2, job["id"], ("cancelled", "succeeded", "failed"))
        assert done["status"] == "cancelled" and done["error_kind"] == "abandoned", done
        await asyncio.sleep(0.5)
        chat = f.chats.get("acc1")
        if chat is not None and any("Tạo Video" in c for c in chat.clicks):
            assert "/cancel" in chat.texts  # đã bấm Tạo Video (bot đang render) thì báo bot huỷ
        assert not (tmp_path / "old.mp4").exists()


async def test_job_still_polled_is_kept(settings: Settings, image: Path, tmp_path: Path) -> None:
    settings.abandon_after = 1.0
    settings.ui_timeout = 5
    f = Factory({"acc1": [Scenario(step=0.6)]})  # render ~3s > abandon_after, nhưng ReelFB vẫn hỏi đều
    async with running(settings, f) as (client, _):
        jid = (await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})).json()["jobId"]
        job = await wait_status(client, jid, ("succeeded", "failed", "cancelled"), timeout=15)
        assert job["status"] == "succeeded", job


async def test_cancel_all_api(settings: Settings, image: Path, tmp_path: Path) -> None:
    settings.ui_timeout = 5
    f = Factory({"acc1": [Scenario(step=0.6)] * 3})
    async with running(settings, f) as (client, db):
        ids = [(await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": f"p{i}"})).json()["jobId"]
               for i in range(3)]
        await asyncio.sleep(0.3)
        r = await client.post("/grok/jobs/cancel-all")
        assert r.json() == {"cancelled": 3}
        for jid in ids:
            assert db.get_job(jid)["status"] == "cancelled"


def test_cancel_all_cli_when_server_down(settings: Settings, image: Path, tmp_path: Path, capsys) -> None:
    from groktele.cli import _cancel_all

    settings.port = 1  # không có server
    db = DB(settings.db_path)
    for i in range(2):
        db.create_job(image_path=str(image), prompt=f"p{i}", aspect="9:16", duration=10, resolution="720p",
                      output_path=str(tmp_path / f"{i}.mp4"))
    _cancel_all(settings)
    assert "2 job" in capsys.readouterr().out
    assert all(j["status"] == "cancelled" for j in db.list_jobs())
