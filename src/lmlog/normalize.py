"""Message normalisation and signature construction.

Variables (UUIDs, numbers, IPs, hashes, timestamps) are replaced by typed
tokens so that millions of similar lines collapse into a small set of
signatures.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
HASH_RE = re.compile(r"\b[0-9a-fA-F]{32,64}\b")
IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?\b")
HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
BRACE_PARAM_RE = re.compile(r"\{[^{}]*\}")
WS_RE = re.compile(r"\s+")

LEVELS = frozenset(
    {"INFO", "ERROR", "CRITICAL", "ALERT", "EMERGENCY", "WARNING", "DEBUG", "NOTICE"}
)

# Context keys that are stable enough to distinguish signatures.
SIG_CONTEXT_KEYS = ("method", "statusCode", "try")


@lru_cache(maxsize=200_000)
def normalize_message(message: str) -> str:
    """Return a de-identified template form of ``message``."""
    s = UUID_RE.sub("UUID", message)
    s = HASH_RE.sub("HASH", s)
    s = IPV4_RE.sub("IP", s)
    s = TS_RE.sub("TS", s)
    s = HEX_RE.sub("HEX", s)
    s = NUMBER_RE.sub("N", s)
    s = BRACE_PARAM_RE.sub("PARAM", s)
    return WS_RE.sub(" ", s).strip()


def signature(
    message: str,
    klass: str | None = None,
    level: str | None = None,
    context_terms: tuple = (),
) -> str:
    """Build a template signature from a message and optional metadata."""
    parts = [normalize_message(message)]
    if klass:
        parts.append("@" + klass)
    if level and level not in LEVELS:
        parts.append("#" + level)
    if context_terms:
        parts.append("|" + "|".join(context_terms))
    return " ".join(parts)


def normalize_value(value: object) -> str:
    """Normalise a context value for comparison."""
    if isinstance(value, (dict, list)):
        try:
            return normalize_message(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return normalize_message(str(value))
    return normalize_message(str(value))


def norm_context_sig(context: dict, keys: tuple = ()) -> tuple:
    """Return normalised values of the selected context keys."""
    return tuple(normalize_value(context[k]) for k in keys if k in context)


def build_signature(entry) -> str:
    """Signature for a parsed :class:`~lmlog.parser.LogEntry`."""
    terms = norm_context_sig(entry.context, SIG_CONTEXT_KEYS)
    return signature(entry.message, klass=entry.klass, level=entry.level, context_terms=terms)
