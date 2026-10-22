"""Chạy client MultiKit THẬT của ReelFB Studio vào server GrokTele (uvicorn, cổng ngẫu nhiên)."""
from __future__ import annotations

import asyncio
import importlib.util
import socket
from pathlib import Path

import pytest
import uvicorn

from test_api_worker import Factory, _nodelay
from groktele.app import build_app
from groktele.config import Settings
from groktele.db import DB

REELFB_CLIENT = Path(r"D:\MMO\ReelFB_Studio\backend\services\multikit.py")


def _load_reelfb():
    spec = importlib.util.spec_from_file_location("reelfb_multikit", REELFB_CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not REELFB_CLIENT.exists(), reason="Không có ReelFB Studio trên máy này")
async def test_reelfb_generate_via_multikit(settings: Settings, image: Path, tmp_path: Path) -> None:
    mk = _load_reelfb()
    port = _free_port()
    app = build_app(settings, db=DB(settings.db_path), chat_factory=Factory(), delay=_nodelay)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        base = f"http://127.0.0.1:{port}"
        async with mk.MultiKitClient(base, "bat-ky-key") as client:
            assert (await client.health())["ok"] is True
            assert (await client.accounts())["summary"]["usable"] == 1

        logs: list[str] = []
        out = tmp_path / "ws" / "phase_001_01.mp4"
        produced = await mk.generate_via_multikit(
            str(image), "camera slowly zooms in", out, "phase_001_01", base_url=base, api_key="bat-ky-key",
            aspect_ratio="9:16", duration=10, resolution="720p", poll_interval=0.1, timeout=60,
            log=lambda lvl, msg: logs.append(f"{lvl} {msg}"),
        )
        assert Path(produced) == out.resolve() and out.stat().st_size > 5000
        assert any("tạo xong" in line for line in logs), logs

        # Chạy lại: file đã có → ReelFB tự bỏ qua (resume); force → clientRef mới, job mới.
        again = await mk.generate_via_multikit(str(image), "camera slowly zooms in", out, "phase_001_01",
                                               base_url=base, poll_interval=0.1, timeout=60, force=True)
        assert Path(again).exists()
    finally:
        server.should_exit = True
        await task
