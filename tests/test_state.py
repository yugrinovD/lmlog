from lmlog.cluster import Cluster
from lmlog.state import State


def make_cluster(signature="sig", count=3):
    cluster = Cluster(signature, count=count)
    cluster.levels["INFO"] = count
    cluster.classes["A"] = count
    cluster.first_ts = "2026-08-25T00:00:00+00:00"
    cluster.last_ts = "2026-08-25T01:00:00+00:00"
    cluster.sample_message = "message"
    cluster.sample_context = {"class": "A"}
    return cluster


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
