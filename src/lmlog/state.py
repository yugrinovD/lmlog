"""Persistent state: SQLite for offsets, signature history, snapshots and cache."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager

from .parser import ts_sort_key


class State:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._is_memory = db_path == ":memory:"
        self._memory_conn: sqlite3.Connection | None = None
        if not self._is_memory:
            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    @contextmanager
    def conn(self):
        if self._is_memory:
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", timeout=30)
                self._memory_conn.row_factory = sqlite3.Row
            try:
                yield self._memory_conn
                self._memory_conn.commit()
            except Exception:
                self._memory_conn.rollback()
                raise
            return

        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init_schema(self) -> None:
        with self.conn() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS offsets (
                    source TEXT PRIMARY KEY,
                    size INTEGER,
                    mtime REAL,
                    lineno INTEGER,
                    inode INTEGER DEFAULT 0
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS clusters (
                    signature TEXT PRIMARY KEY,
                    first_seen TEXT,
                    last_seen TEXT,
                    total_count INTEGER,
                    metadata TEXT
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS explanations (
                    signature TEXT PRIMARY KEY,
                    created TEXT,
                    text TEXT
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshot (
                    signature TEXT PRIMARY KEY,
                    count INTEGER,
                    levels TEXT,
                    classes TEXT,
                    first_ts TEXT,
                    last_ts TEXT,
                    sample_message TEXT,
                    sample_raw TEXT,
                    sample_context TEXT,
                    created TEXT
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_counts (
                    signature TEXT NOT NULL,
                    date TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (signature, date)
                )
                """
            )
            self._ensure_column(con, "offsets", "inode", "INTEGER DEFAULT 0")

    @staticmethod
    def _ensure_column(con: sqlite3.Connection, table: str, column: str, decl: str) -> None:
        columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # --- offsets / progress ---

    def get_offset(self, source: str) -> tuple[int, float, int, int]:
        return self.get_offsets([source]).get(source, (0, 0.0, 0, 0))

    def get_offsets(self, sources: Iterable[str]) -> dict[str, tuple[int, float, int, int]]:
        """Return offsets for many sources using a bounded number of queries."""
        unique = list(dict.fromkeys(sources))
        result: dict[str, tuple[int, float, int, int]] = {}
        if not unique:
            return result
        with self.conn() as con:
            for index in range(0, len(unique), 500):
                chunk = unique[index : index + 500]
                marks = ",".join("?" * len(chunk))
                rows = con.execute(
                    f"SELECT source, size, mtime, lineno, inode FROM offsets WHERE source IN ({marks})",
                    chunk,
                ).fetchall()
                for row in rows:
                    result[row["source"]] = (
                        row["size"],
                        row["mtime"],
                        row["lineno"],
                        row["inode"] or 0,
                    )
        return result

    def prune_offsets(self, log_dir: str, existing: Iterable[str]) -> int:
        """Remove offsets for files under ``log_dir`` that no longer exist."""
        base = os.path.abspath(log_dir).rstrip(os.sep) + os.sep
        keep = set(existing)
        with self.conn() as con:
            rows = con.execute("SELECT source FROM offsets").fetchall()
            stale = [
                row["source"]
                for row in rows
                if row["source"].startswith(base) and row["source"] not in keep
            ]
            if stale:
                con.executemany(
                    "DELETE FROM offsets WHERE source=?", [(source,) for source in stale]
                )
        return len(stale)

    def set_offset(self, source: str, size: int, mtime: float, lineno: int, inode: int = 0) -> None:
        with self.conn() as con:
            con.execute(
                """
                INSERT INTO offsets(source, size, mtime, lineno, inode)
                VALUES(?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    lineno=excluded.lineno,
                    inode=excluded.inode
                """,
                (source, size, mtime, lineno, inode),
            )

    def set_offsets(self, rows: Iterable[tuple]) -> None:
        with self.conn() as con:
            con.executemany(
                """
                INSERT INTO offsets(source, size, mtime, lineno, inode)
                VALUES(?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    lineno=excluded.lineno,
                    inode=excluded.inode
                """,
                list(rows),
            )

    # --- signature history ---

    def known_signatures(self) -> set:
        with self.conn() as con:
            rows = con.execute("SELECT signature FROM clusters").fetchall()
        return {r["signature"] for r in rows}

    def record_clusters(self, clusters: Iterable, mode: str = "incremental") -> None:
        """Update the signature catalog.

        ``mode="incremental"`` adds the delta of the current pass and keeps the
        lifetime first/last timestamps. ``mode="full"`` rebuilds the catalog for
        the ingested dataset (idempotent re-ingests).
        """
        rows = [
            (
                c.signature,
                c.first_key or (ts_sort_key(c.first_ts) if c.first_ts else ""),
                c.last_key or (ts_sort_key(c.last_ts) if c.last_ts else ""),
                c.count,
                json.dumps(
                    {
                        "levels": dict(c.levels),
                        "classes": dict(
                            sorted(c.classes.items(), key=lambda kv: kv[1], reverse=True)[:5]
                        ),
                        "sample": c.sample_message,
                    },
                    ensure_ascii=False,
                ),
            )
            for c in clusters
        ]
        if not rows:
            return
        if mode == "full":
            update = """
                first_seen=excluded.first_seen,
                last_seen=excluded.last_seen,
                total_count=excluded.total_count,
                metadata=excluded.metadata
            """
        else:
            update = """
                first_seen = CASE
                    WHEN clusters.first_seen IS NULL OR clusters.first_seen = '' THEN excluded.first_seen
                    WHEN excluded.first_seen IS NULL OR excluded.first_seen = '' THEN clusters.first_seen
                    WHEN excluded.first_seen < clusters.first_seen THEN excluded.first_seen
                    ELSE clusters.first_seen
                END,
                last_seen = CASE
                    WHEN clusters.last_seen IS NULL OR clusters.last_seen = '' THEN excluded.last_seen
                    WHEN excluded.last_seen IS NULL OR excluded.last_seen = '' THEN clusters.last_seen
                    WHEN excluded.last_seen > clusters.last_seen THEN excluded.last_seen
                    ELSE clusters.last_seen
                END,
                total_count=COALESCE(clusters.total_count, 0) + excluded.total_count,
                metadata=excluded.metadata
            """
        with self.conn() as con:
            con.executemany(
                f"""
                INSERT INTO clusters(signature, first_seen, last_seen, total_count, metadata)
                VALUES(?,?,?,?,?)
                ON CONFLICT(signature) DO UPDATE SET {update}
                """,
                rows,
            )

    # --- explanations cache ---

    def get_explanation(self, signature: str) -> str | None:
        with self.conn() as con:
            row = con.execute(
                "SELECT text FROM explanations WHERE signature=?",
                (signature,),
            ).fetchone()
        return row["text"] if row else None

    def set_explanation(self, signature: str, text: str) -> None:
        with self.conn() as con:
            con.execute(
                """
                INSERT INTO explanations(signature, created, text)
                VALUES(?,?,?)
                ON CONFLICT(signature) DO UPDATE SET
                    created=excluded.created,
                    text=excluded.text
                """,
                (signature, time.strftime("%Y-%m-%d %H:%M:%S"), text),
            )

    # --- snapshot: fast queries over the saved cluster state ---

    def save_snapshot(self, clusters: Iterable, period_hours: int | None) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            (
                c.signature,
                c.count,
                json.dumps(dict(c.levels), ensure_ascii=False),
                json.dumps(dict(c.classes), ensure_ascii=False),
                c.first_ts,
                c.last_ts,
                c.sample_message,
                c.sample_raw,
                json.dumps(c.sample_context, ensure_ascii=False, default=str),
                now,
            )
            for c in clusters
        ]
        with self.conn() as con:
            con.execute("DELETE FROM snapshot")
            if rows:
                con.executemany(
                    """
                    INSERT INTO snapshot(signature, count, levels, classes, first_ts,
                                         last_ts, sample_message, sample_raw,
                                         sample_context, created)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )
            con.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('snapshot_created', ?)",
                (now,),
            )
            con.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('snapshot_period_hours', ?)",
                (str(period_hours) if period_hours else "",),
            )

    def load_snapshot(self):
        from .cluster import Cluster, Clusterer

        clusterer = Clusterer()
        with self.conn() as con:
            rows = con.execute("SELECT * FROM snapshot ORDER BY count DESC").fetchall()
        for row in rows:
            cluster = Cluster(signature=row["signature"], count=row["count"])
            cluster.levels.update(json.loads(row["levels"] or "{}"))
            cluster.classes.update(json.loads(row["classes"] or "{}"))
            cluster.first_ts = row["first_ts"] or ""
            cluster.last_ts = row["last_ts"] or ""
            cluster.first_key = ts_sort_key(cluster.first_ts) if cluster.first_ts else ""
            cluster.last_key = ts_sort_key(cluster.last_ts) if cluster.last_ts else ""
            cluster.sample_message = row["sample_message"] or ""
            cluster.sample_raw = row["sample_raw"] or ""
            try:
                cluster.sample_context = json.loads(row["sample_context"] or "{}")
            except (TypeError, ValueError):
                cluster.sample_context = {}
            clusterer.clusters[row["signature"]] = cluster
        return clusterer

    def snapshot_info(self) -> tuple[str | None, str | None]:
        with self.conn() as con:
            rows = {
                row["key"]: row["value"]
                for row in con.execute("SELECT key, value FROM meta").fetchall()
            }
        return rows.get("snapshot_created"), rows.get("snapshot_period_hours")

    def total_entries(self) -> int:
        with self.conn() as con:
            row = con.execute("SELECT COALESCE(SUM(count), 0) AS total FROM snapshot").fetchone()
        return int(row["total"] or 0)

    # --- daily_counts: per-day frequency history for trend detection ---

    def upsert_daily(self, daily: dict, replace_dates: set) -> None:
        """Write per-day counters.

        ``replace_dates`` are dates that were read completely in this pass;
        their previous values are deleted before inserting the fresh counts.
        Otherwise counters are added (incremental ingest).
        """
        rows = [
            (sig, date, count) for sig, by_date in daily.items() for date, count in by_date.items()
        ]
        with self.conn() as con:
            if replace_dates:
                marks = ",".join("?" * len(replace_dates))
                con.execute(
                    f"DELETE FROM daily_counts WHERE date IN ({marks})", list(replace_dates)
                )
            if rows:
                con.executemany(
                    """
                    INSERT INTO daily_counts(signature, date, count)
                    VALUES(?,?,?)
                    ON CONFLICT(signature, date) DO UPDATE SET
                        count = daily_counts.count + excluded.count
                    """,
                    rows,
                )

    def load_daily(self) -> dict:
        """All per-day counters: ``{signature: {date: count}}``."""
        daily: dict = {}
        with self.conn() as con:
            rows = con.execute("SELECT signature, date, count FROM daily_counts").fetchall()
        for row in rows:
            daily.setdefault(row["signature"], {})[row["date"]] = row["count"]
        return daily

    def load_daily_for(self, signature: str) -> dict:
        out: dict = {}
        with self.conn() as con:
            rows = con.execute(
                "SELECT date, count FROM daily_counts WHERE signature=?",
                (signature,),
            ).fetchall()
        for row in rows:
            out[row["date"]] = row["count"]
        return out

    # --- meta ---

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.conn() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.conn() as con:
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))
