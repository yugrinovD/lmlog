from __future__ import annotations

from types import SimpleNamespace

from lmlog.cluster import Cluster
from lmlog.serve.chat import MAX_CONTEXT_CHARS, build_chat_prompt


class FakeSnapshot:
    created = "2026-01-01T00:00:00+00:00"
    total_entries = 100_000

    def __init__(self, clusters: list[Cluster]):
        self.clusters = clusters

    def summary(self):
        return {
            "lines": 100_000,
            "entries": 100_000,
            "errors": 10,
            "levels": {"INFO": 99_990, "ERROR": 10},
            "top_classes": [{"name": "App\\Service", "count": 1000}],
        }

    def chat_clusters(self, signature, top_n):
        return self.clusters[:top_n]

    def trends(self, days=7, limit=8):
        return [
            {
                "severity": "high",
                "kind": "trend",
                "sample": "trend sample " + "z" * 220,
                "note": "anomalous growth " + "n" * 220,
            }
        ]


def make_cluster(index: int) -> Cluster:
    cluster = Cluster(signature=f"signature-{index}", count=1000 + index)
    cluster.levels["ERROR"] = 1
    cluster.classes[f"App\\Class{index}"] = 100
    cluster.first_ts = "2026-01-01T00:00:00+00:00"
    cluster.last_ts = "2026-01-01T23:59:59+00:00"
    cluster.sample_message = f"message {index} " + "m" * 300
    cluster.sample_raw = f"raw {index} " + "r" * 500
    cluster.sample_context = {"payload": "c" * 800}
    return cluster


def test_chat_prompt_keeps_question_and_closing_tag_when_data_is_truncated():
    snapshot = FakeSnapshot([make_cluster(index) for index in range(12)])
    question = "Почему выросло число ошибок в Storage?"
    prompt = build_chat_prompt(snapshot, question, snapshot.clusters[:8], [])

    assert len(prompt) <= MAX_CONTEXT_CHARS
    assert f"question: {question}" in prompt
    assert prompt.rstrip().endswith("answer:")
    assert "</log_data>" in prompt
    assert "[truncated]" in prompt


def test_chat_prompt_drops_history_before_question():
    snapshot = FakeSnapshot([make_cluster(index) for index in range(12)])
    history = [
        SimpleNamespace(role="user" if index % 2 == 0 else "assistant", content="h" * 3000)
        for index in range(6)
    ]
    question = "Что случилось с consumer?"
    prompt = build_chat_prompt(snapshot, question, snapshot.clusters[:8], history)

    assert len(prompt) <= MAX_CONTEXT_CHARS
    assert f"question: {question}" in prompt
    assert "</log_data>" in prompt
    # The question must survive even after the least important history is dropped.
    assert prompt.count("question:") == 1
