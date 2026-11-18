"""Scoped lease servicing, independent of the workflow's asyncio loop.

A scope owns its non-daemon thread until it has joined. Nested/concurrent scopes
for the same ownership tuple share one thread; failures stop it without retry.
No Session or event-loop object crosses into the thread. Database fencing remains
in Runtime.heartbeat, including after any wait for a database lock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Condition, Event, Lock, Thread, local

_thread_state = local()


class HeartbeatStopped(Exception):
    """Internal cooperative shutdown, never an authorization or a retry signal."""


def check_heartbeat_stop() -> None:
    stop = getattr(_thread_state, "stop", None)
    if stop is not None and stop.is_set():
        raise HeartbeatStopped()


class LeaseHeartbeat:
    def __init__(self, callback: Callable[[], None], interval: float, name: str):
        self._callback = callback
        self._interval = interval
        self._stop = Event()
        self._error_lock = Lock()
        self._error: BaseException | None = None
        self.references = 0
        self.closing = False
        self.thread = Thread(target=self._run, name=name, daemon=False)

    def _run(self) -> None:
        _thread_state.stop = self._stop
        try:
            while not self._stop.wait(self._interval):
                check_heartbeat_stop()
                self._callback()
        except HeartbeatStopped:
            pass
        except BaseException as exc:
            # Do not retry a failed tick, revive a lease, or cancel an in-flight
            # provider whose genuine receipt still needs normal settlement.
            with self._error_lock:
                self._error = exc
            self._stop.set()
        finally:
            del _thread_state.stop

    def check(self) -> None:
        with self._error_lock:
            error = self._error
        if error is not None:
            raise error

    def stop(self) -> None:
        self._stop.set()


class LeaseHeartbeats:
    def __init__(self):
        self._changed = Condition()
        self._active: dict[tuple[str, int, str], LeaseHeartbeat] = {}

    @contextmanager
    def maintain(
        self,
        key: tuple[str, int, str],
        callback: Callable[[], None],
        interval: float,
    ) -> Iterator[LeaseHeartbeat]:
        with self._changed:
            # Do not overlap a replacement with an old scope's in-flight tick.
            while key in self._active and self._active[key].closing:
                self._changed.wait()
            heartbeat = self._active.get(key)
            if heartbeat is None:
                heartbeat = LeaseHeartbeat(callback, interval, "careerforge-lease-" + key[0])
                self._active[key] = heartbeat
                try:
                    heartbeat.thread.start()
                except BaseException:
                    del self._active[key]
                    self._changed.notify_all()
                    raise
            heartbeat.references += 1
        try:
            heartbeat.check()
            yield heartbeat
        finally:
            with self._changed:
                heartbeat.references -= 1
                last = heartbeat.references == 0
                if last:
                    heartbeat.closing = True
                    heartbeat.stop()
            if last:
                # Deliberately synchronous: task cancellation/loop shutdown must
                # not abandon a database writer. An in-flight driver operation
                # must finish or fail before cleanup returns. A hung database or
                # a suspended process cannot be repaired by a keeper thread.
                heartbeat.thread.join()
                with self._changed:
                    del self._active[key]
                    self._changed.notify_all()
