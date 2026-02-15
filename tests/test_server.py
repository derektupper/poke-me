"""Integration tests — spin up a real HTTP server and hit the API."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from pokeme.server import ThreadedHTTPServer, RequestHandler, store, RequestStore


def _free_port():
    """Find a free port to avoid conflicts."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server():
    """Start a pokeme server on a random port, yield its base URL, then shut down."""
    # Replace the global store with a fresh one for test isolation
    import pokeme.server as srv
    original_store = srv.store
    srv.store = RequestStore()

    port = _free_port()
    httpd = ThreadedHTTPServer(("127.0.0.1", port), RequestHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    base = f"http://127.0.0.1:{port}"
    yield base

    httpd.shutdown()
    srv.store = original_store


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def _post(url, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def _post_raw(url, data):
    """POST and return (status, body) even for HTTP error responses."""
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get_raw(url):
    """GET and return (status, body) even for HTTP error responses."""
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- Health check ---

class TestHealth:
    def test_health(self, server):
        data = _get(f"{server}/api/health")
        assert data == {"status": "ok"}


# --- Web UI ---

class TestWebUI:
    def test_serves_html(self, server):
        with urllib.request.urlopen(f"{server}/", timeout=5) as r:
            assert r.status == 200
            ct = r.headers.get("Content-Type", "")
            assert "text/html" in ct
            body = r.read().decode()
            assert "pokeme" in body
            assert "Notification" in body  # browser notification code present


# --- Ask + Answer flow ---

class TestAskAnswer:
    def test_full_flow(self, server):
        """Ask a question, verify pending, answer it, verify answered."""
        # 1. Ask
        status, data = _post(f"{server}/api/ask", {
            "question": "What DB?",
            "agent": "test-bot",
            "task": "choosing infra",
            "context": "We need a database",
        })
        assert status == 200
        rid = data["id"]
        assert len(rid) == 12

        # 2. Pending
        pending = _get(f"{server}/api/pending")
        assert len(pending) == 1
        assert pending[0]["id"] == rid
        assert pending[0]["question"] == "What DB?"
        assert pending[0]["agent"] == "test-bot"
        assert pending[0]["status"] == "pending"

        # 3. Status
        info = _get(f"{server}/api/status/{rid}")
        assert info["status"] == "pending"

        # 4. Answer
        status, data = _post(f"{server}/api/answer", {"id": rid, "answer": "Postgres"})
        assert status == 200
        assert data["status"] == "ok"

        # 5. Status after answer
        info = _get(f"{server}/api/status/{rid}")
        assert info["status"] == "answered"
        assert info["answer"] == "Postgres"

        # 6. Pending should be empty now
        pending = _get(f"{server}/api/pending")
        assert len(pending) == 0

    def test_multiple_agents(self, server):
        """Multiple agents can ask concurrently."""
        _, d1 = _post(f"{server}/api/ask", {"question": "q1", "agent": "agent-a"})
        _, d2 = _post(f"{server}/api/ask", {"question": "q2", "agent": "agent-b"})
        _, d3 = _post(f"{server}/api/ask", {"question": "q3", "agent": "agent-c"})

        pending = _get(f"{server}/api/pending")
        assert len(pending) == 3

        # Answer the middle one
        _post(f"{server}/api/answer", {"id": d2["id"], "answer": "done"})

        pending = _get(f"{server}/api/pending")
        assert len(pending) == 2
        remaining_ids = {p["id"] for p in pending}
        assert d1["id"] in remaining_ids
        assert d3["id"] in remaining_ids

    def test_minimal_ask(self, server):
        """Only 'question' is required."""
        status, data = _post(f"{server}/api/ask", {"question": "yes or no?"})
        assert status == 200
        assert "id" in data


# --- Error handling ---

class TestErrors:
    def test_ask_missing_question(self, server):
        status, data = _post_raw(f"{server}/api/ask", {"agent": "bot"})
        assert status == 400
        assert "missing question" in data["error"]

    def test_ask_empty_body(self, server):
        status, data = _post_raw(f"{server}/api/ask", {})
        assert status == 400

    def test_answer_missing_fields(self, server):
        status, data = _post_raw(f"{server}/api/answer", {"id": "aabbccddeeff"})
        assert status == 400
        assert "missing" in data["error"]

    def test_answer_nonexistent_id(self, server):
        status, data = _post_raw(f"{server}/api/answer", {"id": "aabbccddeeff", "answer": "x"})
        assert status == 404

    def test_answer_twice(self, server):
        _, d = _post(f"{server}/api/ask", {"question": "q"})
        _post(f"{server}/api/answer", {"id": d["id"], "answer": "first"})
        status, data = _post_raw(f"{server}/api/answer", {"id": d["id"], "answer": "second"})
        assert status == 404  # already answered

    def test_status_bad_id(self, server):
        status, data = _get_raw(f"{server}/api/status/INVALID!!")
        assert status == 404

    def test_status_missing_id(self, server):
        status, data = _get_raw(f"{server}/api/status/aabbccddeeff")
        assert status == 404

    def test_unknown_route(self, server):
        status, data = _get_raw(f"{server}/api/nope")
        assert status == 404


# --- Permission flow ---

class TestPermissionFlow:
    def test_permission_full_approve_flow(self, server):
        """Create permission request, verify pending, approve it, verify status."""
        status, data = _post(f"{server}/api/ask", {
            "question": "Delete temp files?",
            "command": "rm -rf /tmp/*",
            "request_type": "permission",
            "agent": "cleanup-bot",
        })
        assert status == 200
        rid = data["id"]

        # Verify it appears in pending with correct type
        pending = _get(f"{server}/api/pending")
        assert len(pending) == 1
        assert pending[0]["request_type"] == "permission"
        assert pending[0]["command"] == "rm -rf /tmp/*"

        # Approve it
        answer = json.dumps({"decision": "approved", "comment": ""})
        status, data = _post(f"{server}/api/answer", {"id": rid, "answer": answer})
        assert status == 200

        # Verify status
        info = _get(f"{server}/api/status/{rid}")
        assert info["status"] == "answered"
        parsed = json.loads(info["answer"])
        assert parsed["decision"] == "approved"

    def test_permission_deny_flow(self, server):
        status, data = _post(f"{server}/api/ask", {
            "question": "Drop database?",
            "command": "DROP DATABASE prod",
            "request_type": "permission",
            "agent": "db-bot",
        })
        rid = data["id"]
        answer = json.dumps({"decision": "denied", "comment": "too dangerous"})
        _post(f"{server}/api/answer", {"id": rid, "answer": answer})
        info = _get(f"{server}/api/status/{rid}")
        parsed = json.loads(info["answer"])
        assert parsed["decision"] == "denied"
        assert parsed["comment"] == "too dangerous"

    def test_permission_missing_command_returns_400(self, server):
        status, data = _post_raw(f"{server}/api/ask", {
            "question": "do something",
            "request_type": "permission",
        })
        assert status == 400
        assert "command" in data["error"]

    def test_question_type_backward_compat(self, server):
        """Old-style request with no request_type should still work."""
        status, data = _post(f"{server}/api/ask", {"question": "hello?"})
        assert status == 200
        info = _get(f"{server}/api/status/{data['id']}")
        assert info["request_type"] == "question"
        assert info["command"] is None
