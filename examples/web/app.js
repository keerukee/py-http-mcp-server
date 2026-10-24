/* Sample MCP Server web UI: notes board (REST + SSE), MCP playground (JSON-RPC over /mcp), activity feed.
 * No frameworks, no build step. All user/agent-provided text is rendered with textContent (XSS-safe). */
"use strict";

// ------------------------------------------------------------------------------------------
// Helpers
// ------------------------------------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (typeof value !== "string" && key in el) el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
function ago(iso) {
  const seconds = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return "just now";
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(seconds / 3600), "hour");
  return rtf.format(Math.round(seconds / 86400), "day");
}
const clock = (iso) => new Date(iso).toLocaleTimeString();
const pretty = (value) => JSON.stringify(value, null, 2);

function toast(message, kind = "web", icon = "") {
  const el = h("div", { class: `toast ${kind}` }, icon ? h("span", null, icon) : null, h("div", null, message));
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 4200);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h("textarea", { style: "position:fixed;opacity:0" });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast("Copied to clipboard", "web", "📋");
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

// ------------------------------------------------------------------------------------------
// State
// ------------------------------------------------------------------------------------------
const state = {
  info: null,
  notes: new Map(),
  activity: [],
  filter: { text: "", showDone: true, tag: null },
  editing: null,
  flash: new Map(), // id -> source
};

// ------------------------------------------------------------------------------------------
// Tabs
// ------------------------------------------------------------------------------------------
function showView(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "playground") Playground.ensureLoaded();
  history.replaceState(null, "", `#${name}`);
}
$$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

// ------------------------------------------------------------------------------------------
// Server info & stats
// ------------------------------------------------------------------------------------------
async function refreshInfo() {
  try {
    const info = await api("GET", "/api/info");
    state.info = info;
    $("#server-title").textContent = info.title || info.name;
    $("#server-version").textContent = `${info.name} v${info.version} · web app + MCP server`;
    $("#mcp-url").textContent = info.mcpUrl;
    $("#stat-tools").textContent = info.tools;
    $("#stat-notes").textContent = info.notes;
    $("#stat-streams").textContent = info.mcpStreams;
    $("#stat-browsers").textContent = info.webSubscribers;
    fillConnectSnippets(info.mcpUrl);
  } catch (err) {
    console.warn("info failed", err);
  }
}

function fillConnectSnippets(url) {
  $("#cfg-antigravity").textContent = pretty({ mcpServers: { "py-sample-mcp": { url } } });
  $("#inspector-url").textContent = url;
  $("#cfg-claude").textContent =
    `# Claude Code\nclaude mcp add --transport http py-sample-mcp ${url}\n\n# Cursor (.cursor/mcp.json)\n` +
    pretty({ mcpServers: { "py-sample-mcp": { url } } });
  $("#cfg-curl").textContent =
    `curl -s -X POST ${url} \\\n  -H "Content-Type: application/json" \\\n  -d '${JSON.stringify({
      jsonrpc: "2.0", id: 1, method: "tools/call",
      params: { name: "add_note", arguments: { title: "Hello from curl", tags: ["cli"] } },
    })}'`;
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy-target]");
  if (btn) copyText($(`#${btn.dataset.copyTarget}`).textContent);
});

// ------------------------------------------------------------------------------------------
// Notes board
// ------------------------------------------------------------------------------------------
async function loadNotes() {
  const notes = await api("GET", "/api/notes");
  state.notes = new Map(notes.map((n) => [n.id, n]));
  renderNotes();
}

function visibleNotes() {
  const { text, showDone, tag } = state.filter;
  const q = text.trim().toLowerCase();
  return [...state.notes.values()]
    .filter((n) => showDone || !n.done)
    .filter((n) => !tag || n.tags.includes(tag))
    .filter((n) => !q || `${n.title} ${n.body} ${n.tags.join(" ")}`.toLowerCase().includes(q))
    .sort((a, b) => Number(a.done) - Number(b.done) || b.id - a.id);
}

function renderTagFilter() {
  const tags = [...new Set([...state.notes.values()].flatMap((n) => n.tags))].sort();
  const root = $("#tag-filter");
  root.replaceChildren(
    ...tags.map((t) =>
      h("span", {
        class: "tag",
        style: state.filter.tag === t ? "background:var(--accent);color:#0b1020" : null,
        title: "Filter by tag",
        onclick: () => { state.filter.tag = state.filter.tag === t ? null : t; renderNotes(); },
      }, `#${t}`)
    )
  );
}

