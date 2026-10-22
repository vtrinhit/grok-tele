"""Chống map nhầm video khi bot vẫn render job đã bị huỷ / timeout / app tắt giữa chừng.

Bot không gắn video với ảnh gốc, nên tài khoản phải chờ video về muộn của render dở trước khi gửi job mới.
"""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path

import pytest

from fake_bot import FakeChat, Scenario
from test_api_worker import Factory, running, wait_account, wait_status
from groktele.config import AccountConfig, Settings
from groktele.db import DB


def _post(client, image: Path, prompt: str, out: Path, **extra):  # noqa: ANN001
    return client.post("/grok/jobs", json={"imagePath": str(image), "prompt": prompt, "outputPath": str(out), **extra})


async def _wait_stage(client, job_id: str, stage: str) -> None:  # noqa: ANN001
    for _ in range(500):
        job = (await client.get(f"/grok/jobs/{job_id}")).json()
        if job["stage"] == stage:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{job_id} không tới stage {stage}")


async def test_cancelled_render_not_given_to_next_job(settings: Settings, image: Path, tmp_path: Path) -> None:
    # A render chậm: video của A về đúng lúc B (nếu đã chạy) đang chờ video.
    f = Factory({"acc1": [Scenario(step=0.6), Scenario(step=0.3)]})
    settings.video_timeout = 20
    settings.ui_timeout = 5
    async with running(settings, f) as (client, db):
        a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
        await _wait_stage(client, a, "generate")
        assert (await client.delete(f"/grok/jobs/{a}")).json() == {"status": "cancelled"}
        b = (await _post(client, image, "JOB-B", tmp_path / "b.mp4")).json()["jobId"]
        job_b = await wait_status(client, b, ("succeeded", "failed"), timeout=20)
        assert job_b["status"] == "succeeded"
        assert (tmp_path / "b.mp4").read_bytes().startswith(b"VIDEO:JOB-B|")
        assert not (tmp_path / "a.mp4").exists()  # video của job đã huỷ bị bỏ
        assert db.get_account("acc1")["orphan_job"] is None


async def test_timeout_rescued_by_late_video(settings: Settings, image: Path, tmp_path: Path) -> None:
    # /cancel không dừng được render: không gửi lại ảnh, dùng luôn video về muộn cho chính job đó.
    settings.video_timeout = 0.5
    settings.ui_timeout = 5
    settings.cancel_quiet = 20
    f = Factory({"acc1": [Scenario(step=0.6, cancel_kills=False)]})
    async with running(settings, f) as (client, _):
        a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
        job = await wait_status(client, a, ("succeeded", "failed"), timeout=20)
        assert job["status"] == "succeeded", job
        assert (tmp_path / "a.mp4").read_bytes().startswith(b"VIDEO:JOB-A|")
        assert len(f.chats["acc1"].photos) == 1  # không render lại


async def test_failed_job_late_video_not_given_to_next_job(settings: Settings, image: Path, tmp_path: Path) -> None:
    settings.max_attempts = 1
    settings.video_timeout = 0.5
    settings.ui_timeout = 5
    f = Factory({"acc1": [Scenario(step=0.6), Scenario(step=0.3)]})
    async with running(settings, f) as (client, _):
        a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
        assert (await wait_status(client, a, ("failed",), timeout=10))["error"]["kind"] == "timeout"
        settings.video_timeout = 20
        b = (await _post(client, image, "JOB-B", tmp_path / "b.mp4")).json()["jobId"]
        job_b = await wait_status(client, b, ("succeeded", "failed"), timeout=20)
        assert job_b["status"] == "succeeded"
        assert (tmp_path / "b.mp4").read_bytes().startswith(b"VIDEO:JOB-B|")


