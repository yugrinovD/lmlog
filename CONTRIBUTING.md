# Contributing

This is a portfolio project, but it is maintained like a real service.

## Local checks

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,serve]"

ruff check .
ruff format --check .
pytest --cov=lmlog
```

## Rules

- Do not commit `.env`, generated databases, uploaded logs or screenshots with
  real production data.
- Use synthetic logs from `lmlog generate` for demos and tests.
- Keep the core package dependency-free; optional features live in extras.
- Add tests for parser, signature and cache changes.
- Use conventional commit prefixes (`feat:`, `fix:`, `docs:`, `test:`).

See [AI_WORKFLOW.md](AI_WORKFLOW.md).
