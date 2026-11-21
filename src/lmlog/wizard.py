"""Interactive assistant (`lmlog` with no subcommand)."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .config import default_db, default_log_dir, default_model, llm_mode
from .detect import detect_trends
from .llm import create_client

_COLORS = {
    "reset": "\033[0m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
    "dim": "\033[2m",
}


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""


def c(text: str, color: str) -> str:
    if not _use_color():
        return text
    return f"{_COLORS.get(color, '')}{text}{_COLORS['reset']}"


def _ask(prompt: str, default: str = "") -> str:
    if not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default else ""
    try:
        return input(f"{prompt}{suffix}: ").strip() or default
    except (EOFError, KeyboardInterrupt):
        return ""


def _db_path(args) -> str:
    return getattr(args, "db", None) or default_db()


def _need_state(args):
    from .state import State

    return State(_db_path(args))


def diag(state, args) -> dict:
    from .daemon import daemon_status

    created, period = state.snapshot_info()
    daily = state.load_daily()
    days = len({d for by_date in daily.values() for d in by_date}) if daily else 0
    daemon = daemon_status(_db_path(args))
    client = create_client(model=getattr(args, "model", default_model()))
    info = {
        "snapshot_created": created,
        "snapshot_period": period,
        "days_history": days,
        "daemon_running": daemon["running"],
        "daemon_pid": daemon["pid"],
        "llm": {
            "mode": llm_mode(),
            "provider": client.label,
            "available": client.health(),
        },
        "cluster_count": 0,
        "model": getattr(args, "model", default_model()),
    }
    from .cli import load_clusters

    clusterer, _ = load_clusters(state)
    info["cluster_count"] = len(clusterer.clusters)
    return info


def print_diag(info: dict) -> None:
    print(c(f"lmlog - log intelligence (v{__version__})", "bold"))
    print("─" * 52)
    if info["daemon_running"]:
        print(
            c("[ok] background refresh: running", "green")
            + f" (PID {info['daemon_pid']}, snapshot updated every 5 min)"
        )
    else:
        print(c("[!] background refresh: not running - the snapshot may be stale", "yellow"))
    if info["snapshot_created"]:
        print(
            c("[ok] snapshot: ", "green")
            + f"{info['snapshot_created']} ({info['cluster_count']} signatures)"
        )
    else:
        print(c("[!] snapshot is empty - run ingest first", "yellow"))
    if info["days_history"]:
        print(c("[ok] trend history: ", "green") + f"{info['days_history']} day(s)")
    else:
        print(c("[!] no trend history yet - it accumulates after ingest", "yellow"))
    llm = info["llm"]
    if llm["available"]:
        print(
            c(f"[ok] LLM ({llm['provider']}, {llm['mode']}): available", "green")
            + f", model {info['model']}"
        )
    else:
        print(
            c(
                f"[!] LLM ({llm['provider']}, {llm['mode']}): unavailable"
                " - heuristic explanations will be used",
                "yellow",
            )
        )
    print()


def ensure_ingested(state, args) -> bool:
    from .cli import load_clusters, run_ingest

    clusterer, _ = load_clusters(state)
    if clusterer.clusters:
        return True
    print(
        c("The snapshot is empty. Reading logs is required (first run may take a while).", "yellow")
    )
    answer = _ask("Read logs now?", "y")
    if answer.lower() in ("y", "yes", "д", "да"):
        result = run_ingest(state, args.log_dir, 24, None, incremental=False)
        if result["status"] != "ok":
            print(c("Ingest did not complete; check the errors above.", "yellow"))
            return False
        print(c("Done. Data loaded.", "green"))
        return True
    print("Cancelled. Run `lmlog ingest` first.")
    return False


def cmd_status(args) -> int:
    from .cli import detect, fmt_count, load_clusters

    state = _need_state(args)
    info = diag(state, args)
    print_diag(info)
    clusterer, _created = load_clusters(state)
    if not clusterer.clusters:
        print("No data.")
        return 0
    findings = detect(list(clusterer.clusters.values()), state.known_signatures())
    errors = [f for f in findings if f.kind == "error"]
    print(c("Current situation:", "bold"))
    print(
        f"  Lines in snapshot: {fmt_count(sum(c.count for c in clusterer.clusters.values()))}, "
        f"signatures: {fmt_count(len(clusterer.clusters))}, errors: {sum(f.errors for f in errors)}"
    )
    if errors:
        print(c("  Top errors:", "bold"))
        for finding in errors[:5]:
            color = {"high": "red", "medium": "yellow", "low": "cyan"}.get(finding.severity, "dim")
            print(
                f"    {c(f'[{finding.severity.upper()}]', color)} x{finding.count}  {finding.sample[:100]}"
            )
    else:
        print(c("  No explicit errors detected.", "green"))
    return 0


def cmd_summary(args) -> int:
    from .cli import detect, fmt_count, load_clusters

    state = _need_state(args)
    if not ensure_ingested(state, args):
        return 1

    clusterer, created = load_clusters(state)
    findings = detect(list(clusterer.clusters.values()), state.known_signatures())
    daily = state.load_daily()

    print()
    print(c("Current situation", "bold"))
    print("─" * 52)
    print(
        f"Lines in snapshot: {fmt_count(sum(c.count for c in clusterer.clusters.values()))}, "
        f"signatures: {fmt_count(len(clusterer.clusters))}"
    )
    print(f"Snapshot: {created or 'none'}")

    errors = [f for f in findings if f.kind == "error"]
    print()
    print(c(f"Errors ({len(errors)}):", "bold"))
    if errors:
        for finding in errors[:5]:
            color = {"high": "red", "medium": "yellow", "low": "cyan"}.get(finding.severity, "dim")
            print(
                f"  {c(f'[{finding.severity.upper()}]', color)} x{finding.count}  {finding.sample[:95]}"
            )
    else:
        print(c("  No explicit errors detected.", "green"))

    if daily:
        trends = detect_trends(list(clusterer.clusters.values()), daily, top_n=5)
        print()
        if trends:
            print(c(f"Anomalies / trends ({len(trends)}):", "bold"))
            for trend in trends[:5]:
                print(f"  [{trend.severity.upper()}] {trend.sample[:80]}")
                print(f"      {trend.note[:140]}")
        else:
            print(c("No anomalies or trends detected.", "green"))

    new = [f for f in findings if f.kind in ("new", "new_trend")]
    if new:
        print()
        print(c(f"New patterns ({len(new)}):", "bold"))
        for finding in new[:3]:
            print(f"  {finding.sample[:90]}")

    frequent = [f for f in findings if f.kind == "frequent"]
    if frequent:
        print()
        print(c(f"Main noise ({len(frequent)}):", "bold"))
        for finding in frequent[:3]:
            print(f"  x{finding.count}  {finding.sample[:90]}")

    print()
    answer = _ask("Explain the key findings with AI (top 3)?", "n")
    if answer.lower() in ("y", "yes", "д", "да"):
        _run_explain(state, args, [f for f in findings if f.kind == "error"][:3])
    return 0


def _run_explain(state, args, findings) -> None:
    from .explain import choose_explanation
    from .llm import create_client

    client = create_client(
        model=getattr(args, "model", default_model()), timeout=getattr(args, "timeout", 120)
    )
    use_llm = client.health()
    if not use_llm:
        print(c(f"{client.label} is unavailable - using heuristic explanations.", "yellow"))
    print()
    for index, finding in enumerate(findings, 1):
        print(c(f"[{index}/{len(findings)}] explanation:", "bold"))
        cached = state.get_explanation(finding.signature)
        if cached:
            print(c("  [cache]", "dim"))
            print(cached)
        else:
            text, source = choose_explanation(finding, client, use_llm, "current situation")
            state.set_explanation(finding.signature, text)
            print(c(f"  [{source}]", "dim"))
            print(text)
        print()


def cmd_wizard(args) -> int:
    state = _need_state(args)
    info = diag(state, args)
    print_diag(info)

    if not sys.stdin.isatty():
        return cmd_summary(args)

    if not info["daemon_running"]:
        answer = _ask("Start background refresh every 5 minutes?", "y")
        if answer.lower() in ("y", "yes", "д", "да"):
            from .daemon import start_daemon

            start_daemon(_db_path(args), args.log_dir, 300)
            info["daemon_running"] = True

    if not ensure_ingested(state, args):
        return 1

    while True:
        print()
        print(c("What would you like to do?", "bold"))
        print("  1) Current situation")
        print("  2) Run a command")
        print("  3) Help")
        print("  4) Exit")
        answer = _ask("Choose an item", "1")
        if answer in ("1", "status"):
            cmd_summary(args)
        elif answer in ("2", "command"):
            _submenu(state, args)
        elif answer in ("3", "help"):
            _help()
        elif answer in ("4", "exit", "q"):
            print("Goodbye!")
            return 0
        else:
            print(c("Unknown choice. Enter 1-4.", "yellow"))


def _submenu(state, args) -> None:
    from .cli import cmd_classes, cmd_errors, cmd_explain, cmd_ingest, cmd_journal, cmd_trends

    while True:
        print()
        print(c("Commands:", "bold"))
        print("  1) Errors")
        print("  2) Explain errors with AI")
        print("  3) Trends / anomalies")
        print("  4) Search by class (e.g. Consumer)")
        print("  5) System journal")
        print("  6) Refresh data (re-read logs)")
        print("  7) Background refresh")
        print("  8) Back")
        answer = _ask("Choose an item", "8")
        if answer in ("1", "errors"):
            cmd_errors(_mkargs(args, since=24))
        elif answer in ("2", "explain"):
            cmd_explain(_mkargs(args, since=24, n=5))
        elif answer in ("3", "trends"):
            cmd_trends(_mkargs(args, n=10))
        elif answer in ("4", "class"):
            name = _ask("Part of the class name (e.g. Consumer)")
            if name:
                cmd_classes(_mkargs(args, name=name))
        elif answer in ("5", "journal"):
            cmd_journal(_mkargs(args, since="-6h"))
        elif answer in ("6", "refresh"):
            cmd_ingest(_mkargs(args, since=24))
        elif answer in ("7", "daemon"):
            _daemon_menu(args)
        else:
            return


def _daemon_menu(args) -> None:
    from .daemon import daemon_status, start_daemon, stop_daemon

    db = _db_path(args)
    status = daemon_status(db)
    print()
    print(c("Background refresh:", "bold"))
    state_text = f"running (PID {status['pid']})" if status["running"] else "not running"
    print(f"  Status: {state_text}")
    answer = _ask("1) Start   2) Stop   3) Back", "3")
    if answer == "1":
        start_daemon(db, args.log_dir, 300)
    elif answer == "2":
        stop_daemon(db)


def _help() -> None:
    print()
    print(c("lmlog - log intelligence for boxed deployments", "bold"))
    print(
        """
