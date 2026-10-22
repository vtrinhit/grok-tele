"""Worker: 1 tài khoản Telegram = 1 vòng lặp asyncio = 1 job tại một thời điểm."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from pathlib import Path

from groktele.bot_driver import (
    AccountExpired,
    BotMaintenance,
    DriverError,
    JobParams,
    JobState,
    RetryableError,
    check_status,
    find_orphan_outcome,
    hours_left,
    human_delay,
    run_job,
    send_cancel,
)
from groktele.chat import AuthRequired, BotChat
from groktele.config import AccountConfig, Settings
from groktele.db import DB, USABLE_STATUSES, now_iso

log = logging.getLogger("groktele.worker")

ChatFactory = Callable[[AccountConfig], Awaitable[BotChat]]

# Trạng thái tài khoản người dùng phải tự xử lý (resume / login lại).
STOPPED_STATUSES = ("expired", "dead", "paused", "disabled")


def _telethon_errors() -> tuple[tuple[type, ...], tuple[type, ...]]:
    """(lỗi session chết, lỗi FloodWait). Import lười để test không cần Telethon."""
    try:
        from telethon import errors as e

        dead = tuple(
            getattr(e, n) for n in (
                "AuthKeyUnregisteredError", "AuthKeyDuplicatedError", "SessionRevokedError",
                "SessionExpiredError", "UserDeactivatedError", "UserDeactivatedBanError",
                "AuthKeyInvalidError", "PhoneNumberBannedError",
            ) if hasattr(e, n)
        )
        flood = tuple(getattr(e, n) for n in ("FloodWaitError", "FloodPremiumWaitError", "SlowModeWaitError")
                      if hasattr(e, n))
        return dead, flood
    except ImportError:  # pragma: no cover
        return (), ()


DEAD_ERRORS, FLOOD_ERRORS = _telethon_errors()


def _input_errors() -> tuple[type, ...]:
    """Lỗi do chính ảnh / prompt: thử lại cũng vô ích → fail ngay."""
    try:
        from telethon import errors as e

        names = ("MediaCaptionTooLongError", "MessageTooLongError", "PhotoInvalidDimensionsError",
                 "PhotoExtInvalidError", "PhotoSaveFileInvalidError", "ImageProcessFailedError",
                 "MediaInvalidError", "PhotoInvalidError", "MediaEmptyError", "FilePartsInvalidError")
        return tuple(getattr(e, n) for n in names if hasattr(e, n))
    except ImportError:  # pragma: no cover
        return ()


INPUT_ERRORS = _input_errors()
NETWORK_ERRORS: tuple[type, ...] = (ConnectionError, OSError, asyncio.IncompleteReadError)


class AccountWorker:
    def __init__(self, account: AccountConfig, settings: Settings, db: DB, chat_factory: ChatFactory,
                 delay: Optional[Callable[[], Awaitable[None]]] = None):
        self.account = account
        self.name = account.name
        self.settings = settings
        self.db = db
        self.chat_factory = chat_factory
        self.delay = delay or (lambda: human_delay(*settings.action_delay))
        self.chat: Optional[BotChat] = None
        self.task: Optional[asyncio.Task] = None
        self.job_task: Optional[asyncio.Task] = None
        self.current_job: Optional[str] = None
        self._next_health = 0.0
        self._stopping = False

    # --------------------------------------------------------------- state
    def _set(self, status: str, reason: Optional[str] = None, cooldown: float = 0.0, **extra) -> None:
        until = time.time() + cooldown if cooldown else None
        self.db.set_account(self.name, status=status, reason=reason, cooldown_until=until, **extra)
        if status not in ("active", "connecting"):
            log.warning("[%s] -> %s%s", self.name, status, f": {reason}" if reason else "")

    def _cooldown(self, seconds: float, reason: str) -> None:
        self._set("cooldown", reason, cooldown=max(1.0, seconds))

    async def _drop_chat(self) -> None:
        if self.chat is not None:
            try:
                await self.chat.close()
            except Exception:
                pass
            self.chat = None

    # ---------------------------------------------------------------- loop
    def start(self) -> None:
        self.db.ensure_account(self.name)
        if not self.account.enabled:
            self._set("disabled", "enabled: false trong accounts.yaml")
        else:
            acc = self.db.get_account(self.name) or {}
            if acc.get("status") != "paused":
                # Khởi động lại app: kiểm tra lại từ đầu (có thể vừa gia hạn hoặc login lại).
                self._set("connecting")
        self.task = asyncio.create_task(self.run(), name=f"worker-{self.name}")

    async def stop(self) -> None:
        self._stopping = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        await self._drop_chat()

    def cancel_job(self, job_id: str) -> bool:
        if self.current_job == job_id and self.job_task and not self.job_task.done():
            self.job_task.cancel()
            return True
        return False

    async def run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # không bao giờ để worker chết
                log.exception("[%s] lỗi không mong đợi trong vòng lặp: %s", self.name, exc)
                await self._drop_chat()
                self._cooldown(self.settings.network_cooldown, f"Lỗi: {exc}")
                await asyncio.sleep(1)

    async def _tick(self) -> None:
        acc = self.db.get_account(self.name) or {}
        status = acc.get("status")
        if status in STOPPED_STATUSES:
            if status in ("dead", "disabled"):
                await self._drop_chat()
            await asyncio.sleep(2)
            return
        until = acc.get("cooldown_until") or 0
        if status == "cooldown" and until > time.time():
            await asyncio.sleep(min(2.0, until - time.time()))
            return

        if self.chat is None:
            if not await self._connect():
                return
            self._next_health = 0.0  # vừa kết nối → kiểm tra /status ngay

        if status in ("cooldown", "connecting"):
            # Hết nghỉ (thường do bảo trì) hoặc vừa resume → hỏi /status trước khi nhận job.
            self._next_health = 0.0

        if time.time() >= self._next_health:
            if not await self._health_check():
                return

        if acc.get("orphan_job") and not await self._resolve_orphan(acc):
            # Bot còn đang render job trước: chưa gửi job mới, kẻo video về muộn bị gán nhầm.
            await asyncio.sleep(self.settings.orphan_poll)
            return

        job = self.db.claim_job(self.name)
        if not job:
            await asyncio.sleep(self.settings.idle_poll)
            return
        await self._run_one(job)

    async def _resolve_orphan(self, acc: dict) -> bool:
        """True = render dở đã có kết quả (hoặc quá hạn chờ), tài khoản nhận job mới được."""
        assert self.chat is not None
        job_id, baseline = acc["orphan_job"], int(acc["orphan_baseline"] or 0)
        outcome, video = await find_orphan_outcome(self.chat, baseline, cancel_at=acc.get("orphan_cancel_at"),
                                                   cancel_quiet=self.settings.cancel_quiet)
        if outcome == "video" and video is not None:
            job = self.db.get_job(job_id)
            if job and self.db.claim_specific(job_id, self.name):
                # Job vẫn đang chờ thử lại → dùng luôn video về muộn, không phải render lại.
                try:
                    size = await self.chat.download(video, Path(job["output_path"]))
                    self.db.finish_success(job_id, result_bytes=size,
                                           duration_ms=video.video_duration_ms or int(job["duration"]) * 1000)
                    log.info("[%s] %s nhận video về muộn: %s (%d KB)", self.name, job_id, job["output_path"],
                             size // 1024)
                except Exception as exc:
                    self.db.requeue(job_id, refund_attempt=True, kind="network", message=f"Tải video muộn lỗi: {exc}")
                    log.warning("[%s] %s tải video về muộn lỗi, xếp lại: %s", self.name, job_id, exc)
            else:
                log.info("[%s] bỏ video về muộn của %s (job đã %s)", self.name, job_id,
                         job["status"] if job else "không còn")
            self.db.clear_orphan(self.name)
            return True
        if outcome == "ended":
            log.info("[%s] render dở của %s đã kết thúc%s", self.name, job_id,
                     " (đã /cancel)" if acc.get("orphan_cancel_at") else " không có video")
            self.db.clear_orphan(self.name)
            return True
        waited = time.time() - float(acc.get("orphan_since") or 0)
        if waited >= self.settings.orphan_grace:
            log.warning("[%s] chờ video về muộn của %s quá %ds, bỏ qua", self.name, job_id, int(waited))
            self.db.clear_orphan(self.name)
            return True
        return False

    async def _connect(self) -> bool:
        self.db.set_account(self.name, status="connecting", reason="Đang kết nối Telegram")
        try:
            self.chat = await self.chat_factory(self.account)
        except AuthRequired as exc:
            self._set("dead", str(exc))
            return False
        except DEAD_ERRORS as exc:
            self._set("dead", f"Session chết: {type(exc).__name__}")
            return False
        except FLOOD_ERRORS as exc:
            self._cooldown(getattr(exc, "seconds", 60) + 5, f"FloodWait khi kết nối: {exc}")
            return False
        except Exception as exc:
            self._cooldown(self.settings.network_cooldown, f"Không kết nối được: {exc}")
            return False
        me = getattr(self.chat, "me", None)
        if me:
            try:
                self.db.set_account(self.name, telegram_user=await me())
            except Exception:
                pass
        log.info("[%s] đã kết nối", self.name)
        return True

    async def _health_check(self) -> bool:
        """Gửi /status. False = tài khoản không nhận job lúc này."""
        assert self.chat is not None
        try:
            info = await check_status(self.chat, timeout=30)
        except FLOOD_ERRORS as exc:
            self._cooldown(getattr(exc, "seconds", 60) + 5, f"FloodWait: {exc}")
            return False
        except DEAD_ERRORS as exc:
            self._set("dead", f"Session chết: {type(exc).__name__}")
            return False
        except NETWORK_ERRORS as exc:
            await self._drop_chat()
            self._cooldown(self.settings.network_cooldown, f"Mất kết nối: {exc}")
            return False
        self.db.set_account(self.name, last_check=now_iso(), expires_at=info.expires_at or None)
        if info.active is False:
            self._set("expired", f"Bot báo hết hạn: {info.text[:200]}")
            return False
        if info.active is None:
            # Bot không trả lời (bảo trì?) → nghỉ rồi hỏi lại.
            self._cooldown(self.settings.maintenance_cooldown / 2, f"/status không trả lời: {info.text[:120]}")
            return False
        self._set("active", None)
        left = hours_left(info.expires_at)
        if left is not None and left < 24:
            log.warning("[%s] gói của bot còn %.1f giờ (hết hạn %s) — nhớ gia hạn", self.name, left, info.expires_at)
        self._next_health = time.time() + self.settings.health_interval
        return True

    # ----------------------------------------------------------------- job
    async def _run_one(self, job: dict) -> None:
        assert self.chat is not None
        job_id = job["id"]
        log.info("[%s] %s bắt đầu (lượt %d): %s", self.name, job_id, job["attempts"], job["prompt"][:60])
        params = JobParams(
            image_path=job["image_path"], prompt=job["prompt"], aspect=job["aspect"],
            duration=int(job["duration"]), resolution=job["resolution"], output_path=job["output_path"],
        )

        def on_stage(stage: str, progress: float) -> None:
            self.db.set_progress(job_id, stage, progress)

        # Dấu render dở ghi vào DB trước khi bấm Tạo Video: app bị kill (06.stop.bat, đóng cửa sổ, mất điện)
        # thì lần chạy sau vẫn biết phải chờ video về muộn của job này.
        state = JobState(on_render_start=lambda baseline: self.db.set_orphan(self.name, job_id, baseline))
        send_cancel_cmd = False  # job bị huỷ / timeout khi bot đang render → /cancel cho đỡ mất thời gian
        self.current_job = job_id
        self.job_task = asyncio.create_task(
            run_job(self.chat, params, ui_timeout=self.settings.ui_timeout,
                    video_timeout=self.settings.video_timeout, on_stage=on_stage, delay=self.delay, state=state)
        )
        try:
            try:
                # Hạn chót tổng: dù lệnh nào bị kẹt, job luôn kết thúc để ReelFB nhận được kết quả.
                # wait_for tự huỷ job_task khi quá hạn.
                result = await asyncio.wait_for(self.job_task, timeout=self._job_deadline())
            except asyncio.TimeoutError:
                raise RetryableError(f"Job vượt hạn chót {int(self._job_deadline())}s", kind="timeout") from None
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                # App đang tắt: trả job về hàng đợi; giữ render để lần sau nhận video về muộn (không /cancel).
                self.db.requeue(job_id, refund_attempt=True)
                if state.orphaned:
                    self.db.set_orphan(self.name, job_id, state.render_baseline)  # type: ignore[arg-type]
                raise
            log.info("[%s] %s đã huỷ", self.name, job_id)
            send_cancel_cmd = True
            return
        except Exception as exc:
            self._handle_failure(job, exc)
            if isinstance(exc, NETWORK_ERRORS):
                await self._drop_chat()
            elif isinstance(exc, RetryableError) and exc.kind == "timeout":
                send_cancel_cmd = True
            return
        finally:
            self.current_job = None
            self.job_task = None
            task = asyncio.current_task()
            if state.render_baseline is not None and state.render_ended:
                self.db.clear_orphan(self.name)  # bot đã trả video hoặc báo lỗi: không còn gì về muộn
            if state.orphaned and not (task is not None and task.cancelling()):
                # Bot có thể vẫn đang render job này → không gửi job mới cho tới khi render đó kết thúc.
                self.db.set_orphan(self.name, job_id, state.render_baseline)  # type: ignore[arg-type]
                if send_cancel_cmd and self.chat is not None:
                    await self._cancel_render(job_id)
                else:
                    log.info("[%s] %s dừng khi bot đang render, chờ video về muộn trước khi nhận job mới",
                             self.name, job_id)

        if self.db.finish_success(job_id, result_bytes=result.bytes,
                                  duration_ms=result.duration_ms or int(job["duration"]) * 1000):
            log.info("[%s] %s xong: %s (%d KB)", self.name, job_id, result.path, result.bytes // 1024)
        else:
            log.info("[%s] %s xong nhưng job đã bị huỷ, bỏ kết quả", self.name, job_id)

    def _job_deadline(self) -> float:
        s = self.settings
        # 2 lần chờ UI + chờ video + upload/download/bấm nút (có hạn riêng trong chat.py) + biên an toàn.
        return s.ui_timeout * 3 + s.video_timeout + s.job_overhead

    async def _cancel_render(self, job_id: str) -> None:
        assert self.chat is not None
        try:
            confirmed = await send_cancel(self.chat, timeout=15)
        except Exception as exc:  # /cancel là tối ưu, lỗi thì vẫn còn cơ chế chờ video về muộn
            log.warning("[%s] gửi /cancel lỗi: %s", self.name, exc)
            return
        if confirmed:
            self.db.set_account(self.name, orphan_cancel_at=time.time())
            log.info("[%s] %s đã /cancel, bot làm mới trạng thái", self.name, job_id)
        else:
            log.warning("[%s] %s gửi /cancel nhưng bot không xác nhận, chờ video về muộn", self.name, job_id)

    def _handle_failure(self, job: dict, exc: BaseException) -> None:
        job_id = job["id"]
        msg = str(exc) or type(exc).__name__
        if isinstance(exc, AccountExpired):
            self.db.requeue(job_id, refund_attempt=True, kind=exc.kind, message=msg)
            self._set("expired", msg)
        elif isinstance(exc, BotMaintenance):
            self.db.requeue(job_id, refund_attempt=True, kind=exc.kind, message=msg)
            self._cooldown(self.settings.maintenance_cooldown, msg)
        elif DEAD_ERRORS and isinstance(exc, DEAD_ERRORS) or isinstance(exc, AuthRequired):
            self.db.requeue(job_id, refund_attempt=True, kind="auth_required", message=msg)
            self._set("dead", f"Session chết: {type(exc).__name__}")
        elif FLOOD_ERRORS and isinstance(exc, FLOOD_ERRORS):
            seconds = int(getattr(exc, "seconds", 60) or 60)
            self.db.requeue(job_id, refund_attempt=True, kind="rate_limited", message=msg)
            self._cooldown(seconds + 5, f"FloodWait {seconds}s")
        elif INPUT_ERRORS and isinstance(exc, INPUT_ERRORS):
            self.db.finish_failed(job_id, kind="invalid_input", message=f"Telegram từ chối ảnh/prompt: {msg}")
            log.error("[%s] %s Telegram từ chối ảnh/prompt, không thử lại: %s", self.name, job_id, msg)
        elif isinstance(exc, (FileNotFoundError, IsADirectoryError, PermissionError)):
            self.db.finish_failed(job_id, kind="invalid_input", message=f"Không đọc/ghi được file: {msg}")
        elif isinstance(exc, NETWORK_ERRORS):
            self.db.requeue(job_id, refund_attempt=True, kind="network", message=msg)
            self._cooldown(self.settings.network_cooldown, f"Mất kết nối: {msg}")
        else:
            kind = exc.kind if isinstance(exc, DriverError) else "unknown"
            if not isinstance(exc, RetryableError):
                log.exception("[%s] %s lỗi không lường trước", self.name, job_id, exc_info=exc)
            if int(job["attempts"]) >= self.settings.max_attempts:
                self.db.finish_failed(job_id, kind=kind, message=msg)
                log.error("[%s] %s thất bại sau %d lượt: %s", self.name, job_id, job["attempts"], msg)
            else:
                self.db.requeue(job_id, refund_attempt=False, kind=kind, message=msg)
                log.warning("[%s] %s lỗi lượt %d, xếp lại: %s", self.name, job_id, job["attempts"], msg)


class Manager:
    """Giữ các worker, trả lời câu hỏi 'còn tài khoản nào chạy được không'."""

    def __init__(self, settings: Settings, db: DB, chat_factory: ChatFactory,
                 delay: Optional[Callable[[], Awaitable[None]]] = None):
        self.settings = settings
        self.db = db
        self.chat_factory = chat_factory
        self.delay = delay
        self.workers: dict[str, AccountWorker] = {}

    async def start(self) -> None:
        n = self.db.reset_running()
        if n:
            log.info("Đưa %d job chạy dở về hàng đợi", n)
        names = {a.name for a in self.settings.accounts}
        for row in self.db.list_accounts():
            if row["name"] not in names:
                self.db.set_account(row["name"], status="disabled", reason="Không còn trong accounts.yaml")
        for acc in self.settings.accounts:
            w = AccountWorker(acc, self.settings, self.db, self.chat_factory, self.delay)
            self.workers[acc.name] = w
            w.start()
        log.info("Khởi động %d worker: %s", len(self.workers), ", ".join(self.workers) or "(chưa có tài khoản)")
        self._fail_task = asyncio.create_task(self._fail_unrunnable_loop(), name="fail-unrunnable")

    async def stop(self) -> None:
        task = getattr(self, "_fail_task", None)
        if task:
            task.cancel()
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)

    def fail_unrunnable(self) -> int:
        """Job đang chờ mà không tài khoản nào chạy được (hết hạn / dead / tạm dừng / tắt) → fail ngay,
        giống MultiKit, để ReelFB không phải chờ tới hết timeout."""
        failed = 0
        for job in self.db.list_jobs(status="queued", limit=500):
            if self.usable_accounts(job["pinned_account"]):
                continue
            states = ", ".join(f"{r['name']}={r['status']}" for r in self.db.list_accounts()
                               if r["name"] in self.workers) or "chưa có tài khoản"
            if self.db.finish_failed(job["id"], kind="no_account_available",
                                     message=f"Không tài khoản nào chạy được ({states})"):
                failed += 1
        if failed:
            log.warning("Fail %d job đang chờ: không còn tài khoản dùng được", failed)
        return failed

    async def _fail_unrunnable_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.no_account_fail_check)
            try:
                self.fail_unrunnable()
            except Exception as exc:  # pragma: no cover
                log.exception("fail_unrunnable lỗi: %s", exc)

    def usable_accounts(self, pinned: Optional[str] = None) -> list[str]:
        out = []
        for row in self.db.list_accounts():
            if row["name"] not in self.workers:
                continue
            if pinned and row["name"] != pinned:
                continue
            if row["status"] in USABLE_STATUSES:
                out.append(row["name"])
        return out

    def cancel(self, job_id: str) -> bool:
        if not self.db.cancel(job_id):
            return False
        for w in self.workers.values():
            w.cancel_job(job_id)
        return True

    def resume(self, name: str) -> bool:
        if name not in self.workers or not self.workers[name].account.enabled:
            return False
        self.db.set_account(name, status="connecting", reason="Resume thủ công", cooldown_until=None)
        self.workers[name]._next_health = 0.0
        return True

    def pause(self, name: str) -> bool:
        if name not in self.workers:
            return False
        self.db.set_account(name, status="paused", reason="Tạm dừng thủ công", cooldown_until=None)
        return True
