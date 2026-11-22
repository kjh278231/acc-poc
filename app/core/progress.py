"""Live progress reporting from deep inside a turn (model calls) without changing engine signatures.

The orchestrator installs a sink in a ContextVar before starting `engine.decide()` as a task; LLM clients call
`report()` / `tracked()` around each model call. Tasks created inside the engine inherit the context, so the sink
sees every call. Without a sink (benchmark direct calls, tests) reporting is a no-op.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

T = TypeVar("T")
Sink = Callable[[str, dict[str, Any]], None]
_SINK: ContextVar[Sink | None] = ContextVar("acc_progress_sink", default=None)


def report(stage: str, **data: Any) -> None:
    sink = _SINK.get()
    if sink is not None:
        sink(stage, data)


@contextmanager
def capture(sink: Sink) -> Iterator[None]:
    tok = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(tok)


async def tracked(model: str, purpose: str, aw: Awaitable[T]) -> T:
    report("model_start", model=model, purpose=purpose)
    t0, ok = time.perf_counter(), False
    try:
        r = await aw
        ok = True
        return r
    finally:
        report("model_end", model=model, purpose=purpose, ok=ok, ms=round((time.perf_counter() - t0) * 1000, 1))
