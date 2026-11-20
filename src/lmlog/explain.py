"""Deterministic, rule-based explanations.

This is the offline fallback for ``lmlog explain``: even without a local LLM
the tool returns a structured, actionable explanation for a finding.
"""

from __future__ import annotations

import re

from .llm import OllamaClient, build_prompt

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


class Rule:
    def __init__(self, name, pattern, severity, what, cause, checks):
        self.name = name
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.severity = severity
        self.what = what
        self.cause = cause
        self.checks = checks


RULES = [
    Rule(
        "storage_http",
        r"failed request to storage|storage request|api-storage|diskSpace",
        "high",
        "An outbound request to the storage service failed.",
        "The storage endpoint is unavailable, returns an error status or the route is missing.",
        [
            "Check the storage service health and the requested URL.",
            "Verify status codes and retry/backoff behaviour.",
            "Confirm the storage integration configuration.",
        ],
    ),
    Rule(
        "disk_env",
        r"disk space|environment variable not found|retention|can't delete files",
        "high",
        "A maintenance or retention task could not complete.",
        "A required environment variable is missing or the storage backend reports a problem.",
        [
            "Check the application environment configuration.",
            "Verify free disk space and retention settings.",
            "Review the cleanup task logs.",
        ],
    ),
    Rule(
        "database",
        r"integrity constraint|duplicate entry|sqlstate|deadlock|database",
        "high",
        "A database operation failed.",
        "A unique constraint was violated, a deadlock occurred or the database is unreachable.",
        [
            "Inspect the failing query and its unique keys.",
            "Check for concurrent writers and retry logic.",
            "Review database health and locks.",
        ],
    ),
    Rule(
        "smtp",
        r"smtp|mail server|invalid response smtp",
        "medium",
        "An email delivery attempt failed.",
        "The SMTP server is unreachable or returns an unexpected response.",
        [
            "Check SMTP connectivity and credentials.",
            "Verify the mail relay configuration.",
            "Look for repeated failures in the same period.",
        ],
    ),
    Rule(
        "consumer",
        r"consumer|actualize|timetable",
        "medium",
        "A background message consumer reported a problem or stopped producing events.",
        "The consumer is blocked, misconfigured or crashing on a specific message.",
        [
            "Check the consumer process state and queue depth.",
            "Inspect the last processed message and exception trace.",
            "Verify broker connectivity.",
        ],
    ),
    Rule(
        "timeout",
        r"timeout|timed out|long cron|executing: \d+ seconds",
        "medium",
        "A task exceeded its expected execution time.",
        "The workload grew, an external dependency is slow, or a query is inefficient.",
        [
            "Compare task duration with the baseline.",
            "Check slow external calls and database queries.",
            "Consider raising timeouts or splitting the task.",
        ],
    ),
    Rule(
        "worker",
        r"worker not found|no worker|worker.*missing",
        "medium",
        "A worker was expected but could not be found.",
        "A background process is not running, crashed earlier, or was never started.",
        [
            "Check the supervisor/process manager status.",
            "Verify worker registration and configuration.",
            "Look for earlier crashes in the same period.",
        ],
    ),
    Rule(
        "memory",
        r"out of memory|allowed memory size|memory limit",
        "high",
        "The process ran out of memory.",
        "A request or batch processes too much data or leaked memory.",
        [
            "Check PHP/process memory limits.",
            "Inspect the request that triggered the failure.",
            "Look for unbounded batch sizes.",
        ],
    ),
    Rule(
        "exception",
        r"uncaught|exception|fatal error",
        "high",
        "An uncaught exception or fatal error occurred.",
        "An unexpected edge case reached production code.",
        [
            "Inspect the stack trace and the failing class.",
            "Reproduce the scenario from the context data.",
            "Check recent deployments and configuration changes.",
        ],
    ),
]


def classify(finding) -> Rule:
    haystack = " ".join(
        [
            getattr(finding, "sample", "") or "",
            " ".join(getattr(finding, "classes", []) or []),
            str(getattr(finding, "context", {}) or ""),
        ]
    )
    for rule in RULES:
        if rule.pattern.search(haystack):
            return rule
    return Rule(
        "generic",
        r".",
        "medium" if getattr(finding, "errors", 0) else "low",
        "A recurring log pattern was detected.",
        "The pattern appears frequently or is new in the observed period.",
        [
            "Inspect a few raw log lines for this signature.",
            "Check whether the pattern correlates with an incident window.",
            "Verify the component that emits it.",
        ],
    )


def heuristic_explain(finding) -> str:
    rule = classify(finding)
    severity = getattr(finding, "severity", "") or rule.severity
    if SEVERITY_ORDER.get(rule.severity, 3) < SEVERITY_ORDER.get(severity, 3):
        severity = rule.severity
    checks = "\n".join(f"- {item}" for item in rule.checks)
    return (
        f"SEVERITY: {severity}\n"
        f"WHAT IS HAPPENING: {rule.what}\n"
        f"LIKELY CAUSE: {rule.cause}\n"
        f"WHAT TO CHECK:\n{checks}\n"
        f"[heuristic: {rule.name}]"
    )


def choose_explanation(
    finding,
    client: OllamaClient | None = None,
    use_llm: bool = True,
    period: str = "",
) -> tuple[str, str]:
    """Return ``(text, source)`` where source is ``llm`` or ``heuristic``."""
    if use_llm:
        client = client or OllamaClient()
        if client.health():
            prompt = build_prompt(
                level="ERROR" if getattr(finding, "errors", 0) else "INFO",
                message=getattr(finding, "sample", "") or "",
                klass=", ".join((getattr(finding, "classes", []) or [])[:3]),
                context=getattr(finding, "context", {}) or {},
                count=getattr(finding, "count", 0),
                period=period,
                errors=getattr(finding, "errors", 0),
            )
            text = client.generate(prompt) or ""
            if text and not text.startswith("[LLM error"):
                return text, "llm"
    return heuristic_explain(finding), "heuristic"
