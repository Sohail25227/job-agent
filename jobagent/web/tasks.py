"""A small worker pool for work that outlives a request.

A live search across every source takes tens of seconds - too long to hold an
HTTP request open, and far too long for a host that kills slow responses. So the
request starts a task, returns immediately, and the page polls for it.

Tasks are keyed, usually by search, so each browser tab can poll the one it
started rather than guessing from a single global slot. Identical work already
in flight is joined rather than duplicated, which is what makes a double-click
or a page refresh harmless.

The pool is deliberately tiny. The work is all waiting on other people's
servers, but the free tier this is built for has a fraction of a CPU, and every
extra concurrent search spends the same shared third-party API quota.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

MAX_WORKERS = 2
# Finished tasks are kept so a page that polls late still sees the outcome.
MAX_REMEMBERED = 40


@dataclass
class TaskState:
    key: str = ""
    name: str = ""
    detail: str = ""
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    result: dict = field(default_factory=dict)
    progress: list[str] = field(default_factory=list)
    ahead: int = 0

    @property
    def running(self) -> bool:
        return self.started_at is not None and self.finished_at is None

    @property
    def waiting(self) -> bool:
        # queued_at is what separates a real queued task from the blank
        # placeholder snapshot() hands back when nothing is running.
        return (
            self.queued_at is not None
            and self.started_at is None
            and self.finished_at is None
        )

    @property
    def elapsed_seconds(self) -> int:
        start = self.started_at or self.queued_at
        if not start:
            return 0
        end = self.finished_at or datetime.now(timezone.utc)
        return int((end - start).total_seconds())

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "detail": self.detail,
            "running": self.running,
            "waiting": self.waiting,
            "busy": self.running or self.waiting,
            "done": self.finished_at is not None,
            "ahead": self.ahead,
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
            "result": self.result,
            "progress": self.progress[-14:],
        }


@dataclass
class _Task:
    state: TaskState
    work: Callable[[Callable[[str], None]], dict]


_cond = threading.Condition()
_pending: deque[_Task] = deque()
_states: OrderedDict[str, TaskState] = OrderedDict()
_workers: list[threading.Thread] = []


def submit(key: str, name: str, detail: str,
           work: Callable[[Callable[[str], None]], dict]) -> TaskState:
    """Queue ``work``, or return the in-flight task if it is already running.

    ``work`` is handed a ``note`` callable so it can report progress while the
    user watches.
    """
    with _cond:
        existing = _states.get(key)
        if existing is not None and existing.finished_at is None:
            return existing

        state = TaskState(
            key=key,
            name=name,
            detail=detail,
            queued_at=datetime.now(timezone.utc),
            ahead=len(_pending),
        )
        _states[key] = state
        while len(_states) > MAX_REMEMBERED:
            oldest, oldest_state = next(iter(_states.items()))
            if oldest_state.finished_at is None:
                break  # never evict something still in flight
            _states.pop(oldest)
        _pending.append(_Task(state, work))
        _cond.notify()
    _ensure_workers()
    return state


def get(key: str) -> TaskState | None:
    with _cond:
        return _states.get(key)


def snapshot(key: str | None = None) -> dict:
    """State for one task, plus how busy the pool is overall."""
    with _cond:
        state = _states.get(key) if key else None
        data = state.as_dict() if state else TaskState().as_dict()
        data["queue_length"] = len(_pending)
        data["active"] = sum(1 for s in _states.values() if s.running)
    return data


def _ensure_workers() -> None:
    with _cond:
        alive = [w for w in _workers if w.is_alive()]
        _workers[:] = alive
        if len(alive) >= MAX_WORKERS:
            return
        worker = threading.Thread(target=_loop, name="jobagent-worker", daemon=True)
        _workers.append(worker)
    worker.start()


def _loop() -> None:
    while True:
        with _cond:
            # Idle workers exit rather than parking forever, so a process that
            # is about to be suspended for inactivity has nothing holding it.
            if not _pending and not _cond.wait_for(lambda: bool(_pending), timeout=120):
                _workers[:] = [w for w in _workers if w is not threading.current_thread()]
                return
            task = _pending.popleft()
            state = task.state
            state.started_at = datetime.now(timezone.utc)
            state.ahead = 0

        def note(message: str, _state: TaskState = state) -> None:
            with _cond:
                _state.progress.append(message)
            log.info("task %s: %s", _state.key, message)

        try:
            result = task.work(note) or {}
            with _cond:
                state.result = result
        except Exception as exc:
            log.error("task %s failed: %s", state.key, exc, exc_info=True)
            with _cond:
                state.error = str(exc)[:400]
        finally:
            with _cond:
                state.finished_at = datetime.now(timezone.utc)