async def test_lost_render_released_after_grace(settings: Settings, image: Path, tmp_path: Path) -> None:
    # Bot không bao giờ trả video (mất task) → hết orphan_grace thì tài khoản nhận job mới.
    settings.max_attempts = 1
    settings.video_timeout = 0.3
    settings.orphan_grace = 1.0
    f = Factory({"acc1": [Scenario(render="silent"), Scenario()]})
    async with running(settings, f) as (client, _):
        a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
        await wait_status(client, a, ("failed",), timeout=10)
        settings.video_timeout = 10
        b = (await _post(client, image, "JOB-B", tmp_path / "b.mp4")).json()["jobId"]
        assert (await wait_status(client, b, ("succeeded", "failed"), timeout=15))["status"] == "succeeded"


async def test_orphan_survives_restart(settings: Settings, image: Path, tmp_path: Path) -> None:
    chat = FakeChat([Scenario(step=0.6)])
    chat.keep_rendering = True
    settings.video_timeout = 30
    settings.ui_timeout = 5
    async with running(settings, Factory(shared=chat)) as (client, db):
        a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
        await _wait_stage(client, a, "generate")
    # App tắt giữa lúc bot render. Mở lại: không gửi ảnh lần 2, nhận video về muộn cho đúng job A.
    assert DB(settings.db_path).get_account("acc1")["orphan_job"] == a
    async with running(settings, Factory(shared=chat)) as (client, _):
        job = await wait_status(client, a, ("succeeded", "failed"), timeout=20)
        assert job["status"] == "succeeded"
        assert (tmp_path / "a.mp4").read_bytes().startswith(b"VIDEO:JOB-A|")
        assert len(chat.photos) == 1


def test_old_database_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE accounts (name TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'connecting', reason TEXT,"
                " cooldown_until REAL, expires_at TEXT, last_check TEXT, jobs_done INTEGER NOT NULL DEFAULT 0,"
                " last_error TEXT, telegram_user TEXT)")
    con.execute("INSERT INTO accounts (name, status) VALUES ('acc1', 'active')")
    con.commit()
    con.close()
    db = DB(path)
    db.set_orphan("acc1", "job_x", 123)
    acc = db.get_account("acc1")
    assert (acc["orphan_job"], acc["orphan_baseline"], acc["status"]) == ("job_x", 123, "active")


async def test_queued_jobs_fail_fast_when_no_account_left(settings: Settings, image: Path, tmp_path: Path) -> None:
    # Tài khoản duy nhất hết hạn giữa chừng → job đang chờ fail ngay (không bắt ReelFB chờ hết timeout).
    f = Factory({"acc1": [Scenario(photo="expired")]})
    async with running(settings, f) as (client, _):
        await wait_account(client, "acc1", "active")
        ids = [(await _post(client, image, f"p{i}", tmp_path / f"{i}.mp4")).json()["jobId"] for i in range(3)]
        for jid in ids:
            job = await wait_status(client, jid, ("failed", "succeeded"), timeout=10)
            assert job["status"] == "failed" and job["error"]["kind"] == "no_account_available", job


async def test_aspect_mapping_and_unknown_account(settings: Settings, image: Path, tmp_path: Path) -> None:
    async with running(settings, Factory()) as (client, _):
        r = await _post(client, image, "x", tmp_path / "x.mp4", aspectRatio="3:2", accountId="acc01@multikit")
        assert r.status_code == 202, r.text
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["input"]["aspectRatio"] == "16:9"
        r = await _post(client, image, "y", tmp_path / "y.mp4", aspectRatio="1:1")
        assert (await client.get(f"/grok/jobs/{r.json()['jobId']}")).json()["input"]["aspectRatio"] == "9:16"


async def test_pinned_to_known_account(settings: Settings, image: Path, tmp_path: Path) -> None:
    settings.accounts = [AccountConfig("acc1", "a"), AccountConfig("acc2", "b")]
    async with running(settings, Factory()) as (client, _):
        jid = (await _post(client, image, "x", tmp_path / "x.mp4", accountId="acc2")).json()["jobId"]
        assert (await wait_status(client, jid, ("succeeded", "failed")))["accountLabel"] == "acc2"


