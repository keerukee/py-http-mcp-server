"""Thread-safe task store for the ``io.modelcontextprotocol/tasks`` extension (SEP-2663)."""

import datetime
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import _json
from .cancellation import CancellationToken

__all__ = ["McpTask", "McpTaskStore", "TERMINAL_STATUSES"]

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_ACTIVE_STATUSES = frozenset({"working", "input_required"})


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class McpTask:
    """A durable / asynchronous MCP task."""

    task_id: str
    #: ``working``, ``input_required``, ``completed``, ``failed`` or ``cancelled``.
    status: str = "working"
    status_message: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    last_updated_at: str = field(default_factory=_now_iso)
    ttl: Optional[int] = None
    poll_interval: Optional[int] = None
    result: Any = None
    error: Any = None
    result_type: str = "complete"
    cancellation_token: CancellationToken = field(default_factory=CancellationToken, repr=False)
    _last_updated_monotonic: float = field(default_factory=time.monotonic, repr=False)

    def touch(self) -> None:
        self.last_updated_at = _now_iso()
        self._last_updated_monotonic = time.monotonic()

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.ttl is None or self.status not in TERMINAL_STATUSES:
            return False
        return ((now or time.monotonic()) - self._last_updated_monotonic) * 1000.0 > self.ttl

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"resultType": self.result_type, "taskId": self.task_id, "status": self.status}
        if self.status_message is not None:
            out["statusMessage"] = self.status_message
        out["createdAt"] = self.created_at
        out["lastUpdatedAt"] = self.last_updated_at
        if self.ttl is not None:
            out["ttl"] = self.ttl
            out["ttlMs"] = self.ttl
        if self.poll_interval is not None:
            out["pollInterval"] = self.poll_interval
            out["pollIntervalMs"] = self.poll_interval
        if self.result is not None:
            out["result"] = _json.to_jsonable(self.result)
        if self.error is not None:
            out["error"] = _json.to_jsonable(self.error)
        return out


class McpTaskStore:
    """Stores tasks in memory. Terminal tasks are evicted once their ``ttl`` (ms) has elapsed."""

    def __init__(self) -> None:
        self._tasks: Dict[str, McpTask] = {}
        self._lock = threading.RLock()

    def _prune(self) -> None:
        now = time.monotonic()
        for task_id in [k for k, t in self._tasks.items() if t.is_expired(now)]:
            del self._tasks[task_id]

    @property
    def tasks(self) -> List[McpTask]:
        with self._lock:
            self._prune()
            return list(self._tasks.values())

    def create_task(self, ttl: Optional[int] = 60000, poll_interval: Optional[int] = 1000,
                    status_message: Optional[str] = None) -> McpTask:
        task = McpTask(task_id=uuid.uuid4().hex, status="working", status_message=status_message,
                       ttl=ttl, poll_interval=poll_interval)
        with self._lock:
            self._prune()
            self._tasks[task.task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[McpTask]:
        with self._lock:
            self._prune()
            return self._tasks.get(task_id)

    def complete_task(self, task_id: str, result: Any, status_message: Optional[str] = None) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status not in _ACTIVE_STATUSES:
                return False
            task.status = "completed"
            task.result = result
            task.status_message = status_message
            task.touch()
            return True

    def fail_task(self, task_id: str, error_message: str, error_code: int = -32603) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status not in _ACTIVE_STATUSES:
                return False
            task.status = "failed"
            task.status_message = error_message
            task.error = {"code": error_code, "message": error_message}
            task.touch()
            return True

    def update_task(self, task_id: str, status: Optional[str] = None, status_message: Optional[str] = None,
                    result: Any = None) -> Optional[McpTask]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if status:
                task.status = status
            if status_message is not None:
                task.status_message = status_message
            if result is not None:
                task.result = result
            task.touch()
            return task

    def cancel_task(self, task_id: str, reason: Optional[str] = None) -> Optional[McpTask]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status in _ACTIVE_STATUSES:
                task.status = "cancelled"
                task.status_message = reason or "Task was cancelled by the client."
                task.touch()
            else:
                return task
        task.cancellation_token.cancel()
        return task
