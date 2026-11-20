import sqlite3

from lmlog.cli import run_ingest
from lmlog.state import SCHEMA_VERSION, State

LEGACY_SCHEMA = """
CREATE TABLE offsets (
    source TEXT PRIMARY KEY, size INTEGER, mtime REAL, lineno INTEGER,
    inode INTEGER DEFAULT 0
);
CREATE TABLE clusters (
    signature TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT,
    total_count INTEGER, metadata TEXT
);
CREATE TABLE explanations (signature TEXT PRIMARY KEY, created TEXT, text TEXT);
CREATE TABLE snapshot (
    signature TEXT PRIMARY KEY, count INTEGER, levels TEXT, classes TEXT,
    first_ts TEXT, last_ts TEXT, sample_message TEXT, sample_raw TEXT,
    sample_context TEXT, created TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE daily_counts (
    signature TEXT NOT NULL, date TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (signature, date)
);
"""


def make_legacy_db(db_path, relative_source):
    con = sqlite3.connect(db_path)
    con.executescript(LEGACY_SCHEMA)
    con.execute(
        "INSERT INTO offsets(source, size, mtime, lineno, inode) VALUES(?,?,?,?,?)",
        (relative_source, 0, 0.0, 0, 0),
    )
    con.execute(
        "INSERT INTO clusters(signature, first_seen, last_seen, total_count, metadata) "
        "VALUES(?,?,?,?,?)",
        ("old", "2026-08-25T11:00:00+03:00", "2026-08-25T11:00:00+03:00", 999, "{}"),
    )
    con.execute(
        "INSERT INTO snapshot(signature, count, levels, classes, first_ts, last_ts, "
        "sample_message, sample_raw, sample_context, created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "old",
            999,
            "{}",
            "{}",
            "2026-08-25T11:00:00+03:00",
            "2026-08-25T11:00:00+03:00",
            "old",
            "",
            "{}",
            "2026-08-25 00:00:00",
        ),
    )
    con.execute(
        "INSERT INTO daily_counts(signature, date, count) VALUES(?,?,?)",
        ("old", "2026-08-25", 999),
    )
    con.commit()
    con.close()


def test_legacy_database_is_rebuilt_on_first_ingest(tmp_path):
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    log_file = logs / "console-2026-08-25.log"
    log_file.write_text(
        "".join(
            f"[2026-08-25T10:00:0{index}+00:00] app.INFO: group {index} "
            '{"class":"A"} {"uid":"x"}\n'
            for index in range(3)
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "legacy.db"
    make_legacy_db(db_path, "data/logs/console-2026-08-25.log")

    state = State(str(db_path))
    assert state.needs_rebuild()
    assert state.get_meta("schema_version") is None

    result = run_ingest(state, str(logs), None, None, incremental=True)
    assert result["status"] == "ok"
    assert not state.needs_rebuild()
    assert state.get_meta("schema_version") == str(SCHEMA_VERSION)

    snapshot = state.load_snapshot()
    assert sum(cluster.count for cluster in snapshot.clusters.values()) == 3
    with state.conn() as con:
        catalog_total = con.execute(
            "SELECT COALESCE(SUM(total_count), 0) FROM clusters"
        ).fetchone()[0]
        sources = [row[0] for row in con.execute("SELECT source FROM offsets")]
    assert catalog_total == 3
    assert sources == [str(log_file)]
    assert state.get_offset("data/logs/console-2026-08-25.log")[0] == 0


def test_new_database_has_current_schema(tmp_path):
    state = State(str(tmp_path / "new.db"))
    assert state.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert not state.needs_rebuild()
