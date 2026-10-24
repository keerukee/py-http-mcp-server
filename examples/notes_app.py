"""Notes board: one in-memory store shared by the Web UI (REST + live SSE) and MCP clients (tools/resources).

Anything an AI agent does through MCP shows up instantly in the browser, and vice versa.
"""

import collections
import datetime
import itertools
import threading
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Callable, Deque, Dict, List, Optional

from py_http_mcp_server import (
    HttpError,
    HttpRequest,
    McpServer,
    Param,
    SessionManager,
    get_current_request,
    mcp_handler,
    mcp_prompt,
    mcp_resource,
    mcp_resource_template,
    mcp_tool,
    sse_response,
)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------------------------
@dataclass
class Note:
    id: int
    title: str
    body: str = ""
    tags: List[str] = field(default_factory=list)
    done: bool = False
    source: str = "web"  # who created it: "web" or "mcp"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


class NoteStore:
    """Thread-safe note storage that notifies listeners on every change."""

    def __init__(self) -> None:
        self._notes: Dict[int, Note] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._listeners: List[Callable[[str, Note, str], None]] = []

    def subscribe(self, listener: Callable[[str, Note, str], None]) -> None:
        self._listeners.append(listener)

    def _emit(self, event: str, note: Note, source: str) -> None:
        for listener in self._listeners:
            listener(event, note, source)

    def list(self, include_done: bool = True, tag: Optional[str] = None) -> List[Note]:
        with self._lock:
            notes = list(self._notes.values())
        if not include_done:
            notes = [n for n in notes if not n.done]
        if tag:
            notes = [n for n in notes if tag.lower() in (t.lower() for t in n.tags)]
        return notes

    def get(self, note_id: int) -> Note:
        with self._lock:
            note = self._notes.get(note_id)
        if note is None:
            raise KeyError(f"Note {note_id} not found")
        return note

    def add(self, title: str, body: str = "", tags: Optional[List[str]] = None, source: str = "web") -> Note:
        title = (title or "").strip()
        if not title:
            raise ValueError("Title is required")
        clean_tags = sorted({t.strip() for t in (tags or []) if t and t.strip()})
        with self._lock:
            note = Note(id=next(self._ids), title=title, body=(body or "").strip(), tags=clean_tags, source=source)
            self._notes[note.id] = note
        self._emit("note.created", note, source)
        return note

    def update(self, note_id: int, source: str, **changes: Any) -> Note:
        with self._lock:
            note = self._notes.get(note_id)
            if note is None:
                raise KeyError(f"Note {note_id} not found")
            if "title" in changes and changes["title"] is not None:
                if not str(changes["title"]).strip():
                    raise ValueError("Title cannot be empty")
                note.title = str(changes["title"]).strip()
            if "body" in changes and changes["body"] is not None:
                note.body = str(changes["body"]).strip()
            if "tags" in changes and changes["tags"] is not None:
                note.tags = sorted({str(t).strip() for t in changes["tags"] if str(t).strip()})
            if "done" in changes and changes["done"] is not None:
                note.done = bool(changes["done"])
            note.updated_at = _now()
        self._emit("note.updated", note, source)
        return note

    def delete(self, note_id: int, source: str) -> Note:
        with self._lock:
            note = self._notes.pop(note_id, None)
        if note is None:
            raise KeyError(f"Note {note_id} not found")
        self._emit("note.deleted", note, source)
        return note


