"""Parser for the Symfony/Monolog line format.

A typical line looks like::

    [2026-08-25T11:44:54.253480+03:00] app.INFO: message {"context"} {"extra"}

The JSON tail is parsed from the end of the line, so no expensive
split/scan of the whole message is required for the common case.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:  # optional acceleration
    import orjson as _orjson
except ImportError:  # pragma: no cover - optional dependency
    _orjson = None


def _loads(text: str):
    return _orjson.loads(text) if _orjson is not None else json.loads(text)


def ts_sort_key(ts: str) -> str:
    """Return a lexicographically sortable UTC key for a Monolog timestamp.

    Log timestamps carry offsets (``+03:00``), so comparing the raw strings is
    wrong when files come from different time zones. Canonicalising to UTC makes
    ``MIN``/``MAX`` and ``<`` comparisons chronological again.
    """
    try:
        # Python 3.10's fromisoformat does not accept the "Z" suffix.
        value = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
    except (TypeError, ValueError):
        return ts
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T[^\]]+)\]\s+"
    r"(?P<channel>[A-Za-z0-9_.-]+)\.(?P<level>[A-Z]+):\s+(?P<body>.*)$"
)

ERROR_LEVELS = frozenset({"ERROR", "CRITICAL", "ALERT", "EMERGENCY"})


@dataclass(slots=True)
class LogEntry:
    ts: str
    level: str
    channel: str
    message: str
    context: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    raw: str = ""
    source: str = ""
    lineno: int = 0
    signature: str = ""

    @property
    def klass(self) -> str | None:
        """Application namespace (``class`` key in the context)."""
        value = self.context.get("class")
        return value if isinstance(value, str) else None

    @property
    def is_error(self) -> bool:
        return self.level in ERROR_LEVELS


def _json_obj_from_end(text: str) -> tuple[int, object] | None:
    """Return the right-most JSON object ``{...}`` at the end of ``text``.

    Scans backwards while tracking string literals, so braces inside JSON
    strings do not break bracket counting.
    """
    stack = 0
    in_str = False
    escape = False
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "}":
            stack += 1
        elif ch == "{":
            if stack == 0:
                return None
            stack -= 1
            if stack == 0:
                try:
                    return i, _loads(text[i:])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _parse_json_tail(body: str) -> tuple[str, dict, dict]:
    context: dict = {}
    extra: dict = {}
    stripped = body.rstrip()
    if "{" not in stripped:
        return stripped, context, extra

    r1 = _json_obj_from_end(stripped)
    if r1 is None:
        return stripped, context, extra
    start1, maybe_extra = r1
    if isinstance(maybe_extra, dict):
        extra = maybe_extra

    head = stripped[:start1].rstrip()
    r2 = _json_obj_from_end(head)
    if r2 is not None and isinstance(r2[1], dict):
        start2, context = r2
        message = head[:start2].rstrip()
    else:
        message = head
        # A single JSON object is usually the context (e.g. {"uid": ...}).
        if extra and ("class" in extra or set(extra) & {"uid"}):
            context, extra = extra, {}
    return message, context, extra


def parse_line(
    raw: str,
    source: str = "",
    lineno: int = 0,
    need_context: bool = True,
) -> LogEntry | None:
    raw = raw.rstrip("\r\n")
    if not raw:
        return None
    match = LINE_RE.match(raw)
    if not match:
        return None

    body = match.group("body")
    if need_context:
        message, context, extra = _parse_json_tail(body)
    else:
        message, context, extra = body, {}, {}

    return LogEntry(
        ts=match.group("ts"),
        level=match.group("level"),
        channel=match.group("channel"),
        message=message,
        context=context,
        extra=extra,
        raw=raw,
        source=source,
        lineno=lineno,
    )


def iter_lines(path: str, source: str = "", need_context: bool = True) -> Iterator[LogEntry]:
    """Yield parsed entries from a text log file."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            entry = parse_line(
                line, source=source or path, lineno=lineno, need_context=need_context
            )
            if entry is not None:
                yield entry