function noteCard(note) {
  if (state.editing === note.id) return noteEditor(note);
  const flash = state.flash.get(note.id);
  const card = h("article", { class: `note ${note.done ? "done" : ""} ${flash ? `flash-${flash}` : ""}` },
    h("div", { class: "note-actions" },
      h("button", { class: "btn small ghost", title: "Edit", onclick: () => { state.editing = note.id; renderNotes(); } }, "✎"),
      h("button", { class: "btn small ghost danger", title: "Delete", onclick: () => deleteNote(note) }, "🗑")
    ),
    h("div", { class: "note-head" },
      h("input", { type: "checkbox", checked: note.done, title: "Mark done", onchange: (e) => toggleDone(note, e.target.checked) }),
      h("div", { class: "note-title" }, note.title)
    ),
    note.body ? h("div", { class: "note-body" }, note.body) : h("div", { style: "height:8px" }),
    h("div", { class: "note-foot" },
      h("span", { class: `badge ${note.source}` }, note.source.toUpperCase()),
      note.tags.map((t) => h("span", { class: "tag", onclick: () => { state.filter.tag = t; renderNotes(); } }, `#${t}`)),
      h("span", { class: "spacer" }),
      h("span", { title: note.updated_at }, `#${note.id} · ${ago(note.updated_at)}`)
    )
  );
  return card;
}

function noteEditor(note) {
  const title = h("input", { type: "text", value: note.title, maxlength: "200" });
  const body = h("textarea", null);
  body.value = note.body;
  const tags = h("input", { type: "text", value: note.tags.join(", ") });
  const save = async () => {
    try {
      const updated = await api("PATCH", `/api/notes/${note.id}`, {
        title: title.value, body: body.value, tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean),
      });
      state.notes.set(updated.id, updated);
      state.editing = null;
      renderNotes();
    } catch (err) { toast(err.message, "error", "⚠️"); }
  };
  setTimeout(() => title.focus(), 0);
  return h("article", { class: "note" },
    h("label", null, "Title"), title,
    h("label", null, "Details"), body,
    h("label", null, "Tags"), tags,
    h("div", { class: "row", style: "margin-top:10px" },
      h("button", { class: "btn primary small", onclick: save }, "Save"),
      h("button", { class: "btn small", onclick: () => { state.editing = null; renderNotes(); } }, "Cancel")
    )
  );
}

function renderNotes() {
  const notes = visibleNotes();
  const root = $("#notes");
  root.replaceChildren(
    ...(notes.length
      ? notes.map(noteCard)
      : [h("div", { class: "empty", style: "grid-column:1/-1" },
          state.notes.size ? "No notes match the current filter." : "No notes yet. Add one here, or ask your AI agent to call add_note.")])
  );
  $("#tab-notes-count").textContent = state.notes.size;
  $("#stat-notes").textContent = state.notes.size;
  renderTagFilter();
  state.flash.clear();
}

async function toggleDone(note, done) {
  try {
    const updated = await api("PATCH", `/api/notes/${note.id}`, { done });
    state.notes.set(updated.id, updated);
    renderNotes();
  } catch (err) { toast(err.message, "error", "⚠️"); }
}

async function deleteNote(note) {
  if (!confirm(`Delete "${note.title}"?`)) return;
  try {
    await api("DELETE", `/api/notes/${note.id}`);
    state.notes.delete(note.id);
    renderNotes();
  } catch (err) { toast(err.message, "error", "⚠️"); }
}

$("#note-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const title = $("#note-title");
  try {
    const note = await api("POST", "/api/notes", {
      title: title.value,
      body: $("#note-body").value,
      tags: $("#note-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
    });
    state.notes.set(note.id, note);
    state.flash.set(note.id, "web");
    e.target.reset();
    title.focus();
    renderNotes();
  } catch (err) { toast(err.message, "error", "⚠️"); }
});
$("#note-form").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#note-form").requestSubmit();
});
$("#note-search").addEventListener("input", (e) => { state.filter.text = e.target.value; renderNotes(); });
$("#show-done").addEventListener("change", (e) => { state.filter.showDone = e.target.checked; renderNotes(); });

// ------------------------------------------------------------------------------------------
// Live events (Server-Sent Events) + activity feed
// ------------------------------------------------------------------------------------------
const EVENT_TEXT = { "note.created": "created", "note.updated": "updated", "note.deleted": "deleted" };

