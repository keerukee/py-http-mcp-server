"""Runs coroutines from synchronous (thread-based) code on a shared background event loop."""

import asyncio
import concurrent.futures
import contextvars
import inspect
import threading
from typing import Any, Optional

from .cancellation import CancellationToken
from .exceptions import McpCancelledError

__all__ = ["AsyncRunner", "default_runner"]


class AsyncRunner:
    """Runs awaitables for synchronous callers.

    By default it owns a daemon thread with a private event loop (started lazily). When hosted inside
    an ASGI application, :meth:`bind_loop` points it at the host's running loop instead, so async
    handlers share the application's loop (and its async resources such as DB pools / HTTP clients).
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._bound_loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Use ``loop`` (e.g. the ASGI server's loop) for coroutines submitted from worker threads."""
        self._bound_loop = loop

    def _target_loop(self) -> asyncio.AbstractEventLoop:
        bound = self._bound_loop
        if bound is not None and bound.is_running() and not bound.is_closed():
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is not bound:  # blocking the bound loop on itself would deadlock
                return bound
        return self._ensure_loop()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def _run() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    try:
                        loop.run_forever()
                    finally:
                        loop.close()

                thread = threading.Thread(target=_run, name="mcp-async-runner", daemon=True)
                thread.start()
                ready.wait()
                self._loop, self._thread = loop, thread
            return self._loop

    def resolve(self, value: Any, token: Optional[CancellationToken] = None) -> Any:
        """If ``value`` is awaitable, run it to completion on the loop and return its result."""
        if not inspect.isawaitable(value):
            return value
        loop = self._target_loop()
        ctx = contextvars.copy_context()
        result: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
        task_holder: dict = {}

        async def _await() -> Any:
            return await value

        def _start() -> None:
            # Creating the task inside ``ctx.run`` makes the task inherit the caller's contextvars.
            task = ctx.run(loop.create_task, _await())
            task_holder["task"] = task

            def _done(t: "asyncio.Task[Any]") -> None:
                if t.cancelled():
                    result.set_exception(McpCancelledError())
                elif t.exception() is not None:
                    result.set_exception(t.exception())  # type: ignore[arg-type]
                else:
                    result.set_result(t.result())

            task.add_done_callback(_done)

        loop.call_soon_threadsafe(_start)

        unregister = None
        if token is not None:
            def _cancel() -> None:
                def _do() -> None:
                    task = task_holder.get("task")
                    if task is not None:
                        task.cancel()
                loop.call_soon_threadsafe(_do)

            unregister = token.register(_cancel)
        try:
            return result.result()
        finally:
            if unregister is not None:
                unregister()

    def shutdown(self) -> None:
        with self._lock:
            loop, self._loop = self._loop, None
            thread, self._thread = self._thread, None
        self._bound_loop = None
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)


#: Process-wide default runner.
default_runner = AsyncRunner()
