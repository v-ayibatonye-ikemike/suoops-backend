"""Helpers for running coroutines from synchronous code paths."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T | None:
    """Run an awaitable from synchronous code, safely.

    When called from a plain synchronous context (no running event loop) the
    coroutine is executed to completion and its result returned — this is the
    normal case for sync service methods, Celery tasks and threadpool workers.

    When a running event loop is detected (i.e. the caller is actually inside
    async code), the coroutine is scheduled as a fire-and-forget task instead of
    calling ``asyncio.run()``, which would raise ``RuntimeError`` for trying to
    nest event loops. In that case ``None`` is returned because the result is not
    available synchronously.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to drive the coroutine to completion.
        return asyncio.run(coro)

    # Already inside an event loop; schedule without blocking.
    logger.debug("run_async invoked inside a running loop; scheduling as task")
    loop.create_task(coro)
    return None
