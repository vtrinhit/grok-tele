"""Nhiều bot song song: 1 tài khoản Telegram × N bot = N luồng, chung 1 kết nối Telegram."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fake_bot import Scenario
from groktele.chat import ClientPool
from groktele.config import AccountConfig, Settings, load_accounts, load_settings, save_account
from test_api_worker import Factory, running, wait_account, wait_status

BOT2 = "GrokKian2_bot"
LANE2 = f"acc1@{BOT2}"


@pytest.fixture
def two_bots(settings: Settings) -> Settings:
    settings.bots = ["GrokKian_bot", BOT2]
    return settings


def test_lanes_keep_the_account_name_for_the_main_bot(two_bots: Settings) -> None:
    two_bots.accounts.append(AccountConfig(name="acc2", session="s2", bots=[BOT2]))
    lanes = two_bots.expand_lanes()
    assert [(a.name, a.bot, a.account) for a in lanes] == [
        ("acc1", "GrokKian_bot", "acc1"),  # tên cũ → database và accountId cũ vẫn dùng được
        (LANE2, BOT2, "acc1"),
        (f"acc2@{BOT2}", BOT2, "acc2"),    # tài khoản tự chọn bot riêng
    ]
    assert all(a.session for a in lanes)


def test_bot_list_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GROKTELE_BOT", "GROKTELE_BOTS"):
        monkeypatch.delenv(var, raising=False)
    assert load_settings(tmp_path).bot_list == ["GrokKian_bot", BOT2]  # mặc định: cả 2 bot
    monkeypatch.setenv("GROKTELE_BOT", "@GrokKian_bot")
    s = load_settings(tmp_path)
    assert s.bot_list == ["GrokKian_bot"] and s.bot == "GrokKian_bot"  # cấu hình cũ: vẫn 1 bot
    monkeypatch.setenv("GROKTELE_BOTS", "@GrokKian2_bot, GrokKian_bot,grokkian2_bot")
    s = load_settings(tmp_path)
    assert s.bot_list == [BOT2, "GrokKian_bot"] and s.bot == BOT2


def test_relogin_keeps_account_bots(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    save_account(path, AccountConfig(name="a", session="s1", bots=[BOT2]))
    save_account(path, AccountConfig(name="a", session="s2"))  # login lại
    save_account(path, AccountConfig(name="b", session="s3"))
    a, b = load_accounts(path)
    assert (a.session, a.bots, b.bots) == ("s2", [BOT2], None)


async def test_two_bots_render_in_parallel_on_one_account(two_bots: Settings, image: Path) -> None:
    slow = Scenario(step=0.15)
    f = Factory({"acc1": [slow], LANE2: [slow]})
    async with running(two_bots, f) as (client, _):
        ids = [(await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": f"p{i}"})).json()["jobId"]
               for i in range(2)]
        # Cả 2 job chạy cùng lúc, mỗi job 1 luồng.
        loop = asyncio.get_running_loop()
        end = loop.time() + 5
        while loop.time() < end:
            accs = (await client.get("/grok/accounts")).json()
            if accs["summary"]["runningJobs"] == 2:
                break
            await asyncio.sleep(0.02)
        assert accs["summary"]["runningJobs"] == 2, accs
        jobs = [await wait_status(client, i, ("succeeded", "failed")) for i in ids]
        assert [j["status"] for j in jobs] == ["succeeded", "succeeded"]
        assert {j["accountId"] for j in jobs} == {"acc1", LANE2}
        assert len(f.chats["acc1"].photos) == 1 and len(f.chats[LANE2].photos) == 1

        accs = (await client.get("/grok/accounts")).json()
        by_id = {a["id"]: a for a in accs["accounts"]}
        assert by_id[LANE2]["bot"] == BOT2 and by_id[LANE2]["telegramAccount"] == "acc1"
        assert by_id["acc1"]["bot"] == "GrokKian_bot"
        assert accs["summary"]["total"] == 2 and accs["summary"]["usable"] == 2
        assert accs["summary"]["telegramAccounts"] == 1


async def test_pin_to_account_uses_any_bot_and_pin_to_lane_uses_that_bot(two_bots: Settings, image: Path) -> None:
    f = Factory()
    async with running(two_bots, f) as (client, _):
        await wait_account(client, "acc1", "active")
        await wait_account(client, LANE2, "active")
        # Ghim đúng luồng bot 2.
        for i in range(3):
            r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": f"lane{i}",
                                                      "accountId": LANE2})
            job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
            assert job["status"] == "succeeded" and job["accountId"] == LANE2
        # Ghim tên tài khoản: bot chính đang tạm dừng thì bot 2 vẫn nhận.
        assert (await client.post(f"/grok/accounts/{LANE2}/pause")).status_code == 200
        r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": "acc", "accountId": "acc1"})
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["accountId"] == "acc1"
        assert (await client.post(f"/grok/accounts/{LANE2}/resume")).status_code == 200
        assert (await client.post("/grok/accounts/acc1@nope/pause")).status_code == 404


async def test_one_bot_down_other_keeps_working(two_bots: Settings, image: Path) -> None:
    class Split(Factory):
        async def __call__(self, account: AccountConfig):  # type: ignore[no-untyped-def]
            self.status = "silent" if account.bot == BOT2 else "active"  # bot 2 không trả lời /status
            return await super().__call__(account)

    async with running(two_bots, Split()) as (client, _):
        await wait_account(client, "acc1", "active")
        for i in range(2):
            r = await client.post("/grok/jobs", json={"imagePath": str(image), "prompt": f"p{i}"})
            job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
            assert job["status"] == "succeeded" and job["accountId"] == "acc1"


async def test_pause_resume_by_account_name_covers_every_bot(two_bots: Settings) -> None:
    async with running(two_bots, Factory()) as (client, _):
        await wait_account(client, LANE2, "active")
        assert (await client.post("/grok/accounts/acc1/pause")).status_code == 200
        await wait_account(client, "acc1", "paused")
        await wait_account(client, LANE2, "paused")
        assert (await client.post("/grok/accounts/acc1/resume")).status_code == 200
        await wait_account(client, "acc1", "active")
        await wait_account(client, LANE2, "active")


async def test_removed_bot_lane_is_disabled_and_hidden(two_bots: Settings) -> None:
    async with running(two_bots, Factory()) as (client, db):
        await wait_account(client, LANE2, "active")
    two_bots.bots = ["GrokKian_bot"]
    async with running(two_bots, Factory()) as (client, db):
        accs = (await client.get("/grok/accounts")).json()["accounts"]
        assert [a["id"] for a in accs] == ["acc1"]
        assert db.get_account(LANE2)["status"] == "disabled"


class FakeClient:
    def __init__(self) -> None:
        self.connected, self.alive, self.connects, self.disconnects = True, True, 1, 0

    def is_connected(self) -> bool:
        return self.connected

    async def get_me(self) -> object:
        if not self.alive:
            raise ConnectionError("dead")
        return object()

    async def connect(self) -> None:
        self.connected, self.alive = True, True
        self.connects += 1

    async def is_user_authorized(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1


async def test_client_pool_shares_one_connection_per_account() -> None:
    pool, made = ClientPool(), []

    async def make() -> FakeClient:
        await asyncio.sleep(0.01)
        made.append(FakeClient())
        return made[-1]

    a, b = await asyncio.gather(pool.acquire("acc1", make), pool.acquire("acc1", make))
    other = await pool.acquire("acc2", make)
    assert a is b and other is not a and len(made) == 2  # 1 session không bao giờ mở 2 kết nối
    await pool.release("acc1", a)
    assert a.disconnects == 0  # luồng kia còn dùng
    await pool.release("acc1", b)
    assert a.disconnects == 1
    assert await pool.acquire("acc1", make) is not a and len(made) == 3  # trả hết rồi → kết nối mới


async def test_client_pool_reconnects_a_dead_shared_connection_in_place() -> None:
    pool = ClientPool()

    async def make() -> FakeClient:
        return FakeClient()

    a = await pool.acquire("acc1", make)
    a.alive = False  # kết nối chết lửng: luồng 1 trả lại, luồng 2 vẫn giữ
    b = await pool.acquire("acc1", make)
    assert b is a and a.alive and a.connects == 2 and a.disconnects == 1
    await pool.release("acc1", a)
    await pool.release("acc1", b)
    assert a.disconnects == 2
