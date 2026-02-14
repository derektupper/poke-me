import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn


# --- Limits ---
MAX_REQUEST_BODY = 64 * 1024  # 64 KB max POST body
MAX_QUESTION_LEN = 2000
MAX_CONTEXT_LEN = 5000
MAX_AGENT_LEN = 100
MAX_TASK_LEN = 200
MAX_ANSWER_LEN = 10000
MAX_PENDING_REQUESTS = 100
ANSWERED_TTL = 300  # evict answered requests after 5 minutes

# Only hex chars allowed in request IDs (defense-in-depth)
VALID_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


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
               agent: str | None = None, task: str | None = None) -> Request | None:
        req = Request(
            id=uuid.uuid4().hex[:12],
            question=_truncate(question, MAX_QUESTION_LEN),
            context=_truncate(context, MAX_CONTEXT_LEN),
            agent=_truncate(agent, MAX_AGENT_LEN),
            task=_truncate(task, MAX_TASK_LEN),
        )
        with self._lock:
            self._evict_stale()
            if sum(1 for r in self._requests.values() if r.status == "pending") >= MAX_PENDING_REQUESTS:
                return None  # too many pending
            self._requests[req.id] = req
        return req

    def get(self, request_id: str) -> Request | None:
        if not VALID_ID_RE.match(request_id):
            return None
        with self._lock:
            return self._requests.get(request_id)

    def pending(self) -> list[Request]:
        with self._lock:
            return [r for r in self._requests.values() if r.status == "pending"]

    def answer(self, request_id: str, text: str) -> bool:
        if not VALID_ID_RE.match(request_id):
            return False
        with self._lock:
            req = self._requests.get(request_id)
            if not req or req.status != "pending":
                return False
            req.status = "answered"
            req.answer = _truncate(text, MAX_ANSWER_LEN)
            req.answered_at = time.time()
            return True

    def has_pending(self) -> bool:
        with self._lock:
            return any(r.status == "pending" for r in self._requests.values())

    def _evict_stale(self) -> None:
        """Remove answered requests older than ANSWERED_TTL. Call under lock."""
        now = time.time()
        stale = [
            rid for rid, r in self._requests.items()
            if r.status == "answered" and r.answered_at is not None and now - r.answered_at > ANSWERED_TTL
        ]
        for rid in stale:
            del self._requests[rid]


# Global store — shared across all handler instances
store = RequestStore()

LOCALHOST_ORIGINS = frozenset({"http://127.0.0.1:9131", "http://localhost:9131"})


