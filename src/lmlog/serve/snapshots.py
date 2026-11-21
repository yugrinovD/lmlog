"""Read-only snapshot views over completed upload jobs."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from ..cluster import Cluster
from ..detect import Finding, detect_trends
from ..state import State


@dataclass
class Snapshot:
    id: str
    created: str | None
    period_hours: str | None
    clusters: list[Cluster]
    by_signature: dict[str, Cluster]
    daily: dict[str, dict[str, int]]
    total_entries: int
    stats: dict | None

    def window(self, days: int = 14) -> list[str]:
        dates = sorted({date for by_date in self.daily.values() for date in by_date})
        return dates[-days:]

    def daily_for(self, signature: str, days: int = 14) -> dict[str, int]:
        window = self.window(days)
        by_date = self.daily.get(signature, {})
        return {date: int(by_date.get(date, 0)) for date in window}

    def signature_view(self, cluster: Cluster, days: int = 14) -> dict:
        return {
            "signature": cluster.signature,
            "count": int(cluster.count),
            "errors": int(cluster.errors),
            "levels": {key: int(value) for key, value in cluster.levels.items()},
            "classes": {key: int(value) for key, value in cluster.classes.items()},
            "first_ts": cluster.first_ts,
            "last_ts": cluster.last_ts,
            "sample_message": cluster.sample_message,
            "sample_raw": cluster.sample_raw[:500],
            "sample_context": cluster.sample_context,
            "daily": self.daily_for(cluster.signature, days),
        }

    def summary(self) -> dict:
        levels: dict[str, int] = {}
        classes: dict[str, int] = {}
        errors = 0
        for cluster in self.clusters:
            errors += cluster.errors
            for level, count in cluster.levels.items():
                levels[level] = levels.get(level, 0) + int(count)
            for klass, count in cluster.classes.items():
                classes[klass] = classes.get(klass, 0) + int(count)
        top_classes = [
            {"name": name, "count": count}
            for name, count in sorted(classes.items(), key=lambda item: item[1], reverse=True)[:10]
        ]
        daily_totals: dict[str, int] = {}
        for by_date in self.daily.values():
            for date, count in by_date.items():
                daily_totals[date] = daily_totals.get(date, 0) + int(count)
        return {
            "snapshot_id": self.id,
            "created": self.created,
            "period_hours": self.period_hours,
            "files": 1 if self.total_entries or self.clusters else 0,
            "lines": int((self.stats or {}).get("lines", 0)),
            "entries": int((self.stats or {}).get("entries", self.total_entries)),
            "signatures": len(self.clusters),
            "errors": errors,
            "levels": levels,
            "top_classes": top_classes,
            "daily_totals": dict(sorted(daily_totals.items())),
            "stats": self.stats,
        }

    def find(self, signature: str) -> Cluster | None:
        return self.by_signature.get(signature)

    def finding(self, cluster: Cluster) -> Finding:
        kind = "error" if cluster.errors else "frequent"
        severity = "high" if cluster.errors >= 3 else ("medium" if cluster.errors else "info")
        return Finding(
            signature=cluster.signature,
            kind=kind,
            severity=severity,
            count=int(cluster.count),
            errors=int(cluster.errors),
            classes=sorted(cluster.classes, key=cluster.classes.get, reverse=True)[:3],
            sample=cluster.sample_message,
            note=(
                f"error cluster: {cluster.errors} errors / {cluster.count} lines"
                if cluster.errors
                else f"frequent pattern: {cluster.count} lines"
            ),
            detail=cluster.sample_raw,
            context=cluster.sample_context,
        )

    def trends(self, days: int = 7, spike: float = 3.0, limit: int = 20) -> list[dict]:
        findings = detect_trends(
            self.clusters,
            self.daily,
            spike_factor=spike,
            days_back=days,
            top_n=limit,
        )
        return [
            {
                "signature": finding.signature,
                "kind": finding.kind,
                "severity": finding.severity,
                "count": finding.count,
                "errors": finding.errors,
                "classes": finding.classes,
                "sample": finding.sample,
                "note": finding.note,
                "detail": finding.detail[:500],
                "daily": self.daily_for(finding.signature, 14),
            }
            for finding in findings
        ]

    def chat_clusters(self, signature: str | None, top_n: int) -> list[Cluster]:
        selected: list[Cluster] = []
        if signature:
            cluster = self.by_signature.get(signature)
            if cluster is not None:
                selected.append(cluster)
        errors = sorted(
            (c for c in self.clusters if c.errors > 0),
            key=lambda c: (c.errors, c.count),
            reverse=True,
        )
        for cluster in errors:
            if len(selected) >= top_n:
                break
            if cluster not in selected:
                selected.append(cluster)
        for cluster in sorted(self.clusters, key=lambda c: c.count, reverse=True):
            if len(selected) >= top_n:
                break
            if cluster not in selected:
                selected.append(cluster)
        return selected[:top_n]


class SnapshotCache:
    """A small LRU of parsed snapshots. Snapshot files are immutable once done."""

    def __init__(self, max_size: int = 8):
        self.max_size = max_size
        self._items: OrderedDict[str, Snapshot] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, job) -> Snapshot:
        with self._lock:
            cached = self._items.get(job.id)
            if cached is not None:
                self._items.move_to_end(job.id)
                return cached
        # Loading is intentionally outside the lock: readers of different
        # snapshots must not serialize behind a large SQLite read.
        state = State(job.db_path)
        created, period = state.snapshot_info()
        clusterer = state.load_snapshot()
        clusters = sorted(clusterer.clusters.values(), key=lambda c: c.count, reverse=True)
        snapshot = Snapshot(
            id=job.id,
            created=created,
            period_hours=period,
            clusters=clusters,
            by_signature=dict(clusterer.clusters),
            daily=state.load_daily(),
            total_entries=state.total_entries(),
            stats=job.stats,
        )
        with self._lock:
            self._items[job.id] = snapshot
            self._items.move_to_end(job.id)
            while len(self._items) > self.max_size:
                self._items.popitem(last=False)
        return snapshot

    def evict(self, snapshot_id: str) -> None:
        with self._lock:
            self._items.pop(snapshot_id, None)
