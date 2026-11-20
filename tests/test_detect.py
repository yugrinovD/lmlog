from lmlog.cluster import Cluster
from lmlog.detect import detect, detect_trends


def make_cluster(signature, levels, classes=None):
    cluster = Cluster(signature)
    cluster.count = sum(levels.values())
    cluster.levels.update(levels)
    cluster.classes.update(classes or {})
    cluster.sample_message = signature
    cluster.first_ts = "2026-08-25T00:00:00+00:00"
    cluster.last_ts = "2026-08-25T23:59:59+00:00"
    return cluster


def test_error_and_frequent_findings():
    errors = make_cluster("boom", {"ERROR": 5}, {"App\\Service": 5})
    noise = make_cluster("noise", {"INFO": 5000}, {"App\\Worker": 5000})
    findings = detect([errors, noise], known={"boom", "noise"})
    kinds = {finding.kind for finding in findings}
    assert "error" in kinds
    assert "frequent" in kinds
    assert findings[0].signature == "boom"


def test_new_signature_requires_unknown():
    cluster = make_cluster("fresh", {"INFO": 3})
    assert any(f.kind == "new" for f in detect([cluster], known=set()))
    assert not any(f.kind == "new" for f in detect([cluster], known={"fresh"}))


def test_trend_spike():
    cluster = make_cluster("spike", {"INFO": 1100})
    daily = {"spike": {"2026-08-24": 100, "2026-08-25": 1100}}
    findings = detect_trends([cluster], daily, top_n=10)
    assert any("anomalous growth" in finding.note for finding in findings)


def test_trend_drop():
    cluster = make_cluster("drop", {"INFO": 1010})
    daily = {"drop": {"2026-08-24": 1000, "2026-08-25": 10}}
    findings = detect_trends([cluster], daily, top_n=10)
    assert any("sharp drop" in finding.note for finding in findings)


def test_trend_new_pattern():
    cluster = make_cluster("newbie", {"INFO": 5})
    daily = {"newbie": {"2026-08-25": 5}}
    findings = detect_trends([cluster], daily, top_n=10)
    assert any(finding.kind == "new_trend" for finding in findings)


def test_full_silence_is_detected_via_global_latest_date():
    silent = make_cluster("silent", {"INFO": 2000})
    other = make_cluster("other", {"INFO": 10})
    daily = {
        "silent": {"2026-08-24": 1000, "2026-08-25": 1000},
        "other": {"2026-08-24": 10, "2026-08-25": 10, "2026-08-26": 10},
    }
    findings = detect_trends([silent, other], daily, top_n=10)
    drop = next(finding for finding in findings if finding.signature == "silent")
    assert drop.count == 0
    assert "sharp drop" in drop.note


def test_spike_then_normal_is_not_a_drop():
    cluster = make_cluster("spiky", {"INFO": 1200})
    other = make_cluster("other", {"INFO": 10})
    daily = {
        "spiky": {"2026-08-23": 100, "2026-08-24": 1000, "2026-08-25": 100},
        "other": {"2026-08-23": 10, "2026-08-24": 10, "2026-08-25": 10},
    }
    findings = detect_trends([cluster, other], daily, top_n=10)
    assert not any(finding.signature == "spiky" for finding in findings)


def test_spike_is_anchored_to_global_latest_date():
    cluster = make_cluster("spiky", {"INFO": 1100})
    other = make_cluster("other", {"INFO": 10})
    daily = {
        "spiky": {"2026-08-23": 100, "2026-08-24": 100, "2026-08-25": 1000},
        "other": {"2026-08-23": 10, "2026-08-24": 10, "2026-08-25": 10},
    }
    findings = detect_trends([cluster, other], daily, top_n=10)
    growth = next(finding for finding in findings if finding.signature == "spiky")
    assert "anomalous growth on 2026-08-25" in growth.note
