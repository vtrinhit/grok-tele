"""HTTP API tương thích MultiKit Grok (`/grok/jobs`, `/grok/accounts`...).

ReelFB Studio dùng provider `multikit` có sẵn, chỉ cần đổi URL sang cổng GrokTele.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from groktele import __version__
from groktele.config import RESOLUTION_MAP, VALID_ASPECTS, VALID_DURATIONS, Settings
from groktele.db import DB, TERMINAL, new_id
from groktele.worker import Manager

log = logging.getLogger("groktele.api")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PROMPT = 2000


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def _err(status: int, code: str, message: str, details: Optional[dict] = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status)


_HTTP_CODES = {400: "invalid_input", 401: "unauthorized", 403: "forbidden_origin", 404: "not_found",
               405: "invalid_input", 409: "conflict", 413: "payload_too_large", 503: "unavailable"}


# ------------------------------------------------------------- serialization
def job_to_api(job: dict) -> dict:
    result = None
    if job["status"] == "succeeded":
        result = {
            "filePath": job["output_path"],
            "bytes": job["result_bytes"],
            "mimeType": "video/mp4",
            "durationMs": job["result_duration_ms"],
        }
    error = None
    if job["status"] in ("failed", "cancelled") and job["error_kind"]:
        error = {"kind": job["error_kind"], "message": job["error_message"] or ""}
    elif job["status"] in ("queued", "running") and job["error_kind"]:
        # Lỗi của lượt trước, job đang được thử lại.
        error = None
    return {
        "jobId": job["id"],
        "status": job["status"],
        "stage": job["stage"],
        "progress": round(float(job["progress"] or 0), 3),
        "attempts": job["attempts"],
        "accountId": job["account"],
        "accountLabel": job["account"],
        "clientRef": job["client_ref"],
        "batchId": job["batch_id"],
        "priority": job["priority"],
        "input": {
            "imagePath": job["image_path"],
            "prompt": job["prompt"],
            "aspectRatio": job["aspect"],
            "duration": job["duration"],
            "resolution": job["resolution"],
            "outputPath": job["output_path"],
        },
        "lastError": ({"kind": job["error_kind"], "message": job["error_message"]}
                      if job["status"] in ("queued", "running") and job["error_kind"] else None),
        "createdAt": job["created_at"],
        "startedAt": job["started_at"],
        "finishedAt": job["finished_at"],
        "result": result,
        "error": error,
    }


# ---------------------------------------------------------------- validation
def _field_error(field: str, message: str, index: Optional[int] = None) -> ApiError:
    details: dict[str, Any] = {"field": field}
    if index is not None:
        details["index"] = index
    return ApiError(400, "invalid_input", f"{field}: {message}", details)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "image.png"


def validate_job(body: Any, settings: Settings, index: Optional[int] = None) -> dict:
    """Body kiểu MultiKit → tham số job (chưa lưu ảnh base64)."""
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_input", "Body phải là JSON object", {"index": index} if index is not None else None)

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _field_error("prompt", "bắt buộc", index)
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT:
        raise _field_error("prompt", f"tối đa {MAX_PROMPT} ký tự", index)

    image_path = body.get("imagePath")
    image_b64 = body.get("imageBase64")
    if not image_path and not image_b64:
        raise _field_error("imagePath", "cần imagePath hoặc imageBase64", index)
    if image_path:
        p = Path(str(image_path))
        if not p.is_absolute():
            raise _field_error("imagePath", "phải là đường dẫn tuyệt đối", index)
        if not p.is_file():
            raise _field_error("imagePath", f"không tồn tại: {p}", index)
        if p.suffix.lower() not in IMAGE_EXTS:
            raise _field_error("imagePath", "chỉ nhận jpg, png, webp", index)
        if p.stat().st_size > MAX_IMAGE_BYTES:
            raise _field_error("imagePath", "ảnh tối đa 10 MB", index)
        image_path = str(p)
    else:
        if not isinstance(image_b64, str):
            raise _field_error("imageBase64", "phải là chuỗi base64", index)
        raw = image_b64.split(",", 1)[1] if image_b64.startswith("data:") else image_b64
        try:
            data = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            raise _field_error("imageBase64", "base64 không hợp lệ", index)
        if not data:
            raise _field_error("imageBase64", "rỗng", index)
        if len(data) > MAX_IMAGE_BYTES:
            raise _field_error("imageBase64", "ảnh tối đa 10 MB", index)
        name = _safe_name(str(body.get("imageFileName") or "image.png"))
        if Path(name).suffix.lower() not in IMAGE_EXTS:
            name += ".png"
        image_path = None
        body = {**body, "_image_bytes": data, "_image_name": name}

    aspect = str(body.get("aspectRatio") or settings.default_aspect)
    if aspect not in VALID_ASPECTS:
        raise _field_error("aspectRatio", f"bot chỉ hỗ trợ {', '.join(VALID_ASPECTS)}", index)

    raw_duration = body.get("duration", settings.default_duration)
    try:
        duration = int(str(raw_duration).strip().rstrip("s"))
    except (TypeError, ValueError):
        raise _field_error("duration", "phải là 6, 10 hoặc 15 (giây)", index)
    if duration not in VALID_DURATIONS:
        raise _field_error("duration", "phải là 6, 10 hoặc 15 (giây)", index)

    res_in = str(body.get("resolution") or settings.default_resolution).lower()
    if res_in not in RESOLUTION_MAP:
        raise _field_error("resolution", "phải là 480p, 720p hoặc 1080p", index)

    output_path = body.get("outputPath")
    output_dir = body.get("outputDir")
    if output_path:
        op = Path(str(output_path))
        if not op.is_absolute():
            raise _field_error("outputPath", "phải là đường dẫn tuyệt đối", index)
        if op.suffix.lower() != ".mp4":
            raise _field_error("outputPath", "phải kết thúc bằng .mp4", index)
    if output_dir and not Path(str(output_dir)).is_absolute():
        raise _field_error("outputDir", "phải là đường dẫn tuyệt đối", index)

    try:
        priority = int(body.get("priority") or 0)
    except (TypeError, ValueError):
        raise _field_error("priority", "phải là số nguyên", index)

    client_ref = body.get("clientRef")
    account = body.get("accountId")
    return {
        "prompt": prompt,
        "image_path": image_path,
        "image_bytes": body.get("_image_bytes"),
        "image_name": body.get("_image_name"),
        "aspect": aspect,
        "duration": duration,
        "resolution": RESOLUTION_MAP[res_in],
        "output_path": str(output_path) if output_path else None,
        "output_dir": str(output_dir) if output_dir else None,
        "priority": priority,
        "client_ref": str(client_ref) if client_ref else None,
        "account": str(account) if account else None,
    }


# ----------------------------------------------------------------------- app
def create_api(settings: Settings, db: DB, manager: Manager, lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="GrokTele", version=__version__, lifespan=lifespan,
                  docs_url="/docs", redoc_url=None)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _err(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _err(exc.status_code, _HTTP_CODES.get(exc.status_code, "internal"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _err(400, "invalid_input", "Body JSON không hợp lệ", {"errors": exc.errors()[:5]})

    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception) -> JSONResponse:
        log.exception("Lỗi API: %s", exc)
        return _err(500, "internal", str(exc) or type(exc).__name__)

    def auth(request: Request) -> None:
        if not settings.api_key:
            return
        header = request.headers.get("authorization", "")
        key = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if key != settings.api_key:
            raise ApiError(401, "unauthorized", "Sai hoặc thiếu API key")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "app": "GrokTele", "version": __version__, "features": ["grok"]}

    r = APIRouter(prefix="/grok", dependencies=[Depends(auth)])

    def _no_account_error(pinned: Optional[str], index: Optional[int] = None) -> ApiError:
        rows = db.list_accounts()
        if not manager.workers:
            why = "no_account"
        elif pinned and pinned not in manager.workers:
            why = "no_account"
        else:
            why = "needs_human"
        details: dict[str, Any] = {"reason": "no_account_available", "why": why,
                                   "accounts": {r_["name"]: r_["status"] for r_ in rows}}
        if index is not None:
            details["index"] = index
        msg = ("no_account_available: chưa có tài khoản nào (chạy python -m groktele login <tên>)"
               if why == "no_account" else
               "no_account_available: mọi tài khoản đều hết hạn / tạm dừng / cần login lại")
        return ApiError(409, "conflict", msg, details)

    def _materialize(v: dict) -> dict:
        """Lưu ảnh base64, quyết định output path."""
        job_id = new_id("job")
        image_path = v["image_path"]
        if not image_path:
            settings.uploads_dir.mkdir(parents=True, exist_ok=True)
            dest = settings.uploads_dir / f"{job_id}_{v['image_name']}"
            dest.write_bytes(v["image_bytes"])
            image_path = str(dest)
        if v["output_path"]:
            output = v["output_path"]
        else:
            folder = Path(v["output_dir"]) if v["output_dir"] else settings.outputs_dir
            stem = _safe_name(v["client_ref"]) if v["client_ref"] else job_id
            output = str(folder / f"{stem}.mp4")
        return {"job_id": job_id, "image_path": image_path, "output_path": output}

    def _create(v: dict, batch_id: Optional[str] = None) -> tuple[dict, bool]:
        if v["client_ref"]:
            existing = db.find_active_by_client_ref(v["client_ref"])
            if existing:
                return existing, True
        m = _materialize(v)
        job = db.create_job(
            job_id=m["job_id"], image_path=m["image_path"], prompt=v["prompt"], aspect=v["aspect"],
            duration=v["duration"], resolution=v["resolution"], output_path=m["output_path"],
            priority=v["priority"], client_ref=v["client_ref"], pinned_account=v["account"], batch_id=batch_id,
        )
        log.info("Nhận %s (%s %ss %s): %s", job["id"], v["aspect"], v["duration"], v["resolution"], v["prompt"][:60])
        return job, False

    @r.post("/jobs", status_code=202)
    async def create_job(request: Request) -> JSONResponse:
        body = await _json(request)
        v = validate_job(body, settings)
        if not manager.usable_accounts(v["account"]):
            raise _no_account_error(v["account"])
        job, dedup = _create(v)
        return JSONResponse({"jobId": job["id"], "status": job["status"], "deduplicated": dedup}, status_code=202)

    @r.post("/jobs/batch", status_code=202)
    async def create_batch(request: Request) -> JSONResponse:
        body = await _json(request)
        if not isinstance(body, dict) or not isinstance(body.get("jobs"), list) or not body["jobs"]:
            raise ApiError(400, "invalid_input", "jobs: cần mảng jobs không rỗng", {"field": "jobs"})
        defaults = body.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise ApiError(400, "invalid_input", "defaults: phải là object", {"field": "defaults"})
        validated = [validate_job({**defaults, **(item if isinstance(item, dict) else {})}, settings, i)
                     for i, item in enumerate(body["jobs"])]
        for i, v in enumerate(validated):
            if not manager.usable_accounts(v["account"]):
                raise _no_account_error(v["account"], i)
        batch_id = new_id("bat")
        ids = [_create(v, batch_id)[0]["id"] for v in validated]
        return JSONResponse({"batchId": batch_id, "jobIds": ids}, status_code=202)

    @r.get("/jobs")
    async def list_jobs(status: Optional[str] = None, batchId: Optional[str] = None,
                        clientRef: Optional[str] = None, limit: int = 50, cursor: Optional[str] = None) -> dict:
        jobs = db.list_jobs(status=status, batch_id=batchId, client_ref=clientRef, limit=limit, cursor=cursor)
        return {"jobs": [job_to_api(j) for j in jobs],
                "nextCursor": jobs[-1]["id"] if len(jobs) >= max(1, min(limit, 500)) else None}

    @r.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        job = db.get_job(job_id)
        if not job:
            raise ApiError(404, "not_found", f"Không có job {job_id}")
        return job_to_api(job)

    @r.delete("/jobs/{job_id}")
    async def cancel_job(job_id: str) -> dict:
        job = db.get_job(job_id)
        if not job:
            raise ApiError(404, "not_found", f"Không có job {job_id}")
        if job["status"] in TERMINAL or not manager.cancel(job_id):
            raise ApiError(409, "conflict", f"Job đã kết thúc ({job['status']})")
        return {"status": "cancelled"}

    @r.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str) -> dict:
        job = db.get_job(job_id)
        if not job:
            raise ApiError(404, "not_found", f"Không có job {job_id}")
        if not db.retry(job_id):
            raise ApiError(409, "conflict", f"Chỉ retry được job failed/cancelled (đang {job['status']})")
        return {"status": "queued"}

    @r.get("/accounts")
    async def accounts(refresh: int = 0) -> dict:
        if refresh:
            for w in manager.workers.values():
                w._next_health = 0.0
        rows = {a["name"]: a for a in db.list_accounts()}
        out = []
        for name, w in manager.workers.items():
            a = rows.get(name) or {}
            status = a.get("status") or "connecting"
            usable = w.account.enabled and status in ("active", "cooldown", "connecting")
            out.append({
                "id": name,
                "label": name,
                "enabled": w.account.enabled,
                "status": status,
                "reason": a.get("reason"),
                "cooldownUntil": a.get("cooldown_until"),
                "runningJobs": 1 if w.current_job else 0,
                "currentJob": w.current_job,
                "jobsDone": a.get("jobs_done", 0),
                "expiresAt": a.get("expires_at"),
                "lastCheck": a.get("last_check"),
                "telegramUser": a.get("telegram_user"),
                "usableFor": {"480p": usable, "720p": usable, "1080p": usable},
            })
        counts = db.counts()
        return {
            "accounts": out,
            "summary": {
                "total": len(out),
                "usable": sum(1 for a in out if a["usableFor"]["720p"]),
                "active": sum(1 for a in out if a["status"] == "active"),
                "queuedJobs": counts["queued"],
                "runningJobs": counts["running"],
            },
        }

    @r.post("/accounts/{name}/pause")
    async def pause(name: str) -> dict:
        if not manager.pause(name):
            raise ApiError(404, "not_found", f"Không có tài khoản {name}")
        return {"name": name, "status": "paused"}

    @r.post("/accounts/{name}/resume")
    async def resume(name: str) -> dict:
        if not manager.resume(name):
            raise ApiError(404, "not_found", f"Không có tài khoản {name} (hoặc enabled: false)")
        return {"name": name, "status": "connecting"}

    @r.get("/stats")
    async def stats() -> dict:
        return db.stats()

    app.include_router(r)
    return app


async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        raise ApiError(400, "invalid_input", "Body phải là JSON hợp lệ")
