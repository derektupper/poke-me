import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn


@dataclass
class Request:
    id: str
    question: str
    context: str | None = None
    agent: str | None = None
    task: str | None = None
    status: str = "pending"
    answer: str | None = None
    created_at: float = field(default_factory=time.time)
    answered_at: float | None = None


class RequestStore:
    """Thread-safe in-memory store for agent requests."""

    def __init__(self):
        self._requests: dict[str, Request] = {}
        self._lock = threading.Lock()

    def create(self, question: str, context: str | None = None,
               agent: str | None = None, task: str | None = None) -> Request:
        req = Request(
            id=uuid.uuid4().hex[:12],
            question=question,
            context=context,
            agent=agent,
            task=task,
        )
        with self._lock:
            self._requests[req.id] = req
        return req

    def get(self, request_id: str) -> Request | None:
        with self._lock:
            return self._requests.get(request_id)

    def pending(self) -> list[Request]:
        with self._lock:
            return [r for r in self._requests.values() if r.status == "pending"]

    def answer(self, request_id: str, text: str) -> bool:
        with self._lock:
            req = self._requests.get(request_id)
            if not req or req.status != "pending":
                return False
            req.status = "answered"
            req.answer = text
            req.answered_at = time.time()
            return True

    def has_pending(self) -> bool:
        with self._lock:
            return any(r.status == "pending" for r in self._requests.values())


# Global store — shared across all handler instances
store = RequestStore()


class JsonMixin:
    """Helpers for JSON request/response handling."""

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


class RequestHandler(JsonMixin, BaseHTTPRequestHandler):
    """HTTP handler for the pokeme server."""

    def log_message(self, format, *args):
        # Suppress default stderr logging
        pass

    def do_GET(self):
        if self.path == "/":
            self._serve_ui()
        elif self.path == "/api/pending":
            reqs = store.pending()
            self.send_json([asdict(r) for r in reqs])
        elif self.path.startswith("/api/status/"):
            request_id = self.path.split("/")[-1]
            req = store.get(request_id)
            if req:
                self.send_json(asdict(req))
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        elif self.path == "/api/health":
            self.send_json({"status": "ok"})
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path == "/api/ask":
            data = self.read_json()
            if not data or "question" not in data:
                self.send_json({"error": "missing question"}, HTTPStatus.BAD_REQUEST)
                return
            req = store.create(
                question=data["question"],
                context=data.get("context"),
                agent=data.get("agent"),
                task=data.get("task"),
            )
            self.send_json({"id": req.id})

        elif self.path == "/api/answer":
            data = self.read_json()
            if not data or "id" not in data or "answer" not in data:
                self.send_json({"error": "missing id or answer"}, HTTPStatus.BAD_REQUEST)
                return
            ok = store.answer(data["id"], data["answer"])
            if ok:
                self.send_json({"status": "ok"})
            else:
                self.send_json({"error": "request not found or already answered"}, HTTPStatus.NOT_FOUND)
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _serve_ui(self):
        body = WEB_UI_HTML.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_server(port: int = 9131, idle_timeout: int = 600):
    """Start the pokeme server. Blocks until idle timeout or killed."""
    server = ThreadedHTTPServer(("127.0.0.1", port), RequestHandler)

    def watchdog():
        last_active = time.time()
        while True:
            time.sleep(30)
            if store.has_pending():
                last_active = time.time()
            elif time.time() - last_active > idle_timeout:
                server.shutdown()
                return

    t = threading.Thread(target=watchdog, daemon=True)
    t.start()
    server.serve_forever()


# ---------------------------------------------------------------------------
# Embedded Web UI
# ---------------------------------------------------------------------------