function setLive(on) {
  $("#live-dot").classList.toggle("on", on);
  $("#live-text").textContent = on ? "Live" : "Reconnecting…";
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.addEventListener("ready", () => { setLive(true); loadNotes().catch(() => {}); refreshInfo(); });
  es.onerror = () => setLive(false);
  es.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (err) { console.warn("bad event", err); }
  };
}

function handleEvent(evt) {
  const note = evt.note;
  if (evt.type === "note.deleted") state.notes.delete(note.id);
  else { state.notes.set(note.id, note); state.flash.set(note.id, evt.source); }
  if (state.editing !== note.id) renderNotes();

  state.activity.unshift(evt);
  renderActivity();

  if (evt.source === "mcp") {
    toast(h("span", null, "AI agent ", h("b", null, EVENT_TEXT[evt.type] || evt.type), " “", note.title, "” via MCP"), "mcp", "🤖");
  }
}

function eventRow(evt) {
  const verb = EVENT_TEXT[evt.type] || evt.type;
  const who = evt.source === "mcp" ? "MCP client" : "Web UI";
  return h("div", { class: "event" },
    h("div", { class: `icon ${evt.source}` }, evt.source === "mcp" ? "🤖" : "🌐"),
    h("div", null,
      h("div", null, h("b", null, who), ` ${verb} note #${evt.note.id} `, h("span", { class: "muted" }, "“"), evt.note.title, h("span", { class: "muted" }, "”")),
      evt.client ? h("div", { class: "client" }, evt.client) : null
    ),
    h("time", { title: evt.at }, clock(evt.at))
  );
}

function renderActivity() {
  const root = $("#feed");
  root.replaceChildren(
    ...(state.activity.length
      ? state.activity.slice(0, 200).map(eventRow)
      : [h("div", { class: "empty" }, "No activity yet. Changes from the browser and from MCP clients will stream in here.")])
  );
  $("#tab-activity-count").textContent = state.activity.length;
}

$("#clear-activity").addEventListener("click", () => { state.activity = []; renderActivity(); });

// ------------------------------------------------------------------------------------------
// MCP client (Streamable HTTP, JSON-RPC 2.0) used by the playground
// ------------------------------------------------------------------------------------------
const Mcp = {
  sessionId: null,
  protocolVersion: "2025-11-25",
  nextId: 1,
  initialized: false,

  headers() {
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      "MCP-Protocol-Version": this.protocolVersion,
    };
    if (this.sessionId) headers["Mcp-Session-Id"] = this.sessionId;
    return headers;
  },

  async post(message) {
    const res = await fetch("/mcp", { method: "POST", headers: this.headers(), body: JSON.stringify(message) });
    const sid = res.headers.get("Mcp-Session-Id");
    if (sid) this.sessionId = sid;
    if (res.status === 202) return null;
    const text = await res.text();
    if ((res.headers.get("Content-Type") || "").includes("text/event-stream")) {
      const dataLine = text.split("\n").find((l) => l.startsWith("data:"));
      return dataLine ? JSON.parse(dataLine.slice(5)) : null;
    }
    return JSON.parse(text);
  },

  async request(method, params) {
    if (!this.initialized && method !== "initialize") await this.initialize();
    const message = { jsonrpc: "2.0", id: this.nextId++, method };
    if (params !== undefined) message.params = params;
    const response = await this.post(message);
    if (response && response.error) {
      const err = new Error(response.error.message);
      err.exchange = { request: message, response };
      throw err;
    }
    return { result: response.result, exchange: { request: message, response } };
  },

  async initialize() {
    this.sessionId = null;
    this.initialized = false;
    const { result } = await this.request("initialize", {
      protocolVersion: "2025-11-25",
      capabilities: {},
      clientInfo: { name: "web-playground", version: "1.0.0" },
    });
    this.protocolVersion = result.protocolVersion;
    this.initialized = true;
    await this.post({ jsonrpc: "2.0", method: "notifications/initialized" });
    $("#pg-session").textContent = `Session ${this.sessionId ? this.sessionId.slice(0, 8) + "…" : "(stateless)"} · protocol ${this.protocolVersion}`;
    return result;
  },
};

