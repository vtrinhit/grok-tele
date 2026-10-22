"""SQLite cho job và trạng thái tài khoản. Mọi hàm đồng bộ, rất nhanh, gọi thẳng từ event loop."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

TERMINAL = ("succeeded", "failed", "cancelled")
# Tài khoản ở các trạng thái này vẫn có thể chạy job (ngay hoặc sau khi hết cooldown).
USABLE_STATUSES = ("active", "cooldown", "connecting")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT UNIQUE NOT NULL,
    batch_id       TEXT,
    client_ref     TEXT,
    status         TEXT NOT NULL,
    stage          TEXT,
    progress       REAL NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    priority       INTEGER NOT NULL DEFAULT 0,
    image_path     TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    aspect         TEXT NOT NULL,
    duration       INTEGER NOT NULL,
    resolution     TEXT NOT NULL,
    output_path    TEXT NOT NULL,
    pinned_account TEXT,
    account        TEXT,
    result_bytes   INTEGER,
    result_duration_ms INTEGER,
    error_kind     TEXT,
    error_message  TEXT,
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_queue ON jobs(status, priority DESC, seq);
CREATE INDEX IF NOT EXISTS ix_jobs_ref ON jobs(client_ref);
CREATE TABLE IF NOT EXISTS accounts (
    name           TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'connecting',
    reason         TEXT,
    cooldown_until REAL,
    expires_at     TEXT,
    last_check     TEXT,
    jobs_done      INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    telegram_user  TEXT,
    orphan_job     TEXT,
    orphan_baseline INTEGER,
    orphan_since   REAL
);
"""

