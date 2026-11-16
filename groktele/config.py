"""Cấu hình: `.env` (api_id/hash, cổng, mặc định) và `accounts.yaml` (session string từng tài khoản)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
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


DEFAULT_BOTS = ("GrokKian_bot", "GrokKian2_bot")


@dataclass
class AccountConfig:
    """1 tài khoản Telegram trong accounts.yaml — và cũng là 1 "luồng" (tài khoản × bot) khi đã qua `expand_lanes`.

    Luồng: `name` = tên luồng (vd `acc1`, `acc1@GrokKian2_bot`), `bot` = bot của luồng, `base` = tên tài khoản gốc.
    """
    name: str
    session: str
    proxy: Optional[str] = None
    enabled: bool = True
    bots: Optional[list[str]] = None  # ghi đè danh sách bot cho riêng tài khoản này (accounts.yaml)
    bot: str = ""
    base: str = ""

    @property
    def account(self) -> str:
        """Tên tài khoản Telegram gốc (các luồng cùng tài khoản dùng chung 1 kết nối)."""
        return self.base or self.name


def clean_bots(raw: Any) -> list[str]:
    """'A, @B' hoặc ['A', 'B'] → ['A', 'B'] (bỏ @, bỏ trùng không phân biệt hoa thường, giữ thứ tự)."""
    items = raw.replace(";", ",").split(",") if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for item in items:
        bot = str(item).strip().lstrip("@")
        if bot and bot.lower() not in (b.lower() for b in out):
            out.append(bot)
    return out


def lane_name(account: str, bot: str, primary: str) -> str:
    """Bot chính giữ nguyên tên tài khoản (tương thích database và accountId cũ); bot khác thêm hậu tố @bot."""
    return account if bot.lower() == primary.lower() else f"{account}@{bot}"


@dataclass
class Settings:
    api_id: int = 0
    api_hash: str = ""
    bot: str = "GrokKian_bot"  # bot chính: luồng của bot này mang đúng tên tài khoản
    # Mọi bot dùng song song (cùng giao diện, chung gói). Rỗng = chỉ bot chính.
    bots: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8888
    api_key: str = ""
    root: Path = ROOT
    default_aspect: str = "9:16"
    default_duration: int = 10
    default_resolution: str = "720p"
    max_attempts: int = 3
    # Grok báo "vi phạm chính sách": tạo lại ngay (không /cancel) tối đa ngần này lượt, rồi fail content_policy.
    # Kiểm duyệt của Grok không ổn định nên mặc định thử thêm 1 lần; đặt 1 để fail luôn.
    policy_attempts: int = 2
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
    # Job không được client hỏi trạng thái trong ngần này giây → coi như bị bỏ (vd ReelFB đã bỏ cuộc khi GrokTele
    # chết) → tự huỷ, không render cho không ai. ReelFB hỏi mỗi ~4s. 0 = tắt.
    abandon_after: float = 300.0
    min_free_mb: float = 1024.0      # ổ chứa outputPath còn ít hơn → job fail ngay (disk_full), không gửi bot
    min_free_data_mb: float = 100.0  # ổ chứa data/ (database, log) còn ít hơn → worker tạm dừng nhận job
    # Lỗi trước khi bot render (không thấy nút / bảng tuỳ chọn, thường do bot đổi giao diện): tài khoản nghỉ ngần
    # này giây trước job tiếp theo, nhân đôi mỗi lần lỗi liên tiếp, tối đa maintenance_cooldown.
    ui_fail_cooldown: float = 30.0
    accounts: list[AccountConfig] = field(default_factory=list)

    @property
    def bot_list(self) -> list[str]:
        return clean_bots(self.bots) or [self.bot]

    def bots_for(self, account: AccountConfig) -> list[str]:
        return clean_bots(account.bots) or self.bot_list

    def expand_lanes(self) -> list[AccountConfig]:
        """Mỗi tài khoản × mỗi bot = 1 luồng (1 worker, 1 job tại một thời điểm)."""
        lanes: list[AccountConfig] = []
        for acc in self.accounts:
            for bot in self.bots_for(acc):
                lanes.append(replace(acc, name=lane_name(acc.name, bot, self.bot), bot=bot, base=acc.name))
        return lanes

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
    # GROKTELE_BOTS (nhiều bot) > GROKTELE_BOT (1 bot, cấu hình cũ) > mặc định cả 2 bot.
    single = _env("GROKTELE_BOT").lstrip("@")
    s.bots = clean_bots(_env("GROKTELE_BOTS")) or ([single] if single else list(DEFAULT_BOTS))
    s.bot = s.bots[0]
    s.host = _env("GROKTELE_HOST", s.host) or s.host
    s.port = int(_env("GROKTELE_PORT", str(s.port)) or s.port)
    s.api_key = _env("GROKTELE_API_KEY")
    s.default_aspect = _env("GROKTELE_DEFAULT_ASPECT", s.default_aspect)
    s.default_duration = int(_env("GROKTELE_DEFAULT_DURATION", str(s.default_duration)))
    s.default_resolution = _env("GROKTELE_DEFAULT_RESOLUTION", s.default_resolution)
    s.max_attempts = int(_env("GROKTELE_MAX_ATTEMPTS", str(s.max_attempts)))
    s.policy_attempts = int(_env("GROKTELE_POLICY_ATTEMPTS", str(s.policy_attempts)))
    s.video_timeout = float(_env("GROKTELE_VIDEO_TIMEOUT", str(s.video_timeout)))
    s.cancel_quiet = float(_env("GROKTELE_CANCEL_QUIET", str(s.cancel_quiet)))
    s.min_free_mb = float(_env("GROKTELE_MIN_FREE_MB", str(s.min_free_mb)))
    s.abandon_after = float(_env("GROKTELE_ABANDON_AFTER", str(s.abandon_after)))
    s.orphan_grace = float(_env("GROKTELE_ORPHAN_GRACE", str(s.orphan_grace)))
    s.ui_fail_cooldown = float(_env("GROKTELE_UI_FAIL_COOLDOWN", str(s.ui_fail_cooldown)))
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
                bots=clean_bots(item.get("bots")) or None,
            )
        )
    return out


def save_account(path: Path, account: AccountConfig) -> None:
    """Thêm hoặc cập nhật tài khoản theo `name`, giữ nguyên các tài khoản khác."""
    accounts = load_accounts(path)
    old = next((a for a in accounts if a.name == account.name), None)
    if old is not None and account.bots is None:
        account.bots = old.bots  # login lại không làm mất danh sách bot riêng của tài khoản
    accounts = [a for a in accounts if a.name != account.name] + [account]
    data = [
        {"name": a.name, "session": a.session, "proxy": a.proxy, "enabled": a.enabled,
         **({"bots": a.bots} if a.bots else {})}
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
