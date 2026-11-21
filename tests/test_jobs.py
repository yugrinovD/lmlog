from __future__ import annotations

import threading

from lmlog.serve.jobs import JobManager


def test_list_is_safe_during_concurrent_creates(tmp_path):
    manager = JobManager(str(tmp_path), max_jobs=200)
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                manager.list(100)
            except BaseException as exc:  # noqa: BLE001 - the test reports the race
                errors.append(exc)
                return

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(50):
            manager.create(f"file-{index}.log", 1)
    finally:
        stop.set()
        thread.join()

    assert not errors
    assert manager.count() == 50


def test_prune_does_not_remove_active_jobs(tmp_path):
    manager = JobManager(str(tmp_path), max_jobs=1)
    first, _ = manager.create("first.log", 1)
    second, _ = manager.create("second.log", 1)

    assert manager.get(first.id) is not None
    assert manager.get(second.id) is not None
