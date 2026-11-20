# lmlog design notes

## Problem

Boxed deployments of large PHP/Symfony applications can produce tens of millions
of structured log lines per day. The overwhelming majority is repeated INFO
output. Engineers need to find the small number of real problems without
shipping gigabytes of logs to an external service and without a GPU.

The design goal is therefore:

- process **50M+ lines per day** on commodity CPU hardware;
- keep memory bounded regardless of input volume;
- surface errors, new patterns and anomalies;
- explain individual findings in natural language when a local model is
  available, and still be useful when it is not.

## Why not feed lines to an LLM

A local CPU model cannot process a line stream. A 3B quantised model answers one
request in hundreds of milliseconds; 50M requests per day is impossible. The
pipeline therefore reduces the stream first and sends only unique, problematic
signatures (typically tens) to the model.

## Pipeline

```text
files/journald
   │  streaming, byte offsets
   ▼
parser ──► normalizer ──► signature ──► clusterer + daily counters
                                              │
                                              ▼
                                        SQLite snapshot
                                              │
                         ┌────────────────────┴────────────────────┐
                         ▼                                         ▼
                  reactive detection                      proactive trends
              (errors, new, noise)                    (spikes, drops, new)
                         └────────────────────┬────────────────────┘
                                              ▼
                                     explain (LLM | heuristic)
```

### 1. Ingest

- Files are opened in **binary mode** and read line by line.
- A byte offset is stored per file. The reader never decodes a partial line:
  only complete lines advance the offset, so an in-progress write is picked up on
  the next pass without double counting.
- Rotation is detected by **inode change** or a decrease in file size. In both
  cases the file is re-read from the beginning.
- Entries are streamed into the clusterer; no list of all entries is created.

### 2. Normalisation and signatures

Variables are replaced by typed tokens:

| Pattern | Token |
| --- | --- |
| UUID | `UUID` |
| long hex / hash | `HEX` / `HASH` |
| IPv4 | `IP` |
| timestamp | `TS` |
| number | `N` |
| placeholder `{...}` | `PARAM` |

The signature is the normalised message plus the optional `class` namespace and
a few stable context keys (`method`, `statusCode`, `try`). This collapses
millions of lines into a small set of templates.

### 3. Clustering

Each signature keeps:

- total count;
- per-level counts (for error detection);
- per-class counts;
- first/last timestamps;
- one sample message/raw line/context for explanations.

Clusters are merged across worker processes by summing counters and combining
first/last timestamps.

### 4. Daily history

Every entry contributes to a `(signature, date, count)` counter. Full ingests
replace the counters for the dates they read; incremental ingests add to them.
This table drives trend detection and is intentionally small.

### 5. Reactive detection

- **Error clusters**: any signature with ERROR/CRITICAL/ALERT/EMERGENCY lines.
- **New patterns**: signatures not present in the historical `clusters` table.
- **Noise**: high-volume patterns (candidates for log-level reduction).

### 6. Proactive trends

- **Spike**: the latest day is greater than the median of previous days by a
  configurable factor (default 3x).
- **Drop**: a previously frequent pattern has almost stopped appearing.
- **New**: a signature first seen inside the observation window.

These are rule-based and require no model.

### 7. Explanation

Two interchangeable backends:

- **Ollama** (`/api/generate`) with a prompt that requests severity, what is
  happening, likely cause and what to check.
- **Heuristic rules** — regex-based categories (storage/HTTP, database, SMTP,
  consumers, timeouts, missing workers, memory, uncaught exceptions) with
  actionable checks.

The heuristic backend is the default fallback. Explanations are cached in SQLite
per signature, so repeated runs for the same template are instant.

## Storage model

SQLite is used for all state:

| Table | Purpose |
| --- | --- |
| `offsets` | per-file byte offset, mtime, inode |
| `clusters` | lifetime signature history (first/last seen, total count) |
| `daily_counts` | per-day frequencies for trends |
| `snapshot` | clusters of the latest ingest for fast queries |
| `explanations` | cache for LLM/heuristic explanations |
| `meta` | snapshot timestamp and period |

WAL mode and `executemany` batching keep writes fast. The snapshot is replaced
on each ingest, while `clusters` and `daily_counts` accumulate history.

## Scaling considerations

- **Memory** is dominated by the number of unique signatures, not by line count.
  Signatures are bounded by the diversity of the application's log templates.
- **IO** is sequential. Binary reads avoid text-mode seek ambiguity.
- **CPU** parallelises across files with `--workers N`; each worker returns
  partial cluster and daily aggregates that are merged in the parent process.
- **JSON parsing** can use `orjson` if installed; otherwise the standard library
  is used.
- **Writes** are batched per ingest, not per line.
- **Benchmark** harness (`lmlog bench`) generates synthetic data and reports
  lines/second and an extrapolated time for 50M lines. This is a reference
  measurement, not a hard limit.

## Deliberate trade-offs

- Raw lines are not retained; only aggregates and one sample per signature. The
  original files remain the source of truth.
- A single SQLite database is assumed. This is appropriate for one host and one
  log set; distributed aggregation is out of scope.
- LLM explanations are per unique signature. This keeps CPU usage predictable.
- The parser targets Monolog; unstructured lines are still counted but have no
  context.

## Possible extensions

- A read-only web dashboard over the snapshot.
- Export to OpenTelemetry / Prometheus.
- Signature fingerprints shipped across installations to recognise known issues.
- A pluggable LLM backend (llama.cpp server, OpenAI-compatible endpoints).
