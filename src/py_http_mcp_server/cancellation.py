"""Cooperative cancellation primitive (the Python counterpart of .NET's ``CancellationToken``)."""

import threading
from typing import Callable, List, Optional

from .exceptions import McpCancelledError

__all__ = ["CancellationToken"]


class CancellationToken:
    """Thread-safe cancellation signal.

    Declare a parameter annotated with ``CancellationToken`` on any tool, resource or prompt
    handler and the server injects a token automatically (it is hidden from the input schema).
    Async handlers are additionally cancelled at their next ``await`` when the token fires.
    """

    __slots__ = ("_event", "_lock", "_callbacks")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[], None]] = []

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        """Signal cancellation and run registered callbacks (once)."""
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for cb in callbacks:
            try:
                cb()
            except Exception:  # pragma: no cover - callbacks must never break cancellation
                pass

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise McpCancelledError()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block up to ``timeout`` seconds; returns True if cancelled. Handy as a cancellable ``sleep``."""
        return self._event.wait(timeout)

    def register(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register ``callback`` to run on cancellation. Returns a function that unregisters it."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)

                def _unregister() -> None:
                    with self._lock:
                        try:
                            self._callbacks.remove(callback)
                        except ValueError:
                            pass

                return _unregister
        callback()
        return lambda: None

    def __repr__(self) -> str:
        return f"CancellationToken(cancelled={self.is_cancelled})"
