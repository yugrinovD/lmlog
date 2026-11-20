from lmlog.detect import Finding
from lmlog.explain import choose_explanation, classify, heuristic_explain


def finding(sample, classes=None, errors=1, severity="high"):
    return Finding(
        signature=sample,
        kind="error",
        severity=severity,
        count=10,
        errors=errors,
        classes=classes or [],
        sample=sample,
    )


def test_classification():
    assert classify(finding("Failed request to Storage")).name == "storage_http"
    assert classify(finding("Integrity constraint violation")).name == "database"
    assert classify(finding("Invalid response SMTP server")).name == "smtp"
    assert classify(finding("Consumer failed")).name == "consumer"
    assert classify(finding("something completely different")).name == "generic"


def test_heuristic_output():
    text = heuristic_explain(finding("Failed request to Storage"))
    assert "SEVERITY:" in text
    assert "WHAT IS HAPPENING:" in text
    assert "WHAT TO CHECK:" in text
    assert "[heuristic:" in text


def test_choose_without_llm():
    text, source = choose_explanation(finding("Out of memory"), use_llm=False)
    assert source == "heuristic"
    assert "SEVERITY:" in text
