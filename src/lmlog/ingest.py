"""Streaming, incremental ingestion of log files and journald.

The reader works on binary files so that byte offsets are exact. Entries are
consumed one by one: no list of all log lines is ever materialised, which keeps
memory usage flat even for tens of millions of lines per day.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass

from .cluster import Cluster, Clusterer
from .normalize import build_signature
from .parser import LogEntry, parse_line

DEFAULT_PATTERNS = ("console-*.log", "prod-*.log", "courses_console-*.log", "*.log")


@dataclass(slots=True)
class Cursor:
    size: int
    mtime: float
    lineno: int
    inode: int


def stat_cursor(path: str) -> Cursor:
    st = os.stat(path)
    return Cursor(size=st.st_size, mtime=st.st_mtime, lineno=0, inode=getattr(st, "st_ino", 0))


def list_log_files(log_dir: str, patterns=DEFAULT_PATTERNS) -> list:
    """List uncompressed log files, oldest first (by mtime)."""
    if not os.path.isdir(log_dir):
        return []
    files = []
    seen = set()
    for name in sorted(os.listdir(log_dir)):
        full = os.path.join(log_dir, name)
        if full in seen or not os.path.isfile(full) or name.endswith(".gz"):
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            files.append(full)
            seen.add(full)
    files.sort(key=os.path.getmtime)
    return files


def iter_file_entries(
    path: str,
    start: int = 0,
    start_lineno: int = 0,
    source: str = "",
) -> Iterator[tuple[LogEntry, int, int]]:
    """Yield ``(entry, end_offset, lineno)`` for complete lines.

    Reads in binary mode so ``seek`` offsets are byte-exact. A trailing line
    without ``\\n`` is skipped: it will be picked up on the next pass once the
    writer finishes it, avoiding double counting.
    """
    source = source or path
    with open(path, "rb") as fh:
        if start:
            fh.seek(start)
        lineno = start_lineno
        while True:
            raw = fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break
            lineno += 1
            end = fh.tell()
            text = raw.decode("utf-8", errors="replace")
            entry = parse_line(text, source=source, lineno=lineno)
            if entry is not None:
                yield entry, end, lineno


def _empty_result() -> dict:
    return {"clusters": {}, "daily": {}, "lines": 0, "entries": 0}


def process_file(task: tuple[str, int, int, str]) -> tuple[tuple, dict]:
    """Process a single file. Top-level so it can run in a worker process.

    Returns ``((source, cursor_tuple), partial_result)``.
    """
    path, start, start_lineno, source = task
    clusterer = Clusterer()
    daily: dict = {}
    lines = 0
    entries = 0
    last_offset = start
    last_lineno = start_lineno

    for entry, end_offset, lineno in iter_file_entries(path, start, start_lineno, source):
        entry.signature = build_signature(entry)
        clusterer.add(entry)
        date = entry.ts[:10]
        by_date = daily.setdefault(entry.signature, {})
        by_date[date] = by_date.get(date, 0) + 1
        last_offset = end_offset
        last_lineno = lineno
        lines += 1
        entries += 1

    cursor = stat_cursor(path)
    result = {
        "clusters": {sig: cluster.to_dict() for sig, cluster in clusterer.clusters.items()},
        "daily": daily,
        "lines": lines,
        "entries": entries,
    }
    return (source, (last_offset, cursor.mtime, last_lineno, cursor.inode)), result


def merge_result(clusterer: Clusterer, daily: dict, result: dict) -> None:
    for sig, data in result["clusters"].items():
        incoming = Cluster.from_dict(data)
        existing = clusterer.clusters.get(sig)
        if existing is None:
            clusterer.clusters[sig] = incoming
        else:
            existing.merge(incoming)
    for sig, by_date in result["daily"].items():
        target = daily.setdefault(sig, {})
        for date, count in by_date.items():
            target[date] = target.get(date, 0) + count


def plan_tasks(state, files: list, full: bool) -> list:
    """Build ``(path, start_offset, start_lineno, source)`` tasks."""
    tasks = []
    for path in files:
        cursor = stat_cursor(path)
        prev_size, _prev_mtime, prev_lineno, prev_inode = state.get_offset(path)
        if full:
            start, start_lineno = 0, 0
        else:
            rotated = bool(
                (prev_inode and cursor.inode and prev_inode != cursor.inode)
                or prev_size > cursor.size
            )
            if rotated or prev_size == 0:
                start, start_lineno = 0, 0
            else:
                start, start_lineno = prev_size, prev_lineno
        tasks.append((path, start, start_lineno, path))
    return tasks


def process_file_safe(task: tuple[str, int, int, str]):
    """Like :func:`process_file`, but a single unreadable file does not abort."""
    try:
        return process_file(task)
    except Exception as exc:  # noqa: BLE001 - best-effort batch ingestion
        print(f"warning: cannot read {task[0]}: {exc}", file=sys.stderr)
        return None


def run_tasks(tasks: list, workers: int = 0) -> list:
    """Run tasks sequentially or in a process pool. Returns result tuples."""
    if workers and workers > 1 and len(tasks) > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(process_file_safe, tasks))
    else:
        results = [process_file_safe(task) for task in tasks]
    return [result for result in results if result is not None]


# --- journald (best effort, optional) ---

_JOURNAL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^ ]*)\s+"
    r"(?P<host>\S+)\s+(?P<unit>\S+?)(?:\[(?P<pid>\d+)\])?:\s+(?P<body>.*)$"
)


def parse_journal_line(line: str) -> LogEntry | None:
    match = _JOURNAL_RE.match(line.rstrip("\n"))
    if not match:
        return None
    body = match.group("body")
    upper = body.upper()
    level = "INFO"
    if "ERROR" in upper or "FAILED" in upper or "INVALID" in upper:
        level = "ERROR"
    elif "WARN" in upper:
        level = "WARNING"
    return LogEntry(
        ts=match.group("ts"),
        level=level,
        channel=match.group("unit"),
        message=body,
        source="journald",
    )


def journal_available() -> bool:
    from shutil import which

    return which("journalctl") is not None


def read_journal(
    since: str = "-24h",
    unit: str | None = None,
    use_sudo: bool = False,
) -> Iterator[LogEntry]:
    """Read systemd journal through ``journalctl`` (read-only, optional)."""
    import subprocess

    if not journal_available():
        return
    cmd = ["journalctl", "--no-pager", "--since", since, "-o", "short-iso"]
    if unit:
        cmd += ["-u", unit]
    if use_sudo:
        cmd = ["sudo", "-n"] + cmd
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            entry = parse_journal_line(line)
            if entry is not None:
                yield entry
    finally:
        proc.wait()
