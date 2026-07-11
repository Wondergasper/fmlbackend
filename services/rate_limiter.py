import os
import time
import uuid
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

    try:
        redis_client = Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
        await redis_client.ping()
        logger.info(f"[rate_limiter] Connected to Redis at {REDIS_URL}")
    except Exception as exc:
        logger.warning(f"[rate_limiter] Could not connect to Redis at {REDIS_URL}: {exc}. Rate limiter will fail open.")
        redis_client = None


async def close_redis() -> None:
    global redis_client
    if redis_client is None:
        return
    try:
        await redis_client.close()
    except Exception as exc:
        logger.warning(f"[rate_limiter] Error closing Redis: {exc}")
    redis_client = None
    logger.info("[rate_limiter] Redis connection closed")


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if any(request.url.path.startswith(path) for path in RATE_LIMIT_EXCLUDED_PATHS):
            return await call_next(request)

        if redis_client is None:
            return await call_next(request)

        key = _build_client_key(request)
        now = time.time()
        clear_before = now - RATE_LIMIT_WINDOW_SECONDS

        try:
            # Atomic pipeline: clean expired hits, fetch count and oldest timestamp
            pipe = redis_client.pipeline()
            pipe.zremrangebyscore(key, 0, clear_before)
            pipe.zcard(key)
            pipe.zrange(key, 0, 0, withscores=True)
            results = await pipe.execute()

            current_count = results[1]
            oldest_items = results[2]

            if current_count >= RATE_LIMIT_MAX_REQUESTS:
                if oldest_items:
                    oldest_ts = oldest_items[0][1]
                    retry_after_sec = max(1, int(oldest_ts + RATE_LIMIT_WINDOW_SECONDS - now + 0.999))
                else:
                    retry_after_sec = RATE_LIMIT_WINDOW_SECONDS

                headers = {
                    "Retry-After": str(retry_after_sec),
                    "X-RateLimit-Limit": str(RATE_LIMIT_MAX_REQUESTS),
                    "X-RateLimit-Remaining": "0",
                }
                return Response(
                    content="Too many requests. Please try again later.",
                    status_code=429,
                    headers=headers,
                )

            # Record current request in sliding window
            member = f"{now}:{uuid.uuid4().hex}"
            pipe = redis_client.pipeline()
            pipe.zadd(key, {member: now})
            pipe.expire(key, RATE_LIMIT_WINDOW_SECONDS)
            await pipe.execute()

            remaining = max(0, RATE_LIMIT_MAX_REQUESTS - (current_count + 1))
            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_MAX_REQUESTS)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            return response

        except Exception as exc:
            logger.warning(f"[rate_limiter] Redis error encountered: {exc}. Failing open.")
            return await call_next(request)
