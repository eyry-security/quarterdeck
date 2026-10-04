"""Small bounded daemon worker pool with cancellable queued futures."""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

_STOP = object()


class DaemonExecutor:
    """Thread executor that never holds interpreter shutdown indefinitely."""

    def __init__(self, max_workers: int, name_prefix: str):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._queue: queue.Queue = queue.Queue()
        self._accepting = True
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"{name_prefix}-{index}",
                daemon=True,
            )
            for index in range(max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, fn: Callable, /, *args: Any, **kwargs: Any) -> Future:
        with self._lock:
            if not self._accepting:
                raise RuntimeError("executor is shutting down")
            future = Future()
            self._queue.put((future, fn, args, kwargs))
            return future

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                future, fn, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()

    def shutdown(self, *, timeout: float, cancel_futures: bool = True) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
        if cancel_futures:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if item is not _STOP:
                        item[0].cancel()
                finally:
                    self._queue.task_done()
        for _thread in self._threads:
            self._queue.put(_STOP)
        deadline = time.monotonic() + max(timeout, 0)
        for thread in self._threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
