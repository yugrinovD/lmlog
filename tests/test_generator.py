from pathlib import Path

from lmlog.generator import generate_to_dir, stream_logs
from lmlog.parser import parse_line


def read_all(directory):
    return "".join(
        Path(path).read_text(encoding="utf-8") for path in sorted(Path(directory).glob("*.log"))
    )


def test_generate_reproducible(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    result = generate_to_dir(str(first), lines=200, seed=3)
    generate_to_dir(str(second), lines=200, seed=3)
    assert result["lines"] == 200
    assert read_all(first) == read_all(second)


def test_generated_lines_are_parseable(tmp_path):
    generate_to_dir(str(tmp_path), lines=100, seed=1, profile="demo")
    lines = read_all(tmp_path).splitlines()
    assert len(lines) == 100
    assert all(parse_line(line) is not None for line in lines)


def test_multi_day_demo_injects_trends(tmp_path):
    generate_to_dir(str(tmp_path), lines=3000, days=3, seed=5, anomaly=True)
    files = sorted(Path(tmp_path).glob("*.log"))
    assert len(files) == 3
    first_day = files[0].read_text(encoding="utf-8")
    last_day = files[-1].read_text(encoding="utf-8")
    assert "Retry policy activated" not in first_day
    assert "Retry policy activated" in last_day


def test_no_anomaly_flag_disables_injection(tmp_path):
    generate_to_dir(str(tmp_path), lines=3000, days=3, seed=5, anomaly=False)
    last_day = sorted(Path(tmp_path).glob("*.log"))[-1].read_text(encoding="utf-8")
    assert "Retry policy activated" not in last_day


def test_generate_with_rotation(tmp_path):
    result = generate_to_dir(str(tmp_path), lines=5000, seed=2, rotate_bytes=20_000)
    files = list(Path(tmp_path).glob("*.log"))
    assert len(files) > 1
    assert result["lines"] == 5000


def test_stream_logs(tmp_path):
    result = stream_logs(str(tmp_path), rate=200, duration=0.15, seed=4)
    assert result["lines"] > 0
    assert Path(result["file"]).exists()
