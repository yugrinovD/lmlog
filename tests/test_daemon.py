import lmlog.daemon as daemon


def test_start_daemon_forwards_since_and_workers(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(daemon, "daemon_status", lambda _path: {"running": False, "pid": None})
    captured = {}

    class FakeProcess:
        pid = 1234

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    pid = daemon.start_daemon(db_path, str(tmp_path / "logs"), 60, since_hours=12, workers=2)

    assert pid == 1234
    cmd = captured["cmd"]
    assert "--since" in cmd
    assert cmd[cmd.index("--since") + 1] == "12"
    assert "--workers" in cmd
    assert cmd[cmd.index("--workers") + 1] == "2"
