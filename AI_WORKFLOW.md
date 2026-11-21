# AI workflow

This project follows the shared [AI Ops Stack AI workflow](https://github.com/yugrinovD/ai-ops-stack/blob/master/AI_WORKFLOW.md).

Additional rules for `lmlog`:

- Synthetic logs only in examples and screenshots.
- Prompt changes must keep log data inside the untrusted `<log_event>` block.
- LLM output is never executed and is rendered as sanitized Markdown in the UI.
- Heuristic fallback must work without a gateway or Ollama.
