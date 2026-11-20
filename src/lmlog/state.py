"""Persistent state: SQLite for offsets, signature history, snapshots and cache."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager


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
        with self.conn() as con:
            row = con.execute(
                "SELECT size, mtime, lineno, inode FROM offsets WHERE source=?",
                (source,),
            ).fetchone()
        if row is None:
            return 0, 0.0, 0, 0
        return row["size"], row["mtime"], row["lineno"], row["inode"] or 0

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

    def record_clusters(self, clusters: Iterable) -> None:
        rows = [
            (
                c.signature,
                c.first_ts,
                c.last_ts,
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
        with self.conn() as con:
            con.executemany(
                """
                INSERT INTO clusters(signature, first_seen, last_seen, total_count, metadata)
                VALUES(?,?,?,?,?)
                ON CONFLICT(signature) DO UPDATE SET
                    first_seen=excluded.first_seen,
                    last_seen=excluded.last_seen,
                    total_count=clusters.total_count+excluded.total_count,
                    metadata=excluded.metadata
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
