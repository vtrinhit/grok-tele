from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from groktele.config import AccountConfig, Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(root=tmp_path, api_id=1, api_hash="x")
    s.ui_timeout = 2.0
    s.video_timeout = 2.0
    s.maintenance_cooldown = 0.3
    s.network_cooldown = 0.2
    s.health_interval = 1000.0
    s.idle_poll = 0.02
    s.action_delay = (0.0, 0.0)
    s.orphan_poll = 0.05
    s.orphan_grace = 8.0
    s.no_account_fail_check = 0.1
    s.accounts = [AccountConfig(name="acc1", session="fake")]
    return s


@pytest.fixture
def image(tmp_path: Path) -> Path:
    p = tmp_path / "in" / "flower.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 1000)
    return p
