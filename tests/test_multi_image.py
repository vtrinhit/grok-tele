"""Video nhiều ảnh (2–6 ảnh, album Telegram). Job 1 ảnh (ReelFB) phải giữ nguyên luồng cũ."""
from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from fake_bot import (
    ALBUM_NO_CAPTION,
    OPTIONS_MULTI,
    PAIR_FIRST_LAST,
    PAIR_REFERENCE,
    START_1,
    START_3,
    FakeChat,
    Scenario,
    start_labels,
)
from groktele.bot_driver import JobParams, RetryableError, classify, find_button, received_count, run_job
from groktele.chat import BotMessage
from groktele.config import Settings
from groktele.db import DB, job_images
from test_api_worker import Factory, running, wait_status


def _params(images: list[Path], tmp_path: Path, **kw) -> JobParams:
    paths = [str(p) for p in images]
    return JobParams(image_path=paths[0], prompt="Camera pushes in", output_path=str(tmp_path / "out" / "v.mp4"),
                     image_paths=paths if len(paths) > 1 else [], **kw)


# ------------------------------------------------------------------ parsing
def test_classify_and_count_multi_image_replies() -> None:
    text = OPTIONS_MULTI.format(n=3, aspect="9:16", duration="15")
    assert classify(text) == "options" and received_count(text) == 3
    ack = ALBUM_NO_CAPTION.format(n=2)
    assert classify(ack) == "options" and received_count(ack) == 2
    assert received_count("✅ Đã nhận 1 bức ảnh kèm kịch bản.") == 1
    assert received_count("hello") is None


def test_find_multi_image_start_buttons() -> None:
    durations = [["3s", "5s", "6s", "7s"], ["8s", "10s", "12s", "✓ 15s"]]
    two = BotMessage(id=1, buttons=[["✓ 📐 Dọc 9:16", "📐 Ngang 16:9"], *durations, [PAIR_FIRST_LAST], [PAIR_REFERENCE]])
    assert find_button(two, "pair", "firstLast")[:2] == (3, 0)
    assert find_button(two, "pair", "reference")[:2] == (4, 0)
    assert find_button(two, "start") is None and find_button(two, "resolution", "720p") is None
    three = BotMessage(id=2, buttons=[["📐 Dọc 9:16"], *durations, [START_3]])
    assert find_button(three, "start_multi", "3")[:2] == (3, 0)
    assert find_button(three, "start") is None  # nút 1 ảnh không được khớp nút nhiều ảnh
    for n in (4, 5, 6):
        msg = BotMessage(id=n, buttons=[["📐 Dọc 9:16"], *durations, start_labels(n)])
        assert find_button(msg, "start_multi", str(n))[:2] == (3, 0)
        assert find_button(msg, "start_multi", "3") is None
    one = BotMessage(id=9, buttons=[[START_1]])
    assert find_button(one, "start")[:2] == (0, 0) and find_button(one, "start_multi", "1") is None


# ------------------------------------------------------------------- driver
@pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
async def test_run_job_album_picks_start_button_by_count(images: list[Path], tmp_path: Path, n: int) -> None:
    chat = FakeChat()
    res = await run_job(chat, _params(images[:n], tmp_path, resolution="1080p"), ui_timeout=2, video_timeout=2)
    assert res.path.exists()
    assert chat.photos == [] and chat.albums == [([str(p) for p in images[:n]], "Camera pushes in")]
    expected = PAIR_REFERENCE if n == 2 else start_labels(n)[0]
    assert chat.started_with == expected
    assert chat.clicks == [expected]  # không bấm nút chất lượng (bot không có), tỷ lệ/thời lượng đã đúng mặc định


async def test_run_job_two_images_first_last(images: list[Path], tmp_path: Path) -> None:
    chat = FakeChat()
    await run_job(chat, _params(images[:2], tmp_path, image_mode="firstLast", aspect="16:9", duration=5),
                  ui_timeout=2, video_timeout=2)
    assert chat.clicks == ["📐 Ngang 16:9", "5s", PAIR_FIRST_LAST]
    assert chat.sel["aspect"] == "16:9" and chat.sel["duration"] == "5"


async def test_run_job_album_long_prompt_waits_for_ack(images: list[Path], tmp_path: Path) -> None:
    chat = FakeChat()
    prompt = "Camera pushes in slowly. " * 60  # > caption 1000 ký tự
    p = _params(images[:3], tmp_path)
    p.prompt = prompt.strip()
    await run_job(chat, p, ui_timeout=2, video_timeout=2)
    assert chat.albums[0][1] == ""
    assert chat.texts == [prompt.strip()]
    assert chat.prompt_after_ack is True
    assert chat.started_with == START_3


async def test_run_job_album_partially_received_is_retried(images: list[Path], tmp_path: Path) -> None:
    chat = FakeChat([Scenario(received=2)])
    with pytest.raises(RetryableError, match="2/3"):
        await run_job(chat, _params(images[:3], tmp_path), ui_timeout=2, video_timeout=2)
    assert chat.started_with is None  # không bấm bắt đầu khi bot thiếu ảnh


async def test_single_image_job_unchanged(images: list[Path], tmp_path: Path) -> None:
    chat = FakeChat()
    await run_job(chat, _params(images[:1], tmp_path, aspect="16:9", duration=15, resolution="1080p"),
                  ui_timeout=2, video_timeout=2)
    assert chat.albums == [] and len(chat.photos) == 1
    assert chat.clicks == ["📐 Ngang 16:9", "15s", "✨ 1080p Full HD", START_1]


