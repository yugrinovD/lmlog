"""Command line interface for lmlog."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .cluster import Clusterer
from .config import default_db, default_log_dir, default_model
from .detect import detect
from .ingest import (
    list_log_files,
    merge_result,
    plan_tasks,
    process_file_into_safe,
    read_journal,
    run_tasks,
)
from .state import State


def fmt_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def parse_size(value: str) -> int:
    """Parse ``1024``, ``256K``, ``10M`` or ``1G`` into bytes."""
    text = str(value).strip().upper()
    if not text:
        return 0
    units = {"K": 1024, "M": 1024**2, "G": 1024**3}
    if text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


def run_ingest(
    state: State,
    log_dir: str,
    since_hours: int | None,
    limit_files: int | None,
    incremental: bool = False,
    workers: int = 0,
) -> dict:
    """Read logs, cluster signatures and persist a snapshot.

    With ``incremental=True`` only new bytes are read and the existing
    snapshot is extended. If a rotated/truncated file is detected, the selected
    file set is rebuilt from scratch so counts stay correct.
    """
    all_files = list_log_files(log_dir)
    files = all_files
    # In incremental mode offsets decide what is new; filtering by mtime could
    # skip files that still contain unread data after an outage.
    if since_hours and not incremental:
        cutoff = time.time() - since_hours * 3600
        files = [path for path in files if os.path.getmtime(path) >= cutoff]
    if limit_files:
        files = files[-limit_files:]

    if os.path.isdir(log_dir):
        state.prune_offsets(log_dir, all_files)

    if not files:
        print(f"No log files found in {log_dir}; nothing to ingest.")
        snapshot = state.load_snapshot() if incremental else Clusterer()
        return {
            "elapsed": 0.0,
            "files": 0,
            "total_entries": 0,
            "total_lines": 0,
            "unique_sigs": len(snapshot.clusters),
            "findings": [],
            "clusterer": snapshot,
            "rate": 0.0,
        }

    tasks, rotated = plan_tasks(state, files, full=not incremental)
    rebuild = bool(incremental and rotated)
    if rebuild:
        print("Rotation detected; rebuilding the snapshot from the selected files.")
        tasks = plan_tasks(state, files, full=True)[0]
    effective_incremental = incremental and not rebuild

    known = state.known_signatures()
    first_run = not known
    started = time.time()
    delta = Clusterer()
    daily: dict = {}
    total_entries = 0
    total_lines = 0
    offset_rows = []

    if workers and workers > 1 and len(tasks) > 1:
        results = run_tasks(tasks, workers)
        for (source, cursor), result in results:
            merge_result(delta, daily, result)
            total_entries += result["entries"]
            total_lines += result["lines"]
            offset_rows.append((source, *cursor))
    else:
        for task in tasks:
            outcome = process_file_into_safe(task, delta, daily)
            if outcome is None:
                continue
            (source, cursor), lines, entries = outcome
            total_lines += lines
            total_entries += entries
            offset_rows.append((source, *cursor))

    if not offset_rows:
        print("No files could be read; the snapshot was left unchanged.")
        snapshot = state.load_snapshot() if effective_incremental else Clusterer()
        return {
            "elapsed": time.time() - started,
            "files": len(files),
            "total_entries": 0,
            "total_lines": 0,
            "unique_sigs": len(snapshot.clusters),
            "findings": [],
            "clusterer": snapshot,
            "rate": 0.0,
        }

    state.set_offsets(offset_rows)
    if effective_incremental:
        snapshot = state.load_snapshot()
        snapshot.merge(delta)
        record_mode = "incremental"
        replace_dates: set[str] = set()
    else:
        snapshot = delta
        record_mode = "full"
        replace_dates = {d for by_date in daily.values() for d in by_date}
    if daily:
        state.upsert_daily(daily, replace_dates)

    findings = detect(list(snapshot.clusters.values()), known)
    if first_run:
        findings = [finding for finding in findings if finding.kind != "new"]
    state.record_clusters(delta.clusters.values(), mode=record_mode)
    state.save_snapshot(snapshot.clusters.values(), since_hours)

    elapsed = time.time() - started
    return {
        "elapsed": elapsed,
        "files": len(files),
        "total_entries": total_entries,
        "total_lines": total_lines,
        "unique_sigs": len(snapshot.clusters),
        "findings": findings,
        "clusterer": snapshot,
        "rate": total_lines / elapsed if elapsed else 0.0,
    }


def load_clusters(state: State) -> tuple[Clusterer, str | None]:
    created, _ = state.snapshot_info()
    return state.load_snapshot(), created


def resolve_clusterer(args, state: State) -> tuple[Clusterer, dict, str | None]:
    """Load clusters from the snapshot (fast) or read files now (``--live``)."""
    live = getattr(args, "live", False) or getattr(args, "incremental", False)
    if live:
        result = run_ingest(
            state,
            args.log_dir,
            args.since,
            args.limit,
            incremental=getattr(args, "incremental", False),
            workers=getattr(args, "workers", 0),
        )
        return result["clusterer"], result, None
    clusterer, created = load_clusters(state)
    if not clusterer.clusters:
        print("Snapshot is empty. Run `lmlog ingest` first (or pass --live).")
        return clusterer, {"findings": [], "clusterer": clusterer}, created
    findings = detect(list(clusterer.clusters.values()), state.known_signatures())
    return clusterer, {"findings": findings, "clusterer": clusterer}, created


def print_findings(findings, with_detail: bool = False) -> None:
    if not findings:
        print("No findings.")
        return
    for finding in findings:
        print("=" * 70)
        print(
            f"[{finding.severity.upper()}] kind={finding.kind}  "
            f"count={fmt_count(finding.count)}  errors={finding.errors}"
        )
        if finding.classes:
            print("  classes: " + ", ".join(finding.classes))
        print("  msg: " + (finding.sample[:160] if finding.sample else ""))
        if finding.note:
            print("  note: " + finding.note)
        if with_detail and finding.detail:
            print("  detail: " + finding.detail[:300].replace("\n", " "))


# --- commands ---


def cmd_generate(args) -> int:
    from .generator import generate_to_dir, stream_logs

    if args.rate:
        result = stream_logs(
            args.out,
            rate=args.rate,
            duration=args.duration,
            profile=args.profile,
            seed=args.seed,
            rotate_bytes=parse_size(args.rotate_size)
            if isinstance(args.rotate_size, str)
            else args.rotate_size,
        )
        print(
            f"Streamed {fmt_count(result['lines'])} lines to {result['file']} "
            f"at ~{args.rate:g}/s ({result['profile']})."
        )
        return 0

    result = generate_to_dir(
        args.out,
        lines=args.lines,
        days=args.days,
        profile=args.profile,
        seed=args.seed,
        rotate_bytes=parse_size(args.rotate_size)
        if isinstance(args.rotate_size, str)
        else args.rotate_size,
        append=args.append,
        anomaly=not args.no_anomaly,
    )
    print(
        f"Generated {fmt_count(result['lines'])} lines over {result['days']} day(s) "
        f"into {args.out} (profile={result['profile']}, seed={result['seed']})."
    )
    for path in result["files"]:
        print(f"  {path}")
    return 0


def cmd_ingest(args) -> int:
    state = State(args.db)
    result = run_ingest(
        state,
        args.log_dir,
        args.since,
        args.limit,
        incremental=args.incremental,
        workers=args.workers,
    )
    print(
        f"Ingest finished: files={result['files']}, lines={fmt_count(result['total_lines'])}, "
        f"entries={fmt_count(result['total_entries'])}, signatures={result['unique_sigs']}, "
        f"time={result['elapsed']:.1f}s ({fmt_count(int(result['rate']))} lines/s)."
    )
    print("Snapshot saved. `scan`, `errors`, `classes` and `explain` now read from it instantly.")
    return 0


def cmd_scan(args) -> int:
    state = State(args.db)
    clusterer, stats, created = resolve_clusterer(args, state)
    suffix = f" (snapshot from {created})" if created else ""
    print(f"Unique signatures: {fmt_count(len(clusterer.clusters))}{suffix}")
    findings = stats["findings"]
    print(f"\n--- Findings ({len(findings)}) ---")
    print_findings(findings, with_detail=args.detail)
    return 0


def cmd_errors(args) -> int:
    state = State(args.db)
    clusterer, _, created = resolve_clusterer(args, state)
    if created:
        print(f"(snapshot from {created})")
    errors = [c for c in clusterer.clusters.values() if c.errors > 0]
    errors.sort(key=lambda c: c.errors, reverse=True)
    print(f"ERROR clusters: {len(errors)}\n")
    for cluster in errors[: args.n]:
        print("=" * 70)
        print(
            f"errors={cluster.errors}  total={fmt_count(cluster.count)}  "
            f"first={cluster.first_ts}  last={cluster.last_ts}"
        )
        if cluster.classes:
            print(
                "  classes: "
                + ", ".join(sorted(cluster.classes, key=cluster.classes.get, reverse=True)[:5])
            )
        print("  msg: " + (cluster.sample_message[:180] if cluster.sample_message else ""))
    return 0


def cmd_classes(args) -> int:
    state = State(args.db)
    clusterer, _, created = resolve_clusterer(args, state)
    if created:
        print(f"(snapshot from {created})")
    needle = args.name.lower()
    matches = []
    for cluster in clusterer.clusters.values():
        hits = [key for key in cluster.classes if needle in key.lower()]
        if hits:
            matches.append((cluster, hits))
    matches.sort(key=lambda item: item[0].count, reverse=True)
    print(f"Matching clusters: {len(matches)}\n")
    for cluster, _hits in matches[: args.n]:
        print("=" * 70)
        print(
            f"count={fmt_count(cluster.count)}  errors={cluster.errors}  "
            f"classes={', '.join(sorted(cluster.classes, key=cluster.classes.get, reverse=True)[:5])}"
        )
        print("  msg: " + (cluster.sample_message[:180] if cluster.sample_message else ""))
    return 0


def cmd_explain(args) -> int:
    from .explain import choose_explanation
    from .llm import OllamaClient

    state = State(args.db)
    client = OllamaClient(model=args.model, timeout=args.timeout)
    use_llm = not args.no_llm
    if use_llm and not client.health():
        print("Ollama is not available; using heuristic explanations.")
        use_llm = False

    clusterer, stats, created = resolve_clusterer(args, state)
    findings = stats["findings"]
    if getattr(args, "klass", None):
        findings = [f for f in findings if args.klass.lower() in " ".join(f.classes).lower()]
    if not findings:
        print("Nothing to explain.")
        return 0

    period = f"last {args.since}h" if args.since else "the whole period"
    if created:
        print(f"(snapshot from {created})")
    top = findings[: args.n]
    print(
        f"Findings: {len(findings)}; explaining top {len(top)} ({'LLM' if use_llm else 'heuristic'}).\n"
    )
    for index, finding in enumerate(top, 1):
        print("=" * 70)
        print(
            f"[{index}/{len(top)}] [{finding.severity.upper()}] kind={finding.kind}  "
            f"count={fmt_count(finding.count)}  errors={finding.errors}"
        )
        if finding.classes:
            print("  classes: " + ", ".join(finding.classes))
        print("  msg: " + (finding.sample[:160] if finding.sample else ""))
        print("  note: " + finding.note)

        cached = None if args.refresh else state.get_explanation(finding.signature)
        if cached:
            print("\n  [cache]")
            print(cached)
            continue
        text, source = choose_explanation(finding, client, use_llm, period)
        state.set_explanation(finding.signature, text)
        print(f"\n  [{source}]")
        print(text)
        print()
    return 0


def cmd_trends(args) -> int:
    from .detect import detect_trends
    from .explain import choose_explanation
    from .llm import OllamaClient

    state = State(args.db)
    if args.live or args.incremental:
        result = run_ingest(
            state,
            args.log_dir,
            args.since,
            args.limit,
            incremental=args.incremental,
            workers=args.workers,
        )
        clusterer = result["clusterer"]
        created = None
    else:
        clusterer, created = load_clusters(state)
    daily = state.load_daily()
    if not daily:
        print("No day-by-day history yet. Run ingest over several days first:")
        print("  lmlog ingest --since 72   (3 days)")
        return 1
    if created:
        print(f"(snapshot from {created})")

    findings = detect_trends(
        list(clusterer.clusters.values()),
        daily,
        spike_factor=args.spike,
        top_n=args.n,
    )
    if not findings:
        print("No anomalies or trends detected.")
        return 0

    client = OllamaClient(model=args.model, timeout=args.timeout)
    use_llm = args.explain and not args.no_llm
    if use_llm and not client.health():
        print("Ollama is not available; using heuristic explanations.")
        use_llm = False

    print(f"Trends: {len(findings)}\n")
    for index, finding in enumerate(findings, 1):
        print("=" * 70)
        print(
            f"[{index}/{len(findings)}] [{finding.severity.upper()}] "
            f"kind={finding.kind}  count={fmt_count(finding.count)}"
        )
        if finding.classes:
            print("  classes: " + ", ".join(finding.classes))
        print("  msg: " + (finding.sample[:160] if finding.sample else ""))
        print("  note: " + finding.note)
        if args.explain:
            key = finding.signature + "|trend"
            cached = None if args.refresh else state.get_explanation(key)
            if cached:
                print("\n  [cache]\n" + cached)
            else:
                text, source = choose_explanation(
                    finding, client, use_llm, "the observation window"
                )
                state.set_explanation(key, text)
                print(f"\n  [{source}]\n" + text)
            print()
    return 0


def cmd_journal(args) -> int:
    from .ingest import journal_available
    from .normalize import signature

    if not journal_available():
        print("journalctl is not available in this environment.")
        return 1

    clusterer = Clusterer()
    count = 0
    errors = 0
    for entry in read_journal(since=args.since, use_sudo=args.sudo, timeout=args.timeout):
        if args.level and entry.level != args.level:
            continue
        count += 1
        if entry.is_error:
            errors += 1
        entry.signature = signature(entry.message, klass=entry.channel, level=entry.level)
        clusterer.add(entry)
        if args.show:
            print(f"{entry.ts} {entry.channel} {entry.level}: {entry.message[:180]}")

    if args.show:
        print()
    print(f"journald entries: {fmt_count(count)}, errors: {fmt_count(errors)}")
    if args.clusters:
        print(f"\n--- Top journald patterns ({args.clusters}) ---")
        for cluster in clusterer.top(lambda c: c.count, args.clusters):
            print(f"{fmt_count(cluster.count):>12}  {cluster.sample_message[:130]}")
    return 0


def cmd_daemon(args) -> int:
    from .daemon import daemon_status, run_foreground, start_daemon, stop_daemon

    db = args.db or default_db()
    if args.foreground:
        return run_foreground(
            db,
            args.log_dir,
            args.interval,
            since_hours=args.since,
            workers=args.workers,
        )
    if args.status:
        status = daemon_status(db)
        if status["running"]:
            print(f"Daemon is running (PID {status['pid']}).")
            if status["last_update"]:
                import datetime

                last = datetime.datetime.fromtimestamp(status["last_update"])
                print(f"Last update: {last.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("Daemon is not running.")
        return 0
    if args.stop:
        return stop_daemon(db)
    return start_daemon(
        db,
        args.log_dir,
        args.interval,
        since_hours=args.since,
        workers=args.workers,
    )


def cmd_bench(args) -> int:
    """Generate synthetic logs and measure ingest throughput."""
    import tempfile

    from .generator import generate_to_dir

    with tempfile.TemporaryDirectory(prefix="lmlog-bench-") as tmp:
        logs_dir = os.path.join(tmp, "logs")
        db_path = os.path.join(tmp, "bench.db")

        started = time.perf_counter()
        generated = generate_to_dir(
            logs_dir,
            lines=args.lines,
            days=args.days,
            profile=args.profile,
            seed=args.seed,
            rotate_bytes=parse_size(args.rotate_size)
            if isinstance(args.rotate_size, str)
            else args.rotate_size,
        )
        generate_elapsed = time.perf_counter() - started

        state = State(db_path)
        started = time.perf_counter()
        result = run_ingest(
            state,
            logs_dir,
            since_hours=None,
            limit_files=None,
            incremental=False,
            workers=args.workers,
        )
        elapsed = time.perf_counter() - started

        lines = result["total_lines"]
        rate = lines / elapsed if elapsed else 0.0
        reference = 50_000_000
        reference_minutes = (reference / rate / 60.0) if rate else float("inf")
        print("Benchmark")
        print("─" * 44)
        print(f"Lines generated : {fmt_count(generated['lines'])} ({generate_elapsed:.1f}s)")
        print(f"Ingest time     : {elapsed:.2f}s")
        print(f"Throughput      : {fmt_count(int(rate))} lines/s")
        print(f"Signatures      : {fmt_count(result['unique_sigs'])}")
        print(f"50M lines at this rate: ~{reference_minutes:.1f} min")
        print()
        print("Note: this is a single-machine throughput measurement, not a hard capacity limit.")
    return 0


# --- argparse ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lmlog",
        description="Local, CPU-first log intelligence: signatures, errors, trends and explanations.",
    )
    parser.add_argument("--version", action="version", version=f"lmlog {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-dir", default=default_log_dir(), help="directory with .log files")
    common.add_argument(
        "--db", default=None, help="SQLite state path (default: $LMLOG_DB or ~/.lmlog/lmlog.db)"
    )
    common.add_argument(
        "--since", type=int, default=None, help="only files modified in the last N hours"
    )
    common.add_argument("--limit", type=int, default=None, help="read only the last N files")
    common.add_argument(
        "--live", action="store_true", help="read log files now instead of using the snapshot"
    )
    common.add_argument(
        "--incremental", action="store_true", help="read only data added since the last ingest"
    )

    gen = sub.add_parser("generate", help="generate synthetic logs for demos and benchmarks")
    gen.add_argument("--out", default=default_log_dir(), help="output directory")
    gen.add_argument("--lines", type=int, default=100_000, help="number of lines (batch mode)")
    gen.add_argument("--days", type=int, default=1, help="spread lines over N days")
    gen.add_argument("--profile", choices=["demo", "mixed", "stress", "errors"], default="demo")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--rotate-size", default="0", help="rotate files after size, e.g. 256M")
    gen.add_argument("--append", action="store_true", help="append to existing files")
    gen.add_argument(
        "--no-anomaly",
        action="store_true",
        help="do not inject the demo spike/drop/new pattern on the last day",
    )
    gen.add_argument("--rate", type=float, default=0, help="stream mode: lines per second")
    gen.add_argument(
        "--duration", type=float, default=None, help="stream mode: stop after N seconds"
    )
    gen.set_defaults(func=cmd_generate)

    ingest = sub.add_parser(
        "ingest", parents=[common], help="read logs and save a cluster snapshot"
    )
    ingest.add_argument(
        "--workers", type=int, default=0, help="parse files in N parallel processes"
    )
    ingest.set_defaults(func=cmd_ingest)

    scan = sub.add_parser(
        "scan", parents=[common], help="show findings (errors, new and frequent patterns)"
    )
    scan.add_argument("--detail", action="store_true", help="show raw detail for findings")
    scan.add_argument("--workers", type=int, default=0)
    scan.set_defaults(func=cmd_scan)

    errors = sub.add_parser("errors", parents=[common], help="show ERROR clusters")
    errors.add_argument("-n", type=int, default=30)
    errors.add_argument("--workers", type=int, default=0)
    errors.set_defaults(func=cmd_errors)

    classes = sub.add_parser("classes", parents=[common], help="search clusters by class/namespace")
    classes.add_argument("name")
    classes.add_argument("-n", type=int, default=20)
    classes.add_argument("--workers", type=int, default=0)
    classes.set_defaults(func=cmd_classes)

    explain = sub.add_parser(
        "explain", parents=[common], help="explain findings (local LLM or heuristic)"
    )
    explain.add_argument("-n", type=int, default=5, help="explain the top N findings")
    explain.add_argument("--model", default=default_model())
    explain.add_argument("--timeout", type=int, default=120)
    explain.add_argument(
        "--klass", default=None, help="only findings matching this class substring"
    )
    explain.add_argument("--no-llm", action="store_true", help="use heuristic explanations only")
    explain.add_argument("--refresh", action="store_true", help="ignore the explanation cache")
    explain.add_argument("--workers", type=int, default=0)
    explain.set_defaults(func=cmd_explain)

    trends = sub.add_parser(
        "trends", parents=[common], help="proactive trends (spikes, drops, new patterns)"
    )
    trends.add_argument("-n", type=int, default=15)
    trends.add_argument(
        "--spike", type=float, default=3.0, help="anomaly threshold versus the median"
    )
    trends.add_argument(
        "--explain", action="store_true", help="explain trends with the LLM (or heuristic)"
    )
    trends.add_argument("--model", default=default_model())
    trends.add_argument("--timeout", type=int, default=120)
    trends.add_argument("--no-llm", action="store_true")
    trends.add_argument("--refresh", action="store_true")
    trends.add_argument("--workers", type=int, default=0)
    trends.set_defaults(func=cmd_trends)

    journal = sub.add_parser("journal", help="read the systemd journal (optional)")
    journal.add_argument("--since", default="-24h")
    journal.add_argument("--level", default=None, choices=["INFO", "ERROR", "WARNING"])
    journal.add_argument("--show", action="store_true")
    journal.add_argument("--sudo", action="store_true", help="call journalctl through sudo -n")
    journal.add_argument(
        "--timeout", type=float, default=30, help="kill journalctl after N seconds"
    )
    journal.add_argument("--clusters", type=int, default=15)
    journal.set_defaults(func=cmd_journal)

    from .wizard import cmd_status, cmd_summary, cmd_wizard

    wizard = sub.add_parser("wizard", parents=[common], help="interactive assistant (default)")
    wizard.add_argument("--model", default=default_model())
    wizard.add_argument("--timeout", type=int, default=120)
    wizard.add_argument("--workers", type=int, default=0)
    wizard.set_defaults(func=cmd_wizard)

    status = sub.add_parser("status", parents=[common], help="short current status")
    status.add_argument("--workers", type=int, default=0)
    status.set_defaults(func=cmd_status)

    summary = sub.add_parser("summary", parents=[common], help="full current status")
    summary.add_argument("--model", default=default_model())
    summary.add_argument("--timeout", type=int, default=120)
    summary.add_argument("--workers", type=int, default=0)
    summary.set_defaults(func=cmd_summary)

    daemon = sub.add_parser("daemon", parents=[common], help="background incremental ingestion")
    daemon.add_argument("--interval", type=int, default=300, help="seconds between ingests")
    daemon.add_argument("--workers", type=int, default=0)
    daemon.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    daemon.add_argument("--status", action="store_true")
    daemon.add_argument("--stop", action="store_true")
    daemon.set_defaults(func=cmd_daemon)

    bench = sub.add_parser("bench", help="measure ingest throughput on synthetic logs")
    bench.add_argument("--lines", type=int, default=1_000_000)
    bench.add_argument("--days", type=int, default=1)
    bench.add_argument("--profile", choices=["demo", "mixed", "stress", "errors"], default="stress")
    bench.add_argument("--seed", type=int, default=42)
    bench.add_argument("--rotate-size", default="0")
    bench.add_argument("--workers", type=int, default=0)
    bench.set_defaults(func=cmd_bench)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        from .wizard import cmd_wizard

        args = argparse.Namespace(
            log_dir=default_log_dir(),
            db=None,
            since=None,
            limit=None,
            incremental=False,
            live=False,
            model=default_model(),
            timeout=120,
            workers=0,
            cmd="wizard",
        )
        if not args.db:
            args.db = default_db()
        return cmd_wizard(args)
    if hasattr(args, "db") and not args.db:
        args.db = default_db()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
