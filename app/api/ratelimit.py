"""Per-client-IP rate limiting, applied as a router dependency.

A dependency runs for every route on the router, so no route can be missed.
Behind a proxy the key would need to be the forwarded client address instead.
"""

import time

from fastapi import HTTPException, Request
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter


class ClientRateLimiter:
    def __init__(self, limit: str) -> None:
        self._limit = parse(limit)
        self._limiter = MovingWindowRateLimiter(MemoryStorage())

    def check(self, request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        if self._limiter.hit(self._limit, client):
            return
        reset_at, _ = self._limiter.get_window_stats(self._limit, client)
        retry_after = max(1, int(reset_at - time.time()))
        raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": str(retry_after)})


async def enforce_rate_limit(request: Request) -> None:
    request.app.state.rate_limiter.check(request)
