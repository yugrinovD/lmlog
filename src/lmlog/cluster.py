"""Clustering of log entries by signature and per-cluster statistics."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .parser import ERROR_LEVELS, ts_sort_key


def _ts_order(left: str, right: str) -> int:
    """Compare two Monolog timestamps, using the fast path when offsets match."""
    if left[-6:] == right[-6:]:
        return (left > right) - (left < right)
    left_key = ts_sort_key(left)
    right_key = ts_sort_key(right)
    return (left_key > right_key) - (left_key < right_key)


@dataclass
class Cluster:
    signature: str
    count: int = 0
    levels: dict = field(default_factory=lambda: defaultdict(int))
    classes: dict = field(default_factory=lambda: defaultdict(int))
    first_ts: str = ""
    last_ts: str = ""
    first_key: str = ""
    last_key: str = ""
    sample_message: str = ""
    sample_raw: str = ""
    sample_context: dict = field(default_factory=dict)

    def add(self, entry) -> None:
        self.count += 1
        self.levels[entry.level] += 1
        klass = entry.klass
        if klass:
            self.classes[klass] += 1
        if not self.first_ts or _ts_order(entry.ts, self.first_ts) < 0:
            self.first_ts = entry.ts
            self.first_key = ""
        if not self.last_ts or _ts_order(entry.ts, self.last_ts) > 0:
            self.last_ts = entry.ts
            self.last_key = ""
        if not self.sample_message:
            self.sample_message = entry.message
        if not self.sample_raw:
            self.sample_raw = entry.raw[:500]
        if not self.sample_context:
            self.sample_context = dict(entry.context)

    def merge(self, other: Cluster) -> None:
        self.count += other.count
        for level, value in other.levels.items():
            self.levels[level] += value
        for klass, value in other.classes.items():
            self.classes[klass] += value
        self.ensure_keys()
        other.ensure_keys()
        if other.first_key and (not self.first_key or other.first_key < self.first_key):
            self.first_key = other.first_key
            self.first_ts = other.first_ts
        if other.last_key and other.last_key > self.last_key:
            self.last_key = other.last_key
            self.last_ts = other.last_ts
        if not self.sample_message:
            self.sample_message = other.sample_message
        if not self.sample_raw:
            self.sample_raw = other.sample_raw
        if not self.sample_context:
            self.sample_context = other.sample_context

    def ensure_keys(self) -> None:
        if self.first_ts and not self.first_key:
            self.first_key = ts_sort_key(self.first_ts)
        if self.last_ts and not self.last_key:
            self.last_key = ts_sort_key(self.last_ts)

    @property
    def errors(self) -> int:
        return sum(self.levels.get(level, 0) for level in ERROR_LEVELS)

    def to_dict(self) -> dict:
        self.ensure_keys()
        return {
            "signature": self.signature,
            "count": self.count,
            "levels": dict(self.levels),
            "classes": dict(self.classes),
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "first_key": self.first_key,
            "last_key": self.last_key,
            "sample_message": self.sample_message,
            "sample_raw": self.sample_raw,
            "sample_context": self.sample_context,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Cluster:
        cluster = cls(signature=data["signature"], count=data["count"])
        cluster.levels.update(data.get("levels", {}))
        cluster.classes.update(data.get("classes", {}))
        cluster.first_ts = data.get("first_ts", "")
        cluster.last_ts = data.get("last_ts", "")
        cluster.first_key = data.get("first_key") or (
            ts_sort_key(cluster.first_ts) if cluster.first_ts else ""
        )
        cluster.last_key = data.get("last_key") or (
            ts_sort_key(cluster.last_ts) if cluster.last_ts else ""
        )
        cluster.sample_message = data.get("sample_message", "")
        cluster.sample_raw = data.get("sample_raw", "")
        cluster.sample_context = data.get("sample_context", {})
        return cluster


class Clusterer:
    def __init__(self) -> None:
        self.clusters: dict[str, Cluster] = {}

    def add(self, entry) -> Cluster:
        sig = getattr(entry, "signature", None) or entry.message
        cluster = self.clusters.get(sig)
        if cluster is None:
            cluster = Cluster(signature=sig)
            self.clusters[sig] = cluster
        cluster.add(entry)
        return cluster

    def merge(self, other: Clusterer) -> None:
        for sig, cluster in other.clusters.items():
            existing = self.clusters.get(sig)
            if existing is None:
                self.clusters[sig] = cluster
            else:
                existing.merge(cluster)

    def top(self, key, n: int = 20, reverse: bool = True):
        return sorted(self.clusters.values(), key=key, reverse=reverse)[:n]

    def by_level(self, level: str, n: int = 50):
        out = [c for c in self.clusters.values() if c.levels.get(level, 0) > 0]
        return sorted(out, key=lambda c: c.levels.get(level, 0), reverse=True)[:n]