What it is:
  lmlog finds real errors, anomalies and noise in very large volumes of
  structured logs and explains them in plain language (optionally via a
  local LLM).

Reading the output:
  [severity]   - HIGH (red), MEDIUM (yellow), LOW, INFO.
  kind=error   - errors (exceptions, failed requests, etc.)
  kind=trend   - anomaly from day-by-day history (spike or drop)
  kind=new     - a pattern that appeared recently
  kind=frequent- a high-volume pattern (often log noise)

Commands:
  lmlog                  - this menu
  lmlog generate         - create synthetic logs for a demo
  lmlog ingest           - read logs and save a snapshot
  lmlog status           - short current status
  lmlog summary          - full current status
  lmlog errors           - error clusters
  lmlog explain          - explain findings (local LLM or heuristic)
  lmlog trends           - anomalies from history
  lmlog classes <name>   - search by class/namespace
  lmlog journal          - system journal (optional)
  lmlog daemon           - background incremental ingest
  lmlog bench            - measure ingest throughput

Tips:
  * Run `lmlog ingest` once; every query after that is instant.
  * `lmlog daemon` keeps the snapshot fresh every 5 minutes.
  * AI explanations use gogate (default) or direct Ollama in standalone mode;
    without either, heuristic explanations are used.
  * State is stored in $LMLOG_DB (default: ~/.lmlog/lmlog.db).
"""
    )


def _mkargs(args, **overrides):
    base = {
        "log_dir": getattr(args, "log_dir", default_log_dir()),
        "db": _db_path(args),
        "since": getattr(args, "since", None),
        "limit": getattr(args, "limit", None),
        "incremental": False,
        "live": False,
        "workers": getattr(args, "workers", 0),
        "n": 10,
        "detail": False,
        "name": "",
        "model": getattr(args, "model", default_model()),
        "timeout": getattr(args, "timeout", 120),
        "klass": None,
        "spike": 3.0,
        "explain": False,
        "no_llm": False,
        "refresh": False,
        "show": False,
        "clusters": 15,
        "level": None,
        "sudo": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)