// ------------------------------------------------------------------------------------------
// Playground UI
// ------------------------------------------------------------------------------------------
const Playground = {
  kind: "tools",
  loaded: false,
  data: { tools: [], resources: [], templates: [], prompts: [] },
  selected: null,

  async ensureLoaded() {
    if (!this.loaded) await this.reload();
  },

  async reload() {
    try {
      await Mcp.initialize();
      const [tools, resources, templates, prompts] = await Promise.all([
        Mcp.request("tools/list"), Mcp.request("resources/list"),
        Mcp.request("resources/templates/list"), Mcp.request("prompts/list"),
      ]);
      this.data = {
        tools: tools.result.tools, resources: resources.result.resources,
        templates: templates.result.resourceTemplates, prompts: prompts.result.prompts,
      };
      this.loaded = true;
      this.renderList();
    } catch (err) {
      toast(`MCP error: ${err.message}`, "error", "⚠️");
    }
  },

  setKind(kind) {
    this.kind = kind;
    this.selected = null;
    $$("#pg-seg button").forEach((b) => b.classList.toggle("active", b.dataset.kind === kind));
    $("#pg-detail").replaceChildren(h("p", { class: "muted" }, "Pick an item on the left."));
    this.renderList();
  },

  renderList() {
    const titles = { tools: "Tools", resources: "Resources & templates", prompts: "Prompts" };
    $("#pg-list-title").textContent = titles[this.kind];
    let items = [];
    if (this.kind === "tools") {
      items = this.data.tools.map((t) => ({
        key: `tool:${t.name}`, name: t.name, desc: t.description, hints: toolHints(t), open: () => this.openTool(t),
      }));
    } else if (this.kind === "resources") {
      items = [
        ...this.data.resources.map((r) => ({ key: `res:${r.uri}`, name: r.uri, desc: r.description || r.name, hints: [r.mimeType], open: () => this.openResource(r) })),
        ...this.data.templates.map((t) => ({ key: `tpl:${t.uriTemplate}`, name: t.uriTemplate, desc: t.description || t.name, hints: ["template", t.mimeType], open: () => this.openTemplate(t) })),
      ];
    } else {
      items = this.data.prompts.map((p) => ({ key: `prompt:${p.name}`, name: p.name, desc: p.description, hints: [], open: () => this.openPrompt(p) }));
    }
    $("#pg-list").replaceChildren(
      ...items.map((item) =>
        h("button", {
          class: `item ${this.selected === item.key ? "active" : ""}`,
          onclick: () => { this.selected = item.key; this.renderList(); item.open(); },
        },
          h("div", { class: "name" }, item.name),
          item.desc ? h("div", { class: "desc" }, item.desc) : null,
          item.hints.length ? h("div", { class: "hints" }, item.hints.map((x) => (x instanceof Node ? x : h("span", { class: "hint" }, x)))) : null
        )
      )
    );
  },

  openTool(tool) {
    const schema = tool.inputSchema || { properties: {} };
    const required = new Set(schema.required || []);
    const fields = Object.entries(schema.properties || {}).map(([name, prop]) => fieldFor(name, prop, required.has(name)));
    const resultBox = h("div", { class: "result" });
    const run = async () => {
      const args = {};
      try {
        for (const f of fields) {
          const value = f.read();
          if (value !== undefined) args[f.name] = value;
        }
      } catch (err) { toast(err.message, "error", "⚠️"); return; }
      runBtn.disabled = true;
      try {
        const { result, exchange } = await Mcp.request("tools/call", { name: tool.name, arguments: args });
        renderToolResult(resultBox, result, exchange);
      } catch (err) {
        renderError(resultBox, err);
      } finally { runBtn.disabled = false; }
    };
    const runBtn = h("button", { class: "btn primary", onclick: run }, "▶ Call tool");
    $("#pg-detail").replaceChildren(
      h("h2", null, tool.title || tool.name),
      h("p", { class: "muted", style: "margin-top:-6px" }, tool.description || ""),
      h("div", { class: "hints" }, toolHints(tool)),
      fields.length ? fields.map((f) => f.el) : h("p", { class: "muted" }, "This tool takes no arguments."),
      h("div", { class: "row", style: "margin-top:14px" }, runBtn,
        tool.outputSchema ? h("span", { class: "muted", style: "font-size:12px" }, "Returns structured output") : null),
      tool.outputSchema ? h("details", null, h("summary", null, "Output schema"), h("pre", { class: "json" }, pretty(tool.outputSchema))) : null,
      resultBox
    );
  },

  async openResource(resource) {
    const box = h("div", { class: "result" });
    $("#pg-detail").replaceChildren(h("h2", null, resource.name || resource.uri), h("p", { class: "muted" }, resource.uri), box);
    await readResource(box, resource.uri);
  },

  openTemplate(template) {
    const vars = [...template.uriTemplate.matchAll(/\{(\w+)\}/g)].map((m) => m[1]);
    const inputs = vars.map((v) => ({ v, el: h("input", { type: "text", placeholder: v, value: v === "id" ? "1" : "" }) }));
    const box = h("div", { class: "result" });
    const go = () => {
      let uri = template.uriTemplate;
      for (const { v, el } of inputs) uri = uri.replace(`{${v}}`, encodeURIComponent(el.value));
      readResource(box, uri);
    };
    $("#pg-detail").replaceChildren(
      h("h2", null, template.name || template.uriTemplate),
      h("p", { class: "muted", style: "margin-top:-6px" }, template.description || ""),
      inputs.map(({ v, el }) => [h("label", null, v), el]),
      h("div", { class: "row", style: "margin-top:14px" }, h("button", { class: "btn primary", onclick: go }, "Read resource")),
      box
    );
  },

  openPrompt(prompt) {
    const args = (prompt.arguments || []).map((a) => ({ a, el: h("input", { type: "text", placeholder: a.description || a.name }) }));
    const box = h("div", { class: "result" });
    const go = async () => {
      const values = {};
      for (const { a, el } of args) if (el.value) values[a.name] = el.value;
      try {
        const { result, exchange } = await Mcp.request("prompts/get", { name: prompt.name, arguments: values });
        box.classList.remove("error");
        box.replaceChildren(
          ...result.messages.map((m) => h("div", { class: "content-block" }, h("div", { class: "kind" }, m.role), h("div", { style: "white-space:pre-wrap" }, m.content.text ?? pretty(m.content)))),
          rawExchange(exchange)
        );
      } catch (err) { renderError(box, err); }
    };
    $("#pg-detail").replaceChildren(
      h("h2", null, prompt.title || prompt.name),
      h("p", { class: "muted", style: "margin-top:-6px" }, prompt.description || ""),
      args.map(({ a, el }) => [h("label", null, `${a.name}${a.required ? " *" : ""}`), el]),
      h("div", { class: "row", style: "margin-top:14px" }, h("button", { class: "btn primary", onclick: go }, "Get prompt")),
      box
    );
  },
};

