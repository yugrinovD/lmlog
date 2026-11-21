import json

from lmlog.config import gateway_base_url
from lmlog.llm import GatewayClient, OllamaClient, create_client


class Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def clear_gateway_env(monkeypatch):
    monkeypatch.delenv("GOGATE_API_KEY", raising=False)
    monkeypatch.delenv("GOGATE_API_KEY_FILE", raising=False)
    monkeypatch.delenv("GOGATE_BASE_URL", raising=False)
    monkeypatch.delenv("LMLOG_LLM_MODE", raising=False)


def test_gateway_base_url_normalizes_v1(monkeypatch):
    clear_gateway_env(monkeypatch)
    monkeypatch.setenv("GOGATE_BASE_URL", "http://gogate:8080/v1/")
    assert gateway_base_url() == "http://gogate:8080"


def test_gateway_client_normalizes_v1(monkeypatch):
    clear_gateway_env(monkeypatch)
    client = GatewayClient(base_url="http://gogate:8080/v1/", api_key="gk_test", model="qwen2.5:3b")
    assert client.base_url == "http://gogate:8080"


def test_create_client_respects_mode(monkeypatch):
    clear_gateway_env(monkeypatch)
    assert isinstance(create_client(), GatewayClient)
    monkeypatch.setenv("LMLOG_LLM_MODE", "direct")
    assert isinstance(create_client(), OllamaClient)


def test_gateway_health_checks_api_key_and_model(monkeypatch):
    clear_gateway_env(monkeypatch)
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append(request)
        return Response({"object": "list", "data": [{"id": "qwen2.5:3b"}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = GatewayClient(base_url="http://gogate:8080", api_key="gk_test", model="qwen2.5:3b")
    assert client.health() is True
    assert calls[0].full_url == "http://gogate:8080/v1/models"
    assert calls[0].get_header("Authorization") == "Bearer gk_test"


def test_gateway_health_is_false_for_unknown_model(monkeypatch):
    clear_gateway_env(monkeypatch)

    def fake_urlopen(request, timeout=0):
        return Response({"data": [{"id": "another-model"}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = GatewayClient(base_url="http://gogate:8080", api_key="gk_test", model="qwen2.5:3b")
    assert client.health() is False


def test_gateway_generate_parses_openai_response(monkeypatch):
    clear_gateway_env(monkeypatch)
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["auth"] = request.get_header("Authorization")
        return Response({"choices": [{"message": {"role": "assistant", "content": "  hello  "}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = GatewayClient(base_url="http://gogate:8080", api_key="gk_test", model="qwen2.5:3b")
    assert client.generate("explain this", num_predict=42, temperature=0.1) == "hello"
    assert captured["url"] == "http://gogate:8080/v1/chat/completions"
    assert captured["auth"] == "Bearer gk_test"
    assert captured["body"]["model"] == "qwen2.5:3b"
    assert captured["body"]["max_tokens"] == 42
    assert captured["body"]["temperature"] == 0.1
    assert captured["body"]["messages"][0]["content"] == "explain this"


def test_gateway_generate_reports_errors_without_secrets(monkeypatch):
    clear_gateway_env(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(OSError("connection refused")),
    )
    client = GatewayClient(
        base_url="http://gogate:8080", api_key="super-secret", model="qwen2.5:3b"
    )
    text = client.generate("hello")
    assert text.startswith("[LLM error:")
    assert "super-secret" not in text
    assert client.health() is False


def test_gateway_reads_api_key_file(monkeypatch, tmp_path):
    clear_gateway_env(monkeypatch)
    key_file = tmp_path / "key"
    key_file.write_text("gk_from_file\n", encoding="utf-8")
    monkeypatch.setenv("GOGATE_API_KEY_FILE", str(key_file))
    client = GatewayClient(base_url="http://gogate:8080", model="qwen2.5:3b")
    assert client.api_key == "gk_from_file"


def test_gateway_requires_api_key(monkeypatch):
    clear_gateway_env(monkeypatch)
    client = GatewayClient(base_url="http://gogate:8080", model="qwen2.5:3b")
    assert client.health() is False
    assert client.generate("hello").startswith("[LLM error:")
