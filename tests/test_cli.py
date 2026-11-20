from lmlog.cli import main


def test_end_to_end(tmp_path, capsys):
    logs = tmp_path / "logs"
    db = tmp_path / "state.db"

    assert main(["generate", "--out", str(logs), "--lines", "3000", "--seed", "1"]) == 0
    assert main(["ingest", "--log-dir", str(logs), "--db", str(db)]) == 0
    assert main(["scan", "--db", str(db)]) == 0
    assert main(["errors", "--db", str(db), "-n", "3"]) == 0
    assert main(["classes", "--db", str(db), "Handler", "-n", "3"]) == 0
    assert main(["explain", "--db", str(db), "--no-llm", "-n", "2"]) == 0
    assert main(["trends", "--db", str(db), "-n", "2"]) == 0
    assert main(["trends", "--db", str(db), "--log-dir", str(logs), "--live", "-n", "2"]) == 0
    assert main(["status", "--db", str(db)]) == 0

    output = capsys.readouterr().out
    assert "Ingest finished" in output
    assert "Findings" in output
    assert "heuristic" in output


def test_bench_smoke(capsys):
    assert main(["bench", "--lines", "2000", "--profile", "stress"]) == 0
    output = capsys.readouterr().out
    assert "Throughput" in output
    assert "50M lines" in output