function toolHints(tool) {
  const a = tool.annotations || {};
  const hints = [];
  if (a.readOnlyHint) hints.push(h("span", { class: "hint ro" }, "read-only"));
  if (a.destructiveHint) hints.push(h("span", { class: "hint ds" }, "destructive"));
  if (a.idempotentHint) hints.push(h("span", { class: "hint" }, "idempotent"));
  if (tool.outputSchema) hints.push(h("span", { class: "hint" }, "structured"));
  return hints;
}

function fieldFor(name, prop, required) {
  const type = prop.type || "string";
  const labelText = `${name}${required ? " *" : ""}`;
  const help = prop.description ? h("span", { class: "muted" }, ` — ${prop.description}`) : null;
  let input;
  let read;
  if (prop.enum) {
    input = h("select", null, required ? null : h("option", { value: "" }, "(default)"), prop.enum.map((v) => h("option", { value: String(v) }, String(v))));
    if (prop.default !== undefined) input.value = String(prop.default);
    read = () => (input.value === "" ? undefined : input.value);
  } else if (type === "boolean") {
    input = h("select", null, h("option", { value: "" }, prop.default !== undefined ? `(default: ${prop.default})` : "(default)"),
      h("option", { value: "true" }, "true"), h("option", { value: "false" }, "false"));
    read = () => (input.value === "" ? undefined : input.value === "true");
  } else if (type === "integer" || type === "number") {
    input = h("input", { type: "number", step: type === "integer" ? "1" : "any", placeholder: prop.default !== undefined ? `default: ${prop.default}` : "" });
    read = () => {
      if (input.value === "") { if (required) throw new Error(`'${name}' is required`); return undefined; }
      return Number(input.value);
    };
  } else if (type === "array" && (!prop.items || ["string", "integer", "number"].includes(prop.items.type))) {
    input = h("input", { type: "text", placeholder: "comma separated values" });
    read = () => {
      if (!input.value.trim()) return required ? [] : undefined;
      const parts = input.value.split(",").map((s) => s.trim()).filter(Boolean);
      return prop.items && prop.items.type !== "string" ? parts.map(Number) : parts;
    };
  } else if (type === "array" || type === "object") {
    input = h("textarea", { placeholder: type === "array" ? "[ ... ] JSON" : "{ ... } JSON" });
    read = () => {
      if (!input.value.trim()) return undefined;
      try { return JSON.parse(input.value); } catch { throw new Error(`'${name}' must be valid JSON`); }
    };
  } else {
    input = name === "body" ? h("textarea", null) : h("input", { type: "text", placeholder: prop.default !== undefined ? `default: ${prop.default}` : "" });
    read = () => {
      if (input.value === "") { if (required) throw new Error(`'${name}' is required`); return undefined; }
      return input.value;
    };
  }
  return { name, el: h("div", null, h("label", null, h("b", null, labelText), help), input), read };
}

