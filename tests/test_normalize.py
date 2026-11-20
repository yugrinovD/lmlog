from lmlog.normalize import (
    build_signature,
    norm_context_sig,
    normalize_message,
    signature,
)
from lmlog.parser import LogEntry


def test_normalize_variables():
    text = (
        "user 12345 uuid 123e4567-e89b-12d3-a456-426614174000 "
        "ip 10.0.0.1 ts 2026-08-25T11:44:54Z hash abcdefabcdefabcdef"
    )
    normalized = normalize_message(text)
    assert "12345" not in normalized
    assert "123e4567" not in normalized
    assert "10.0.0.1" not in normalized
    assert "2026-08-25" not in normalized
    assert "UUID" in normalized
    assert "IP" in normalized
    assert "TS" in normalized
    assert "HEX" in normalized


def test_signature_includes_class_and_context():
    result = signature("User 42", klass="Domain\\User", level="INFO", context_terms=("GET",))
    assert result.startswith("User N")
    assert "@Domain\\User" in result
    assert "|GET" in result


def test_build_signature_is_stable_for_variable_ids():
    a = LogEntry(
        ts="t", level="INFO", channel="app", message="Group 42 stopped", context={"class": "A"}
    )
    b = LogEntry(
        ts="t", level="INFO", channel="app", message="Group 99 stopped", context={"class": "A"}
    )
    assert build_signature(a) == build_signature(b)


def test_norm_context_sig_keeps_selected_keys():
    terms = norm_context_sig(
        {"method": "GET", "statusCode": 404, "other": 1}, ("method", "statusCode")
    )
    assert terms == ("GET", "N")
