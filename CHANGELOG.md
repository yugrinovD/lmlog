# Changelog

All notable changes to this project are documented here.

## [0.1.0] - 2026-11-21

### Added

- Streaming log ingest with byte offsets and rotation detection.
- Signature clustering, ERROR clusters, trends and deterministic explanations.
- Optional LLM explanations through `gogate` or direct Ollama.
- `lmlog serve` HTTP API with jobs, SSE progress, snapshots, explain and chat.
- Sanitized sample logs and a safer prompt budget.
- Tests for parser, API, gateway mode, prompt and job concurrency.
