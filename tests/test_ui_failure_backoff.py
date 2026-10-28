"""Lỗi giao diện bot trước khi render: tài khoản nghỉ (tăng dần) thay vì gửi ảnh dồn dập cho cả hàng đợi.

Sự cố 28/10: bot đổi giao diện, mỗi lượt lỗi sau ~7s và job được nhận lại ngay → 27 ảnh trong 7 phút, mọi job fail.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fake_bot import Scenario
from groktele.config import Settings
from test_api_worker import Factory, running, wait_account, wait_status


def _job(image: Path, ref: str) -> dict:
    return {"imagePath": str(image), "prompt": "camera zoom", "clientRef": ref}


async def test_ui_failures_back_off_instead_of_flooding_the_bot(settings: Settings, image: Path) -> None:
    settings.ui_timeout = 0.2
    settings.ui_fail_cooldown = 0.6
    settings.maintenance_cooldown = 10.0
    factory = Factory({"acc1": [Scenario(photo="silent") for _ in range(20)]})
    async with running(settings, factory) as (client, db):
        for i in range(5):
            assert (await client.post("/grok/jobs", json=_job(image, f"j{i}"))).status_code == 202
        acc = await wait_account(client, "acc1", "cooldown")
        assert "Lỗi giao diện bot 1 lần" in acc["reason"]
        await asyncio.sleep(1.6)
        # Không nghỉ: ~0.25s/lượt → ~6 ảnh. Có nghỉ 0.6s rồi 1.2s → tối đa 2 ảnh.
        photos = len(factory.chats["acc1"].photos)
        assert photos <= 2, photos
        assert "Lỗi giao diện bot 2 lần" in db.get_account("acc1")["reason"]


async def test_success_resets_the_backoff(settings: Settings, image: Path) -> None:
    settings.ui_timeout = 0.2
    settings.ui_fail_cooldown = 0.2
    factory = Factory({"acc1": [Scenario(photo="silent")]})  # lượt 1 lỗi, sau đó bot bình thường
    async with running(settings, factory) as (client, _):
        first = (await client.post("/grok/jobs", json=_job(image, "a"))).json()["jobId"]
        job = await wait_status(client, first, ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["attempts"] == 2
        second = (await client.post("/grok/jobs", json=_job(image, "b"))).json()["jobId"]
        job = await wait_status(client, second, ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["attempts"] == 1


async def test_errors_after_render_start_do_not_trigger_ui_backoff(settings: Settings, image: Path) -> None:
    settings.ui_fail_cooldown = 30.0  # nếu bị áp nhầm, job thứ 2 sẽ không kịp chạy trong test
    factory = Factory({"acc1": [Scenario(render="error")]})
    async with running(settings, factory) as (client, db):
        job_id = (await client.post("/grok/jobs", json=_job(image, "r"))).json()["jobId"]
        job = await wait_status(client, job_id, ("succeeded", "failed"), timeout=15)
        assert job["status"] == "succeeded" and job["attempts"] == 2
        assert "giao diện" not in ((db.get_account("acc1") or {}).get("reason") or "")
