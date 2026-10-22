from __future__ import annotations

from pathlib import Path

import pytest

from fake_bot import EXPIRED, MAINTENANCE, RENDER_ERROR, STATUS_ACTIVE, FakeChat, Scenario
from groktele.bot_driver import (
    AccountExpired,
    BotMaintenance,
    JobParams,
    RetryableError,
    check_status,
    classify,
    find_button,
    norm,
    progress_percent,
    run_job,
)
from groktele.chat import BotMessage

REAL_TEXTS = {
    "⏳ Đã nhận yêu cầu Tạo Video [1 Ảnh | Tỷ lệ 9:16 | 10s | 1080p]. Bot sẽ gửi video tại đây ngay khi hoàn tất!\n\n"
    "🤖 Bot là AI nên có thể mắc sai sót.\n🔄 Nếu quá trình tạo video báo lỗi, bạn hãy gửi lại ảnh và prompt để tạo "
    "lại nhé.": "accepted",
    "✅ Đã nhận 1 bức ảnh kèm kịch bản.\n📐 Tỷ lệ: 9:16 | ⏱ Thời lượng: 10s | 🎞 Chất lượng: 720p\n"
    "👇 Hãy chọn thông số rồi bấm Tạo Video bên dưới:": "options",
    "🎬 Đang tạo video (9:16 | 1080p): 30%": "progress",
    "✅ Nhiệm vụ đã hoàn thành, nếu bạn muốn tạo thêm hãy gửi nhé!": "done",
    STATUS_ACTIVE: "status",
    "🛠 Hiện đang đóng bot để bảo trì, bot sẽ quay trở lại ngay đây thôi": "maintenance",
    "LOA LOA LOA, Bot sẽ tạm đóng trong khoảng 10 phút để xử lý sự cố đợi video quá 10p của bạn": "maintenance",
    "🎉 Bot đã hết bảo trì và hoạt động trở lại! Bạn có thể gửi ảnh và prompt để tạo video nhé.": "back_online",
    "🎁 Bot đã bảo trì xong và đã tự động cộng vào cho bạn thêm 12h sử dụng bot": "back_online",
    "❗Bot vừa phải reset mạng do VPS chập chờn, những tác vụ chưa hoàn thành bạn vui lòng gửi lại giúp bot nhé ạ.": "reset",
    EXPIRED: "expired",
    RENDER_ERROR: "error",
    "🌸 Chào mừng bạn đến với Bot Tạo Video AI Grok! 🎬✨\n\n🎁 Quà tặng trải nghiệm miễn phí:\n- 01 lượt Tạo Video AI": None,
    "💌Hello anh em, bot đã cập nhật thêm chức năng tạo ảnh và chức năng nối dài video": None,
    "🆔 ID Telegram của bạn: 1790523934": None,
}


@pytest.mark.parametrize("text,expected", list(REAL_TEXTS.items()))
def test_classify_real_bot_texts(text: str, expected: str) -> None:
    assert classify(text) == expected


def test_progress_percent() -> None:
    assert progress_percent("🎬 Đang tạo video (9:16 | 1080p): 99%") == 99
    assert progress_percent("hello") is None


def test_find_button_and_selection() -> None:
    msg = BotMessage(id=1, buttons=[
        ["✓📐 Dọc 9:16", "📐 Ngang 16:9"], ["6s", "✓ 10s", "15s"], ["✓ ⚡ 720p", "✨ 1080p Full HD"],
        ["🎬 Tạo Video (từ ảnh + prompt)"],
    ])
    assert find_button(msg, "aspect", "16:9")[:2] == (0, 1)
    assert find_button(msg, "duration", "6")[:2] == (1, 0)
    assert find_button(msg, "duration", "15")[:2] == (1, 2)
    assert find_button(msg, "duration", "10")[:2] == (1, 1)
    assert find_button(msg, "resolution", "1080p")[:2] == (2, 1)
    assert find_button(msg, "start")[:2] == (3, 0)
    assert norm("✓ ⚡ 720p") == "720p"


def _params(image: Path, tmp_path: Path, **kw) -> JobParams:
    return JobParams(image_path=str(image), prompt="Tạo video hoa nở", output_path=str(tmp_path / "out" / "v.mp4"), **kw)