async def test_timeout_sends_cancel_and_retries_at_once(settings: Settings, image: Path, tmp_path: Path) -> None:
    # Timeout → /cancel → bot dừng render → thử lại ngay, không chờ video về muộn.
    settings.video_timeout = 0.5
    settings.ui_timeout = 5
    settings.orphan_grace = 60  # nếu không nhờ /cancel thì sẽ phải chờ rất lâu
    f = Factory({"acc1": [Scenario(render="silent"), Scenario(step=0.05)]})
    async with running(settings, f) as (client, db):
        a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
        job = await wait_status(client, a, ("succeeded", "failed"), timeout=10)
        assert job["status"] == "succeeded" and job["attempts"] == 2, job
        chat = f.chats["acc1"]
        assert "/cancel" in chat.texts and len(chat.photos) == 2
        assert db.get_account("acc1")["orphan_job"] is None


async def test_reelfb_cancel_sends_cancel_and_frees_account(settings: Settings, image: Path, tmp_path: Path) -> None:
    settings.ui_timeout = 5
    settings.video_timeout = 30
    settings.orphan_grace = 60
    f = Factory({"acc1": [Scenario(step=0.6), Scenario(step=0.05)]})
    async with running(settings, f) as (client, _):
        a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
        await _wait_stage(client, a, "generate")
        await client.delete(f"/grok/jobs/{a}")
        b = (await _post(client, image, "JOB-B", tmp_path / "b.mp4")).json()["jobId"]
        job_b = await wait_status(client, b, ("succeeded", "failed"), timeout=8)
        assert job_b["status"] == "succeeded"
        assert (tmp_path / "b.mp4").read_bytes().startswith(b"VIDEO:JOB-B|")
        assert "/cancel" in f.chats["acc1"].texts


async def test_cancel_not_honoured_still_no_mismatch(settings: Settings, image: Path, tmp_path: Path) -> None:
    # Bot xác nhận /cancel nhưng vẫn render và gửi video: phải bỏ video đó, B nhận đúng video của B.
    settings.ui_timeout = 5
    settings.video_timeout = 30
    settings.cancel_quiet = 20
    f = Factory({"acc1": [Scenario(step=0.6, cancel_kills=False), Scenario(step=0.3)]})
    async with running(settings, f) as (client, _):
        a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
        await _wait_stage(client, a, "generate")
        await client.delete(f"/grok/jobs/{a}")
        b = (await _post(client, image, "JOB-B", tmp_path / "b.mp4")).json()["jobId"]
        job_b = await wait_status(client, b, ("succeeded", "failed"), timeout=20)
        assert job_b["status"] == "succeeded"
        assert (tmp_path / "b.mp4").read_bytes().startswith(b"VIDEO:JOB-B|")


async def test_render_marker_written_before_any_abort(settings: Settings, image: Path, tmp_path: Path) -> None:
    # 06.stop.bat dùng taskkill /F: không có bước dọn dẹp. Dấu render dở phải có trong DB ngay khi bot bắt đầu render.
    settings.ui_timeout = 5
    settings.video_timeout = 30
    f = Factory({"acc1": [Scenario(step=0.6)]})
    async with running(settings, f) as (client, db):
        a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
        await _wait_stage(client, a, "generate")
        assert db.get_account("acc1")["orphan_job"] == a
        job = await wait_status(client, a, ("succeeded", "failed"), timeout=20)
        assert job["status"] == "succeeded"
        assert db.get_account("acc1")["orphan_job"] is None  # xong thì xoá dấu


