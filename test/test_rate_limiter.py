import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from services import rate_limiter
from services.rate_limiter import RateLimitMiddleware, init_redis, close_redis


def create_test_app():
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/test")
    def api_test():
        return {"status": "success"}

    return app


@pytest.fixture
def test_client():
    app = create_test_app()
    return TestClient(app)


def test_excluded_paths_bypass_rate_limiter(test_client):
    """Excluded paths such as /health should bypass rate limiting entirely."""
    mock_redis = AsyncMock()
    mock_redis.pipeline = MagicMock()
    with patch("services.rate_limiter.redis_client", mock_redis):
        response = test_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_rate_limiter_fail_open_when_redis_none(test_client):
    """When redis_client is None, request should proceed normally (fail-open)."""
    with patch("services.rate_limiter.redis_client", None):
        response = test_client.get("/api/test")
        assert response.status_code == 200
        assert response.json() == {"status": "success"}


def test_rate_limiter_fail_open_on_redis_exception(test_client):
    """When Redis operations raise an exception, middleware should log and fail open."""
    mock_redis = AsyncMock()
    mock_pipeline = MagicMock()
    mock_pipeline.execute = AsyncMock(side_effect=Exception("Redis connection error"))
    mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

    with patch("services.rate_limiter.redis_client", mock_redis):
        response = test_client.get("/api/test")
        assert response.status_code == 200
        assert response.json() == {"status": "success"}


def test_rate_limiter_allowed_under_limit(test_client):
    """Requests under the rate limit should pass and return rate limit headers."""
    mock_redis = AsyncMock()
    mock_pipeline = MagicMock()
    mock_pipeline.execute = AsyncMock(return_value=[0, 2, []])
    mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

    with patch("services.rate_limiter.redis_client", mock_redis):
        with patch("services.rate_limiter.RATE_LIMIT_MAX_REQUESTS", 10):
            response = test_client.get("/api/test")
            assert response.status_code == 200
            assert response.headers.get("X-RateLimit-Limit") == "10"
            assert response.headers.get("X-RateLimit-Remaining") == "7"


def test_rate_limiter_exceeded_returns_429_with_retry_after(test_client):
    """When limit is exceeded, return 429 Too Many Requests with Retry-After header."""
    import time
    now = time.time()
    mock_redis = AsyncMock()
    mock_pipeline = MagicMock()
    mock_pipeline.execute = AsyncMock(return_value=[0, 10, [("member", now - 30)]])
    mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

    with patch("services.rate_limiter.redis_client", mock_redis):
        with patch("services.rate_limiter.RATE_LIMIT_MAX_REQUESTS", 10):
            with patch("services.rate_limiter.RATE_LIMIT_WINDOW_SECONDS", 60):
                response = test_client.get("/api/test")
                assert response.status_code == 429
                assert "Too many requests" in response.text
                assert "Retry-After" in response.headers
                retry_after = response.headers["Retry-After"]
                assert retry_after.isdigit()
                assert int(retry_after) > 0
                assert response.headers.get("X-RateLimit-Limit") == "10"
                assert response.headers.get("X-RateLimit-Remaining") == "0"
