from lmlog.cluster import Cluster
from lmlog.state import State


def make_cluster(
    signature="sig",
    count=3,
    first_ts="2026-08-25T00:00:00+00:00",
    last_ts="2026-08-25T01:00:00+00:00",
):
    cluster = Cluster(signature, count=count)
    cluster.levels["INFO"] = count
    cluster.classes["A"] = count
    cluster.first_ts = first_ts
    cluster.last_ts = last_ts
    cluster.sample_message = "message"
    cluster.sample_context = {"class": "A"}
    return cluster


def catalog_row(state, signature="sig"):
    with state.conn() as con:
        return con.execute("SELECT * FROM clusters WHERE signature=?", (signature,)).fetchone()


def test_offsets_roundtrip(tmp_path):
    state = State(str(tmp_path / "state.db"))
    assert state.get_offset("/tmp/a.log") == (0, 0.0, 0, 0)
    state.set_offset("/tmp/a.log", 100, 123.5, 10, 999)
    assert state.get_offset("/tmp/a.log") == (100, 123.5, 10, 999)
    state.set_offsets([("/tmp/b.log", 5, 1.0, 2, 7)])
    assert state.get_offset("/tmp/b.log") == (5, 1.0, 2, 7)


def test_snapshot_roundtrip(tmp_path):
    state = State(str(tmp_path / "state.db"))
    state.save_snapshot([make_cluster()], 24)
    loaded = state.load_snapshot()
    assert len(loaded.clusters) == 1
    cluster = loaded.clusters["sig"]
    assert cluster.count == 3
    assert cluster.errors == 0
    assert cluster.sample_context == {"class": "A"}
    assert state.total_entries() == 3
    created, period = state.snapshot_info()
    assert created
    assert period == "24"


def test_daily_counts_replace_and_add(tmp_path):
    state = State(str(tmp_path / "state.db"))
    state.upsert_daily({"s": {"2026-08-25": 5}}, {"2026-08-25"})
    assert state.load_daily() == {"s": {"2026-08-25": 5}}
    state.upsert_daily({"s": {"2026-08-25": 2}}, set())
    assert state.load_daily_for("s") == {"2026-08-25": 7}
    state.upsert_daily({"s": {"2026-08-25": 3}}, {"2026-08-25"})
    assert state.load_daily_for("s") == {"2026-08-25": 3}


def test_explanations_and_meta(tmp_path):
    state = State(str(tmp_path / "state.db"))
    assert state.get_explanation("sig") is None
    state.set_explanation("sig", "text")
    assert state.get_explanation("sig") == "text"
    state.set_meta("k", "v")
    assert state.get_meta("k") == "v"


def test_known_signatures_and_record(tmp_path):
    state = State(str(tmp_path / "state.db"))
    state.record_clusters([make_cluster("one")])
    state.record_clusters([make_cluster("one")])
    assert state.known_signatures() == {"one"}


def test_in_memory_database_persists_between_calls():
    state = State(":memory:")
    state.set_meta("k", "v")
    assert state.get_meta("k") == "v"


def test_clusters_is_a_catalog_incremental_adds_full_resets(tmp_path):
    state = State(str(tmp_path / "state.db"))
    state.record_clusters([make_cluster("sig", 3)], mode="incremental")
    state.record_clusters([make_cluster("sig", 3)], mode="incremental")
    assert catalog_row(state)["total_count"] == 6

    state.record_clusters([make_cluster("sig", 3)], mode="full")
    assert catalog_row(state)["total_count"] == 3
    state.record_clusters([make_cluster("sig", 3)], mode="full")
    assert catalog_row(state)["total_count"] == 3


def test_catalog_first_and_last_use_utc_min_max(tmp_path):
    state = State(str(tmp_path / "state.db"))
    state.record_clusters(
        [make_cluster("sig", 1, "2026-08-25T11:00:00+03:00", "2026-08-25T11:00:00+03:00")],
        mode="incremental",
    )
    state.record_clusters(
        [make_cluster("sig", 1, "2026-08-25T09:00:00+00:00", "2026-08-25T10:00:00+00:00")],
        mode="incremental",
    )
    row = catalog_row(state)
    assert row["first_seen"] == "2026-08-25T08:00:00+00:00"
    assert row["last_seen"] == "2026-08-25T10:00:00+00:00"


def test_batch_offsets_and_pruning(tmp_path):
    state = State(str(tmp_path / "state.db"))
    state.set_offsets(
        [
            ("/data/logs/a.log", 1, 1.0, 1, 1),
            ("/data/logs/b.log", 2, 2.0, 2, 2),
            ("/other/c.log", 3, 3.0, 3, 3),
        ]
    )
    offsets = state.get_offsets(["/data/logs/a.log", "/data/logs/b.log", "/missing"])
    assert offsets["/data/logs/a.log"] == (1, 1.0, 1, 1)
    assert "/missing" not in offsets

    removed = state.prune_offsets("/data/logs", ["/data/logs/a.log"])
    assert removed == 1
    assert state.get_offset("/data/logs/b.log") == (0, 0.0, 0, 0)
    assert state.get_offset("/other/c.log")[0] == 3
