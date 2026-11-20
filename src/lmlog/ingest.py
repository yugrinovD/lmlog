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


_gz_warned = False


def list_log_files(log_dir: str, patterns=DEFAULT_PATTERNS) -> list[str]:
    """List uncompressed log files (absolute paths), oldest first by mtime."""
    global _gz_warned
    if not os.path.isdir(log_dir):
        return []
    base = os.path.abspath(log_dir)
    files: list[str] = []
    seen: set[str] = set()
    skipped_gz = False
    for name in sorted(os.listdir(base)):
        full = os.path.join(base, name)
        if not os.path.isfile(full):
            continue
        if name.endswith(".gz"):
            skipped_gz = True
            continue
        if full in seen:
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            files.append(full)
            seen.add(full)
    if skipped_gz and not _gz_warned:
        print(
            "warning: skipping *.gz log files (only uncompressed files are read)",
            file=sys.stderr,
        )
        _gz_warned = True
    files.sort(key=os.path.getmtime)
    return files


def iter_file_entries(
    path: str,
    start: int = 0,
    start_lineno: int = 0,
    source: str = "",
) -> Iterator[tuple[LogEntry | None, int, int]]:
    """Yield ``(entry_or_none, end_offset, lineno)`` for every complete line.

    ``entry`` is ``None`` for lines that do not match the Monolog format, which
    lets callers report physical line count separately from parsed entries.
    Reads in binary mode so ``seek`` offsets are byte-exact. A trailing line
    without ``\\n`` is skipped and picked up once the writer finishes it.
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
            yield parse_line(text, source=source, lineno=lineno), end, lineno


def process_file_into(
    task: tuple[str, int, int, str],
    clusterer: Clusterer,
    daily: dict,
) -> tuple[tuple, int, int]:
    """Stream one file into the given clusterer/daily counters.

    Returns ``((source, cursor_tuple), lines, entries)``. No per-file
    serialisation is involved, which keeps the sequential path allocation-light.
    """
    path, start, start_lineno, source = task
    lines = 0
    entries = 0
    last_offset = start
    last_lineno = start_lineno

    for entry, end_offset, lineno in iter_file_entries(path, start, start_lineno, source):
        lines += 1
        last_offset = end_offset
        last_lineno = lineno
        if entry is None:
            continue
        entries += 1
        entry.signature = build_signature(entry)
        clusterer.add(entry)
        date = entry.ts[:10]
        by_date = daily.setdefault(entry.signature, {})
        by_date[date] = by_date.get(date, 0) + 1

    cursor = stat_cursor(path)
    return (source, (last_offset, cursor.mtime, last_lineno, cursor.inode)), lines, entries


def process_file(task: tuple[str, int, int, str]) -> tuple[tuple, dict]:
    """Process a single file. Top-level so it can run in a worker process.

    Returns ``((source, cursor_tuple), partial_result)``.
    """
    clusterer = Clusterer()
    daily: dict = {}
    cursor, lines, entries = process_file_into(task, clusterer, daily)
    result = {
        "clusters": {sig: cluster.to_dict() for sig, cluster in clusterer.clusters.items()},
        "daily": daily,
        "lines": lines,
        "entries": entries,
    }
    return cursor, result


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


def plan_tasks(state, files: list, full: bool) -> tuple[list, bool]:
    """Build ``(path, start_offset, start_lineno, source)`` tasks.

    Returns ``(tasks, rotated)`` where ``rotated`` is true when an incremental
    read detected a truncated or replaced file.
    """
    offsets = state.get_offsets(files)
    tasks = []
    rotated_any = False
    for path in files:
        cursor = stat_cursor(path)
        prev_size, _prev_mtime, prev_lineno, prev_inode = offsets.get(path, (0, 0.0, 0, 0))
        if full:
            start, start_lineno = 0, 0
        else:
            rotated = bool(
                (prev_inode and cursor.inode and prev_inode != cursor.inode)
                or prev_size > cursor.size
            )
            if rotated:
                rotated_any = True
                start, start_lineno = 0, 0
            elif prev_size == 0:
                start, start_lineno = 0, 0
            else:
                start, start_lineno = prev_size, prev_lineno
        tasks.append((path, start, start_lineno, path))
    return tasks, rotated_any


def process_file_safe(task: tuple[str, int, int, str]):
    """Like :func:`process_file`, but a single unreadable file does not abort."""
    try:
        return process_file(task)
    except Exception as exc:  # noqa: BLE001 - best-effort batch ingestion
        print(f"warning: cannot read {task[0]}: {exc}", file=sys.stderr)
        return None


def process_file_into_safe(task: tuple[str, int, int, str], clusterer: Clusterer, daily: dict):
    """Sequential counterpart of :func:`process_file_safe` without serialisation."""
    try:
        return process_file_into(task, clusterer, daily)
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
    timeout: float | None = 30,
) -> Iterator[LogEntry]:
    """Read systemd journal through ``journalctl`` (read-only, optional).

    ``timeout`` bounds the total wall-clock time; the process is killed when it
    expires, so a stuck journalctl cannot hang the CLI or the daemon.
    """
    import subprocess
    import threading

    if not journal_available():
        return
    cmd = ["journalctl", "--no-pager", "--since", since, "-o", "short-iso"]
    if unit:
        cmd += ["-u", unit]
    if use_sudo:
        cmd = ["sudo", "-n"] + cmd
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert proc.stdout is not None

    def kill_if_running() -> None:
        if proc.poll() is None:
            proc.kill()

    timer = threading.Timer(timeout, kill_if_running) if timeout and timeout > 0 else None
    if timer is not None:
        timer.daemon = True
        timer.start()
    try:
        for line in proc.stdout:
            entry = parse_journal_line(line)
            if entry is not None:
                yield entry
    finally:
        if timer is not None:
            timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
