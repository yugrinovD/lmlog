"""Configuration helpers: paths, environment variables and defaults.

All runtime settings can be overridden with environment variables, which keeps
the CLI, the daemon and the Docker image consistent.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODEL = "qwen2.5:3b"
DEFAULT_OLLAMA_HOST = "127.0.0.1:11434"


def home_dir() -> str:
    """Directory for persistent state (database, daemon pid/log)."""
    return os.environ.get("LMLOG_HOME") or os.path.expanduser("~/.lmlog")


def default_db() -> str:
    """Path to the SQLite state database."""
    explicit = os.environ.get("LMLOG_DB")
    if explicit:
        return explicit
    return os.path.join(home_dir(), "lmlog.db")


def default_log_dir() -> str:
    """Directory where log files are read from / generated into."""
    return os.environ.get("LMLOG_LOG_DIR") or os.path.join("data", "logs")


def default_model() -> str:
    return os.environ.get("LMLOG_MODEL") or DEFAULT_MODEL


def ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST


def ensure_parent(path: str) -> None:
    parent = Path(path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