async def test_run_job_success_default_options(image: Path, tmp_path: Path) -> None:
    chat = FakeChat()
    stages = []
    res = await run_job(chat, _params(image, tmp_path), ui_timeout=2, video_timeout=2,
                        on_stage=lambda s, p: stages.append((s, round(p, 2))))
    assert res.path.exists() and res.bytes >= 5000
    assert res.path.read_bytes().startswith("VIDEO:Tạo video hoa nở|".encode())
    assert chat.clicks == ["🎬 Tạo Video (từ ảnh + prompt)"]  # mặc định đã đúng → chỉ bấm Tạo Video
    names = [s for s, _ in stages]
    assert names[0] == "upload" and "options" in names and "generate" in names and names[-1] == "download"
    assert any(s == "generate" and p > 0.5 for s, p in stages)  # đọc % từ tin bot sửa


async def test_run_job_changes_options(image: Path, tmp_path: Path) -> None:
    chat = FakeChat()
    await run_job(chat, _params(image, tmp_path, aspect="16:9", duration=15, resolution="1080p"),
                  ui_timeout=2, video_timeout=2)
    assert chat.clicks == ["📐 Ngang 16:9", "15s", "✨ 1080p Full HD", "🎬 Tạo Video (từ ảnh + prompt)"]
    assert chat.sel == {"aspect": "16:9", "duration": "15", "res": "1080p"}


async def test_maintenance_on_photo(image: Path, tmp_path: Path) -> None:
    with pytest.raises(BotMaintenance):
        await run_job(FakeChat([Scenario(photo="maintenance")]), _params(image, tmp_path), ui_timeout=1)


async def test_maintenance_during_render(image: Path, tmp_path: Path) -> None:
    with pytest.raises(BotMaintenance):
        await run_job(FakeChat([Scenario(render="maintenance")]), _params(image, tmp_path), ui_timeout=1,
                      video_timeout=1)


async def test_expired(image: Path, tmp_path: Path) -> None:
    with pytest.raises(AccountExpired):
        await run_job(FakeChat([Scenario(photo="expired")]), _params(image, tmp_path), ui_timeout=1)


async def test_render_error(image: Path, tmp_path: Path) -> None:
    with pytest.raises(RetryableError) as ei:
        await run_job(FakeChat([Scenario(render="error")]), _params(image, tmp_path), ui_timeout=1, video_timeout=1)
    assert ei.value.kind == "unknown"


async def test_video_timeout(image: Path, tmp_path: Path) -> None:
    with pytest.raises(RetryableError) as ei:
        await run_job(FakeChat([Scenario(render="silent")]), _params(image, tmp_path), ui_timeout=1,
                      video_timeout=0.3)
    assert ei.value.kind == "timeout"


async def test_options_timeout(image: Path, tmp_path: Path) -> None:
    with pytest.raises(RetryableError) as ei:
        await run_job(FakeChat([Scenario(photo="silent")]), _params(image, tmp_path), ui_timeout=0.3)
    assert ei.value.kind == "timeout"


async def test_click_ignored_raises(image: Path, tmp_path: Path) -> None:
    with pytest.raises(RetryableError):
        await run_job(FakeChat([Scenario(ignore_clicks=True)]), _params(image, tmp_path, duration=15),
                      ui_timeout=1)


async def test_ignores_late_video_from_previous_job(image: Path, tmp_path: Path) -> None:
    chat = FakeChat([Scenario(render="silent"), Scenario()])
    with pytest.raises(RetryableError):
        await run_job(chat, _params(image, tmp_path), ui_timeout=1, video_timeout=0.2)
    # Video của job 1 về muộn, trước khi job 2 bắt đầu.
    late = chat.bot_says(video=True)
    late.raw = "JOB1"
    res = await run_job(chat, JobParams(image_path=str(image), prompt="job 2", output_path=str(tmp_path / "2.mp4")),
                        ui_timeout=2, video_timeout=2)
    assert res.path.read_bytes().startswith(b"VIDEO:job 2|")


async def test_check_status() -> None:
    info = await check_status(FakeChat(status="active"), timeout=1)
    assert info.active is True and info.expires_at == "18:24 25/10/2026"
    info = await check_status(FakeChat(status="expired"), timeout=1)
    assert info.active is False
    info = await check_status(FakeChat(status="silent"), timeout=0.2)
    assert info.active is None
