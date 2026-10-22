from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import pytest

from fake_bot import FakeChat, Scenario
from groktele.app import build_app
from groktele.config import AccountConfig, Settings
from groktele.db import DB


class Factory:
    def __init__(self, scenarios: Optional[dict[str, list[Scenario]]] = None, status: str = "active",
                 fail_connect: Optional[Exception] = None):
        self.scenarios = scenarios or {}
        self.status = status
        self.fail_connect = fail_connect
        self.chats: dict[str, FakeChat] = {}

    async def __call__(self, account: AccountConfig) -> FakeChat:
        if self.fail_connect:
            raise self.fail_connect
        chat = FakeChat(self.scenarios.get(account.name), status=self.status, name=account.name)
        self.chats[account.name] = chat
        return chat


async def _nodelay() -> None:
    return None


@asynccontextmanager
async def running(settings: Settings, factory: Factory) -> AsyncIterator[tuple[httpx.AsyncClient, DB]]:
    db = DB(settings.db_path)
    app = build_app(settings, db=db, chat_factory=factory, delay=_nodelay)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, db


async def wait_status(client: httpx.AsyncClient, job_id: str, statuses: tuple[str, ...], timeout: float = 8.0) -> dict:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        job = (await client.get(f"/grok/jobs/{job_id}")).json()
        if job["status"] in statuses:
            return job
        await asyncio.sleep(0.03)
    raise AssertionError(f"job {job_id} kẹt ở {job}")


async def wait_account(client: httpx.AsyncClient, name: str, status: str, timeout: float = 5.0) -> dict:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        accs = {a["id"]: a for a in (await client.get("/grok/accounts")).json()["accounts"]}
        if accs.get(name, {}).get("status") == status:
            return accs[name]
        await asyncio.sleep(0.03)
    raise AssertionError(f"{name} không về {status}: {accs.get(name)}")


async def test_health(settings: Settings) -> None:
    async with running(settings, Factory()) as (client, _):
        r = await client.get("/health")
        assert r.json()["ok"] is True and r.json()["app"] == "GrokTele"


async def test_job_end_to_end_multikit_shape(settings: Settings, image: Path, tmp_path: Path) -> None:
    out = tmp_path / "reelfb" / "phase_01_01.mp4"
    async with running(settings, Factory()) as (client, _):
        r = await client.post("/grok/jobs", json={
            "imagePath": str(image), "prompt": "camera zoom", "aspectRatio": "16:9", "duration": 15,
            "resolution": "480p", "mode": "custom", "outputPath": str(out), "priority": 0, "clientRef": "reelfb:p1",
        })
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued" and body["deduplicated"] is False
        job = await wait_status(client, body["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded", job
        assert job["result"]["filePath"] == str(out) and out.exists()
        assert job["result"]["bytes"] == out.stat().st_size and job["result"]["mimeType"] == "video/mp4"
        assert job["accountLabel"] == "acc1" and job["attempts"] == 1 and job["progress"] == 1
        assert job["input"]["resolution"] == "720p"  # 480p → 720p
        # clientRef trùng → trả job cũ
        again = (await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "camera zoom",
                                                        "clientRef": "reelfb:p1"})).json()
        assert again == {"jobId": body["jobId"], "status": "succeeded", "deduplicated": True}
        stats = (await client.get("/grok/stats")).json()
        assert stats["jobs"]["succeeded"] == 1


async def test_validation_errors(settings: Settings, image: Path) -> None:
    async with running(settings, Factory()) as (client, _):
        cases = [
            ({"imagePath": str(image)}, "prompt"),
            ({"prompt": "x"}, "imagePath"),
            ({"imagePath": "relative.png", "prompt": "x"}, "imagePath"),
            ({"imagePath": str(image), "prompt": "x", "aspectRatio": "1:1"}, "aspectRatio"),
            ({"imagePath": str(image), "prompt": "x", "duration": 7}, "duration"),
            ({"imagePath": str(image), "prompt": "x", "resolution": "4k"}, "resolution"),
            ({"imagePath": str(image), "prompt": "x", "outputPath": "C:/a.mov"}, "outputPath"),
        ]
        for body, field in cases:
            r = await client.post("/grok/jobs", json=body)
            assert r.status_code == 400, (body, r.text)
            assert r.json()["error"]["code"] == "invalid_input"
            assert r.json()["error"]["details"]["field"] == field
        r = await client.get("/grok/jobs/nope")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_base64_image_and_output_dir(settings: Settings, image: Path, tmp_path: Path) -> None:
    b64 = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()
    async with running(settings, Factory()) as (client, _):
        r = await client.post("/grok/jobs", json={"imageBase64": b64, "imageFileName": "a b.png", "prompt": "p",
                                                  "outputDir": str(tmp_path / "outdir"), "clientRef": "s/1"})
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded"
        assert Path(job["result"]["filePath"]) == tmp_path / "outdir" / "s_1.mp4"
        assert Path(job["input"]["imagePath"]).parent == settings.uploads_dir


async def test_retry_then_success(settings: Settings, image: Path) -> None:
    f = Factory({"acc1": [Scenario(render="error"), Scenario(render="error"), Scenario()]})
    async with running(settings, f) as (client, _):
        r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["attempts"] == 3


async def test_fail_after_max_attempts(settings: Settings, image: Path) -> None:
    f = Factory({"acc1": [Scenario(render="error")] * 3})
    async with running(settings, f) as (client, _):
        r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "failed" and job["attempts"] == 3
        assert job["error"]["kind"] == "unknown"
        # retry thủ công
        assert (await client.post(f"/grok/jobs/{job['jobId']}/retry")).json() == {"status": "queued"}
        job = await wait_status(client, job["jobId"], ("succeeded",))
        assert job["attempts"] == 1