function rawExchange(exchange) {
  return h("details", null, h("summary", null, "Raw JSON-RPC exchange"),
    h("pre", { class: "json" }, `→ request\n${pretty(exchange.request)}\n\n← response\n${pretty(exchange.response)}`));
}

function renderError(box, err) {
  box.classList.add("error");
  box.replaceChildren(h("div", { class: "content-block" }, h("div", { class: "kind" }, "JSON-RPC error"), err.message),
    err.exchange ? rawExchange(err.exchange) : null);
}

function contentBlock(c) {
  const kind = h("div", { class: "kind" }, c.type);
  switch (c.type) {
    case "text": return h("div", { class: "content-block" }, kind, h("div", { style: "white-space:pre-wrap" }, c.text));
    case "image": return h("div", { class: "content-block" }, kind, h("img", { src: `data:${c.mimeType};base64,${c.data}`, alt: "image content" }));
    case "audio": return h("div", { class: "content-block" }, kind, h("audio", { controls: true, src: `data:${c.mimeType};base64,${c.data}` }));
    case "resource_link": return h("div", { class: "content-block" }, kind,
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); Playground.setKind("resources"); const box = h("div", { class: "result" }); $("#pg-detail").replaceChildren(h("h2", null, c.uri), box); readResource(box, c.uri); } }, c.uri),
      c.description ? h("div", { class: "muted" }, c.description) : null);
    case "resource": return h("div", { class: "content-block" }, kind, h("div", { class: "muted" }, c.resource.uri), h("div", { style: "white-space:pre-wrap" }, c.resource.text ?? "(binary)"));
    default: return h("div", { class: "content-block" }, kind, h("pre", { class: "json" }, pretty(c)));
  }
}

function renderToolResult(box, result, exchange) {
  box.classList.toggle("error", !!result.isError);
  box.replaceChildren(
    h("h3", null, result.isError ? "Tool error" : "Result"),
    ...(result.content || []).map(contentBlock),
    result.structuredContent !== undefined
      ? h("div", null, h("h3", null, "structuredContent"), h("pre", { class: "json" }, pretty(result.structuredContent)))
      : null,
    rawExchange(exchange)
  );
}

async function readResource(box, uri) {
  try {
    const { result, exchange } = await Mcp.request("resources/read", { uri });
    box.classList.remove("error");
    box.replaceChildren(
      ...result.contents.map((c) => {
        let body = c.text;
        if (c.text !== undefined && (c.mimeType || "").includes("json")) {
          try { body = pretty(JSON.parse(c.text)); } catch { /* keep raw */ }
        }
        return h("div", { class: "content-block" }, h("div", { class: "kind" }, `${c.mimeType} · ${c.uri}`),
          c.blob !== undefined ? h("div", { class: "muted" }, `binary (${c.blob.length} base64 chars)`) : h("pre", { class: "json" }, body));
      }),
      rawExchange(exchange)
    );
  } catch (err) { renderError(box, err); }
}

$$("#pg-seg button").forEach((b) => b.addEventListener("click", () => Playground.setKind(b.dataset.kind)));
$("#pg-reconnect").addEventListener("click", () => Playground.reload());

// ------------------------------------------------------------------------------------------
// Boot
// ------------------------------------------------------------------------------------------
(async function boot() {
  await refreshInfo();
  try { await loadNotes(); } catch (err) { toast(`Failed to load notes: ${err.message}`, "error", "⚠️"); }
  try { state.activity = await api("GET", "/api/activity"); } catch { state.activity = []; }
  renderActivity();
  connectEvents();
  setInterval(refreshInfo, 5000);
  setInterval(() => { if (!state.editing) renderNotes(); }, 60000); // refresh relative timestamps
  const initial = location.hash.replace("#", "");
  if (["notes", "playground", "activity", "connect"].includes(initial)) showView(initial);
})();
