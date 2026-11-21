"""Background upload jobs and SSE event fan-out for ``lmlog serve``."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..cluster import Clusterer
from ..detect import detect, detect_trends
from ..ingest import iter_file_entries, stat_cursor
from ..normalize import build_signature
from ..state import State

LOGGER = logging.getLogger("lmlog.serve")

ALLOWED_EXTENSIONS = {".log", ".txt", ".jsonl"}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_filename(raw: str | None) -> tuple[str, str]:
    """Return ``(display_name, extension)`` without trusting the upload name.

    The display name is only cosmetic: storage always uses a generated name.
    """
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = _CONTROL_CHARS.sub("", name)
    name = _UNSAFE_NAME.sub("_", name)
    name = name.strip(". ")[:200]
    if not name:
        name = "upload.log"
    extension = os.path.splitext(name)[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        extension = ".log"
        name = f"{os.path.splitext(name)[0] or 'upload'}.log"
    return name, extension


@dataclass
class Job:
    id: str
    filename: str
    stored_name: str
    size_bytes: int
    db_path: str
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict = field(default_factory=dict)
    stats: dict | None = None
    error: str | None = None
    events: deque = field(default_factory=lambda: deque(maxlen=500))
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    next_event_id: int = 1

    def view(self) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": dict(self.progress),
            "stats": self.stats,
            "error": self.error,
        }

    def to_disk(self) -> dict:
        data = self.view()
        data["stored_name"] = self.stored_name
        return data

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed"}

    @property
    def last_event_id(self) -> int:
        return self.events[-1]["id"] if self.events else 0


class JobManager:
    def __init__(
        self,
        data_dir: str,
        max_upload_bytes: int = 50 * 1024 * 1024,
        max_jobs: int = 50,
        job_ttl_hours: int = 168,
        concurrency: int = 2,
    ):
        self.data_dir = os.path.abspath(data_dir)
        self.jobs_dir = os.path.join(self.data_dir, "jobs")
        self.max_upload_bytes = max_upload_bytes
        self.max_jobs = max_jobs
        self.job_ttl_hours = job_ttl_hours
        self.concurrency = concurrency
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        os.makedirs(self.jobs_dir, exist_ok=True)
        self._load_existing()

    # --- persistence ---

    def _job_dir(self, job_id: str) -> str:
        return os.path.join(self.jobs_dir, job_id)

    def _job_json(self, job_id: str) -> str:
        return os.path.join(self._job_dir(job_id), "job.json")

    def save(self, job: Job) -> None:
        self._save(job)

    def _save(self, job: Job) -> None:
        path = self._job_json(job.id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(job.to_disk(), fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _load_existing(self) -> None:
        if not os.path.isdir(self.jobs_dir):
            return
        restored = 0
        for entry in os.scandir(self.jobs_dir):
            if not entry.is_dir() or not _is_uuid(entry.name):
                continue
            path = os.path.join(entry.path, "job.json")
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                job = Job(
                    id=data["id"],
                    filename=data.get("filename", "upload.log"),
                    stored_name=data.get("stored_name", "input.log"),
                    size_bytes=int(data.get("size_bytes", 0)),
                    db_path=os.path.join(entry.path, "state.db"),
                    status=data.get("status", "failed"),
                    created_at=data.get("created_at", utc_now()),
                    started_at=data.get("started_at"),
                    finished_at=data.get("finished_at"),
                    progress=data.get("progress") or {},
                    stats=data.get("stats"),
                    error=data.get("error"),
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                LOGGER.warning("cannot restore job %s: %s", entry.name, exc)
                continue
            if job.status in {"queued", "running"}:
                job.status = "failed"
                job.error = "server restarted before the job finished"
                job.finished_at = utc_now()
                self._save(job)
            self._jobs[job.id] = job
            restored += 1
        if restored:
            LOGGER.info("restored %d upload jobs", restored)
        self._prune()

    # --- creation and lookup ---

    def create(self, original_name: str | None, size_bytes: int) -> tuple[Job, str]:
        job_id = str(uuid.uuid4())
        filename, extension = sanitize_filename(original_name)
        directory = self._job_dir(job_id)
        os.makedirs(directory, exist_ok=False)
        stored_name = f"input{extension}"
        job = Job(
            id=job_id,
            filename=filename,
            stored_name=stored_name,
            size_bytes=size_bytes,
            db_path=os.path.join(directory, "state.db"),
        )
        self._jobs[job_id] = job
        self._save(job)
        self._prune()
        return job, os.path.join(directory, stored_name)

    def get(self, job_id: str) -> Job | None:
        if not _is_uuid(job_id):
            return None
        return self._jobs.get(job_id)

    def list(self, limit: int = 20) -> list[Job]:
        items = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return items[: max(1, min(limit, 100))]

    def remove(self, job_id: str) -> bool:
        job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        shutil.rmtree(self._job_dir(job_id), ignore_errors=True)
        return True

    # --- execution ---

    async def enqueue(self, job: Job) -> None:
        self._loop = asyncio.get_running_loop()
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        task = asyncio.create_task(self._run(job), name=f"lmlog-job-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job: Job) -> None:
        assert self._semaphore is not None
        async with self._semaphore:
            if job.status == "failed":
                return
            job.status = "running"
            job.started_at = utc_now()
            self._save(job)
            self._publish(job, "started", {"status": "running", "size_bytes": job.size_bytes})
            try:
                stats = await asyncio.to_thread(self._pipeline, job)
            except Exception:  # noqa: BLE001 - report a safe message
                LOGGER.exception("job %s failed", job.id)
                job.status = "failed"
                job.error = "ingest failed; see the server log for details"
                job.finished_at = utc_now()
                job.progress = {**job.progress, "percent": 100.0}
                self._save(job)
                self._publish(job, "failed", {"status": "failed", "message": job.error})
                return
            job.status = "completed"
            job.finished_at = utc_now()
            job.stats = stats
            job.progress = {
                "phase": "completed",
                "percent": 100.0,
                "lines": stats.get("lines", 0),
                "entries": stats.get("entries", 0),
                "bytes": job.size_bytes,
                "total_bytes": job.size_bytes,
            }
            self._save(job)
            self._publish(job, "completed", {"status": "completed", **stats})

    def _pipeline(self, job: Job) -> dict:
        path = os.path.join(self._job_dir(job.id), job.stored_name)
        size = os.path.getsize(path)
        if size <= 0:
            raise ValueError("uploaded file is empty")
        state = State(job.db_path)
        clusterer = Clusterer()
        daily: dict[str, dict[str, int]] = {}
        lines = 0
        entries = 0
        last_offset = 0
        last_lineno = 0
        started = time.perf_counter()
        last_report = started

        def report(force: bool = False) -> None:
            nonlocal last_report
            now = time.perf_counter()
            if not force and now - last_report < 0.25:
                return
            last_report = now
            if self._loop is None:
                return
            payload = {
                "phase": "ingest",
                "percent": round(min(99.0, last_offset / max(size, 1) * 100.0), 1),
                "lines": lines,
                "entries": entries,
                "bytes": last_offset,
                "total_bytes": size,
            }
            self._loop.call_soon_threadsafe(self._publish, job, "progress", payload)

        for entry, end_offset, lineno in iter_file_entries(path):
            lines += 1
            last_offset = end_offset
            last_lineno = lineno
            if entry is None:
                report()
                continue
            entries += 1
            entry.signature = build_signature(entry)
            clusterer.add(entry)
            date = entry.ts[:10]
            by_date = daily.setdefault(entry.signature, {})
            by_date[date] = by_date.get(date, 0) + 1
            report()

        if lines == 0:
            raise ValueError("uploaded file has no complete lines")
        report(force=True)
        cursor = stat_cursor(path)
        state.replace_rebuild(
            [(job.filename, cursor.size, cursor.mtime, last_lineno, cursor.inode)],
            clusterer.clusters.values(),
            clusterer.clusters.values(),
            daily,
            None,
        )
        clusters = list(clusterer.clusters.values())
        findings = [f for f in detect(clusters, known=set()) if f.kind != "new"]
        trends = detect_trends(clusters, daily, days_back=7)
        elapsed = max(time.perf_counter() - started, 1e-9)
        total_errors = sum(cluster.errors for cluster in clusters)
        levels: dict[str, int] = {}
        for cluster in clusters:
            for level, count in cluster.levels.items():
                levels[level] = levels.get(level, 0) + int(count)
        return {
            "lines": lines,
            "entries": entries,
            "signatures": len(clusters),
            "errors": total_errors,
            "levels": levels,
            "findings": len(findings),
            "trends": len(trends),
            "elapsed_seconds": round(elapsed, 3),
            "lines_per_second": int(lines / elapsed),
        }

    # --- events ---

    def _publish(self, job: Job, event: str, data: dict) -> None:
        event_id = job.next_event_id
        job.next_event_id += 1
        payload = {"id": event_id, "event": event, "data": data}
        job.events.append(payload)
        if event == "progress":
            job.progress = dict(data)
        for queue in list(job.subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A slow consumer must not block ingest or other subscribers.
                continue

    def subscribe(self, job: Job) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        job.subscribers.add(queue)
        return queue

    def unsubscribe(self, job: Job, queue: asyncio.Queue) -> None:
        job.subscribers.discard(queue)

    def events_since(self, job: Job, last_id: int) -> list[dict]:
        return [event for event in job.events if event["id"] > last_id]

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for job in self._jobs.values():
            if not job.terminal:
                job.status = "failed"
                job.error = "server stopped before the job finished"
                job.finished_at = utc_now()
                self._save(job)

    # --- retention ---

    def _prune(self) -> None:
        cutoff = time.time() - self.job_ttl_hours * 3600
        ordered = sorted(self._jobs.values(), key=lambda j: j.created_at)
        remove: list[str] = []
        for job in ordered:
            try:
                created = datetime.fromisoformat(job.created_at).timestamp()
            except ValueError:
                created = time.time()
            if created < cutoff:
                remove.append(job.id)
        for job in ordered[: max(0, len(ordered) - self.max_jobs)]:
            if job.id not in remove:
                remove.append(job.id)
        for job_id in remove:
            self.remove(job_id)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def sse_event(event: dict) -> str:
    data = json.dumps(event["data"], ensure_ascii=False, separators=(",", ":"))
    return f"id: {event['id']}\nevent: {event['event']}\ndata: {data}\n\n"
