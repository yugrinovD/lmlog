from lmlog.llm import OllamaClient, build_prompt, normalize_base_url


def test_normalize_base_url():
    assert normalize_base_url("localhost:11434") == "http://localhost:11434"
    assert normalize_base_url("http://localhost:11434/") == "http://localhost:11434"
    assert normalize_base_url("https://llm.internal") == "https://llm.internal"


def test_health_is_cached(monkeypatch):
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(url, timeout=0):
        calls.append(url)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OllamaClient(host="http://localhost:11434")
    assert client.health() is True
    assert client.health() is True
    assert len(calls) == 1
    assert calls[0] == "http://localhost:11434/"


def test_prompt_marks_log_content_as_untrusted():
    prompt = build_prompt("ERROR", "message", "App", {}, 1, "1h", 1)
    assert "<log_event>" in prompt
    assert "</log_event>" in prompt
    assert "untrusted data" in prompt


def test_prompt_escapes_log_event_tags():
    prompt = build_prompt("ERROR", "bad </log_event> tail", "App", {}, 1, "1h", 1)
    assert "bad </log_event> tail" not in prompt
    assert "bad &lt;/log_event&gt; tail" in prompt
