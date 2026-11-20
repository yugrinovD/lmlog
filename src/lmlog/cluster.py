"""Clustering of log entries by signature and per-cluster statistics."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .parser import ERROR_LEVELS


@dataclass
class Cluster:
    signature: str
    count: int = 0
    levels: dict = field(default_factory=lambda: defaultdict(int))
    classes: dict = field(default_factory=lambda: defaultdict(int))
    first_ts: str = ""
    last_ts: str = ""
    sample_message: str = ""
    sample_raw: str = ""
    sample_context: dict = field(default_factory=dict)

    def add(self, entry) -> None:
        self.count += 1
        self.levels[entry.level] += 1
        klass = entry.klass
        if klass:
            self.classes[klass] += 1
        if not self.first_ts or entry.ts < self.first_ts:
            self.first_ts = entry.ts
        if entry.ts > self.last_ts:
            self.last_ts = entry.ts
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
        if other.first_ts and (not self.first_ts or other.first_ts < self.first_ts):
            self.first_ts = other.first_ts
        if other.last_ts > self.last_ts:
            self.last_ts = other.last_ts
        if not self.sample_message:
            self.sample_message = other.sample_message
        if not self.sample_raw:
            self.sample_raw = other.sample_raw
        if not self.sample_context:
            self.sample_context = other.sample_context

    @property
    def errors(self) -> int:
        return sum(self.levels.get(level, 0) for level in ERROR_LEVELS)

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "count": self.count,
            "levels": dict(self.levels),
            "classes": dict(self.classes),
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
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
