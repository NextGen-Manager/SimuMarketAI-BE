from __future__ import annotations

import hashlib

from redis.asyncio import Redis

from app.core.errors import RateLimitError


class AuthRateLimiter:
    def __init__(self, redis: Redis, *, limit: int = 5, window_seconds: int = 60) -> None:
        self._redis = redis
        self._limit = limit
        self._window_seconds = window_seconds

    async def check(self, action: str, identifier: str) -> None:
        digest = hashlib.sha256(identifier.casefold().encode("utf-8")).hexdigest()
        key = f"auth-rate:{action}:{digest}"
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, self._window_seconds)
        if count > self._limit:
            raise RateLimitError()