WEB_UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pokeme</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #0f0f0f;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 2rem;
  }

  header {
    text-align: center;
    margin-bottom: 2rem;
  }
  header h1 {
    font-size: 1.8rem;
    font-weight: 700;
    color: #ff6b35;
    letter-spacing: -0.5px;
  }
  header p {
    color: #888;
    margin-top: 0.3rem;
    font-size: 0.9rem;
  }

  #empty {
    text-align: center;
    color: #555;
    margin-top: 4rem;
    font-size: 1.1rem;
  }

  .card {
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-left: 4px solid #ff6b35;
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 1rem;
    max-width: 640px;
    margin-left: auto;
    margin-right: auto;
    transition: border-color 0.2s;
  }
  .card:hover { border-color: #444; }

  .card .meta {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
    margin-bottom: 0.75rem;
  }
  .card .meta span {
    font-size: 0.75rem;
    background: #252525;
    color: #aaa;
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
  }
  .card .meta .agent-tag { color: #ff6b35; }

  .card .question {
    font-size: 1.15rem;
    font-weight: 600;
    margin-bottom: 0.5rem;
    line-height: 1.4;
  }

  .card .context {
    color: #888;
    font-size: 0.9rem;
    margin-bottom: 1rem;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  .card .answer-form {
    display: flex;
    gap: 0.5rem;
  }
  .card textarea {
    flex: 1;
    background: #111;
    border: 1px solid #333;
    color: #e0e0e0;
    border-radius: 6px;
    padding: 0.6rem 0.8rem;
    font-size: 0.95rem;
    font-family: inherit;
    resize: vertical;
    min-height: 42px;
    max-height: 200px;
  }
  .card textarea:focus { outline: none; border-color: #ff6b35; }

  .card button {
    background: #ff6b35;
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 0 1.2rem;
    font-size: 0.9rem;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.15s;
  }
  .card button:hover { background: #e85d2a; }
  .card button:disabled { background: #444; cursor: default; }

  .answered {
    border-left-color: #2ea043;
    opacity: 0.5;
  }
  .answered .answer-form { display: none; }
  .answered .answer-text {
    color: #2ea043;
    font-size: 0.9rem;
    margin-top: 0.5rem;
  }
</style>
</head>
<body>

<header>
  <h1>pokeme</h1>
  <p>Your agents need help</p>
</header>

<div id="cards"></div>
<div id="empty">No pending requests. Agents will appear here when they need input.</div>

<script>
const cardsEl = document.getElementById("cards");
const emptyEl = document.getElementById("empty");
const answered = new Map(); // id -> answer text, to keep showing briefly

async function fetchPending() {
  try {
    const res = await fetch("/api/pending");
    return await res.json();
  } catch { return []; }
}

function timeAgo(ts) {
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}

function renderCard(req) {
  const div = document.createElement("div");
  div.className = "card";
  div.dataset.id = req.id;

  let meta = `<span>${timeAgo(req.created_at)}</span>`;
  if (req.agent) meta += `<span class="agent-tag">${esc(req.agent)}</span>`;
  if (req.task) meta += `<span>${esc(req.task)}</span>`;

  let contextHtml = "";
  if (req.context) {
    contextHtml = `<div class="context">${esc(req.context)}</div>`;
  }

  div.innerHTML = `
    <div class="meta">${meta}</div>
    <div class="question">${esc(req.question)}</div>
    ${contextHtml}
    <div class="answer-form">
      <textarea rows="1" placeholder="Type your answer..." onkeydown="handleKey(event, '${req.id}')"></textarea>
      <button onclick="submitAnswer('${req.id}')">Send</button>
    </div>
  `;
  return div;
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

async function submitAnswer(id) {
  const card = document.querySelector(`[data-id="${id}"]`);
  const textarea = card.querySelector("textarea");
  const btn = card.querySelector("button");
  const text = textarea.value.trim();
  if (!text) return;

  btn.disabled = true;
  btn.textContent = "Sending...";

  try {
    await fetch("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, answer: text }),
    });
    answered.set(id, text);
    card.classList.add("answered");
    card.querySelector(".answer-form").innerHTML = `<div class="answer-text">Answered: ${esc(text)}</div>`;
    setTimeout(() => { answered.delete(id); }, 5000);
  } catch {
    btn.disabled = false;
    btn.textContent = "Send";
  }
}

function handleKey(e, id) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submitAnswer(id);
  }
}

async function poll() {
  const pending = await fetchPending();
  const currentIds = new Set(pending.map(r => r.id));

  // Remove cards that are no longer pending (and not recently answered)
  for (const card of [...cardsEl.children]) {
    const id = card.dataset.id;
    if (!currentIds.has(id) && !answered.has(id)) {
      card.remove();
    }
  }

  // Add new cards
  const existingIds = new Set([...cardsEl.children].map(c => c.dataset.id));
  for (const req of pending) {
    if (!existingIds.has(req.id) && !answered.has(req.id)) {
      cardsEl.prepend(renderCard(req));
      // Focus the new card's textarea
      const ta = cardsEl.firstChild.querySelector("textarea");
      if (ta) ta.focus();
    }
  }

  emptyEl.style.display = (cardsEl.children.length === 0) ? "block" : "none";
}

poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--port", type=int, default=9131)
    _a = _p.parse_args()
    run_server(port=_a.port)
