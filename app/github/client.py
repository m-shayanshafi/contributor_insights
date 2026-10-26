"""GitHub GraphQL transport: authentication, retries and rate-limit budgeting.

Errors carry status codes and GraphQL error types only, never the token or the
upstream response body.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretStr

from app.github.validation import validate_login

logger = logging.getLogger(__name__)

# Hard-coded host: no part of the URL comes from configuration or input.
GRAPHQL_URL = "https://api.github.com/graphql"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)
RETRY_STATUSES = frozenset({502, 503, 504})

# Every query should select this so the client can track its budget.
RATE_LIMIT_FIELDS = "rateLimit { cost remaining resetAt }"

USER_QUERY = f"""
query($login: String!) {{
  user(login: $login) {{ login }}
  {RATE_LIMIT_FIELDS}
}}
"""


class GitHubError(Exception):
    pass


class NotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    def __init__(self, message: str, reset_at: float | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class Upstream(GitHubError):
    pass


class GitHubClient:
    def __init__(
        self,
        token: SecretStr,
        *,
        max_retries: int = 3,
        low_budget: int = 100,
        max_budget_wait: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "contributor-insights",
            },
            timeout=TIMEOUT,
        )
        self._max_retries = max_retries
        self._low_budget = low_budget
        self._max_budget_wait = max_budget_wait
        self._sleep = sleep
        self._clock = clock
        self._remaining: int | None = None
        self._reset_at: float | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def get_user(self, login: str) -> str:
        """Return the canonical login. Raises NotFound for unknown users, orgs and bots."""
        data = await self.graphql(USER_QUERY, {"login": validate_login(login)})
        user = data.get("user")
        if not user:
            raise NotFound("user not found")
        return user["login"]

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a query and return its `data`. Inputs go only through `variables`."""
        await self._wait_for_budget()
        body = await self._post({"query": query, "variables": variables})
        self._record_budget(body.get("data") or {})
        _raise_for_graphql_errors(body)
        return body["data"]

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            last = attempt == self._max_retries
            try:
                response = await self._http.post(GRAPHQL_URL, json=payload)
            except httpx.TransportError as exc:
                if last:
                    raise Upstream(f"GitHub unreachable: {type(exc).__name__}") from None
                await self._backoff(attempt, None, f"transport error {type(exc).__name__}")
                continue

            status = response.status_code
            if status == 200:
                return response.json()
            if status in RETRY_STATUSES or _is_secondary_limit(response):
                if not last:
                    await self._backoff(attempt, response, f"status {status}")
                    continue
                if status in RETRY_STATUSES:
                    raise Upstream(f"GitHub returned {status}")
                raise RateLimited("GitHub secondary rate limit", _reset_header(response))
            if status == 401:
                raise Upstream("GitHub rejected the token (401)")
            raise Upstream(f"GitHub returned {status}")
        raise AssertionError("unreachable")

    async def _backoff(self, attempt: int, response: httpx.Response | None, reason: str) -> None:
        delay = float(2**attempt)
        if response is not None and response.headers.get("Retry-After", "").isdigit():
            delay = float(response.headers["Retry-After"])
        logger.warning("GitHub %s, retrying in %.0fs", reason, delay)
        await self._sleep(delay)

    async def _wait_for_budget(self) -> None:
        if self._remaining is None or self._reset_at is None:
            return
        if self._remaining >= self._low_budget:
            return
        wait = self._reset_at - self._clock()
        if wait <= 0:
            return
        if wait > self._max_budget_wait:
            raise RateLimited("GitHub rate-limit budget exhausted", self._reset_at)
        logger.info("GitHub budget low (%d left), sleeping %.0fs", self._remaining, wait)
        await self._sleep(wait)

    def _record_budget(self, data: dict[str, Any]) -> None:
        rate = data.get("rateLimit")
        if not rate:
            return
        self._remaining = rate["remaining"]
        self._reset_at = datetime.fromisoformat(rate["resetAt"]).timestamp()


def _is_secondary_limit(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    return "Retry-After" in response.headers or response.headers.get("x-ratelimit-remaining") == "0"


def _reset_header(response: httpx.Response) -> float | None:
    value = response.headers.get("x-ratelimit-reset", "")
    return float(value) if value.isdigit() else None


def _raise_for_graphql_errors(body: dict[str, Any]) -> None:
    errors = body.get("errors") or []
    types = {e.get("type") for e in errors}
    if "RATE_LIMITED" in types:
        raise RateLimited("GitHub rate limit reached")
    # A missing user comes back as a NOT_FOUND error alongside `user: null`,
    # which callers handle; any other error type is an upstream failure.
    if types - {"NOT_FOUND"}:
        raise Upstream(f"GitHub GraphQL errors: {sorted(str(t) for t in types)}")
    if body.get("data") is None:
        raise NotFound("not found") if errors else Upstream("GitHub returned no data")
