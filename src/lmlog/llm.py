"""Optional local LLM integration (Ollama, CPU-first).

Only the standard library is used (``urllib``), so the tool works in
air-gapped environments without extra Python packages.
"""

from __future__ import annotations

import json
import urllib.request

from .config import default_model, ollama_host

PROMPT_TEMPLATE = """You are an expert in analysing application logs of a boxed
PHP/Symfony deployment. Explain what the log event means, how critical it is and
what an engineer should check.

Answer in the following format (concise, plain text):
SEVERITY: high|medium|low
WHAT IS HAPPENING: 1-2 sentences
LIKELY CAUSE: 1-2 sentences
WHAT TO CHECK:
- item
- item

Log event:
- Level: {level}
- Message: {message}
- Class/namespace: {klass}
- Context: {context}
- Frequency: {count} times ({period})
- Errors: {errors}

Answer strictly in the format above, with no extra text."""


class OllamaClient:
    def __init__(self, host: str = "", model: str = "", timeout: int = 120):
        self.host = host or ollama_host()
        self.model = model or default_model()
        self.timeout = timeout

    def generate(self, prompt: str, num_predict: int = 400, temperature: float = 0.2) -> str | None:
        url = f"http://{self.host}/api/generate"
        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": num_predict, "temperature": temperature},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data.get("response", "").strip()
        except Exception as exc:  # noqa: BLE001 - optional integration
            return f"[LLM error: {exc}]"

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://{self.host}/", timeout=3) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001 - optional integration
            return False


def build_prompt(
    level: str,
    message: str,
    klass: str,
    context: dict,
    count: int,
    period: str,
    errors: int,
) -> str:
    return PROMPT_TEMPLATE.format(
        level=level or "-",
        message=message or "-",
        klass=klass or "-",
        context=json.dumps(context, ensure_ascii=False)[:1500] if context else "{}",
        count=count,
        period=period or "-",
        errors=errors,
    )
