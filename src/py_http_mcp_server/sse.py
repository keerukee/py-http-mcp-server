"""Server-Sent Events sessions used by the Streamable HTTP ``GET`` stream."""

import itertools
import logging
import queue
import threading
from typing import Any, Dict, Iterator, List, Optional

from . import _json

__all__ = ["SseSession", "SessionManager", "format_sse_event"]

logger = logging.getLogger("py_http_mcp_server")

_CLOSE = object()


def format_sse_event(event: str, data: str, event_id: Optional[str] = None) -> bytes:
    lines = [f"id: {event_id}"] if event_id is not None else []
    lines.append(f"event: {event}")
    lines.extend(f"data: {line}" for line in (data.splitlines() or [""]))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class SseSession:
    """A thread-safe outbound SSE channel for one connected client.

    ``send_event`` / ``send_message`` can be called from any thread; the transport drains the
    queue via :meth:`iter_stream`, emitting keep-alive comments while idle.
    """

    def __init__(self, session_id: str, last_event_id: Optional[str] = None, keepalive_interval: float = 15.0) -> None:
        self.session_id = session_id
        self.last_event_id = last_event_id
        self.keepalive_interval = keepalive_interval
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._counter = itertools.count(1)
        self._closed = threading.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def send_event(self, event: str, data: str, event_id: Optional[str] = None) -> None:
        if self.closed:
            raise RuntimeError(f"SSE session {self.session_id} is closed")
        eid = event_id if event_id is not None else str(next(self._counter))
        self._queue.put(format_sse_event(event, data, eid))

    def send_message(self, message: Any) -> None:
        """Send a JSON-RPC message (request/notification) to the client as an SSE ``message`` event."""
        self.send_event("message", _json.dumps(message))

    def close(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            self._queue.put(_CLOSE)

    def iter_stream(self) -> Iterator[bytes]:
        while not self.closed:
            try:
                item = self._queue.get(timeout=self.keepalive_interval)
            except queue.Empty:
                yield b": keep-alive\n\n"
                continue
            if item is _CLOSE:
                break
            yield item


class SessionManager:
    """Registry of active SSE sessions keyed by ``Mcp-Session-Id``."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SseSession] = {}
        self._lock = threading.Lock()

    def add(self, session: SseSession) -> None:
        with self._lock:
            previous = self._sessions.get(session.session_id)
            self._sessions[session.session_id] = session
        if previous is not None and previous is not session:
            previous.close()

    def get(self, session_id: str) -> Optional[SseSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def remove(self, session_id: str, session: Optional[SseSession] = None) -> Optional[SseSession]:
        with self._lock:
            current = self._sessions.get(session_id)
            if current is None or (session is not None and current is not session):
                return None
            del self._sessions[session_id]
        current.close()
        return current

    def all(self) -> List[SseSession]:
        with self._lock:
            return list(self._sessions.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def broadcast(self, message: Any) -> int:
        """Send ``message`` to every open session; returns how many received it."""
        sent = 0
        for session in self.all():
            try:
                session.send_message(message)
                sent += 1
            except RuntimeError:
                pass
        return sent

    def close_all(self) -> None:
        with self._lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            session.close()
