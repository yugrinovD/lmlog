"""FastAPI application for ``lmlog serve``.

The module is imported lazily by the CLI so the core package keeps its
zero-dependency footprint.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import __version__
from ..config import (
    default_model,
    llm_concurrency,
    llm_mode,
    serve_api_token,
    serve_concurrency,
    serve_data_dir,
    serve_job_ttl_hours,
    serve_max_jobs,
    serve_max_upload_bytes,
)
from ..explain import choose_explanation
from ..llm import create_client
from ..state import State
from .chat import answer_chat, citations
from .jobs import ALLOWED_EXTENSIONS, JobManager, sse_event
from .schemas import (
    ChatRequest,
    ChatResponse,
    ExplainRequest,
    ExplainResponse,
    JobCreated,
    JobView,
    SignaturePage,
    SummaryView,
    TrendView,
)
from .snapshots import Snapshot, SnapshotCache

LOGGER = logging.getLogger("lmlog.serve")
MAX_JSON_BODY = 1 << 20
bearer = HTTPBearer(auto_error=False)


def _error(code: str, message: str, http_status: int, request: Request, **extra) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", ""),
            **extra,
        }
    }
    headers = {"WWW-Authenticate": "Bearer"} if http_status == 401 else None
    return JSONResponse(body, status_code=http_status, headers=headers)


def create_app(
    data_dir: str | None = None,
    api_token: str | None = None,
    max_upload_mb: float | None = None,
    use_llm: bool = True,
    max_jobs: int | None = None,
    job_ttl_hours: int | None = None,
    concurrency: int | None = None,
) -> FastAPI:
    token = (api_token if api_token is not None else serve_api_token()).strip()
    if not token:
        raise RuntimeError("LMLOG_API_TOKEN is required; refusing to start an unauthenticated API")
    upload_bytes = (
        int(max(1.0, min(float(max_upload_mb), 500.0)) * 1024 * 1024)
        if max_upload_mb is not None
        else serve_max_upload_bytes()
    )
    manager = JobManager(
        data_dir or serve_data_dir(),
        max_upload_bytes=upload_bytes,
        max_jobs=max_jobs if max_jobs is not None else serve_max_jobs(),
        job_ttl_hours=job_ttl_hours if job_ttl_hours is not None else serve_job_ttl_hours(),
        concurrency=concurrency if concurrency is not None else serve_concurrency(),
    )
    snapshots = SnapshotCache(max_size=8)
    llm_semaphore = threading.BoundedSemaphore(llm_concurrency())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        LOGGER.info("lmlog serve started", extra={"data_dir": manager.data_dir})
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(
        title="lmlog serve",
        version=__version__,
        description=(
            "Thin HTTP API over lmlog snapshots: upload logs, watch ingest progress "
            "over SSE, inspect signatures/errors/trends and ask grounded questions."
        ),
        lifespan=lifespan,
    )
    app.state.manager = manager
    app.state.snapshots = snapshots
    app.state.use_llm = use_llm
    app.state.llm_semaphore = llm_semaphore

    # --- middleware -------------------------------------------------------

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id", "").strip()[:128]
        if not request_id:
            request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        if (
            request.method in {"POST", "PUT", "PATCH"}
            and request.url.path.startswith("/api/v1/")
            and not request.url.path.endswith("/jobs")
        ):
            raw_length = request.headers.get("content-length")
            if raw_length:
                try:
                    if int(raw_length) > MAX_JSON_BODY:
                        return _error(
                            "request_too_large",
                            "JSON request body is too large",
                            413,
                            request,
                        )
                except ValueError:
                    return _error("invalid_request", "invalid Content-Length", 400, request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code", "http_error"))
            message = str(detail.get("message", detail.get("detail", "request failed")))
            extra = {k: v for k, v in detail.items() if k not in {"code", "message"}}
        else:
            code = {
                400: "invalid_request",
                401: "unauthorized",
                404: "not_found",
                409: "conflict",
                413: "file_too_large",
                415: "unsupported_media_type",
                422: "invalid_request",
            }.get(exc.status_code, "http_error")
            message = str(detail)
            extra = {}
        return _error(code, message, exc.status_code, request, **extra)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        errors = []
        for item in exc.errors()[:20]:
            errors.append(
                {
                    "path": ".".join(str(part) for part in item.get("loc", [])),
                    "message": item.get("msg", "invalid value"),
                }
            )
        return _error("invalid_request", "request validation failed", 422, request, details=errors)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        LOGGER.exception("unhandled error on %s", request.url.path)
        return _error("internal_error", "internal server error", 500, request)

    # --- auth -------------------------------------------------------------

    def require_auth(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
    ) -> None:
        supplied = credentials.credentials if credentials else ""
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(supplied, token)
        ):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "missing or invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    # --- helpers ----------------------------------------------------------

    def completed_job(job_id: str):
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": "job not found"}
            )
        if job.status == "failed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "job_failed",
                    "message": job.error or "job failed",
                },
            )
        if job.status != "completed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "job_not_ready",
                    "message": f"job is {job.status}",
                    "status": job.status,
                },
            )
        return job

    def load_snapshot(job_id: str) -> tuple[object, Snapshot]:
        job = completed_job(job_id)
        try:
            snapshot = snapshots.get(job)
        except Exception as exc:  # noqa: BLE001 - corrupt or missing SQLite state
            LOGGER.exception("cannot load snapshot %s", job_id)
            raise HTTPException(
                status_code=500,
                detail={"code": "snapshot_unavailable", "message": "snapshot is unavailable"},
            ) from exc
        return job, snapshot

    def snapshot_meta(snapshot: Snapshot) -> dict:
        return {
            "created": snapshot.created,
            "period_hours": snapshot.period_hours,
            "total_entries": snapshot.total_entries,
            "signatures": len(snapshot.clusters),
        }

    def parse_last_event_id(request: Request) -> int:
        raw = (
            request.headers.get("last-event-id") or request.query_params.get("last_event_id") or "0"
        )
        try:
            return max(0, int(raw))
        except ValueError:
            return 0

    # --- health -----------------------------------------------------------

    @app.get("/healthz", tags=["system"])
    def healthz():
        return {
            "status": "ok",
            "version": __version__,
            "jobs": len(manager._jobs),
            "llm": {
                "mode": llm_mode(),
                "model": default_model(),
            },
        }

    # --- jobs -------------------------------------------------------------

    @app.post(
        "/api/v1/jobs",
        response_model=JobCreated,
        status_code=202,
        dependencies=[Depends(require_auth)],
        tags=["jobs"],
    )
    async def create_job(request: Request, file: Annotated[UploadFile, File(...)]):
        original = os.path.basename((file.filename or "").replace("\\", "/"))
        extension = os.path.splitext(original)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail={
                    "code": "unsupported_file_type",
                    "message": "only .log, .txt and .jsonl files are supported",
                },
            )
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > manager.max_upload_bytes + 2 * 1024 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "code": "file_too_large",
                            "message": f"upload limit is {manager.max_upload_bytes} bytes",
                        },
                    )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_request", "message": "invalid Content-Length"},
                ) from exc

        job, path = manager.create(original, 0)
        size = 0
        try:
            with open(path, "wb") as destination:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > manager.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail={
                                "code": "file_too_large",
                                "message": f"upload limit is {manager.max_upload_bytes} bytes",
                            },
                        )
                    destination.write(chunk)
            if size == 0:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "empty_file", "message": "uploaded file is empty"},
                )
            job.size_bytes = size
            manager.save(job)
        except BaseException:
            manager.remove(job.id)
            raise
        finally:
            await file.close()
        await manager.enqueue(job)
        return {
            "job_id": job.id,
            "status": job.status,
            "filename": job.filename,
            "size_bytes": size,
        }

    @app.get(
        "/api/v1/jobs",
        response_model=list[JobView],
        dependencies=[Depends(require_auth)],
        tags=["jobs"],
    )
    def list_jobs(limit: int = 20):
        limit = max(1, min(limit, 100))
        return [job.view() for job in manager.list(limit)]

    @app.get(
        "/api/v1/jobs/{job_id}",
        response_model=JobView,
        dependencies=[Depends(require_auth)],
        tags=["jobs"],
    )
    def get_job(job_id: str):
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": "job not found"}
            )
        return job.view()

    @app.get(
        "/api/v1/jobs/{job_id}/events",
        dependencies=[Depends(require_auth)],
        tags=["jobs"],
    )
    async def job_events(job_id: str, request: Request):
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": "job not found"}
            )
        last_event_id = parse_last_event_id(request)
        if job.terminal and last_event_id >= job.last_event_id:
            return Response(status_code=204)

        async def stream():
            queue = manager.subscribe(job)
            last = last_event_id
            try:
                yield "retry: 3000\n\n"
                for event in manager.events_since(job, last):
                    last = int(event["id"])
                    yield sse_event(event)
                if job.terminal:
                    return
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if int(event["id"]) <= last:
                        continue
                    last = int(event["id"])
                    yield sse_event(event)
                    if event["event"] in {"completed", "failed"}:
                        return
            finally:
                manager.unsubscribe(job, queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # --- snapshots --------------------------------------------------------

    @app.get(
        "/api/v1/snapshots/{snapshot_id}/summary",
        response_model=SummaryView,
        dependencies=[Depends(require_auth)],
        tags=["snapshots"],
    )
    def snapshot_summary(snapshot_id: str):
        _, snapshot = load_snapshot(snapshot_id)
        return snapshot.summary()

    @app.get(
        "/api/v1/snapshots/{snapshot_id}/signatures",
        response_model=SignaturePage,
        dependencies=[Depends(require_auth)],
        tags=["snapshots"],
    )
    def snapshot_signatures(
        snapshot_id: str,
        q: str = "",
        level: str | None = None,
        has_errors: bool | None = None,
        sort: str = "count",
        order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        _, snapshot = load_snapshot(snapshot_id)
        if len(q) > 200:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_query", "message": "q is too long"},
            )
        if level and level.upper() not in {
            "DEBUG",
            "INFO",
            "NOTICE",
            "WARNING",
            "WARN",
            "ERROR",
            "CRITICAL",
            "ALERT",
            "EMERGENCY",
        }:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_level", "message": "unknown log level"},
            )
        if sort not in {"count", "errors", "last_ts", "first_ts", "signature"}:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_sort", "message": "unknown sort field"},
            )
        if order not in {"asc", "desc"}:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_order", "message": "order must be asc or desc"},
            )
        limit = max(1, min(limit, 200))
        offset = max(0, min(offset, 1_000_000))
        needle = q.strip().lower()
        filtered = []
        for cluster in snapshot.clusters:
            if level and not any(
                key.upper() == level.upper() and int(value) > 0
                for key, value in cluster.levels.items()
            ):
                continue
            if has_errors is True and cluster.errors == 0:
                continue
            if has_errors is False and cluster.errors > 0:
                continue
            if needle:
                haystack = " ".join(
                    [
                        cluster.signature,
                        cluster.sample_message,
                        " ".join(cluster.classes),
                    ]
                ).lower()
                if needle not in haystack:
                    continue
            filtered.append(cluster)
        reverse = order == "desc"
        if sort == "errors":
            filtered.sort(key=lambda c: (c.errors, c.count), reverse=reverse)
        elif sort == "last_ts":
            filtered.sort(key=lambda c: c.last_key or c.last_ts, reverse=reverse)
        elif sort == "first_ts":
            filtered.sort(key=lambda c: c.first_key or c.first_ts, reverse=reverse)
        elif sort == "signature":
            filtered.sort(key=lambda c: c.signature, reverse=reverse)
        else:
            filtered.sort(key=lambda c: c.count, reverse=reverse)
        page = filtered[offset : offset + limit]
        return {
            "items": [snapshot.signature_view(cluster) for cluster in page],
            "total": len(filtered),
            "limit": limit,
            "offset": offset,
            "snapshot": snapshot_meta(snapshot),
        }

    @app.get(
        "/api/v1/snapshots/{snapshot_id}/errors",
        response_model=SignaturePage,
        dependencies=[Depends(require_auth)],
        tags=["snapshots"],
    )
    def snapshot_errors(snapshot_id: str, limit: int = 50, offset: int = 0):
        _, snapshot = load_snapshot(snapshot_id)
        limit = max(1, min(limit, 200))
        offset = max(0, min(offset, 1_000_000))
        errors = sorted(
            (cluster for cluster in snapshot.clusters if cluster.errors > 0),
            key=lambda cluster: (cluster.errors, cluster.count),
            reverse=True,
        )
        page = errors[offset : offset + limit]
        return {
            "items": [snapshot.signature_view(cluster) for cluster in page],
            "total": len(errors),
            "limit": limit,
            "offset": offset,
            "snapshot": snapshot_meta(snapshot),
        }

    @app.get(
        "/api/v1/snapshots/{snapshot_id}/trends",
        response_model=list[TrendView],
        dependencies=[Depends(require_auth)],
        tags=["snapshots"],
    )
    def snapshot_trends(
        snapshot_id: str,
        days: int = 7,
        spike: float = 3.0,
        limit: int = 20,
    ):
        _, snapshot = load_snapshot(snapshot_id)
        days = max(1, min(days, 30))
        limit = max(1, min(limit, 100))
        if spike <= 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_spike", "message": "spike must be greater than zero"},
            )
        return snapshot.trends(days=days, spike=spike, limit=limit)

    # --- explain / chat ---------------------------------------------------

    @app.post(
        "/api/v1/explain",
        response_model=ExplainResponse,
        dependencies=[Depends(require_auth)],
        tags=["ai"],
    )
    def explain(payload: ExplainRequest):
        job, snapshot = load_snapshot(payload.snapshot_id)
        cluster = snapshot.find(payload.signature)
        if cluster is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "signature_not_found", "message": "signature not found"},
            )
        state = State(job.db_path)
        if not payload.refresh:
            cached = state.get_explanation(payload.signature)
            if cached:
                return {
                    "signature": payload.signature,
                    "text": cached,
                    "source": "cache",
                    "cached": True,
                    "model": None,
                }
        finding = snapshot.finding(cluster)
        use_llm = payload.use_llm and app.state.use_llm
        client = create_client()
        if use_llm and not client.health():
            use_llm = False
        with app.state.llm_semaphore:
            text, source = choose_explanation(
                finding,
                client,
                use_llm,
                period=f"snapshot from {snapshot.created or 'unknown time'}",
            )
        state.set_explanation(payload.signature, text)
        return {
            "signature": payload.signature,
            "text": text,
            "source": source,
            "cached": False,
            "model": default_model() if source == "llm" else None,
        }

    @app.post(
        "/api/v1/chat",
        response_model=ChatResponse,
        dependencies=[Depends(require_auth)],
        tags=["ai"],
    )
    def chat(payload: ChatRequest):
        _, snapshot = load_snapshot(payload.snapshot_id)
        selected = snapshot.chat_clusters(payload.signature, payload.top_n)
        if payload.signature and not any(
            cluster.signature == payload.signature for cluster in selected
        ):
            raise HTTPException(
                status_code=404,
                detail={"code": "signature_not_found", "message": "signature not found"},
            )
        with app.state.llm_semaphore:
            answer, source, model = answer_chat(
                snapshot,
                payload.question,
                selected,
                payload.history,
                payload.use_llm and app.state.use_llm,
            )
        return {
            "answer": answer,
            "source": source,
            "citations": citations(snapshot, selected),
            "model": model,
            "note": (
                "Grounded on the snapshot's top signatures and the selected cluster. "
                "No retrieval or embeddings are used."
            ),
        }

    return app
