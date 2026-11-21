"""Background daemon that keeps the snapshot fresh with incremental ingests."""

from __future__ import annotations

import os
import sys
import time

PID_FILE = "daemon.pid"
LOG_FILE = "daemon.log"


def _base_dir(db_path: str) -> str:
    return os.path.dirname(os.path.abspath(db_path))


def daemon_status(db_path: str) -> dict:
    base = _base_dir(db_path)
    pid_file = os.path.join(base, PID_FILE)
    log_file = os.path.join(base, LOG_FILE)
    info = {
        "running": False,
        "pid": None,
        "log_file": log_file,
        "last_update": None,
    }
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as fh:
                pid = int(fh.read().strip())
            if os.path.exists(f"/proc/{pid}"):
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as fh:
                        cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
                    if "daemon" in cmdline and "lmlog" in cmdline:
                        info["running"] = True
                        info["pid"] = pid
                except OSError:
                    pass
        except (ValueError, OSError):
            pass
    if info["running"] and os.path.exists(log_file):
        info["last_update"] = os.path.getmtime(log_file)
    return info


def start_daemon(
    db_path: str,
    log_dir: str,
    interval: int,
    since_hours: int | None = None,
    workers: int = 0,
) -> int:
    base = _base_dir(db_path)
    os.makedirs(base, exist_ok=True)
    pid_file = os.path.join(base, PID_FILE)

    status = daemon_status(db_path)
    if status["running"]:
        print(f"Daemon is already running (PID {status['pid']}).")
        return status["pid"]

    import subprocess

    cmd = [
        sys.executable,
        "-u",
        "-m",
        "lmlog",
        "daemon",
        "--foreground",
        "--interval",
        str(interval),
        "--db",
        db_path,
    ]
    if log_dir:
        cmd += ["--log-dir", log_dir]
    if since_hours is not None:
        cmd += ["--since", str(since_hours)]
    if workers:
        cmd += ["--workers", str(workers)]
    log_file = os.path.join(base, LOG_FILE)
    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    with open(pid_file, "w") as fh:
        fh.write(str(proc.pid))
    print(f"Daemon started (PID {proc.pid}). Log: {log_file}")
    return proc.pid


def stop_daemon(db_path: str) -> int:
    base = _base_dir(db_path)
    pid_file = os.path.join(base, PID_FILE)
    status = daemon_status(db_path)
    if not status["running"]:
        print("Daemon is not running.")
        return 0
    import signal

    try:
        os.kill(status["pid"], signal.SIGTERM)
        print(f"Daemon stopped (PID {status['pid']}).")
    except OSError as exc:
        print(f"Could not stop the daemon: {exc}")
    try:
        os.remove(pid_file)
    except OSError:
        pass
    return 0


def run_foreground(
    db_path: str,
    log_dir: str,
    interval: int,
    since_hours: int | None = None,
    workers: int = 0,
) -> int:
    from .cli import run_ingest
    from .state import State

    print(
        f"lmlog daemon: interval={interval}s log_dir={log_dir} db={db_path}",
        flush=True,
    )
    while True:
        started = time.time()
        try:
            state = State(db_path)
            result = run_ingest(
                state,
                log_dir,
                since_hours,
                None,
                incremental=True,
                workers=workers,
            )
            if result["status"] == "ok":
                print(
                    f"[{time.strftime('%H:%M:%S')}] ingest: +{result['total_lines']} lines, "
                    f"signatures={result['unique_sigs']}, {result['elapsed']:.1f}s",
                    flush=True,
                )
            elif result["status"] == "aborted":
                print(
                    f"[{time.strftime('%H:%M:%S')}] ingest aborted; snapshot left unchanged",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 - keep the daemon alive
            print(f"[{time.strftime('%H:%M:%S')}] ingest failed: {exc}", flush=True)
        sleep_for = max(1, interval - (time.time() - started))
        time.sleep(sleep_for)