async def test_hard_kill_then_restart_uses_late_video(settings: Settings, image: Path, tmp_path: Path) -> None:
    # Mô phỏng kill cứng: DB còn job 'running' + dấu render dở, bot vẫn đang render ở phía Telegram.
    from groktele.bot_driver import JobParams, JobState, run_job

    chat = FakeChat([Scenario(step=0.6)])
    chat.keep_rendering = True
    db = DB(settings.db_path)
    job = db.create_job(image_path=str(image), prompt="JOB-A", aspect="9:16", duration=10, resolution="720p",
                        output_path=str(tmp_path / "a.mp4"))
    db.ensure_account("acc1")
    db.claim_job("acc1")
    state = JobState(on_render_start=lambda b: db.set_orphan("acc1", job["id"], b))
    t = asyncio.create_task(run_job(chat, JobParams(str(image), "JOB-A", output_path=str(tmp_path / "x.mp4")),
                                    ui_timeout=5, video_timeout=30, state=state))
    while state.render_baseline is None:
        await asyncio.sleep(0.02)
    t.cancel()  # process chết: không requeue, không dọn dẹp gì
    with contextlib.suppress(asyncio.CancelledError):
        await t
    assert db.get_job(job["id"])["status"] == "running"
    db.close()

    async with running(settings, Factory(shared=chat)) as (client, _):
        done = await wait_status(client, job["id"], ("succeeded", "failed"), timeout=20)
        assert done["status"] == "succeeded"
        assert (tmp_path / "a.mp4").read_bytes().startswith(b"VIDEO:JOB-A|")
        assert len(chat.photos) == 1  # không gửi ảnh lần 2


async def test_stalled_telegram_call_never_hangs_the_job(settings: Settings, image: Path, tmp_path: Path) -> None:
    # Tải video bị treo (kết nối chết lửng): job vẫn phải kết thúc, ReelFB nhận được trạng thái cuối.
    from groktele import chat as chat_mod

    async def hang(self, msg, path, progress=None):  # noqa: ANN001
        await asyncio.sleep(3600)

    settings.max_attempts = 1
    settings.ui_timeout = 2
    settings.video_timeout = 2
    settings.job_overhead = 1  # hạn chót tổng = 2*3 + 2 + 1 = 9s
    f = Factory({"acc1": [Scenario()]})
    FakeChat.download, orig = hang, FakeChat.download
    try:
        async with running(settings, f) as (client, _):
            a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
            job = await wait_status(client, a, ("succeeded", "failed"), timeout=20)
            assert job["status"] == "failed" and job["error"]["kind"] == "timeout", job
    finally:
        FakeChat.download = orig


async def test_io_timeout_raises_stall() -> None:
    from groktele.chat import IO_TIMEOUT, TelegramStall, _io

    IO_TIMEOUT["read"], old = 0.05, IO_TIMEOUT["read"]
    try:
        with pytest.raises(TelegramStall):
            await _io(asyncio.sleep(5), "read", "test")
    finally:
        IO_TIMEOUT["read"] = old


async def test_bot_error_then_still_renders_is_cancelled(settings: Settings, image: Path, tmp_path: Path) -> None:
    # Bot báo "Không hoàn tất tác vụ" rồi vẫn render: tool phải /cancel trước khi tạo lại,
    # và video của render bị lỗi (nếu vẫn về) không được lọt sang job khác.
    import fake_bot

    settings.ui_timeout = 5
    settings.video_timeout = 30
    old = fake_bot.RENDER_ERROR
    fake_bot.RENDER_ERROR = ("⚠️ Không hoàn tất tác vụ (loi tai file video: context deadline exceeded). "
                             "Bạn hãy gửi lại ảnh và prompt để tạo lại nhé! Mã: c188d4f8")
    f = Factory({"acc1": [Scenario(render="error"), Scenario(step=0.05), Scenario(step=0.05)]})
    try:
        async with running(settings, f) as (client, db):
            a = (await _post(client, image, "JOB-A", tmp_path / "a.mp4")).json()["jobId"]
            b = (await _post(client, image, "JOB-B", tmp_path / "b.mp4")).json()["jobId"]
            ja = await wait_status(client, a, ("succeeded", "failed"), timeout=20)
            jb = await wait_status(client, b, ("succeeded", "failed"), timeout=20)
            assert ja["status"] == jb["status"] == "succeeded"
            assert ja["attempts"] == 2
            assert (tmp_path / "a.mp4").read_bytes().startswith(b"VIDEO:JOB-A|")
            assert (tmp_path / "b.mp4").read_bytes().startswith(b"VIDEO:JOB-B|")
            assert "/cancel" in f.chats["acc1"].texts
    finally:
        fake_bot.RENDER_ERROR = old
