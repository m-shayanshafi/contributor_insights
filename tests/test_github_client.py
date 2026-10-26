import logging

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.github.client import GRAPHQL_URL, GitHubClient, NotFound, RateLimited, Upstream
from app.github.validation import InvalidLogin, validate_login

TOKEN = "github_pat_TESTONLY_abcdefghijklmnop"
NOW = 1_800_000_000.0
RESET_SOON = "2027-01-15T08:00:30+00:00"  # NOW + 30s
RESET_LATER = "2027-01-15T09:00:00+00:00"  # NOW + 1h


def ok(data, remaining=4000, reset=RESET_LATER):
    return httpx.Response(
        200, json={"data": {**data, "rateLimit": {"cost": 1, "remaining": remaining, "resetAt": reset}}}
    )


@pytest.fixture
async def client():
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    gh = GitHubClient(SecretStr(TOKEN), sleep=fake_sleep, clock=lambda: NOW)
    gh.sleeps = sleeps
    yield gh
    await gh.aclose()


@respx.mock
async def test_sends_bearer_token_and_variables(client):
    route = respx.post(GRAPHQL_URL).mock(return_value=ok({"user": {"login": "Frenck"}}))
    assert await client.get_user("frenck") == "Frenck"
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert b'"variables":{"login":"frenck"}' in request.content.replace(b" ", b"")


@respx.mock
async def test_retries_server_errors_then_succeeds(client):
    respx.post(GRAPHQL_URL).mock(
        side_effect=[httpx.Response(502), httpx.Response(503), ok({"user": {"login": "a"}})]
    )
    assert await client.get_user("a") == "a"
    assert client.sleeps == [1.0, 2.0]


@respx.mock
async def test_retries_transport_errors(client):
    respx.post(GRAPHQL_URL).mock(
        side_effect=[httpx.ConnectTimeout("boom"), ok({"user": {"login": "a"}})]
    )
    assert await client.get_user("a") == "a"


@respx.mock
async def test_gives_up_after_max_retries(client):
    respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(Upstream, match="503"):
        await client.get_user("a")
    assert len(client.sleeps) == 3


@respx.mock
async def test_secondary_limit_honours_retry_after(client):
    limited = httpx.Response(403, headers={"Retry-After": "7"})
    respx.post(GRAPHQL_URL).mock(side_effect=[limited, ok({"user": {"login": "a"}})])
    assert await client.get_user("a") == "a"
    assert client.sleeps == [7.0]


@respx.mock
async def test_secondary_limit_exhausted_raises_rate_limited(client):
    respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(RateLimited):
        await client.get_user("a")


@respx.mock
async def test_plain_403_is_not_retried(client):
    route = respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(Upstream, match="403"):
        await client.get_user("a")
    assert route.call_count == 1


@respx.mock
async def test_sleeps_before_next_call_when_budget_low(client):
    respx.post(GRAPHQL_URL).mock(return_value=ok({"user": {"login": "a"}}, remaining=5, reset=RESET_SOON))
    await client.get_user("a")
    assert client.sleeps == []
    await client.get_user("a")
    assert client.sleeps == [30.0]


@respx.mock
async def test_raises_when_budget_reset_is_far_away(client):
    route = respx.post(GRAPHQL_URL).mock(return_value=ok({"user": {"login": "a"}}, remaining=5))
    await client.get_user("a")
    with pytest.raises(RateLimited) as exc_info:
        await client.get_user("a")
    assert exc_info.value.reset_at == NOW + 3600
    assert route.call_count == 1


@respx.mock
async def test_unknown_user_raises_not_found(client):
    body = {"data": {"user": None}, "errors": [{"type": "NOT_FOUND", "message": "nope"}]}
    respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(NotFound):
        await client.get_user("ghost-user")


@respx.mock
async def test_graphql_rate_limited_error(client):
    body = {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}
    respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RateLimited):
        await client.graphql("query { viewer { login } }", {})


@respx.mock
async def test_other_graphql_errors_are_upstream(client):
    body = {"data": None, "errors": [{"type": "INTERNAL", "message": "x"}]}
    respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(Upstream, match="INTERNAL"):
        await client.graphql("query { viewer { login } }", {})


@pytest.mark.parametrize("login", ["dependabot[bot]", "x repo:other/repo", "a:b", "-lead", "", "a" * 40])
async def test_invalid_logins_never_reach_github(client, login):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(GRAPHQL_URL)
        with pytest.raises(InvalidLogin):
            await client.get_user(login)
        assert route.call_count == 0


def test_valid_logins_accepted():
    for login in ["frenck", "Frenck", "home-assistant", "a", "a" * 39]:
        assert validate_login(login) == login


@respx.mock
async def test_token_never_in_errors_or_logs(client, caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(GRAPHQL_URL).mock(side_effect=[httpx.Response(502), httpx.Response(401)])
    with pytest.raises(Upstream) as exc_info:
        await client.get_user("a")
    assert "401" in str(exc_info.value)
    assert TOKEN not in str(exc_info.value)
    assert TOKEN not in repr(exc_info.value)
    assert TOKEN not in caplog.text
