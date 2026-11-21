import lmlog.ingest as ingest


class FakeProcess:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def kill(self):
        self._alive = False

    def wait(self):
        self._alive = False
        return 0


def test_read_journal_passes_timeout_and_parses(monkeypatch):
    monkeypatch.setattr(ingest, "journal_available", lambda: True)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProcess(["2026-08-25T10:00:00+00:00 host unit[1]: hello\n"])

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    entries = list(ingest.read_journal(since="-1h", timeout=1))
    assert len(entries) == 1
    assert entries[0].message == "hello"
    assert "journalctl" in captured["cmd"]