# ---------------------------------------------------------------------------------------------
# MCP surface (tools, resources, prompt) -- what AI agents see
# ---------------------------------------------------------------------------------------------
@mcp_handler
class NotesMcpTools:
    def __init__(self, store: NoteStore) -> None:
        self.store = store

    @mcp_tool("list_notes", "Lists notes on the shared notes board (also visible in the web UI).", read_only_hint=True)
    def list_notes(
        self,
        include_done: Annotated[bool, Param("Include completed notes")] = True,
        tag: Annotated[Optional[str], Param("Only notes with this tag")] = None,
    ) -> Dict[str, Any]:
        notes = self.store.list(include_done, tag)
        return {"count": len(notes), "notes": [asdict(n) for n in notes]}

    @mcp_tool("add_note", "Adds a note to the shared board. It appears live in the web UI.",
              use_structured_output=True, idempotent_hint=False, destructive_hint=False)
    def add_note(
        self,
        title: Annotated[str, Param("Short title of the note")],
        body: Annotated[str, Param("Optional details")] = "",
        tags: Annotated[Optional[List[str]], Param("Optional tags, e.g. ['work', 'urgent']")] = None,
    ) -> Note:
        return self.store.add(title, body, tags, source="mcp")

    @mcp_tool("complete_note", "Marks a note as done (or not done).", use_structured_output=True, idempotent_hint=True,
              destructive_hint=False)
    def complete_note(
        self,
        id: Annotated[int, Param("Note id")],  # noqa: A002
        done: Annotated[bool, Param("True = done, False = reopen")] = True,
    ) -> Note:
        return self.store.update(id, "mcp", done=done)

    @mcp_tool("update_note", "Edits a note's title, body or tags.", use_structured_output=True, destructive_hint=False)
    def update_note(
        self,
        id: Annotated[int, Param("Note id")],  # noqa: A002
        title: Optional[str] = None,
        body: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Note:
        return self.store.update(id, "mcp", title=title, body=body, tags=tags)

    @mcp_tool("delete_note", "Permanently deletes a note.", destructive_hint=True)
    def delete_note(self, id: Annotated[int, Param("Note id")]) -> str:  # noqa: A002
        note = self.store.delete(id, "mcp")
        return f"Deleted note {note.id}: {note.title}"

    @mcp_resource("notes://all", "All Notes", "Every note on the board as JSON.", "application/json")
    def all_notes(self) -> List[Dict[str, Any]]:
        return [asdict(n) for n in self.store.list()]

    @mcp_resource_template("notes://{id}", "Note", "A single note by id.", "application/json")
    def one_note(self, id: int) -> Dict[str, Any]:  # noqa: A002
        return asdict(self.store.get(id))

    @mcp_prompt("summarize_notes", "Asks the model to summarize open notes and suggest priorities.")
    def summarize_notes(self, focus: Annotated[str, Param("Optional focus area, e.g. 'work'")] = "") -> str:
        open_notes = self.store.list(include_done=False)
        lines = "\n".join(f"- [{n.id}] {n.title} {n.tags} {n.body}".rstrip() for n in open_notes) or "- (no open notes)"
        focus_line = f" Focus on: {focus}." if focus else ""
        return f"Here are my open notes:\n{lines}\n\nSummarize them and suggest what to do first.{focus_line}"


# ---------------------------------------------------------------------------------------------
# Wiring: web routes + live events
# ---------------------------------------------------------------------------------------------
def install(server: McpServer, store: Optional[NoteStore] = None) -> NoteStore:
    """Register the notes MCP tools and the REST/SSE web API on ``server``."""
    store = store or NoteStore()
    ui_events = SessionManager()  # browser EventSource subscribers
    activity: Deque[Dict[str, Any]] = collections.deque(maxlen=200)

    def on_change(event: str, note: Note, source: str) -> None:
        request = get_current_request()
        entry = {
            "type": event,
            "source": source,
            "note": asdict(note),
            "at": _now(),
            "client": (request.header("User-Agent") or "")[:80] if request else "",
        }
        activity.appendleft(entry)
        ui_events.broadcast(entry)               # -> browsers
        server.notify_resources_list_changed()   # -> MCP clients holding a GET stream

    store.subscribe(on_change)
    server.register(NotesMcpTools(store))

    def _note_id(request: HttpRequest) -> int:
        try:
            return int(request.path_params["id"])
        except ValueError:
            raise HttpError(400, "Note id must be an integer") from None

    def _guard(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except KeyError as exc:
            raise HttpError(404, str(exc).strip("'\"")) from None
        except ValueError as exc:
            raise HttpError(400, str(exc)) from None

    @server.route("/api/notes", methods=["GET"])
    def list_notes(request: HttpRequest) -> List[Dict[str, Any]]:
        q = request.query
        include_done = q.get("include_done", "true").lower() != "false"
        return [asdict(n) for n in store.list(include_done, q.get("tag") or None)]

    @server.route("/api/notes", methods=["POST"])
    def create_note(request: HttpRequest) -> Any:
        data = request.json() or {}
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in tags.split(",")]
        note = _guard(lambda: store.add(data.get("title", ""), data.get("body", ""), tags, source="web"))
        return asdict(note), 201

    @server.route("/api/notes/{id}", methods=["GET"])
    def get_note(request: HttpRequest) -> Dict[str, Any]:
        return asdict(_guard(lambda: store.get(_note_id(request))))

    @server.route("/api/notes/{id}", methods=["PATCH"])
    def patch_note(request: HttpRequest) -> Dict[str, Any]:
        data = request.json() or {}
        allowed = {k: data.get(k) for k in ("title", "body", "tags", "done") if k in data}
        return asdict(_guard(lambda: store.update(_note_id(request), "web", **allowed)))

    @server.route("/api/notes/{id}", methods=["DELETE"])
    def delete_note(request: HttpRequest) -> None:
        _guard(lambda: store.delete(_note_id(request), "web"))

    @server.route("/api/activity")
    def get_activity(request: HttpRequest) -> List[Dict[str, Any]]:
        return list(activity)

    @server.route("/api/events")
    def events(request: HttpRequest) -> Any:
        return sse_response(ui_events, keepalive_interval=15.0, initial_event=("ready", "{}"))

    @server.route("/api/info")
    def info(request: HttpRequest) -> Dict[str, Any]:
        o, r = server.options, server.registry
        return {
            "name": o.server_name,
            "title": o.server_title,
            "version": o.server_version,
            "mcpUrl": f"{request.base_url}{o.endpoint}",
            "protocolVersions": list(o.supported_protocol_versions),
            "tools": len(r.tools),
            "resources": len(r.resources),
            "resourceTemplates": len(r.resource_templates),
            "prompts": len(r.prompts),
            "mcpStreams": len(server.sessions),
            "webSubscribers": len(ui_events),
            "notes": len(store.list()),
        }

    # A little starter content
    store.add("Try the MCP Playground tab", "Call add_note from the browser or from your AI agent.", ["demo"])
    store.add("Ask your agent to summarize notes", "Use the 'summarize_notes' prompt.", ["demo", "mcp"])
    activity.clear()
    return store
