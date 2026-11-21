"""Synthetic log generator for demos, tests and benchmarks.

The generator produces realistic Symfony/Monolog lines with a controllable
mix of INFO noise, warnings and recurring errors. It is deterministic for a
given seed and can emit millions of lines without holding them in memory.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

TZ = dt.timezone.utc

PROFILES = ("demo", "mixed", "stress", "errors")


@dataclass
class Event:
    level: str
    message: str
    context: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


def _uid(rng: random.Random) -> dict:
    return {"uid": "".join(rng.choice("0123456789abcdef") for _ in range(32))}


def _noise(rng: random.Random) -> Event:
    return Event(
        "INFO",
        "Start the process of auto stopping sample jobs",
        {
            "class": "App\\UseCase\\Sample\\StopSample\\StopSampleHandler",
            "sampleJobId": rng.randint(1, 500),
        },
        _uid(rng),
    )


def _noise_done(rng: random.Random) -> Event:
    return Event(
        "INFO",
        "The process of auto stopping sample jobs completed",
        {
            "class": "App\\UseCase\\Sample\\StopSample\\StopSampleHandler",
            "sampleJobId": rng.randint(1, 500),
        },
        _uid(rng),
    )


def _user_joined(rng: random.Random) -> Event:
    return Event(
        "INFO",
        "User joined the sample session",
        {"class": "App\\Domain\\Sample\\SampleService", "userId": rng.randint(1, 100000)},
        _uid(rng),
    )


def _storage_404(rng: random.Random) -> Event:
    return Event(
        "ERROR",
        "Failed request to Storage {method} {url} {statusCode}",
        {
            "method": "GET",
            "url": "https://storage.example.internal/api/app.php/diskSpace",
            "statusCode": rng.choice([404, 404, 404, 500, 503]),
            "try": rng.randint(1, 3),
        },
        _uid(rng),
    )


def _disk_env(rng: random.Random) -> Event:
    return Event(
        "ERROR",
        'Can\'t delete files. Exception: Environment variable not found: "SAMPLE_RETENTION_MONTHS".',
        {},
        _uid(rng),
    )


def _disk_space(rng: random.Random) -> Event:
    return Event(
        "ERROR",
        "Storage Disk Space Error:Cannot complete Storage request in 3 attempts, see log for details",
        {},
        _uid(rng),
    )


def _db_error(rng: random.Random) -> Event:
    return Event(
        "ERROR",
        "Uncaught PHP Exception Doctrine\\DBAL\\Exception\\IntegrityConstraintViolationException: "
        '"An exception occurred while executing a query: SQLSTATE[23000]: Integrity constraint '
        'violation: 1062 Duplicate entry" at /srv/app/src/Infrastructure/Repository.php line 142',
        {
            "class": "App\\Infrastructure\\Repository\\SampleRepository",
            "exception": "IntegrityConstraintViolationException",
        },
        _uid(rng),
    )


def _consumer_error(rng: random.Random) -> Event:
    return Event(
        "WARNING",
        'Consumer "SampleEventConsumer" failed to process message, retrying',
        {"class": "App\\Infrastructure\\Messaging\\SampleEventConsumer"},
        _uid(rng),
    )


def _smtp_error(rng: random.Random) -> Event:
    return Event(
        "ERROR",
        "Invalid response SMTP server",
        {"class": "App\\Infrastructure\\Mail\\Mailer"},
        _uid(rng),
    )


def _long_cron(rng: random.Random) -> Event:
    return Event(
        "WARNING",
        f"Long cron worker executing: {rng.randint(30, 120)} seconds",
        {"class": "App\\Infrastructure\\Cron\\CronWorker"},
        _uid(rng),
    )


def _worker_missing(rng: random.Random) -> Event:
    return Event(
        "ERROR",
        "Worker not found: transcription-worker",
        {"class": "Infrastructure\\Worker\\WorkerRegistry"},
        _uid(rng),
    )


def _new_pattern(rng: random.Random) -> Event:
    return Event(
        "INFO",
        "Feature flag evaluated asynchronously",
        {"class": "Domain\\Feature\\FeatureFlagService", "flag": "async-evaluation"},
        _uid(rng),
    )


def _new_anomaly(rng: random.Random) -> Event:
    return Event(
        "WARNING",
        "Retry policy activated for the storage client",
        {"class": "Infrastructure\\Storage\\RetryPolicy", "attempt": rng.randint(1, 3)},
        _uid(rng),
    )


_WEIGHTS: dict[str, list[tuple]] = {
    "demo": [
        (_noise, 45),
        (_noise_done, 25),
        (_user_joined, 10),
        (_storage_404, 6),
        (_disk_env, 2),
        (_disk_space, 2),
        (_db_error, 2),
        (_consumer_error, 2),
        (_smtp_error, 2),
        (_long_cron, 2),
        (_worker_missing, 1),
        (_new_pattern, 1),
    ],
    "mixed": [
        (_noise, 40),
        (_noise_done, 20),
        (_user_joined, 10),
        (_storage_404, 8),
        (_db_error, 5),
        (_consumer_error, 5),
        (_smtp_error, 3),
        (_long_cron, 3),
        (_worker_missing, 3),
        (_new_pattern, 3),
    ],
    "stress": [
        (_noise, 70),
        (_noise_done, 26),
        (_user_joined, 2),
        (_storage_404, 1),
        (_db_error, 0.5),
        (_new_pattern, 0.5),
    ],
    "errors": [
        (_storage_404, 20),
        (_disk_env, 10),
        (_disk_space, 10),
        (_db_error, 20),
        (_consumer_error, 10),
        (_smtp_error, 10),
        (_long_cron, 10),
        (_worker_missing, 10),
    ],
    # Internal profile for the last day of a multi-day demo: a storage error
    # spike, a consumer that goes silent and a brand-new pattern.
    "demo_trend": [
        (_noise, 35),
        (_noise_done, 20),
        (_user_joined, 8),
        (_storage_404, 25),
        (_db_error, 2),
        (_smtp_error, 2),
        (_long_cron, 2),
        (_worker_missing, 1),
        (_consumer_error, 0.01),
        (_new_anomaly, 5),
    ],
}


def make_event(rng: random.Random, profile: str = "demo") -> Event:
    weights = _WEIGHTS.get(profile, _WEIGHTS["demo"])
    factories = [item[0] for item in weights]
    values = [item[1] for item in weights]
    factory = rng.choices(factories, weights=values, k=1)[0]
    return factory(rng)


def format_line(ts: dt.datetime, event: Event) -> str:
    context = json.dumps(event.context, ensure_ascii=False, separators=(",", ":"))
    extra = json.dumps(event.extra, ensure_ascii=False, separators=(",", ":"))
    stamp = ts.isoformat(timespec="microseconds")
    return f"[{stamp}] app.{event.level}: {event.message} {context} {extra}"


def _day_start(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=TZ)


def iter_events_for_day(
    day: dt.date,
    count: int,
    rng: random.Random,
    profile: str,
) -> Iterator[tuple[dt.datetime, Event]]:
    start = _day_start(day)
    span_us = 24 * 3600 * 1_000_000
    step = max(1, span_us // max(count, 1))
    for index in range(count):
        jitter = rng.randint(0, max(0, step // 2))
        yield start + dt.timedelta(microseconds=index * step + jitter), make_event(rng, profile)


class _DayWriter:
    def __init__(self, out_dir: str, day: dt.date, rotate_bytes: int, append: bool):
        self.out_dir = out_dir
        self.day = day
        self.rotate_bytes = rotate_bytes
        self.append = append
        self.index = 0
        self.path = ""
        self.paths: list[str] = []
        self.fh = None
        self.size = 0
        self._open()

    def _candidate(self) -> str:
        suffix = "" if self.index == 0 else f"-{self.index:03d}"
        return os.path.join(self.out_dir, f"console-{self.day.isoformat()}{suffix}.log")

    def _open(self) -> None:
        self.path = self._candidate()
        mode = "a" if (self.append and self.index == 0) else "w"
        self.fh = open(self.path, mode, encoding="utf-8")
        self.size = os.path.getsize(self.path) if mode == "a" else 0
        if self.path not in self.paths:
            self.paths.append(self.path)

    def write(self, line: str) -> None:
        data = line.encode("utf-8")
        if self.rotate_bytes and self.size and self.size + len(data) > self.rotate_bytes:
            self.close()
            self.index += 1
            self._open()
        assert self.fh is not None
        self.fh.write(line)
        self.size += len(data)

    def close(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def generate_to_dir(
    out_dir: str,
    lines: int | None = None,
    days: int = 1,
    profile: str = "demo",
    seed: int = 42,
    rotate_bytes: int = 0,
    append: bool = False,
    anomaly: bool = True,
) -> dict:
    """Generate a deterministic batch of log lines. Returns summary stats.

    For multi-day demos the last day receives an injected storage-error spike, a
    silent consumer and a brand-new pattern so ``lmlog trends`` has something
    meaningful to show. Disable with ``anomaly=False``.
    """
    os.makedirs(out_dir, exist_ok=True)
    days = max(1, days)
    lines = 0 if lines is None else max(0, lines)
    rng = random.Random(seed)
    today = dt.datetime.now(TZ).date()

    per_day = lines // days
    remainder = lines % days
    total_written = 0
    files: list[str] = []

    for offset in range(days):
        count = per_day + (1 if offset < remainder else 0)
        day = today - dt.timedelta(days=days - 1 - offset)
        if not append:
            # Remove stale rotated files from a previous run for this day so a
            # re-run is deterministic.
            for stale in glob.glob(os.path.join(out_dir, f"console-{day.isoformat()}*.log")):
                os.remove(stale)
        last_day = offset == days - 1
        day_profile = profile
        if anomaly and days > 1 and last_day and profile in ("demo", "mixed"):
            day_profile = "demo_trend"
        writer = _DayWriter(out_dir, day, rotate_bytes, append=append)
        for index, (ts, event) in enumerate(iter_events_for_day(day, count, rng, day_profile)):
            if day_profile == "demo_trend":
                if index == 0:
                    event = _consumer_error(rng)
                elif index == 1:
                    event = _new_anomaly(rng)
            writer.write(format_line(ts, event) + "\n")
            total_written += 1
        writer.close()
        files.extend(writer.paths)

    return {
        "lines": total_written,
        "days": days,
        "files": sorted(set(files)),
        "profile": profile,
        "seed": seed,
    }


def stream_logs(
    out_dir: str,
    rate: float = 50.0,
    duration: float | None = None,
    profile: str = "demo",
    seed: int = 42,
    rotate_bytes: int = 0,
) -> dict:
    """Append lines in real time until ``duration`` seconds elapse."""
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(seed)
    day = dt.datetime.now(TZ).date()
    writer = _DayWriter(out_dir, day, rotate_bytes, append=True)
    started = time.monotonic()
    written = 0
    interval = 1.0 / max(rate, 0.001)
    next_ts = dt.datetime.now(TZ)
    try:
        while True:
            now = time.monotonic()
            if duration is not None and now - started >= duration:
                break
            next_ts = next_ts + dt.timedelta(seconds=interval)
            writer.write(format_line(next_ts, make_event(rng, profile)) + "\n")
            written += 1
            target = started + written * interval
            sleep_for = target - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        pass
    finally:
        writer.close()
    return {
        "lines": written,
        "file": writer.path,
        "files": writer.paths,
        "profile": profile,
        "rate": rate,
    }
