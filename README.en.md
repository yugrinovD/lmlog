# lmlog

**Local, CPU-first log intelligence: signatures, errors, anomalies and explanations for very large log volumes.**

`lmlog` turns millions of structured log lines into a small set of readable
signatures, detects errors, new patterns and anomalies from daily history, and
explains findings in plain language — deterministically or with a local LLM.

It targets **50M+ log lines per day**, needs no GPU, sends no data outside the
host and works in air-gapped environments.

[Русская версия](README.md)

---

## Why

In boxed deployments of large PHP/Symfony applications most log volume is noise:
tens of millions of repeated INFO lines per day. Real errors get lost, and
grepping through gigabytes of files does not scale.

`lmlog` solves three problems:

1. **Reduction**: millions of lines → dozens of unique signatures with frequency
   and trend.
2. **Detection**: errors, new patterns, abnormal growth and patterns that went
   silent.
3. **Explanation**: a local LLM or a deterministic heuristic fallback answers
   what is happening, how critical it is and what to check.

## Features

- **Streaming ingest** — memory does not grow with the number of lines; no list
  of entries is ever materialised.
- **Exact byte offsets + inode** — correct incremental reads and rotation
  detection.
- **Normalisation** — UUIDs, numbers, IPs, hashes and timestamps collapse into
  typed tokens.
- **Parallel parsing** — `--workers N` across files/cores.
- **Detection** — error clusters, new signatures, high-volume noise, spikes and
  drops.
- **Daily trends** — `daily_counts` in SQLite with a median baseline.
- **Local LLM** — Ollama (CPU), explanation cache in SQLite; if the model is
  unavailable, heuristics are used automatically.
- **Log generator** — realistic synthetic stream for demos and benchmarks.
- **Docker + Compose** — generator, CLI, daemon and an optional Ollama profile.
- **Zero mandatory dependencies** — standard library only; `orjson` is picked up
  automatically if installed.

## Architecture

```mermaid
flowchart LR
    A[Log files / journald] -->|stream, byte offsets| B[Parser]
    B --> C[Normalizer]
    C --> D[Signature]
    D --> E[Clusterer + daily counters]
    E --> F[(SQLite snapshot)]
    F --> G[Detect: errors / new / noise]
    F --> H[Trends: spikes / drops / new]
    G --> I{Explain}
    H --> I
    I -->|available| J[Local LLM via Ollama]
    I -->|fallback| K[Heuristic rules]
```

The heavy pass (`ingest`) reads files and stores a cluster snapshot in SQLite.
All queries (`scan`, `errors`, `trends`, `explain`) read the snapshot and are
instant. The `lmlog daemon` process periodically reads only new bytes and keeps
the snapshot fresh.

## Quick start: Docker

```bash
cp .env.example .env
make demo
```

`make demo` builds the image, generates synthetic logs, runs `ingest` and shows
findings and error clusters.

Useful targets:

```bash
make generate     # generate logs (LMLOG_DEMO_LINES, LMLOG_DEMO_DAYS)
make ingest       # read logs and save a snapshot
make scan         # findings
make errors       # error clusters
make explain      # explanations (heuristic, no LLM required)
make trends       # anomalies from history
make daemon-up    # background incremental ingest
make bench        # measure throughput
make llm          # start Ollama and pull the model
make down         # stop services
```

Manual container usage:

```bash
docker compose run --rm lmlog ingest
docker compose run --rm lmlog scan
docker compose run --rm lmlog errors
docker compose run --rm lmlog explain --no-llm
```

## Quick start: local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 1. Generate demo logs into data/logs
lmlog generate --lines 200000 --days 1 --profile demo

# 2. Read logs and save a snapshot
lmlog ingest

# 3. Instant queries over the snapshot
lmlog scan
lmlog errors
lmlog classes Handler
lmlog explain --no-llm
```

State lives in `~/.lmlog/lmlog.db` by default; logs are read from `data/logs`
(override with `--log-dir` / `LMLOG_LOG_DIR`).

### Example output

```text
$ lmlog ingest
Ingest finished: files=1, lines=200 000, entries=200 000, signatures=12,
time=2.2s (90 415 lines/s).
Snapshot saved. `scan`, `errors`, `classes` and `explain` now read from it instantly.

$ lmlog errors -n 3
ERROR clusters: 6

======================================================================
errors=1189  total=1 189  first=2026-08-25T00:00:31  last=2026-08-25T23:59:43
  msg: Failed request to Storage {method} {url} {statusCode}
======================================================================
errors=419  total=419  first=2026-08-25T00:02:32  last=2026-08-25T23:55:38
  msg: Storage Disk Space Error:Cannot complete Storage request in 3 attempts
======================================================================
errors=410  total=410  first=2026-08-25T00:02:35  last=2026-08-25T23:53:49
  classes: Infrastructure\Repository\EventTimetableRepository
  msg: Uncaught PHP Exception Doctrine\DBAL\Exception\IntegrityConstraintViolationException
