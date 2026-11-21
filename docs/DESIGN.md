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
- Rotation is detected by **inode change** or a decrease in file size. During an
  incremental pass this switches the run into **rebuild mode**: every file in
  `log_dir` is re-read from the beginning and the snapshot/daily/catalog are
  replaced instead of appended. A full ingest is always such a rebuild and
  ignores `--since`/`--limit`.
- Detection covers rename/create rotation and truncation that shrinks the file.
  **copytruncate** can be missed if the file is truncated and grows back to (or
  beyond) its previous size between checks; this is a documented limitation.
- A legacy 1.0.0 database sets `rebuild_required` and is rebuilt automatically
  on the first ingest (schema migration 1 → 2).
- Entries are streamed into the clusterer; no list of all entries is created.
- Physical lines and parsed entries are counted separately, so malformed lines
  are visible in statistics without being treated as events.

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
- first/last timestamps (stored for display, compared via canonical UTC keys);
- one sample message/raw line/context for explanations.

Clusters are merged across worker processes by summing counters and combining
first/last timestamps. Within a file, same-offset timestamps take a fast string
comparison path; mixed offsets are parsed and normalised to UTC.

### 4. Daily history

Every entry contributes to a `(signature, date, count)` counter. Full ingests
(including rotation-triggered rebuilds) replace the counters for the dates they
read; incremental ingests add only the delta. This table drives trend detection
and is intentionally small.

### 5. Reactive detection

- **Error clusters**: any signature with ERROR/CRITICAL/ALERT/EMERGENCY lines.
- **New patterns**: signatures not present in the historical `clusters` table.
- **Noise**: high-volume patterns (candidates for log-level reduction).

### 6. Proactive trends

- All comparisons are anchored to the **globally latest date** across all
  signatures; missing days count as zero.
- **Spike**: the latest day is greater than the median of active previous days
  in the observation window by a configurable factor (default 3x).
- **Drop**: a previously frequent pattern has stopped or sharply dropped. The
  baseline is the minimum active previous day, so a single spike followed by
  normal volume is not misread as a drop; a complete silence is detected.
- **New**: a signature first seen (on a single day) inside the observation window.

These are rule-based and require no model.

### 7. Explanation

Two interchangeable LLM transports and one deterministic backend:

- **gogate gateway** (default) — OpenAI-compatible `/v1/chat/completions`; the
  prompt requests severity, what is happening, likely cause and what to check.
  `lmlog` acts as a gateway consumer and does not own retries, limits, cache or
  metrics.
- **Direct Ollama** (`/api/generate`) — standalone development only
  (`LMLOG_LLM_MODE=direct`).
- **Heuristic rules** — regex-based categories (storage/HTTP, database, SMTP,
  consumers, timeouts, missing workers, memory, uncaught exceptions) with
  actionable checks.

The heuristic backend is the default fallback when the gateway/Ollama is
unavailable. Explanations are cached in SQLite per signature, so repeated runs
for the same template are instant.

## Storage model

SQLite is used for all state:

| Table | Purpose |
| --- | --- |
| `offsets` | per-file byte offset, mtime, inode |
| `clusters` | signature catalog (first/last seen in UTC, total count) |
| `daily_counts` | per-day frequencies for trends |
| `snapshot` | clusters of the latest ingest plus stored UTC sort keys |
| `explanations` | cache for LLM/heuristic explanations |
| `meta` | snapshot timestamp, period, `schema_version`, `rebuild_required` |

WAL mode and `executemany` batching keep writes fast. A full rebuild runs as a
single transaction in `replace_rebuild`: offsets, snapshot, catalog and daily
counters are cleared and re-inserted together, and `schema_version` is bumped to
2. Incremental ingests extend the snapshot and add a delta to `clusters` /
`daily_counts`. Snapshot rows store canonical UTC `first_key`/`last_key`, so
queries do not re-parse timestamps.

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
- Rotation during an incremental pass triggers a full rebuild of `log_dir`.
  This is more expensive than per-file contribution tracking, but it is simple
  and correct for rename/create rotation and shrinking files.
- copytruncate with a fast regrow to the previous size can be missed; the
  documentation states this explicitly rather than overclaiming.
- Compressed `*.gz` files are skipped (with a warning when the name matches the
  log patterns). Streaming compressed files would need a different offset model.

## Possible extensions

- A read-only web dashboard over the snapshot.
- Export to OpenTelemetry / Prometheus.
- Signature fingerprints shipped across installations to recognise known issues.
- Additional gateway providers (configured in gogate, not in lmlog).
