"""Configuration helpers: paths, environment variables and defaults.

All runtime settings can be overridden with environment variables, which keeps
the CLI, the daemon and the Docker image consistent.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = "qwen2.5:3b"
DEFAULT_OLLAMA_HOST = "127.0.0.1:11434"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8080"
LLM_MODES = ("gateway", "direct")


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


def llm_mode() -> str:
    """Return the configured LLM mode: ``gateway`` (default) or ``direct``.

    ``gateway`` sends OpenAI-compatible requests to gogate. ``direct`` keeps the
    legacy standalone behaviour and talks to Ollama's native API.
    """
    value = (os.environ.get("LMLOG_LLM_MODE") or "gateway").strip().lower()
    return value if value in LLM_MODES else "gateway"


def gateway_base_url() -> str:
    """Base URL of the gogate public listener (without ``/v1``)."""
    value = (os.environ.get("GOGATE_BASE_URL") or DEFAULT_GATEWAY_URL).strip()
    value = value.rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value


def gateway_api_key() -> str:
    """API key used by lmlog when calling gogate.

    ``GOGATE_API_KEY`` takes precedence. ``GOGATE_API_KEY_FILE`` is a
    convenience for Docker secrets and file-based local setups.
    """
    explicit = (os.environ.get("GOGATE_API_KEY") or "").strip()
    if explicit:
        return explicit
    path = (os.environ.get("GOGATE_API_KEY_FILE") or "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def serve_data_dir() -> str:
    """Directory for uploaded jobs and their SQLite snapshots."""
    explicit = os.environ.get("LMLOG_SERVE_DATA_DIR")
    if explicit:
        return explicit
    return os.path.join(home_dir(), "serve")


def serve_api_token() -> str:
    """Bearer token required by the HTTP API. Empty means the API is disabled."""
    return os.environ.get("LMLOG_API_TOKEN") or ""


def serve_max_upload_bytes() -> int:
    """Hard upload limit in bytes (default 50 MiB)."""
    raw = os.environ.get("LMLOG_SERVE_MAX_UPLOAD_MB") or "50"
    try:
        megabytes = float(raw)
    except ValueError:
        megabytes = 50.0
    if megabytes <= 0:
        megabytes = 50.0
    return int(min(megabytes, 500.0) * 1024 * 1024)


def serve_max_jobs() -> int:
    raw = os.environ.get("LMLOG_SERVE_MAX_JOBS") or "50"
    try:
        value = int(raw)
    except ValueError:
        value = 50
    return max(1, min(value, 500))


def serve_job_ttl_hours() -> int:
    raw = os.environ.get("LMLOG_SERVE_JOB_TTL_HOURS") or "168"
    try:
        value = int(raw)
    except ValueError:
        value = 168
    return max(1, min(value, 24 * 365))


def serve_concurrency() -> int:
    raw = os.environ.get("LMLOG_SERVE_CONCURRENCY") or "2"
    try:
        value = int(raw)
    except ValueError:
        value = 2
    return max(1, min(value, 16))


def llm_concurrency() -> int:
    raw = os.environ.get("LMLOG_LLM_MAX_CONCURRENCY") or "1"
    try:
        value = int(raw)
    except ValueError:
        value = 1
    return max(1, min(value, 8))
