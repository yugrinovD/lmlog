"""Rule-based detection of errors, new patterns, noise and anomalies."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Finding:
    signature: str
    kind: str  # 'error' | 'new' | 'frequent' | 'trend' | 'new_trend'
    severity: str  # 'high' | 'medium' | 'low' | 'info'
    count: int
    errors: int
    classes: list
    sample: str
    note: str = ""
    detail: str = ""
    context: dict = field(default_factory=dict)


def _severity_for(count: int, errors: int, kind: str) -> str:
    if errors > 0 or kind == "error":
        return "high" if errors >= 3 or count >= 10 else "medium"
    if kind == "new":
        return "low"
    if count >= 1000:
        return "medium"
    return "info"


def _top_classes(cluster, n: int = 3) -> list:
    return sorted(cluster.classes, key=cluster.classes.get, reverse=True)[:n]


def detect(clusters, known: set, min_error_count: int = 1, top_trend: int = 15) -> list:
    findings: list[Finding] = []
    seen: set[str] = set()

    # 1. ERROR / CRITICAL clusters
    error_clusters = [c for c in clusters if c.errors > 0]
    for cluster in sorted(error_clusters, key=lambda c: c.errors, reverse=True)[:top_trend]:
        if cluster.errors < min_error_count:
            continue
        findings.append(
            Finding(
                signature=cluster.signature,
                kind="error",
                severity=_severity_for(cluster.count, cluster.errors, "error"),
                count=cluster.count,
                errors=cluster.errors,
                classes=_top_classes(cluster),
                sample=cluster.sample_message,
                note=f"error cluster: {cluster.errors} errors / {cluster.count} lines",
                detail=cluster.sample_raw,
                context=cluster.sample_context,
            )
        )
        seen.add(cluster.signature)

    # 2. Signatures never seen before
    for cluster in sorted(clusters, key=lambda c: c.count, reverse=True)[:200]:
        if cluster.signature in known or cluster.signature in seen or cluster.errors > 0:
            continue
        findings.append(
            Finding(
                signature=cluster.signature,
                kind="new",
                severity="low",
                count=cluster.count,
                errors=cluster.errors,
                classes=_top_classes(cluster),
                sample=cluster.sample_message,
                note="new signature (not seen before)",
                context=cluster.sample_context,
            )
        )
        seen.add(cluster.signature)

    # 3. Very frequent INFO patterns (noise / logging volume candidates)
    for cluster in sorted(clusters, key=lambda c: c.count, reverse=True)[:top_trend]:
        if cluster.count < 1000:
            break
        if cluster.signature in seen:
            continue
        findings.append(
            Finding(
                signature=cluster.signature,
                kind="frequent",
                severity="info",
                count=cluster.count,
                errors=cluster.errors,
                classes=_top_classes(cluster),
                sample=cluster.sample_message,
                note=f"frequent pattern: {cluster.count} lines (candidate for log-level reduction)",
                context=cluster.sample_context,
            )
        )
        seen.add(cluster.signature)

    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings.sort(key=lambda f: (severity_rank[f.severity], -f.count))
    return findings


def _median(values: list) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 0:
        return (ordered[mid - 1] + ordered[mid]) / 2.0
    return float(ordered[mid])


def detect_trends(
    clusters,
    daily: dict,
    spike_factor: float = 3.0,
    min_baseline_days: int = 2,
    min_abs_count: int = 10,
    top_n: int = 20,
    days_back: int = 7,
) -> list:
    """Proactive trends: spikes, silent patterns and recently appeared ones."""
    findings: list[Finding] = []
    by_sig = {c.signature: c for c in clusters}

    # 1. Anomalous growth versus the median of previous days
    for sig, by_date in daily.items():
        dates = sorted(by_date)
        if len(dates) < 2:
            continue
        last_date = dates[-1]
        last_count = by_date[last_date]
        baseline = _median([by_date[d] for d in dates[:-1]])
        if baseline <= 0 or last_count < min_abs_count:
            continue
        ratio = last_count / baseline
        if ratio < spike_factor:
            continue
        cluster = by_sig.get(sig)
        severity = "high" if ratio >= spike_factor * 3 else "medium"
        findings.append(
            Finding(
                signature=sig,
                kind="trend",
                severity=severity,
                count=last_count,
                errors=cluster.errors if cluster else 0,
                classes=_top_classes(cluster) if cluster else [],
                sample=cluster.sample_message if cluster else "",
                note=f"anomalous growth: {last_count} vs median {baseline:.0f} (x{ratio:.1f}). History: "
                + ", ".join(f"{d[5:]}:{by_date[d]}" for d in dates),
                detail=cluster.sample_raw if cluster else "",
                context=cluster.sample_context if cluster else {},
            )
        )

    # 2. Sharp drop ("silent" pattern)
    for cluster in clusters:
        by_date = daily.get(cluster.signature, {})
        dates = sorted(by_date)
        if len(dates) < min_baseline_days:
            continue
        baseline = _median([by_date[d] for d in dates[:-1]])
        if baseline < min_abs_count:
            continue
        last_date = dates[-1]
        last_count = by_date.get(last_date, 0)
        if 0 < last_count < baseline / spike_factor:
            findings.append(
                Finding(
                    signature=cluster.signature,
                    kind="trend",
                    severity="low",
                    count=last_count,
                    errors=cluster.errors,
                    classes=_top_classes(cluster),
                    sample=cluster.sample_message,
                    note=f"sharp drop: {last_count} vs median {baseline:.0f} (pattern went silent). History: "
                    + ", ".join(f"{d[5:]}:{by_date[d]}" for d in dates),
                    context=cluster.sample_context,
                )
            )

    # 3. Patterns first seen recently
    import datetime as dt

    max_date = max((d for by_date in daily.values() for d in by_date), default="")
    for sig, by_date in daily.items():
        dates = sorted(by_date)
        if not dates:
            continue
        cluster = by_sig.get(sig)
        if cluster is None:
            continue
        first_date = dates[0]
        if len(dates) == 1 and max_date:
            try:
                is_recent = (
                    dt.date.fromisoformat(max_date) - dt.date.fromisoformat(first_date)
                ).days <= days_back
            except ValueError:
                is_recent = False
            if is_recent:
                findings.append(
                    Finding(
                        signature=sig,
                        kind="new_trend",
                        severity="low",
                        count=cluster.count,
                        errors=cluster.errors,
                        classes=_top_classes(cluster),
                        sample=cluster.sample_message,
                        note=f"new pattern during the observation window (first seen {first_date})",
                        context=cluster.sample_context,
                    )
                )

    seen: set[str] = set()
    out: list[Finding] = []
    for finding in findings:
        if finding.signature in seen:
            continue
        seen.add(finding.signature)
        out.append(finding)
    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    out.sort(key=lambda f: (severity_rank[f.severity], -f.count))
    return out[:top_n]
