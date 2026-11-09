"""One shared cap on simultaneous Venice requests (models, voice, safety), with priorities.

Hubie works ahead: while he talks he already writes and voices lines for outcomes that may not
happen. During a Venice slow spell those speculative calls used to crowd out the ones he was
actually waiting on. Every Venice call takes a slot here; urgent work (the line he needs next, a
chat reply, a safety check) always gets a free slot before speculative work does.

A task marks itself speculative with `speculative()`; everything it awaits inherits that (context
variables follow the task), so the brain, safety and TTS code don't need to know who called them.
"""
import asyncio
import contextlib
import contextvars
import heapq
import itertools

MAX_CONCURRENT = 4
URGENT, SPECULATIVE = 0, 1

_priority: contextvars.ContextVar[int] = contextvars.ContextVar("venice_priority", default=URGENT)
_active = 0
_waiters: list = []                 # heap of (priority, seq, future)
_seq = itertools.count()


def speculative():
    """Mark the current task's Venice calls as speculative (lower priority)."""
    _priority.set(SPECULATIVE)


@contextlib.asynccontextmanager
async def slot():
    global _active
    if _active < MAX_CONCURRENT and not _waiters:
        _active += 1
    else:
        fut = asyncio.get_running_loop().create_future()
        entry = (_priority.get(), next(_seq), fut)
        heapq.heappush(_waiters, entry)
        try:
            await fut                   # resolved by a releasing slot, which hands its place over
        except asyncio.CancelledError:
            if entry in _waiters:
                _waiters.remove(entry)
                heapq.heapify(_waiters)
            elif fut.done() and not fut.cancelled():
                _release()              # we were handed a slot just as we got cancelled
            raise
    try:
        yield
    finally:
        _release()


def _release():
    global _active
    while _waiters:
        _, _, fut = heapq.heappop(_waiters)
        if not fut.done():
            fut.set_result(None)        # the slot passes straight to the most urgent waiter
            return
    _active -= 1