class JsonMixin:
    """Helpers for JSON request/response handling."""

    def _cors_origin(self) -> str:
        """Return the Origin header only if it's a localhost origin."""
        origin = self.headers.get("Origin", "")
        if origin in LOCALHOST_ORIGINS:
            return origin
        # Also accept any localhost:<port> variant
        if origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
            return origin
        return ""

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return None
        if length > MAX_REQUEST_BODY:
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
            if req is None:
                self.send_json({"error": "too many pending requests"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
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
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
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
    # Update the localhost origins set to include the actual port
    global LOCALHOST_ORIGINS
    LOCALHOST_ORIGINS = frozenset({
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    })

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
    background: #0a0a0b;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 2rem;
  }

  /* --- Logo --- */
  .logo {
    text-align: center;
    margin-bottom: 2.5rem;
  }
  .logo svg { display: block; margin: 0 auto; }

  /* --- Logo pulse animation on overlap --- */
  @keyframes overlap-pulse {
    0%   { opacity: 0.6; }
    50%  { opacity: 1; }
    100% { opacity: 0.6; }
  }
  .overlap-glow { animation: overlap-pulse 3s ease-in-out infinite; }

  /* --- Empty state --- */
  #empty {
    text-align: center;
    color: #444;
    margin-top: 4rem;
    font-size: 1rem;
  }
  #empty .empty-icon {
    font-size: 2.5rem;
    margin-bottom: 0.5rem;
    opacity: 0.3;
  }

  /* --- Card --- */
  .card {
    background: #131316;
    border: 1px solid #1e1e24;
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 1rem;
    max-width: 640px;
    margin-left: auto;
    margin-right: auto;
    transition: border-color 0.2s, box-shadow 0.2s;
    position: relative;
    overflow: hidden;
  }
  .card::before {
    content: "";
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 4px;
    border-radius: 4px 0 0 4px;
  }
  .card:hover { border-color: #2a2a33; box-shadow: 0 4px 24px rgba(0,0,0,0.3); }

  /* --- Card header with avatar --- */
  .card-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.85rem;
  }

  .agent-avatar {
    width: 44px;
    height: 44px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.1rem;
    flex-shrink: 0;
    position: relative;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
  }
  .agent-avatar .avatar-icon {
    filter: drop-shadow(0 1px 2px rgba(0,0,0,0.3));
  }

  @keyframes status-blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .agent-avatar .status-dot {
    position: absolute;
    bottom: -2px;
    right: -2px;
    width: 12px;
    height: 12px;
    background: #2ea043;
    border-radius: 50%;
    border: 2px solid #131316;
    animation: status-blink 2s ease-in-out infinite;
  }

  .card-header-text {
    flex: 1;
    min-width: 0;
  }
  .card-header-text .agent-name {
    font-weight: 600;
    font-size: 0.9rem;
    color: #e0e0e0;
  }
  .card-header-text .task-label {
    font-size: 0.78rem;
    color: #666;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .card-header .time-badge {
    font-size: 0.7rem;
    color: #555;
    background: #1a1a1f;
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  /* --- Question & context --- */
  .card .question {
    font-size: 1.1rem;
    font-weight: 600;
    margin-bottom: 0.5rem;
    line-height: 1.45;
    color: #f0f0f0;
  }
  .card .context {
    color: #777;
    font-size: 0.85rem;
    margin-bottom: 1rem;
    line-height: 1.55;
    white-space: pre-wrap;
    border-left: 2px solid #222;
    padding-left: 0.75rem;
  }

  /* --- Answer form --- */
  .card .answer-form {
    display: flex;
    gap: 0.5rem;
    align-items: flex-end;
  }
  .card textarea {
    flex: 1;
    background: #0e0e11;
    border: 1px solid #252530;
    color: #e0e0e0;
    border-radius: 8px;
    padding: 0.6rem 0.8rem;
    font-size: 0.92rem;
    font-family: inherit;
    resize: vertical;
    min-height: 42px;
    max-height: 200px;
    transition: border-color 0.15s;
  }
  .card textarea:focus { outline: none; border-color: #ff6b35; box-shadow: 0 0 0 3px rgba(255,107,53,0.1); }

  .card .send-btn {
    background: #ff6b35;
    color: #fff;
    border: none;
    border-radius: 8px;
    width: 42px;
    height: 42px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: background 0.15s, transform 0.1s;
    flex-shrink: 0;
  }
  .card .send-btn:hover { background: #e85d2a; transform: scale(1.05); }
  .card .send-btn:active { transform: scale(0.95); }
  .card .send-btn:disabled { background: #333; cursor: default; transform: none; }
  .card .send-btn svg { width: 20px; height: 20px; }

  /* --- Answered state --- */
  .answered { opacity: 0.45; }
  .answered::before { background: #2ea043 !important; }
  .answered .answer-form { display: none; }
  .answered .answer-text {
    color: #2ea043;
    font-size: 0.85rem;
    margin-top: 0.5rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  /* --- Slide-in animation --- */
  @keyframes card-enter {
    from { opacity: 0; transform: translateY(-12px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .card { animation: card-enter 0.3s ease-out; }

  /* --- Notification banner --- */
  .notif-banner {
    max-width: 640px;
    margin: 0 auto 1.5rem;
    background: #1a1620;
    border: 1px solid #2a2035;
    border-radius: 10px;
    padding: 0.75rem 1rem;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    font-size: 0.82rem;
    color: #999;
    animation: card-enter 0.3s ease-out;
  }
  .notif-banner .notif-icon { font-size: 1.1rem; flex-shrink: 0; }
  .notif-banner button {
    background: #ff6b35;
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 0.35rem 0.75rem;
    font-size: 0.78rem;
    font-weight: 600;
    cursor: pointer;
    margin-left: auto;
    flex-shrink: 0;
    transition: background 0.15s;
  }
  .notif-banner button:hover { background: #e85d2a; }
</style>
</head>
<body>

<!-- Logo SVG — Venn: agent + human, overlap = conversation -->
<div class="logo">
  <svg width="260" height="68" viewBox="0 0 260 68" fill="none" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="g1" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="#ff6b35"/>
        <stop offset="100%" stop-color="#ff8c5a"/>
      </linearGradient>
      <linearGradient id="g2" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="#6366f1"/>
        <stop offset="100%" stop-color="#818cf8"/>
      </linearGradient>
      <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5">
        <stop offset="0%" stop-color="#ff6b35" stop-opacity="0.7"/>
        <stop offset="100%" stop-color="#ff6b35" stop-opacity="0"/>
      </radialGradient>
    </defs>

    <!-- Agent circle (left, indigo) — spread apart for less overlap -->
    <circle cx="26" cy="36" r="24" fill="url(#g2)" opacity="0.2"/>
    <circle cx="26" cy="36" r="24" stroke="#6366f1" stroke-width="1.5" fill="none" opacity="0.5"/>

    <!-- Human circle (right, orange) -->
    <circle cx="60" cy="36" r="24" fill="url(#g1)" opacity="0.2"/>
    <circle cx="60" cy="36" r="24" stroke="#ff6b35" stroke-width="1.5" fill="none" opacity="0.5"/>

    <!-- Overlap glow — the conversation spark (narrower sliver) -->
    <clipPath id="clip-left"><circle cx="26" cy="36" r="24"/></clipPath>
    <circle class="overlap-glow" cx="60" cy="36" r="24" fill="#ff6b35" opacity="0.5" clip-path="url(#clip-left)"/>

    <!-- Agent icon: robot face — scaled up, centered in left circle -->
    <g transform="translate(13, 22) scale(1.3)">
      <rect x="0" y="3" width="18" height="14" rx="3" fill="#6366f1" opacity="0.4"/>
      <rect x="0" y="3" width="18" height="14" rx="3" stroke="#818cf8" stroke-width="1.5" fill="none"/>
      <circle cx="5.5" cy="10" r="2.2" fill="#818cf8"/>
      <circle cx="12.5" cy="10" r="2.2" fill="#818cf8"/>
      <line x1="9" y1="-1" x2="9" y2="3" stroke="#818cf8" stroke-width="1.5" stroke-linecap="round"/>
      <circle cx="9" cy="-2" r="1.8" fill="#818cf8" opacity="0.8"/>
      <rect x="4" y="14.5" width="10" height="1.8" rx="0.9" fill="#818cf8" opacity="0.5"/>
    </g>

    <!-- Human icon: person bust — scaled up, centered in right circle -->
    <g transform="translate(48, 20) scale(1.3)">
      <circle cx="8" cy="5" r="5.5" fill="#ff6b35" opacity="0.35"/>
      <circle cx="8" cy="5" r="5.5" stroke="#ff8c5a" stroke-width="1.5" fill="none"/>
      <path d="M-1 22 C-1 15, 3 12, 8 12 C13 12, 17 15, 17 22" fill="#ff6b35" opacity="0.35"/>
      <path d="M-1 22 C-1 15, 3 12, 8 12 C13 12, 17 15, 17 22" stroke="#ff8c5a" stroke-width="1.5" fill="none"/>
    </g>

    <!-- "pokeme" text -->
    <text x="96" y="46" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
          font-size="32" font-weight="800" letter-spacing="-1" fill="#ffffff">poke<tspan fill="#ff6b35">me</tspan></text>
  </svg>
</div>

<div id="notif-banner" class="notif-banner" style="display:none">
  <span class="notif-icon">🔔</span>
  <span>Enable browser notifications so you know when agents need help — even when this tab is in the background.</span>
  <button id="notif-enable-btn">Enable</button>
</div>

<div id="cards"></div>
<div id="empty">
  <div class="empty-icon">
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#333" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="10"/>
      <path d="M8 14s1.5 2 4 2 4-2 4-2"/>
      <line x1="9" y1="9" x2="9.01" y2="9"/>
      <line x1="15" y1="9" x2="15.01" y2="9"/>
    </svg>
  </div>
  All quiet. Agents will appear here when they need input.
</div>

<script>
const cardsEl = document.getElementById("cards");
const emptyEl = document.getElementById("empty");
const answered = new Map();

// --- Browser Notification API ---
let notifPermission = Notification.permission;

async function requestNotifPermission() {
  if (!("Notification" in window)) return;
  if (notifPermission === "default") {
    notifPermission = await Notification.requestPermission();
  }
}

function fireNotification(agentName, question) {
  if (notifPermission !== "granted") return;
  try {
    const n = new Notification("pokeme — " + (agentName || "Agent") + " needs input", {
      body: question.length > 120 ? question.slice(0, 117) + "..." : question,
      icon: "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="24" cy="32" r="22" fill="%236366f1" opacity="0.3"/><circle cx="40" cy="32" r="22" fill="%23ff6b35" opacity="0.3"/></svg>'),
      tag: "pokeme",
      renotify: true,
    });
    n.onclick = () => { window.focus(); n.close(); };
  } catch {}
}

// Ask for permission right away
requestNotifPermission();

// Show enable-notifications banner if permission not yet granted
const notifBanner = document.getElementById("notif-banner");
const notifBtn = document.getElementById("notif-enable-btn");

function updateNotifBanner() {
  if (!("Notification" in window) || notifPermission === "granted") {
    notifBanner.style.display = "none";
  } else if (notifPermission === "denied") {
    notifBanner.style.display = "none";  // can't ask again, browser blocked it
  } else {
    notifBanner.style.display = "flex";
  }
}

notifBtn.addEventListener("click", async () => {
  notifPermission = await Notification.requestPermission();
  updateNotifBanner();
});

updateNotifBanner();

// --- Agent icon system ---
// Each agent gets a deterministic color + icon based on name hash
const AGENT_COLORS = [
  ["#6366f1","#818cf8"], // indigo
  ["#8b5cf6","#a78bfa"], // violet
  ["#ec4899","#f472b6"], // pink
  ["#f59e0b","#fbbf24"], // amber
  ["#10b981","#34d399"], // emerald
  ["#06b6d4","#22d3ee"], // cyan
  ["#f97316","#fb923c"], // orange
  ["#ef4444","#f87171"], // red
  ["#3b82f6","#60a5fa"], // blue
  ["#14b8a6","#2dd4bf"], // teal
];

// Rich filled SVG icons — more visual weight, gradient-friendly
const AGENT_ICONS = [
  // robot head — filled
  `<rect x="5" y="7" width="14" height="11" rx="2.5" fill="currentColor" opacity="0.3"/>
   <rect x="5" y="7" width="14" height="11" rx="2.5" stroke="currentColor" stroke-width="1.5" fill="none"/>
   <circle cx="9" cy="12.5" r="1.8" fill="currentColor"/>
   <circle cx="15" cy="12.5" r="1.8" fill="currentColor"/>
   <line x1="12" y1="3" x2="12" y2="7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
   <circle cx="12" cy="2.5" r="1.5" fill="currentColor"/>
   <rect x="8" y="15" width="8" height="1.5" rx="0.75" fill="currentColor" opacity="0.5"/>`,
  // terminal — filled screen
  `<rect x="3" y="4" width="18" height="16" rx="3" fill="currentColor" opacity="0.2"/>
   <rect x="3" y="4" width="18" height="16" rx="3" stroke="currentColor" stroke-width="1.5" fill="none"/>
   <polyline points="7,9 10,12 7,15" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
   <line x1="13" y1="15" x2="17" y2="15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>`,
  // brain — filled lobes
  `<path d="M12 3C9 3 7 5 7 7.5c0 .5.1 1 .3 1.5C6 9.5 5 11 5 12.5 5 14.5 6.5 16 8 16.5V20h8v-3.5c1.5-.5 3-2 3-3.5 0-1.5-1-3-2.3-3.5.2-.5.3-1 .3-1.5C17 5 15 3 12 3z"
     fill="currentColor" opacity="0.25" stroke-linejoin="round"/>
   <path d="M12 3C9 3 7 5 7 7.5c0 .5.1 1 .3 1.5C6 9.5 5 11 5 12.5 5 14.5 6.5 16 8 16.5V20h8v-3.5c1.5-.5 3-2 3-3.5 0-1.5-1-3-2.3-3.5.2-.5.3-1 .3-1.5C17 5 15 3 12 3z"
     stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>
   <path d="M12 7v10" stroke="currentColor" stroke-width="1" opacity="0.5" stroke-dasharray="2 2"/>`,
  // lightning bolt — filled
  `<polygon points="13,2 3,14 12,14 11,22 21,10 12,10" fill="currentColor" opacity="0.3"/>
   <polygon points="13,2 3,14 12,14 11,22 21,10 12,10" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>`,
  // gear — filled center
  `<circle cx="12" cy="12" r="4" fill="currentColor" opacity="0.25"/>
   <circle cx="12" cy="12" r="4" stroke="currentColor" stroke-width="1.5" fill="none"/>
   <circle cx="12" cy="12" r="1.5" fill="currentColor"/>
   <path d="M12 2v3M12 19v3M3.5 7l2.6 1.5M17.9 15.5l2.6 1.5M3.5 17l2.6-1.5M17.9 8.5l2.6-1.5"
     stroke="currentColor" stroke-width="2" stroke-linecap="round"/>`,
  // code brackets — bold
  `<path d="M8 4L3 12l5 8" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
   <path d="M16 4l5 8-5 8" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
   <line x1="14" y1="6" x2="10" y2="18" stroke="currentColor" stroke-width="1.5" opacity="0.4" stroke-linecap="round"/>`,
  // sparkle/magic — filled star
  `<path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" fill="currentColor" opacity="0.25"/>
   <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>
   <circle cx="12" cy="12" r="2" fill="currentColor"/>`,
  // eye — filled iris
  `<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" fill="currentColor" opacity="0.15"/>
   <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" stroke="currentColor" stroke-width="1.5" fill="none"/>
   <circle cx="12" cy="12" r="3.5" fill="currentColor" opacity="0.35"/>
   <circle cx="12" cy="12" r="3.5" stroke="currentColor" stroke-width="1.5" fill="none"/>
   <circle cx="12" cy="12" r="1.5" fill="currentColor"/>`,
  // shield — filled
  `<path d="M12 2L4 6v5c0 5.5 3.4 10.3 8 12 4.6-1.7 8-6.5 8-12V6z" fill="currentColor" opacity="0.2"/>
   <path d="M12 2L4 6v5c0 5.5 3.4 10.3 8 12 4.6-1.7 8-6.5 8-12V6z" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>
   <polyline points="9,12 11,14 15,10" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`,
  // flame — filled
  `<path d="M12 22c4-2 7-6 7-10 0-5-3-8-5-9.5C13 4 12 6 12 8c0-3-2-5.5-4-7C6 4 5 7 5 10c0 5 3 10 7 12z"
     fill="currentColor" opacity="0.3"/>
   <path d="M12 22c4-2 7-6 7-10 0-5-3-8-5-9.5C13 4 12 6 12 8c0-3-2-5.5-4-7C6 4 5 7 5 10c0 5 3 10 7 12z"
     stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>
   <path d="M12 18c2-1 3.5-3 3.5-5.5 0-2-1-3.5-2-4.5-.3.8-.8 1.5-1.5 2 0-1.5-1-3-2-3.5-.5 1.5-1.5 3-1.5 4.5 0 3 1.5 5.5 3.5 7z"
     fill="currentColor" opacity="0.4"/>`,
];

function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

function agentAvatar(name, task, requestId) {
  // Color is based on agent name (consistent per agent)
  // Icon is based on requestId (unique per request, visually distinct)
  const colorKey = name || "agent";
  const iconKey = requestId || (name || "") + (task || "");
  const ch = hashStr(colorKey);
  const ih = hashStr(iconKey);
  const [bg, fg] = AGENT_COLORS[ch % AGENT_COLORS.length];
  const icon = AGENT_ICONS[ih % AGENT_ICONS.length];
  return `<div class="agent-avatar" style="background:${bg}">
    <svg class="avatar-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" style="color:${fg}">${icon}</svg>
    <div class="status-dot"></div>
  </div>`;
}

function agentAccent(name) {
  const colorKey = name || "agent";
  const h = hashStr(colorKey);
  return AGENT_COLORS[h % AGENT_COLORS.length][0];
}

// --- Core ---
async function fetchPending() {
  try {
    const res = await fetch("/api/pending");
    return await res.json();
  } catch { return []; }
}

function timeAgo(ts) {
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 5) return "just now";
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

const SEND_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`;

function renderCard(req) {
  const div = document.createElement("div");
  div.className = "card";
  div.dataset.id = req.id;

  const accent = agentAccent(req.agent);
  div.style.setProperty("--accent", accent);
  const agentName = req.agent || "anonymous";

  let taskHtml = "";
  if (req.task) taskHtml = `<div class="task-label">${esc(req.task)}</div>`;

  let contextHtml = "";
  if (req.context) {
    contextHtml = `<div class="context">${esc(req.context)}</div>`;
  }

  div.innerHTML = `
    <style>.card[data-id="${esc(req.id)}"]::before { background: ${accent}; }</style>
    <div class="card-header">
      ${agentAvatar(req.agent, req.task, req.id)}
      <div class="card-header-text">
        <div class="agent-name">${esc(agentName)}</div>
        ${taskHtml}
      </div>
      <span class="time-badge">${timeAgo(req.created_at)}</span>
    </div>
    <div class="question">${esc(req.question)}</div>
    ${contextHtml}
    <div class="answer-form">
      <textarea rows="1" placeholder="Type your answer..."></textarea>
      <button class="send-btn" title="Send">${SEND_ICON}</button>
    </div>
  `;

  const textarea = div.querySelector("textarea");
  const btn = div.querySelector(".send-btn");
  btn.addEventListener("click", () => submitAnswer(req.id));
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitAnswer(req.id);
    }
  });

  return div;
}

async function submitAnswer(id) {
  const card = document.querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (!card) return;
  const textarea = card.querySelector("textarea");
  const btn = card.querySelector(".send-btn");
  const text = textarea.value.trim();
  if (!text) return;

  btn.disabled = true;

  try {
    await fetch("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, answer: text }),
    });
    answered.set(id, text);
    card.classList.add("answered");
    card.querySelector(".answer-form").innerHTML = `<div class="answer-text">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#2ea043" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      ${esc(text)}</div>`;
    setTimeout(() => { answered.delete(id); }, 5000);
  } catch {
    btn.disabled = false;
  }
}

async function poll() {
  const pending = await fetchPending();
  const currentIds = new Set(pending.map(r => r.id));

  for (const card of [...cardsEl.children]) {
    const id = card.dataset.id;
    if (!currentIds.has(id) && !answered.has(id)) {
      card.remove();
    }
  }

  const existingIds = new Set([...cardsEl.children].map(c => c.dataset.id));
  for (const req of pending) {
    if (!existingIds.has(req.id) && !answered.has(req.id)) {
      cardsEl.prepend(renderCard(req));
      const ta = cardsEl.firstChild.querySelector("textarea");
      if (ta) ta.focus();
      // Fire browser notification for new requests
      fireNotification(req.agent, req.question);
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
