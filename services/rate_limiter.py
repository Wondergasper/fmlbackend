import os
import logging
from typing import Optional

from fastapi import Request, Response
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "100"))
RATE_LIMIT_KEY_PREFIX = os.getenv("RATE_LIMIT_KEY_PREFIX", "rate_limit")
RATE_LIMIT_EXCLUDED_PATHS = ["/docs", "/redoc", "/openapi.json", "/health"]

redis_client: Optional[Redis] = None


def _build_client_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    elif request.client:
        client_ip = request.client.host
    else:
        client_ip = "unknown"
    return f"{RATE_LIMIT_KEY_PREFIX}:{client_ip}"


async def init_redis() -> None:
    global redis_client
    if redis_client is not None:
        return

    redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    await redis_client.ping()
    logger.info(f"[rate_limiter] Connected to Redis at {REDIS_URL}")


async def close_redis() -> None:
    global redis_client
    if redis_client is None:
        return
    await redis_client.close()
    redis_client = None
    logger.info("[rate_limiter] Redis connection closed")


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if any(request.url.path.startswith(path) for path in RATE_LIMIT_EXCLUDED_PATHS):
            return await call_next(request)

        if redis_client is None:
            return await call_next(request)

        key = _build_client_key(request)
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, RATE_LIMIT_WINDOW_SECONDS)

        remaining = max(0, RATE_LIMIT_MAX_REQUESTS - count)
        if count > RATE_LIMIT_MAX_REQUESTS:
            ttl = await redis_client.ttl(key)
            headers = {
                "Retry-After": str(ttl if ttl and ttl > 0 else RATE_LIMIT_WINDOW_SECONDS),
                "X-RateLimit-Limit": str(RATE_LIMIT_MAX_REQUESTS),
                "X-RateLimit-Remaining": "0",
            }
            return Response(
                content="Too many requests. Please try again later.",
                status_code=429,
                headers=headers,
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_MAX_REQUESTS)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
