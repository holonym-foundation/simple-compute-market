"""Unit tests for AgentRateLimitMiddleware + SlidingWindowCounter.

The provisioning API is buyer-facing in the seller deployment, so per-agent
rate limiting is enabled there (compose/seller.yml). These tests pin the
behavior the deployment relies on: only POSTs are limited, limits are
per-agent (one agent's burst can't throttle another), the window slides, and
an over-limit request gets a 429 with a Retry-After header.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from middleware import rate_limit
from middleware.rate_limit import AgentRateLimitMiddleware, SlidingWindowCounter


class TestSlidingWindowCounter:
    def test_allows_up_to_limit_then_blocks(self):
        c = SlidingWindowCounter(max_requests=3)
        assert [c.is_allowed("a") for _ in range(3)] == [True, True, True]
        assert c.is_allowed("a") is False

    def test_remaining_decrements(self):
        c = SlidingWindowCounter(max_requests=3)
        assert c.remaining("a") == 3
        c.is_allowed("a")
        assert c.remaining("a") == 2

    def test_limits_are_per_key(self):
        c = SlidingWindowCounter(max_requests=1)
        assert c.is_allowed("a") is True
        assert c.is_allowed("b") is True  # different key, own budget
        assert c.is_allowed("a") is False

    def test_window_slides(self, monkeypatch):
        t = {"now": 1000.0}
        monkeypatch.setattr(rate_limit.time, "monotonic", lambda: t["now"])
        c = SlidingWindowCounter(max_requests=2, window_seconds=60)
        assert c.is_allowed("a") is True
        assert c.is_allowed("a") is True
        assert c.is_allowed("a") is False
        t["now"] += 61  # advance past the window; old hits expire
        assert c.is_allowed("a") is True


def _client(enabled: bool, max_requests: int = 2) -> TestClient:
    app = FastAPI()
    # Added first -> innermost: runs AFTER agent_id is set on request.state.
    app.add_middleware(AgentRateLimitMiddleware, enabled=enabled, max_requests=max_requests)

    # Added last -> outermost: sets agent_id from a header before the limiter.
    class _SetAgentId(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            agent = request.headers.get("X-Agent-Id")
            if agent:
                request.state.agent_id = agent
            return await call_next(request)

    app.add_middleware(_SetAgentId)

    @app.post("/api/v1/hosts/h/containers/")
    def _create() -> dict:
        return {"ok": True}

    @app.get("/api/v1/leases/")
    def _list() -> dict:
        return {"ok": True}

    return TestClient(app)


class TestAgentRateLimitMiddleware:
    def test_disabled_is_passthrough(self):
        c = _client(enabled=False, max_requests=1)
        for _ in range(5):
            assert c.post("/api/v1/hosts/h/containers/", headers={"X-Agent-Id": "a"}).status_code == 200

    def test_get_requests_not_limited(self):
        c = _client(enabled=True, max_requests=1)
        for _ in range(5):
            assert c.get("/api/v1/leases/", headers={"X-Agent-Id": "a"}).status_code == 200

    def test_no_agent_id_not_limited(self):
        c = _client(enabled=True, max_requests=1)
        for _ in range(5):
            assert c.post("/api/v1/hosts/h/containers/").status_code == 200

    def test_blocks_over_limit_with_429_and_retry_after(self):
        c = _client(enabled=True, max_requests=2)
        h = {"X-Agent-Id": "a"}
        assert c.post("/api/v1/hosts/h/containers/", headers=h).status_code == 200
        assert c.post("/api/v1/hosts/h/containers/", headers=h).status_code == 200
        resp = c.post("/api/v1/hosts/h/containers/", headers=h)
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After") == "60"
        assert "X-RateLimit-Remaining" in resp.headers

    def test_limit_is_per_agent(self):
        c = _client(enabled=True, max_requests=1)
        assert c.post("/api/v1/hosts/h/containers/", headers={"X-Agent-Id": "a"}).status_code == 200
        # different agent has its own budget
        assert c.post("/api/v1/hosts/h/containers/", headers={"X-Agent-Id": "b"}).status_code == 200
        # original agent is now over its limit
        assert c.post("/api/v1/hosts/h/containers/", headers={"X-Agent-Id": "a"}).status_code == 429

    def test_remaining_header_on_success(self):
        c = _client(enabled=True, max_requests=5)
        resp = c.post("/api/v1/hosts/h/containers/", headers={"X-Agent-Id": "a"})
        assert resp.status_code == 200
        assert int(resp.headers["X-RateLimit-Remaining"]) == 4