# ---------------------------------------------------------------------- API
async def test_api_multi_image_job_end_to_end(settings: Settings, images: list[Path], tmp_path: Path) -> None:
    factory = Factory()
    async with running(settings, factory) as (client, _):
        paths = [str(p) for p in images[:4]]
        r = await client.post("/grok/jobs", json={"imagePaths": paths, "prompt": "mix", "resolution": "1080p",
                                                  "multiImageMode": "firstLast", "mode": "custom"})
        assert r.status_code == 202, r.text
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded", job
        inp = job["input"]
        assert inp["imagePaths"] == paths and inp["imagePath"] == paths[0]
        assert inp["resolution"] == "720p"  # nhiều ảnh: bot chỉ làm 720p
        assert inp["multiImageMode"] is None  # chỉ có nghĩa khi đúng 2 ảnh
        chat = factory.chats["acc1"]
        assert chat.albums[0][0] == paths and chat.started_with == start_labels(4)[0]


async def test_api_two_images_mode(settings: Settings, images: list[Path]) -> None:
    factory = Factory()
    async with running(settings, factory) as (client, _):
        body = {"imagePaths": [str(images[0]), str(images[1])], "prompt": "turn around"}
        default = (await client.post("/grok/jobs", json=body)).json()["jobId"]
        job = await wait_status(client, default, ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["input"]["multiImageMode"] == "reference"
        assert factory.chats["acc1"].started_with == PAIR_REFERENCE
        fl = (await client.post("/grok/jobs", json={**body, "multiImageMode": "FIRSTLAST"})).json()["jobId"]
        job = await wait_status(client, fl, ("succeeded", "failed"))
        assert job["status"] == "succeeded" and job["input"]["multiImageMode"] == "firstLast"
        assert factory.chats["acc1"].started_with == PAIR_FIRST_LAST


async def test_api_images_base64(settings: Settings, images: list[Path]) -> None:
    factory = Factory()
    async with running(settings, factory) as (client, _):
        b64 = [base64.b64encode(p.read_bytes()).decode() for p in images[:2]]
        b64[1] = "data:image/png;base64," + b64[1]
        r = await client.post("/grok/jobs", json={"imagesBase64": b64, "prompt": "x"})
        assert r.status_code == 202, r.text
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded"
        saved = [Path(x) for x in job["input"]["imagePaths"]]
        assert len(saved) == 2 and all(s.parent == settings.uploads_dir for s in saved)
        assert [s.read_bytes() for s in saved] == [p.read_bytes() for p in images[:2]]


async def test_api_single_item_list_is_a_normal_job(settings: Settings, image: Path) -> None:
    factory = Factory()
    async with running(settings, factory) as (client, _):
        r = await client.post("/grok/jobs", json={"imagePaths": [str(image)], "prompt": "x", "resolution": "1080p",
                                                  "multiImageMode": "firstLast"})
        job = await wait_status(client, r.json()["jobId"], ("succeeded", "failed"))
        assert job["status"] == "succeeded"
        assert job["input"]["imagePaths"] == [str(image)] and job["input"]["resolution"] == "1080p"
        assert job["input"]["multiImageMode"] is None
        chat = factory.chats["acc1"]
        assert chat.albums == [] and len(chat.photos) == 1 and chat.started_with == START_1


async def test_api_multi_image_validation(settings: Settings, image: Path, images: list[Path]) -> None:
    paths = [str(p) for p in images]
    async with running(settings, Factory()) as (client, _):
        cases = [
            ({"imagePaths": paths, "prompt": "x"}, "imagePaths"),  # 7 ảnh > 6
            ({"imagePaths": [], "prompt": "x"}, "imagePaths"),
            ({"imagePaths": "C:/a.png", "prompt": "x"}, "imagePaths"),
            ({"imagePaths": [paths[0], "relative.png"], "prompt": "x"}, "imagePaths"),
            ({"imagePaths": paths[:2], "imagePath": str(image), "prompt": "x"}, "imagePaths"),
            ({"imagePaths": paths[:2], "imagesBase64": ["aGk="], "prompt": "x"}, "imagePaths"),
            ({"imagesBase64": ["aGk=", 5], "prompt": "x"}, "imagesBase64"),
            ({"imagePaths": paths[:2], "prompt": "x", "multiImageMode": "blend"}, "multiImageMode"),
        ]
        for body, field in cases:
            r = await client.post("/grok/jobs", json=body)
            assert r.status_code == 400, (body, r.text)
            assert r.json()["error"]["details"]["field"] == field, (body, r.text)
        r = await client.post("/grok/jobs", json={"imagePaths": [paths[0], "relative.png"], "prompt": "x"})
        assert "ảnh 2" in r.json()["error"]["message"]


# ----------------------------------------------------------------------- DB
def test_db_migrates_old_jobs_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = DB(path)
    db.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE jobs DROP COLUMN image_paths")
    con.execute("ALTER TABLE jobs DROP COLUMN image_mode")
    con.commit()
    con.close()
    db = DB(path)  # DB cũ trên máy người dùng: tự thêm cột khi mở
    one = db.create_job(image_path="C:/a.png", prompt="p", aspect="9:16", duration=10, resolution="720p",
                        output_path="C:/o.mp4")
    two = db.create_job(image_path="C:/a.png", prompt="p", aspect="9:16", duration=10, resolution="720p",
                        output_path="C:/o2.mp4", image_paths=["C:/a.png", "C:/b.png"], image_mode="firstLast")
    assert job_images(one) == ["C:/a.png"] and one["image_mode"] is None
    assert job_images(two) == ["C:/a.png", "C:/b.png"] and two["image_mode"] == "firstLast"
    db.close()
