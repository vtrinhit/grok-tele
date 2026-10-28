"""Bấm Tạo Video, bot từ chối "đang chuẩn bị khởi động lại, bấm lại sau 30 giây" (thấy thật ngày 28/10).

Trước đây driver bỏ qua popup, chờ 60s 'Đã nhận yêu cầu', /cancel, chờ thêm cancel_quiet rồi mới thử lại (mất 1 lượt).
"""
from __future__ import annotations

from pathlib import Path

from fake_bot import RESTARTING, Scenario
from groktele.bot_driver import restart_refusal
from groktele.config import Settings
from test_api_worker import Factory, running, wait_status


def test_restart_refusal_parsing() -> None:
    assert restart_refusal(RESTARTING) == 30.0
    assert restart_refusal("Đã nhận yêu cầu tạo Video!") is None
    assert restart_refusal(None) is None
    assert restart_refusal("Có lỗi, bấm lại sau") is None  # thiếu số giây / không phải khởi động lại


async def test_restart_refusal_requeues_without_cancel_or_losing_an_attempt(settings: Settings, image: Path) -> None:
    settings.ui_timeout = 10.0  # nếu vẫn chờ 'Đã nhận yêu cầu' như cũ, test sẽ chậm thấy rõ
    factory = Factory({"acc1": [Scenario(start_answer=RESTARTING)]})  # lượt 1 bị từ chối, sau đó bình thường
    async with running(settings, factory) as (client, db):
        r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "camera zoom"})
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"), timeout=6)
        assert job["status"] == "succeeded", job
        assert job["attempts"] == 1  # lượt bị từ chối không tính
        chat = factory.chats["acc1"]
        assert "/cancel" not in chat.texts  # bot không render → không /cancel
        assert len(chat.photos) == 2
        assert not (db.get_account("acc1") or {}).get("orphan_job")


async def test_unknown_start_popup_keeps_old_behaviour(settings: Settings, image: Path) -> None:
    factory = Factory({"acc1": [Scenario(start_answer="⏳ Đang xử lý...")]})  # popup lạ, bot vẫn render
    async with running(settings, factory) as (client, _):
        r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "camera zoom"})
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["attempts"] == 1
        assert len(factory.chats["acc1"].photos) == 1
