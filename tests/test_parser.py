from lmlog.parser import iter_lines, parse_line

LINE = (
    "[2026-08-25T11:44:54.253480+03:00] app.INFO: Start work "
    '{"class":"Foo.Bar","groupWorkId":225} {"uid":"abc"}'
)


def test_parse_full_line():
    entry = parse_line(LINE)
    assert entry is not None
    assert entry.ts.startswith("2026-08-25T11:44")
    assert entry.channel == "app"
    assert entry.level == "INFO"
    assert entry.message == "Start work"
    assert entry.klass == "Foo.Bar"
    assert entry.context["groupWorkId"] == 225
    assert entry.extra["uid"] == "abc"
    assert not entry.is_error


def test_parse_without_json():
    entry = parse_line("[2026-08-25T11:44:54+00:00] app.INFO: hello world")
    assert entry is not None
    assert entry.message == "hello world"
    assert entry.context == {}
    assert entry.extra == {}


def test_single_json_is_treated_as_context():
    entry = parse_line('[2026-08-25T11:44:54+00:00] app.INFO: hi {"uid":"x"}')
    assert entry is not None
    assert entry.message == "hi"
    assert entry.context == {"uid": "x"}
    assert entry.extra == {}


def test_braces_inside_json_strings():
    entry = parse_line(
        '[2026-08-25T11:44:54+00:00] app.INFO: weird {"class":"X","msg":"a {b} c"} {"uid":"u"}'
    )
    assert entry is not None
    assert entry.message == "weird"
    assert entry.context["msg"] == "a {b} c"
    assert entry.extra == {"uid": "u"}


def test_error_levels():
    entry = parse_line("[2026-08-25T11:44:54+00:00] app.ERROR: boom")
    assert entry is not None
    assert entry.is_error


def test_invalid_lines():
    assert parse_line("") is None
    assert parse_line("not a monolog line") is None


def test_iter_lines(tmp_path):
    path = tmp_path / "console.log"
    path.write_text(LINE + "\ninvalid\n", encoding="utf-8")
    entries = list(iter_lines(str(path)))
    assert len(entries) == 1
    assert entries[0].source == str(path)
