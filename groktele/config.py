"""Cấu hình: `.env` (api_id/hash, cổng, mặc định) và `accounts.yaml` (session string từng tài khoản)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import yaml

def _detect_root() -> Path:
    """Thư mục chứa .env / accounts.yaml / data. Ưu tiên GROKTELE_HOME, rồi thư mục source, rồi thư mục hiện tại."""
    env = os.environ.get("GROKTELE_HOME", "").strip()
    if env:
        return Path(env).resolve()
    source = Path(__file__).resolve().parent.parent
    if (source / "pyproject.toml").exists():
        return source
    return Path.cwd().resolve()


ROOT = _detect_root()


def _read_text(path: Path) -> str:
    # utf-8-sig: bỏ BOM mà Notepad (Windows Server) hay thêm; replace: file ANSI có dấu cũng không làm crash.
    return path.read_text(encoding="utf-8-sig", errors="replace")

VALID_ASPECTS = ("9:16", "16:9")
# Bot chỉ có nút Dọc 9:16 / Ngang 16:9 (video thực tế giữ tỷ lệ ảnh gốc). Tỷ lệ khác map về gần nhất.
ASPECT_MAP = {
    "9:16": "9:16", "16:9": "16:9",
    "1:1": "9:16", "2:3": "9:16", "3:4": "9:16", "4:5": "9:16",
    "3:2": "16:9", "4:3": "16:9", "21:9": "16:9",
}
VALID_DURATIONS = (6, 10, 15)
# Bot chỉ có 720p và 1080p; 480p (mặc định MultiKit) được nâng lên 720p.
RESOLUTION_MAP = {"480p": "720p", "720p": "720p", "1080p": "1080p"}


def load_dotenv(path: Path) -> None:
    """KEY=VALUE mỗi dòng; không ghi đè biến môi trường đã có."""
    if not path.exists():
        return
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class AccountConfig:
    name: str
    session: str
    proxy: Optional[str] = None
    enabled: bool = True


@dataclass
class Settings:
    api_id: int = 0
    api_hash: str = ""
    bot: str = "GrokKian_bot"
    host: str = "127.0.0.1"
    port: int = 8888
    api_key: str = ""
    root: Path = ROOT
    default_aspect: str = "9:16"
    default_duration: int = 10
    default_resolution: str = "720p"
    max_attempts: int = 3
    ui_timeout: float = 60.0
    video_timeout: float = 600.0  # video thật ~1–4 phút; quá 10 phút thì /cancel và thử lại
    # Sau /cancel, không thấy dấu hiệu render (video, tin % được cập nhật) trong ngần này giây → coi như đã huỷ.
    # Nếu /cancel không dừng được render thì video muộn có thể lọt sang job sau → khi đó tăng lên 60 (.env).
    cancel_quiet: float = 20.0
    job_overhead: float = 900.0  # thêm vào hạn chót tổng mỗi job (upload, download, bấm nút)
    maintenance_cooldown: float = 300.0
    network_cooldown: float = 30.0
    health_interval: float = 1800.0
    idle_poll: float = 1.0
    orphan_grace: float = 600.0  # chờ tối đa video về muộn của job bị huỷ / timeout trước khi nhận job mới
    orphan_poll: float = 5.0     # chu kỳ kiểm tra lịch sử chat khi đang chờ video về muộn
    no_account_fail_check: float = 10.0  # chu kỳ fail job đang chờ khi không còn tài khoản dùng được
    action_delay: tuple[float, float] = (0.6, 1.5)
    upload_ttl_days: int = 7
    accounts: list[AccountConfig] = field(default_factory=list)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "groktele.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "groktele.log"

    @property
    def accounts_file(self) -> Path:
        return self.root / "accounts.yaml"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_settings(root: Optional[Path] = None) -> Settings:
    root = Path(root or ROOT)
    load_dotenv(root / ".env")
    s = Settings(root=root)
    s.api_id = int(_env("TG_API_ID", "0") or 0)
    s.api_hash = _env("TG_API_HASH")
    s.bot = _env("GROKTELE_BOT", s.bot).lstrip("@") or s.bot
    s.host = _env("GROKTELE_HOST", s.host) or s.host
    s.port = int(_env("GROKTELE_PORT", str(s.port)) or s.port)
    s.api_key = _env("GROKTELE_API_KEY")
    s.default_aspect = _env("GROKTELE_DEFAULT_ASPECT", s.default_aspect)
    s.default_duration = int(_env("GROKTELE_DEFAULT_DURATION", str(s.default_duration)))
    s.default_resolution = _env("GROKTELE_DEFAULT_RESOLUTION", s.default_resolution)
    s.max_attempts = int(_env("GROKTELE_MAX_ATTEMPTS", str(s.max_attempts)))
    s.video_timeout = float(_env("GROKTELE_VIDEO_TIMEOUT", str(s.video_timeout)))
    s.cancel_quiet = float(_env("GROKTELE_CANCEL_QUIET", str(s.cancel_quiet)))
    s.orphan_grace = float(_env("GROKTELE_ORPHAN_GRACE", str(s.orphan_grace)))
    s.accounts = load_accounts(s.accounts_file)
    return s


def load_accounts(path: Path) -> list[AccountConfig]:
    if not path.exists():
        return []
    raw = yaml.safe_load(_read_text(path)) or []
    if isinstance(raw, dict):
        raw = raw.get("accounts") or []
    out: list[AccountConfig] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("name") or not item.get("session"):
            continue
        out.append(
            AccountConfig(
                name=str(item["name"]).strip(),
                session=str(item["session"]).strip(),
                proxy=(str(item["proxy"]).strip() or None) if item.get("proxy") else None,
                enabled=bool(item.get("enabled", True)),
            )
        )
    return out


def save_account(path: Path, account: AccountConfig) -> None:
    """Thêm hoặc cập nhật tài khoản theo `name`, giữ nguyên các tài khoản khác."""
    accounts = load_accounts(path)
    accounts = [a for a in accounts if a.name != account.name] + [account]
    data = [
        {"name": a.name, "session": a.session, "proxy": a.proxy, "enabled": a.enabled}
        for a in accounts
    ]
    path.write_text(
        "# Session string = toàn quyền tài khoản Telegram. Không chia sẻ file này.\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10_000),
        encoding="utf-8",
    )


def parse_proxy(url: Optional[str]) -> Optional[dict[str, Any]]:
    """`socks5://user:pass@host:port` | `socks4://…` | `http://…` → dict proxy của Telethon."""
    if not url:
        return None
    u = urlparse(url if "://" in url else f"socks5://{url}")
    scheme = (u.scheme or "socks5").lower()
    if scheme not in ("socks5", "socks4", "http"):
        raise ValueError(f"Proxy không hỗ trợ: {scheme} (dùng socks5, socks4 hoặc http)")
    if not u.hostname or not u.port:
        raise ValueError(f"Proxy thiếu host hoặc port: {url}")
    proxy: dict[str, Any] = {"proxy_type": scheme, "addr": u.hostname, "port": u.port, "rdns": True}
    if u.username:
        proxy["username"] = unquote(u.username)
    if u.password:
        proxy["password"] = unquote(u.password)
    return proxy
