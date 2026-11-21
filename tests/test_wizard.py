import argparse

from lmlog.wizard import cmd_wizard


def test_wizard_non_tty_summarizes_and_ingests(tmp_path, monkeypatch, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "console-2026-08-25.log").write_text(
        "[2026-08-25T10:00:00+00:00] app.ERROR: boom {}\n", encoding="utf-8"
    )
    db = str(tmp_path / "state.db")

    monkeypatch.setenv("LMLOG_LLM_MODE", "direct")
    monkeypatch.setattr("lmlog.llm.OllamaClient.health", lambda self, force=False: False)
    args = argparse.Namespace(
        log_dir=str(logs),
        db=db,
        since=None,
        limit=None,
        incremental=False,
        live=False,
        model="qwen2.5:3b",
        timeout=30,
        workers=0,
    )

    assert cmd_wizard(args) == 0
    output = capsys.readouterr().out
    assert "Current situation" in output
    assert "Errors" in output
