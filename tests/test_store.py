"""Unit tests for the in-memory RequestStore."""

import time
import threading
from pokeme.server import RequestStore, VALID_ID_RE, MAX_PENDING_REQUESTS, ANSWERED_TTL


class TestRequestCreation:
    """Creating requests and retrieving them."""

    def test_create_returns_request(self):
        s = RequestStore()
        req = s.create("What colour?")
        assert req is not None
        assert req.question == "What colour?"
        assert req.status == "pending"
        assert req.answer is None

    def test_id_format(self):
        s = RequestStore()
        req = s.create("q")
        assert VALID_ID_RE.match(req.id), f"ID {req.id!r} does not match expected hex format"

    def test_optional_fields(self):
        s = RequestStore()
        req = s.create("q", context="ctx", agent="bot-1", task="fixing bugs")
        assert req.context == "ctx"
        assert req.agent == "bot-1"
        assert req.task == "fixing bugs"

    def test_optional_fields_default_to_none(self):
        s = RequestStore()
        req = s.create("q")
        assert req.context is None
        assert req.agent is None
        assert req.task is None


class TestGet:
    """Retrieving requests by ID."""

    def test_get_existing(self):
        s = RequestStore()
        req = s.create("hello")
        got = s.get(req.id)
        assert got is req

    def test_get_missing_returns_none(self):
        s = RequestStore()
        assert s.get("aabbccddeeff") is None

    def test_get_rejects_invalid_id(self):
        s = RequestStore()
        s.create("hello")
        # too short
        assert s.get("abc") is None
        # bad characters
        assert s.get("ZZZZZZZZZZZZ") is None
        # path traversal attempt
        assert s.get("../../etc/pas") is None


class TestAnswer:
    """Answering pending requests."""

    def test_answer_success(self):
        s = RequestStore()
        req = s.create("pick a number")
        ok = s.answer(req.id, "42")
        assert ok is True
        assert req.status == "answered"
        assert req.answer == "42"
        assert req.answered_at is not None

    def test_answer_nonexistent_returns_false(self):
        s = RequestStore()
        assert s.answer("aabbccddeeff", "nope") is False

    def test_answer_twice_returns_false(self):
        s = RequestStore()
        req = s.create("q")
        s.answer(req.id, "first")
        ok = s.answer(req.id, "second")
        assert ok is False
        assert req.answer == "first"  # unchanged

    def test_answer_rejects_invalid_id(self):
        s = RequestStore()
        assert s.answer("not-valid!!!", "x") is False


class TestPending:
    """Listing and checking pending requests."""

    def test_pending_empty(self):
        s = RequestStore()
        assert s.pending() == []
        assert s.has_pending() is False

    def test_pending_lists_only_unanswered(self):
        s = RequestStore()
        r1 = s.create("q1")
        r2 = s.create("q2")
        s.answer(r1.id, "a1")
        pending = s.pending()
        assert len(pending) == 1
        assert pending[0].id == r2.id

    def test_has_pending_true(self):
        s = RequestStore()
        s.create("q")
        assert s.has_pending() is True


class TestTruncation:
    """Field length limits are enforced."""

    def test_long_question_truncated(self):
        s = RequestStore()
        req = s.create("x" * 5000)
        assert len(req.question) == 2000

    def test_long_agent_truncated(self):
        s = RequestStore()
        req = s.create("q", agent="a" * 500)
        assert len(req.agent) == 100

    def test_long_answer_truncated(self):
        s = RequestStore()
        req = s.create("q")
        s.answer(req.id, "y" * 20000)
        assert len(req.answer) == 10000


class TestPendingLimit:
    """Cannot exceed MAX_PENDING_REQUESTS."""

    def test_rejects_when_full(self):
        s = RequestStore()
        for i in range(MAX_PENDING_REQUESTS):
            assert s.create(f"q{i}") is not None
        # one more should be rejected
        assert s.create("overflow") is None

    def test_answering_frees_slot(self):
        s = RequestStore()
        reqs = []
        for i in range(MAX_PENDING_REQUESTS):
            reqs.append(s.create(f"q{i}"))
        # full
        assert s.create("overflow") is None
        # answer one
        s.answer(reqs[0].id, "done")
        # now there's room
        assert s.create("fits") is not None


class TestEviction:
    """Stale answered requests are evicted."""

    def test_evict_stale(self):
        s = RequestStore()
        req = s.create("old question")
        s.answer(req.id, "old answer")
        # Manually backdate the answered_at
        req.answered_at = time.time() - ANSWERED_TTL - 10
        # Creating a new request triggers eviction
        s.create("new question")
        assert s.get(req.id) is None

    def test_recent_answered_not_evicted(self):
        s = RequestStore()
        req = s.create("recent")
        s.answer(req.id, "ans")
        # Still fresh
        s.create("trigger eviction")
        assert s.get(req.id) is not None


class TestThreadSafety:
    """Concurrent access doesn't corrupt the store."""

    def test_concurrent_creates(self):
        s = RequestStore()
        results = []

        def worker():
            for _ in range(50):
                r = s.create("q")
                if r:
                    results.append(r.id)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All IDs should be unique
        assert len(results) == len(set(results))
        # All should be retrievable
        for rid in results:
            assert s.get(rid) is not None