```

## Portfolio demo scenario

1. **Generate**: `lmlog generate --lines 200000 --days 7 --profile demo` — seven
   days of realistic flow (noise + recurring errors). For multi-day demos the
   generator deliberately injects an error spike, a silent consumer and a new
   pattern, so `trends` shows meaningful output immediately (`--no-anomaly`
   disables this).
2. **Ingest**: `lmlog ingest` — snapshot and daily history.
3. **Triage**: `lmlog scan` and `lmlog errors` — errors, new patterns, noise.
4. **Trends**: `lmlog trends` — spikes, silent patterns, new templates.
5. **Explain**: `lmlog explain --no-llm` — structured heuristics; with
   `make llm` and `lmlog explain` — local model explanations.
6. **Daemon**: `make daemon-up` — background refresh every 5 minutes.
7. **Benchmark**: `lmlog bench --lines 1000000` — throughput.

## Commands

| Command | Purpose |
| --- | --- |
| `lmlog` / `lmlog wizard` | Interactive assistant |
| `lmlog generate` | Generate synthetic logs |
| `lmlog ingest` | Read logs and save a snapshot |
| `lmlog scan` | Findings: errors, new and frequent patterns |
| `lmlog errors` | ERROR/CRITICAL clusters |
| `lmlog classes <name>` | Search by class/namespace |
| `lmlog explain` | Explanations (LLM or heuristic) |
| `lmlog trends` | Proactive trends from daily history |
| `lmlog status` / `summary` | Current situation |
| `lmlog daemon` | Background incremental ingest |
| `lmlog bench` | Throughput measurement |
| `lmlog journal` | System journal (if `journalctl` is available) |

Full help: `lmlog <command> --help`.

## Scale and performance

`lmlog` is designed for **50M+ lines per day**:

- streaming ingest: memory depends on the number of unique signatures, not on
  the number of lines;
- binary file reads with byte offsets and inode tracking;
- SQLite in WAL mode with batched `executemany` writes;
- optional multi-process parsing (`--workers N`);
- optional `orjson` acceleration for JSON parsing.

Measurement on a synthetic stress profile (single process, regular dev machine):

```text
$ lmlog bench --lines 1000000
Throughput      : ~90 000 lines/s
50M lines at this rate: ~9.2 min
```

Numbers depend on hardware, line format and JSON share. This is a reference
point, not a hard capacity limit: the tool is built for tens of millions of
lines per day.

## Log format

Symfony/Monolog is supported:

```text
[2026-08-25T11:44:54.253480+00:00] app.INFO: message {"context"} {"extra"}
```

- `context` may contain `class` — the application namespace.
- `extra` often contains `uid`.
- systemd journal is read through `journalctl` when available (optional).

Default file patterns: `console-*.log`, `prod-*.log`, `courses_console-*.log`,
`*.log`. The directory is set with `--log-dir` or `LMLOG_LOG_DIR`.

## Local LLM

`lmlog explain` first tries Ollama and falls back to heuristic explanations when
the model is unavailable, so the demo always works.

```bash
make llm                     # docker compose profile llm + ollama pull
lmlog explain                # LLM if available
lmlog explain --no-llm       # heuristics only
lmlog explain --refresh      # ignore the cache
```

Settings: `OLLAMA_HOST` (default `127.0.0.1:11434`), `LMLOG_MODEL` (default
`qwen2.5:3b`). Explanations are cached per signature in SQLite.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `LMLOG_HOME` | `~/.lmlog` | State directory (daemon pid/log) |
| `LMLOG_DB` | `$LMLOG_HOME/lmlog.db` | SQLite path |
| `LMLOG_LOG_DIR` | `data/logs` | Log directory |
| `LMLOG_MODEL` | `qwen2.5:3b` | Ollama model |
| `OLLAMA_HOST` | `127.0.0.1:11434` | Ollama endpoint |
| `NO_COLOR` | — | Disable ANSI colours |

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
lmlog bench --lines 1000000
```

CI: GitHub Actions, Python 3.11/3.12, lint + tests.

## Project layout

```text
src/lmlog/
  parser.py      # Monolog parsing
  normalize.py   # normalisation and signatures
  cluster.py     # clustering and statistics
  ingest.py      # streaming file and journald reads
  state.py       # SQLite: offsets, snapshot, history, cache
  detect.py      # errors, new patterns, trends
  explain.py     # heuristic explanations
  llm.py         # Ollama client
  generator.py   # synthetic log generator
  cli.py         # command line interface
  wizard.py      # interactive mode
  daemon.py      # background refresh
tests/           # pytest
examples/        # sample log
```

## Limitations

- Optimised for structured Monolog logs; arbitrary text is treated as a message
  without context.
- journald requires `journalctl` (optional inside containers).
- A CPU LLM explains dozens of unique templates, not the line stream — this is a
  deliberate architectural choice.
- SQLite is a single state store; this is sufficient for one host and one log set.

## License

MIT. See [LICENSE](LICENSE).
