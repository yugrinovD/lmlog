from types import SimpleNamespace

from lmlog.cluster import Cluster, Clusterer


def entry(ts, level, message, klass=None, raw=None):
    context = {"class": klass} if klass else {}
    return SimpleNamespace(
        ts=ts,
        level=level,
        message=message,
        context=context,
        klass=klass,
        raw=raw or message,
        signature=message,
    )


def test_cluster_statistics():
    clusterer = Clusterer()
    clusterer.add(entry("2026-08-25T10:00:00+00:00", "INFO", "start", "A"))
    clusterer.add(entry("2026-08-25T10:00:01+00:00", "ERROR", "start", "A"))
    cluster = clusterer.clusters["start"]
    assert cluster.count == 2
    assert cluster.errors == 1
    assert cluster.classes["A"] == 2
    assert cluster.first_ts == "2026-08-25T10:00:00+00:00"
    assert cluster.last_ts == "2026-08-25T10:00:01+00:00"
    assert cluster.sample_message == "start"


def test_cluster_merge_and_roundtrip():
    left = Cluster("sig")
    left.add(entry("2026-08-25T10:00:00+00:00", "INFO", "sig", "A"))
    right = Cluster("sig")
    right.add(entry("2026-08-25T09:00:00+00:00", "ERROR", "sig", "B"))
    left.merge(right)
    assert left.count == 2
    assert left.errors == 1
    assert left.first_ts == "2026-08-25T09:00:00+00:00"

    restored = Cluster.from_dict(left.to_dict())
    assert restored.signature == "sig"
    assert restored.count == 2
    assert restored.errors == 1
    assert dict(restored.classes) == {"A": 1, "B": 1}


def test_clusterer_merge():
    a = Clusterer()
    a.add(entry("2026-08-25T10:00:00+00:00", "INFO", "x"))
    b = Clusterer()
    b.add(entry("2026-08-25T10:00:01+00:00", "INFO", "x"))
    a.merge(b)
    assert a.clusters["x"].count == 2


def test_mixed_timezones_are_ordered_chronologically():
    cluster = Cluster("sig")
    # 11:00+03:00 == 08:00Z, which is earlier than 09:00+00:00.
    cluster.add(entry("2026-08-25T11:00:00+03:00", "INFO", "sig"))
    cluster.add(entry("2026-08-25T09:00:00+00:00", "INFO", "sig"))
    assert cluster.first_ts == "2026-08-25T11:00:00+03:00"
    assert cluster.last_ts == "2026-08-25T09:00:00+00:00"


def test_merge_mixed_timezones():
    left = Cluster("sig")
    left.add(entry("2026-08-25T11:00:00+03:00", "INFO", "sig"))
    right = Cluster("sig")
    right.add(entry("2026-08-25T09:00:00+00:00", "INFO", "sig"))
    left.merge(right)
    assert left.first_ts == "2026-08-25T11:00:00+03:00"
    assert left.last_ts == "2026-08-25T09:00:00+00:00"


def test_z_timestamp_suffix_is_supported():
    cluster = Cluster("sig")
    cluster.add(entry("2026-08-25T11:00:00Z", "INFO", "sig"))
    cluster.add(entry("2026-08-25T12:00:00+00:00", "INFO", "sig"))
    assert cluster.first_ts == "2026-08-25T11:00:00Z"
    assert cluster.last_ts == "2026-08-25T12:00:00+00:00"