async def test_maintenance_requeues_without_attempt(settings: Settings, image: Path) -> None:
    f = Factory({"acc1": [Scenario(photo="maintenance"), Scenario()]})
    async with running(settings, f) as (client, _):
        r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["attempts"] == 1
        assert f.chats["acc1"].texts.count("/status") >= 2  # hỏi lại /status sau khi hết nghỉ


async def test_expired_account_pauses_and_job_moves(settings: Settings, image: Path) -> None:
    settings.accounts = [AccountConfig("acc1", "a"), AccountConfig("acc2", "b")]
    f = Factory({"acc1": [Scenario(photo="expired")], "acc2": [Scenario(render="silent")] + [Scenario()] * 5})
    settings.video_timeout = 0.5
    async with running(settings, f) as (client, db):
        await wait_account(client, "acc1", "active")
        await wait_account(client, "acc2", "active")
        ids = [(await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": f"p{i}"})).json()["jobId"]
               for i in range(3)]
        for i in ids:
            job = await wait_status(client, i, ("succeeded", "failed"), timeout=10)
            assert job["status"] == "succeeded", job
        acc1 = await wait_account(client, "acc1", "expired")
        assert "hết hạn" in (acc1["reason"] or "")
        # resume → /status lại → active
        assert (await client.post("/grok/accounts/acc1/resume")).status_code == 200
        await wait_account(client, "acc1", "active")


async def test_409_when_no_usable_account(settings: Settings, image: Path) -> None:
    settings.accounts = []
    async with running(settings, Factory()) as (client, _):
        r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "conflict" and err["details"]["reason"] == "no_account_available"


async def test_409_when_status_expired(settings: Settings, image: Path) -> None:
    async with running(settings, Factory(status="expired")) as (client, _):
        await wait_account(client, "acc1", "expired")
        r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})
        assert r.status_code == 409 and r.json()["error"]["details"]["why"] == "needs_human"


async def test_dead_session(settings: Settings, image: Path) -> None:
    from groktele.chat import AuthRequired

    async with running(settings, Factory(fail_connect=AuthRequired("login lại"))) as (client, _):
        acc = await wait_account(client, "acc1", "dead")
        assert "login" in acc["reason"]


async def test_cancel_running_job(settings: Settings, image: Path) -> None:
    f = Factory({"acc1": [Scenario(render="silent"), Scenario()]})
    settings.video_timeout = 30
    async with running(settings, f) as (client, _):
        jid = (await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})).json()["jobId"]
        await wait_status(client, jid, ("running",))
        await asyncio.sleep(0.2)
        r = await client.delete(f"/grok/jobs/{jid}")
        assert r.json() == {"status": "cancelled"}
        assert (await client.delete(f"/grok/jobs/{jid}")).status_code == 409
        # worker rảnh lại và chạy job tiếp theo
        j2 = (await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "q"})).json()["jobId"]
        assert (await wait_status(client, j2, ("succeeded", "failed")))["status"] == "succeeded"


async def test_batch_parallel_two_accounts(settings: Settings, image: Path, tmp_path: Path) -> None:
    settings.accounts = [AccountConfig("acc1", "a"), AccountConfig("acc2", "b")]
    async with running(settings, Factory()) as (client, _):
        r = await client.post("/grok/jobs/batch", json={
            "defaults": {"duration": 6, "resolution": "1080p"},
            "jobs": [{"imagePath": str(image), "prompt": f"p{i}", "outputPath": str(tmp_path / f"o{i}.mp4")}
                     for i in range(6)],
        })
        assert r.status_code == 202
        ids = r.json()["jobIds"]
        jobs = [await wait_status(client, i, ("succeeded", "failed"), timeout=15) for i in ids]
        assert all(j["status"] == "succeeded" for j in jobs)
        assert {j["accountLabel"] for j in jobs} == {"acc1", "acc2"}
        for i, j in enumerate(jobs):  # mỗi file đúng prompt của job đó
            assert Path(j["result"]["filePath"]).read_bytes().startswith(f"VIDEO:p{i}|".encode())
        listed = (await client.get("/grok/jobs", params={"batchId": r.json()["batchId"]})).json()
        assert len(listed["jobs"]) == 6
        # batch có phần tử lỗi → 400, không tạo gì
        bad = await client.post("/grok/jobs/batch", json={"jobs": [{"imagePath": str(image), "prompt": "x"},
                                                                    {"prompt": "y"}]})
        assert bad.status_code == 400 and bad.json()["error"]["details"]["index"] == 1


async def test_api_key(settings: Settings, image: Path) -> None:
    settings.api_key = "secret"
    async with running(settings, Factory()) as (client, _):
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/grok/accounts")).status_code == 401
        assert (await client.get("/grok/accounts", headers={"Authorization": "Bearer secret"})).status_code == 200
        assert (await client.get("/grok/accounts", headers={"X-API-Key": "secret"})).status_code == 200


async def test_restart_requeues_running_job(settings: Settings, image: Path) -> None:
    f = Factory({"acc1": [Scenario(render="silent")]})
    settings.video_timeout = 30
    async with running(settings, f) as (client, _):
        jid = (await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "p"})).json()["jobId"]
        await wait_status(client, jid, ("running",))
    # app tắt giữa chừng → lần chạy sau job được làm lại và xong
    async with running(settings, Factory()) as (client, _):
        job = await wait_status(client, jid, ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["attempts"] == 1
