import os

from lmlog.cli import run_ingest
from lmlog.state import State


def log_line(ts, level, message, klass="App.Service"):
    context = f'{{"class":"{klass}"}}' if klass else "{}"
    return f'[{ts}] app.{level}: {message} {context} {{"uid":"x"}}\n'


def write_log(path, lines):
    path.write_text("".join(lines), encoding="utf-8")


def test_full_and_incremental_ingest(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(
        path,
        [
            log_line("2026-08-25T10:00:00+00:00", "INFO", "start group 1"),
            log_line("2026-08-25T10:00:01+00:00", "INFO", "start group 2"),
            log_line("2026-08-25T10:00:02+00:00", "ERROR", "boom"),
        ],
    )

    state = State(str(tmp_path / "state.db"))
    result = run_ingest(state, str(logs), None, None)
    assert result["files"] == 1
    assert result["total_lines"] == 3
    assert result["total_entries"] == 3
    assert result["unique_sigs"] == 2
    assert state.get_offset(str(path))[0] == os.path.getsize(path)
    assert len(state.load_snapshot().clusters) == 2
    assert state.load_daily()

    result = run_ingest(state, str(logs), None, None, incremental=True)
    assert result["total_lines"] == 0

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(log_line("2026-08-25T10:00:03+00:00", "INFO", "start group 3"))
        fh.write(log_line("2026-08-25T10:00:04+00:00", "ERROR", "boom"))
    result = run_ingest(state, str(logs), None, None, incremental=True)
    assert result["total_lines"] == 2


def test_rotation_by_shrinking_file(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(
        path,
        [
            log_line("2026-08-25T10:00:00+00:00", "INFO", "one"),
            log_line("2026-08-25T10:00:01+00:00", "INFO", "two"),
            log_line("2026-08-25T10:00:02+00:00", "INFO", "three"),
        ],
    )
    state = State(str(tmp_path / "state.db"))
    assert run_ingest(state, str(logs), None, None)["total_lines"] == 3

    write_log(path, [log_line("2026-08-25T11:00:00+00:00", "INFO", "rotated")])
    result = run_ingest(state, str(logs), None, None, incremental=True)
    assert result["total_lines"] == 1


def test_trailing_partial_line_is_not_lost(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    first = log_line("2026-08-25T10:00:00+00:00", "INFO", "complete")
    second = log_line("2026-08-25T10:00:01+00:00", "INFO", "partial line").rstrip("\n")
    path.write_text(first + second, encoding="utf-8")

    state = State(str(tmp_path / "state.db"))
    assert run_ingest(state, str(logs), None, None)["total_lines"] == 1

    path.write_text(first + second + "\n", encoding="utf-8")
    result = run_ingest(state, str(logs), None, None, incremental=True)
    assert result["total_lines"] == 1


def test_empty_directory_does_not_wipe_snapshot(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(path, [log_line("2026-08-25T10:00:00+00:00", "INFO", "one")])
    state = State(str(tmp_path / "state.db"))
    assert run_ingest(state, str(logs), None, None)["total_lines"] == 1

    empty = tmp_path / "empty"
    empty.mkdir()
    result = run_ingest(state, str(empty), None, None)
    assert result["total_lines"] == 0
    assert len(state.load_snapshot().clusters) == 1


def test_parallel_workers(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for index in range(4):
        path = logs / f"console-2026-08-2{index}.log"
        write_log(
            path,
            [
                log_line(f"2026-08-2{index}T10:00:0{n}+00:00", "INFO", f"group {n}")
                for n in range(20)
            ],
        )
    state = State(str(tmp_path / "state.db"))
    result = run_ingest(state, str(logs), None, None, workers=2)
    assert result["files"] == 4
    assert result["total_lines"] == 80


def catalog_total(state):
    with state.conn() as con:
        row = con.execute("SELECT COALESCE(SUM(total_count), 0) AS total FROM clusters").fetchone()
    return int(row["total"])


def test_repeated_full_ingest_does_not_inflate_catalog(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(path, [log_line("2026-08-25T10:00:00+00:00", "INFO", "group 1") for _ in range(3)])

    state = State(str(tmp_path / "state.db"))
    assert run_ingest(state, str(logs), None, None)["total_lines"] == 3
    assert catalog_total(state) == 3
    assert run_ingest(state, str(logs), None, None)["total_lines"] == 3
    assert catalog_total(state) == 3


def test_incremental_adds_only_delta_to_catalog(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(path, [log_line("2026-08-25T10:00:00+00:00", "INFO", "group 1") for _ in range(3)])
    state = State(str(tmp_path / "state.db"))
    run_ingest(state, str(logs), None, None)
    assert catalog_total(state) == 3

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(log_line("2026-08-25T11:00:00+00:00", "INFO", "group 1"))
        fh.write(log_line("2026-08-25T11:00:01+00:00", "INFO", "group 1"))
    assert run_ingest(state, str(logs), None, None, incremental=True)["total_lines"] == 2
    assert catalog_total(state) == 5


def test_rotation_triggers_rebuild_instead_of_double_counting(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(path, [log_line("2026-08-25T10:00:00+00:00", "INFO", "group 1") for _ in range(3)])
    state = State(str(tmp_path / "state.db"))
    run_ingest(state, str(logs), None, None)
    assert sum(c.count for c in state.load_snapshot().clusters.values()) == 3

    write_log(path, [log_line("2026-08-25T12:00:00+00:00", "INFO", "group 1") for _ in range(2)])
    result = run_ingest(state, str(logs), None, None, incremental=True)
    assert result["total_lines"] == 2
    assert sum(c.count for c in state.load_snapshot().clusters.values()) == 2
    assert catalog_total(state) == 2


def test_total_lines_counts_invalid_lines_separately(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    path.write_text(
        log_line("2026-08-25T10:00:00+00:00", "INFO", "one")
        + "this line is not monolog\n"
        + log_line("2026-08-25T10:00:01+00:00", "INFO", "two"),
        encoding="utf-8",
    )
    state = State(str(tmp_path / "state.db"))
    result = run_ingest(state, str(logs), None, None)
    assert result["total_lines"] == 3
    assert result["total_entries"] == 2


def test_offset_keys_are_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(path, [log_line("2026-08-25T10:00:00+00:00", "INFO", "one")])
    state = State(str(tmp_path / "state.db"))
    run_ingest(state, "logs", None, None)
    assert state.get_offset(str(path))[0] > 0


def test_missing_file_keeps_offset_in_incremental(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    first = logs / "console-2026-08-24.log"
    second = logs / "console-2026-08-25.log"
    write_log(first, [log_line("2026-08-24T10:00:00+00:00", "INFO", "one")])
    write_log(second, [log_line("2026-08-25T10:00:00+00:00", "INFO", "two")])
    state = State(str(tmp_path / "state.db"))
    run_ingest(state, str(logs), None, None)

    second.unlink()
    run_ingest(state, str(logs), None, None, incremental=True)
    assert state.get_offset(str(second))[0] > 0

    run_ingest(state, str(logs), None, None)
    assert state.get_offset(str(second))[0] == 0
    assert state.get_offset(str(first))[0] > 0


def test_temporarily_missing_file_is_not_double_counted(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(path, [log_line("2026-08-25T10:00:00+00:00", "INFO", "one")])
    state = State(str(tmp_path / "state.db"))
    run_ingest(state, str(logs), None, None)
    before = sum(c.count for c in state.load_snapshot().clusters.values())

    backup = logs / "console-2026-08-25.log.bak"
    path.rename(backup)
    run_ingest(state, str(logs), None, None, incremental=True)
    backup.rename(path)
    result = run_ingest(state, str(logs), None, None, incremental=True)

    assert result["total_lines"] == 0
    assert sum(c.count for c in state.load_snapshot().clusters.values()) == before


def test_gz_files_are_skipped_with_warning(tmp_path, capsys):
    import lmlog.ingest as ingest

    logs = tmp_path / "logs"
    logs.mkdir()
    write_log(
        logs / "console-2026-08-25.log", [log_line("2026-08-25T10:00:00+00:00", "INFO", "one")]
    )
    (logs / "old.log.gz").write_bytes(b"compressed")
    ingest._gz_warned = False
    files = ingest.list_log_files(str(logs))
    assert len(files) == 1
    assert "skipping *.gz" in capsys.readouterr().err


def test_full_ingest_ignores_filters(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    write_log(
        logs / "console-2026-08-24.log", [log_line("2026-08-24T10:00:00+00:00", "INFO", "one")]
    )
    write_log(
        logs / "console-2026-08-25.log", [log_line("2026-08-25T10:00:00+00:00", "INFO", "two")]
    )
    state = State(str(tmp_path / "state.db"))

    result = run_ingest(state, str(logs), since_hours=1, limit_files=1)
    assert result["status"] == "ok"
    assert result["files"] == 2
    assert sum(cluster.count for cluster in state.load_snapshot().clusters.values()) == 2


def test_incremental_partial_failure_does_not_commit_prefix(tmp_path, monkeypatch):
    import lmlog.ingest as ingest

    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(path, [log_line("2026-08-25T10:00:00+00:00", "INFO", "one")])
    state = State(str(tmp_path / "state.db"))
    run_ingest(state, str(logs), None, None)

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(log_line("2026-08-25T11:00:00+00:00", "INFO", "two"))
        fh.write(log_line("2026-08-25T11:00:01+00:00", "INFO", "three"))

    original = ingest.build_signature

    def boom(_entry):
        raise RuntimeError("boom")

    monkeypatch.setattr(ingest, "build_signature", boom)
    result = run_ingest(state, str(logs), None, None, incremental=True)
    assert result["status"] == "noop"
    assert sum(cluster.count for cluster in state.load_snapshot().clusters.values()) == 1

    monkeypatch.setattr(ingest, "build_signature", original)
    result = run_ingest(state, str(logs), None, None, incremental=True)
    assert result["total_lines"] == 2
    assert sum(cluster.count for cluster in state.load_snapshot().clusters.values()) == 3


def test_rebuild_failure_leaves_snapshot_unchanged(tmp_path, monkeypatch):
    import lmlog.ingest as ingest

    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "console-2026-08-25.log"
    write_log(path, [log_line("2026-08-25T10:00:00+00:00", "INFO", "one")])
    state = State(str(tmp_path / "state.db"))
    run_ingest(state, str(logs), None, None)

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(log_line("2026-08-25T11:00:00+00:00", "INFO", "two"))
        fh.write(log_line("2026-08-25T11:00:01+00:00", "INFO", "three"))

    original = ingest.build_signature

    def boom(_entry):
        raise RuntimeError("boom")

    monkeypatch.setattr(ingest, "build_signature", boom)
    result = run_ingest(state, str(logs), None, None)
    assert result["status"] == "aborted"
    assert sum(cluster.count for cluster in state.load_snapshot().clusters.values()) == 1

    monkeypatch.setattr(ingest, "build_signature", original)
    result = run_ingest(state, str(logs), None, None)
    assert result["status"] == "ok"
    assert sum(cluster.count for cluster in state.load_snapshot().clusters.values()) == 3


def test_full_rebuild_removes_missing_signatures_and_dates(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    old = logs / "console-2026-08-24.log"
    new = logs / "console-2026-08-25.log"
    write_log(old, [log_line("2026-08-24T10:00:00+00:00", "INFO", "alpha")])
    write_log(new, [log_line("2026-08-25T10:00:00+00:00", "INFO", "beta")])
    state = State(str(tmp_path / "state.db"))
    run_ingest(state, str(logs), None, None)

    old.unlink()
    run_ingest(state, str(logs), None, None)
    with state.conn() as con:
        signatures = {row[0] for row in con.execute("SELECT signature FROM clusters")}
        dates = {row[0] for row in con.execute("SELECT DISTINCT date FROM daily_counts")}
    assert not any("alpha" in signature for signature in signatures)
    assert any("beta" in signature for signature in signatures)
    assert dates == {"2026-08-25"}


def test_irrelevant_gz_does_not_warn(tmp_path, capsys):
    import lmlog.ingest as ingest

    logs = tmp_path / "logs"
    logs.mkdir()
    write_log(
        logs / "console-2026-08-25.log", [log_line("2026-08-25T10:00:00+00:00", "INFO", "one")]
    )
    (logs / "notes.txt.gz").write_bytes(b"compressed")
    ingest._gz_warned = False
    files = ingest.list_log_files(str(logs))
    assert len(files) == 1
    assert "skipping" not in capsys.readouterr().err
