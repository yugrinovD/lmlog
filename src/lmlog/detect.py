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
    """Proactive trends: spikes, silent patterns and recently appeared ones.

    All comparisons are anchored to the globally latest date across *all*
    signatures, not to the last date of a particular signature. Missing days
    count as zero, so a completely silent pattern is detected.
    """
    import datetime as dt

    if spike_factor <= 0:
        raise ValueError("spike_factor must be greater than zero")
    if not daily:
        return []

    all_dates = sorted({date for by_date in daily.values() for date in by_date})
    if not all_dates:
        return []
    latest = all_dates[-1]

    try:
        latest_day = dt.date.fromisoformat(latest)
        window = [
            (latest_day - dt.timedelta(days=offset)).isoformat()
            for offset in range(days_back - 1, -1, -1)
        ]
    except ValueError:
        window = all_dates[-days_back:]
    if not window:
        return []
    previous_dates = window[:-1]
    by_sig = {cluster.signature: cluster for cluster in clusters}
    findings: list[Finding] = []

    def history(by_date: dict) -> str:
        return ", ".join(f"{date[5:]}:{by_date.get(date, 0)}" for date in window)

    for sig, by_date in daily.items():
        latest_count = by_date.get(latest, 0)
        previous_counts = [by_date.get(date, 0) for date in previous_dates]
        active_previous = [count for count in previous_counts if count > 0]
        baseline = _median(active_previous)
        cluster = by_sig.get(sig)

        # 1. Anomalous growth versus the median of active previous days.
        if active_previous and baseline > 0 and latest_count >= min_abs_count:
            ratio = latest_count / baseline
            if ratio >= spike_factor:
                severity = "high" if ratio >= spike_factor * 3 else "medium"
                findings.append(
                    Finding(
                        signature=sig,
                        kind="trend",
                        severity=severity,
                        count=latest_count,
                        errors=cluster.errors if cluster else 0,
                        classes=_top_classes(cluster) if cluster else [],
                        sample=cluster.sample_message if cluster else "",
                        note=f"anomalous growth on {latest}: {latest_count} vs median "
                        f"{baseline:.0f} (x{ratio:.1f}). History: {history(by_date)}",
                        detail=cluster.sample_raw if cluster else "",
                        context=cluster.sample_context if cluster else {},
                    )
                )
                continue

        # 2. Sharp drop, including a complete silence (latest_count == 0).
        # Use the minimum active baseline so a single spike followed by normal
        # volume is not misread as a drop.
        required_previous = max(1, min_baseline_days - 1)
        drop_baseline = min(active_previous) if active_previous else 0
        if len(active_previous) >= required_previous and drop_baseline >= min_abs_count:
            if latest_count < drop_baseline / spike_factor:
                findings.append(
                    Finding(
                        signature=sig,
                        kind="trend",
                        severity="low",
                        count=latest_count,
                        errors=cluster.errors if cluster else 0,
                        classes=_top_classes(cluster) if cluster else [],
                        sample=cluster.sample_message if cluster else "",
                        note=f"sharp drop on {latest}: {latest_count} vs baseline "
                        f"{drop_baseline:.0f} (pattern went silent). History: {history(by_date)}",
                        detail=cluster.sample_raw if cluster else "",
                        context=cluster.sample_context if cluster else {},
                    )
                )
                continue

        # 3. Patterns first seen inside the observation window.
        non_zero_dates = [date for date, count in by_date.items() if count > 0]
        if not non_zero_dates:
            continue
        first_seen = min(non_zero_dates)
        if len(non_zero_dates) == 1 and first_seen >= window[0]:
            count = latest_count or sum(by_date.get(date, 0) for date in window)
            findings.append(
                Finding(
                    signature=sig,
                    kind="new_trend",
                    severity="low",
                    count=count,
                    errors=cluster.errors if cluster else 0,
                    classes=_top_classes(cluster) if cluster else [],
                    sample=cluster.sample_message if cluster else "",
                    note=f"new pattern during the observation window (first seen {first_seen})",
                    context=cluster.sample_context if cluster else {},
                )
            )

    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings.sort(key=lambda finding: (severity_rank[finding.severity], -finding.count))
    return findings[:top_n]