# Cột thêm sau bản đầu: DB cũ (đã chạy trên VPS) được ALTER khi mở.
_MIGRATIONS = {
    "accounts": {"orphan_job": "TEXT", "orphan_baseline": "INTEGER", "orphan_since": "REAL"},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str) -> str:
    # Tiền tố thời gian để id tăng dần theo thời điểm tạo, dễ đọc log.
    return f"{prefix}_{int(time.time() * 1000):013x}{uuid.uuid4().hex[:8]}"


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            for table, cols in _MIGRATIONS.items():
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for col, typ in cols.items():
                    if col not in have:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    # ---------------------------------------------------------------- jobs
    def create_job(self, *, image_path: str, prompt: str, aspect: str, duration: int, resolution: str,
                   output_path: str, priority: int = 0, client_ref: Optional[str] = None,
                   pinned_account: Optional[str] = None, batch_id: Optional[str] = None,
                   job_id: Optional[str] = None) -> dict:
        job_id = job_id or new_id("job")
        self._exec(
            """INSERT INTO jobs (id, batch_id, client_ref, status, priority, image_path, prompt, aspect,
                                 duration, resolution, output_path, pinned_account, created_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, batch_id, client_ref, int(priority), image_path, prompt, aspect, int(duration),
             resolution, output_path, pinned_account, now_iso()),
        )
        return self.get_job(job_id)  # type: ignore[return-value]

    def find_active_by_client_ref(self, client_ref: str) -> Optional[dict]:
        """Job cùng clientRef chưa failed/cancelled (idempotency giống MultiKit)."""
        return self._one(
            "SELECT * FROM jobs WHERE client_ref = ? AND status NOT IN ('failed','cancelled') "
            "ORDER BY seq DESC LIMIT 1",
            (client_ref,),
        )

    def get_job(self, job_id: str) -> Optional[dict]:
        return self._one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def list_jobs(self, *, status: Optional[str] = None, batch_id: Optional[str] = None,
                  client_ref: Optional[str] = None, limit: int = 50, cursor: Optional[str] = None) -> list[dict]:
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(status)
        if batch_id:
            where.append("batch_id = ?")
            args.append(batch_id)
        if client_ref:
            where.append("client_ref = ?")
            args.append(client_ref)
        if cursor:
            where.append("seq < (SELECT seq FROM jobs WHERE id = ?)")
            args.append(cursor)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return self._all(sql, tuple(args))

    def claim_job(self, account: str) -> Optional[dict]:
        """Nhận job queued ưu tiên cao nhất, cũ nhất cho `account`. Nguyên tử."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """SELECT id FROM jobs WHERE status = 'queued'
                         AND (pinned_account IS NULL OR pinned_account = ?)
                       ORDER BY priority DESC, seq ASC LIMIT 1""",
                    (account,),
                ).fetchone()
                if row:
                    self._conn.execute(
                        """UPDATE jobs SET status = 'running', account = ?, stage = 'upload', progress = 0,
                                  attempts = attempts + 1, started_at = ?, error_kind = NULL, error_message = NULL
                           WHERE id = ? AND status = 'queued'""",
                        (account, now_iso(), row["id"]),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_job(row["id"]) if row else None

    def claim_specific(self, job_id: str, account: str) -> bool:
        """Nhận đúng 1 job đang queued (dùng khi video về muộn của chính job đó tới nơi). Không tính thêm lượt."""
        return self._exec(
            """UPDATE jobs SET status = 'running', account = ?, stage = 'download', progress = 0.92,
                      started_at = COALESCE(started_at, ?), error_kind = NULL, error_message = NULL
               WHERE id = ? AND status = 'queued'""",
            (account, now_iso(), job_id),
        ) > 0

    def set_orphan(self, account: str, job_id: str, baseline: int) -> None:
        self.set_account(account, orphan_job=job_id, orphan_baseline=int(baseline), orphan_since=time.time())

    def clear_orphan(self, account: str) -> None:
        self.set_account(account, orphan_job=None, orphan_baseline=None, orphan_since=None)

    def set_progress(self, job_id: str, stage: str, progress: float) -> None:
        self._exec("UPDATE jobs SET stage = ?, progress = ? WHERE id = ? AND status = 'running'",
                   (stage, max(0.0, min(1.0, float(progress))), job_id))

    def requeue(self, job_id: str, *, refund_attempt: bool, kind: Optional[str] = None,
                message: Optional[str] = None) -> bool:
        """Trả job đang chạy về hàng đợi. `refund_attempt` = lỗi không do job (bảo trì, mạng...)."""
        attempts = "MAX(attempts - 1, 0)" if refund_attempt else "attempts"
        return self._exec(
            f"""UPDATE jobs SET status = 'queued', account = NULL, stage = NULL, progress = 0, started_at = NULL,
                       attempts = {attempts}, error_kind = ?, error_message = ?
                WHERE id = ? AND status = 'running'""",
            (kind, message, job_id),
        ) > 0

    def finish_success(self, job_id: str, *, result_bytes: int, duration_ms: Optional[int]) -> bool:
        ok = self._exec(
            """UPDATE jobs SET status = 'succeeded', stage = 'download', progress = 1, result_bytes = ?,
                      result_duration_ms = ?, error_kind = NULL, error_message = NULL, finished_at = ?
               WHERE id = ? AND status = 'running'""",
            (int(result_bytes), duration_ms, now_iso(), job_id),
        ) > 0
        if ok:
            job = self.get_job(job_id)
            if job and job["account"]:
                self._exec("UPDATE accounts SET jobs_done = jobs_done + 1 WHERE name = ?", (job["account"],))
        return ok

    def finish_failed(self, job_id: str, *, kind: str, message: str) -> bool:
        return self._exec(
            """UPDATE jobs SET status = 'failed', error_kind = ?, error_message = ?, finished_at = ?
               WHERE id = ? AND status IN ('running', 'queued')""",
            (kind, message, now_iso(), job_id),
        ) > 0

    def cancel(self, job_id: str) -> bool:
        return self._exec(
            """UPDATE jobs SET status = 'cancelled', error_kind = 'cancelled', error_message = 'Đã huỷ',
                      finished_at = ?
               WHERE id = ? AND status IN ('queued', 'running')""",
            (now_iso(), job_id),
        ) > 0

    def retry(self, job_id: str) -> bool:
        return self._exec(
            """UPDATE jobs SET status = 'queued', attempts = 0, account = NULL, stage = NULL, progress = 0,
                      error_kind = NULL, error_message = NULL, started_at = NULL, finished_at = NULL
               WHERE id = ? AND status IN ('failed', 'cancelled')""",
            (job_id,),
        ) > 0

    def reset_running(self) -> int:
        """Khởi động lại app: job đang chạy dở quay về hàng đợi (vẫn tính lượt đã dùng)."""
        return self._exec(
            "UPDATE jobs SET status = 'queued', account = NULL, stage = NULL, progress = 0 WHERE status = 'running'"
        )

    def fail_queued_without_account(self, kind: str, message: str) -> int:
        """Dùng khi chắc chắn không tài khoản nào chạy được nữa."""
        return self._exec(
            "UPDATE jobs SET status = 'failed', error_kind = ?, error_message = ?, finished_at = ? "
            "WHERE status = 'queued'",
            (kind, message, now_iso()),
        )

    def counts(self) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        out = {s: 0 for s in ("queued", "running", "succeeded", "failed", "cancelled")}
        out.update({r["status"]: r["n"] for r in rows})
        return out

    def stats(self) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="milliseconds")
        since = since.replace("+00:00", "Z")
        rows = self._all(
            "SELECT status, started_at, finished_at FROM jobs WHERE finished_at >= ? "
            "AND status IN ('succeeded', 'failed')",
            (since,),
        )
        ok = sum(1 for r in rows if r["status"] == "succeeded")
        bad = sum(1 for r in rows if r["status"] == "failed")
        ms = [
            (parse_iso(r["finished_at"]) - parse_iso(r["started_at"])).total_seconds() * 1000
            for r in rows
            if r["status"] == "succeeded" and r["started_at"] and r["finished_at"]
        ]
        return {
            "jobs": self.counts(),
            "last24h": {
                "succeeded": ok,
                "failed": bad,
                "successRate": round(ok / (ok + bad), 4) if ok + bad else None,
                "avgJobMs": int(sum(ms) / len(ms)) if ms else None,
            },
        }

    # ------------------------------------------------------------ accounts
    def ensure_account(self, name: str) -> None:
        self._exec("INSERT OR IGNORE INTO accounts (name, status) VALUES (?, 'connecting')", (name,))

    def get_account(self, name: str) -> Optional[dict]:
        return self._one("SELECT * FROM accounts WHERE name = ?", (name,))

    def list_accounts(self) -> list[dict]:
        return self._all("SELECT * FROM accounts ORDER BY name")

    def set_account(self, name: str, **fields: Any) -> None:
        if not fields:
            return
        self.ensure_account(name)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE accounts SET {cols} WHERE name = ?", (*fields.values(), name))

    def running_job_for(self, account: str) -> Optional[dict]:
        return self._one("SELECT * FROM jobs WHERE status = 'running' AND account = ? LIMIT 1", (account,))
