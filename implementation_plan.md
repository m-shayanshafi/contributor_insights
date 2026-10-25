# Implementation Plan

This plan builds the service described in [design.md](design.md) in 11 steps. Each step:

- stays **under 500 lines of code to review**, tests included. Generated fixture JSON and lock files don't count.
- ends with `pytest` passing and a short demo command you can run.
- is reviewed and approved by you before the next step starts. After you approve a step, it is committed as one git commit, so each step is a clean diff.

Line counts are estimates. If a step looks like it will go over 500, it gets split before you review it.

| # | Step | Est. lines | Outcome |
|---|---|---|---|
| 1 | Project setup and config | ~250 | App boots, `/healthz` works, secrets handled safely |
| 2 | SQLite store | ~400 | Schema and repository with upserts and window queries |
| 3 | Metrics engine | ~400 | Six metrics, deltas and series as pure functions |
| 4 | GitHub client: transport | ~300 | Authenticated GraphQL calls with retries and rate limiting |
| 5 | GitHub client: contributor queries | ~350 | PRs and reviews for a login, with date slicing |
| 6 | Contributor sync | ~450 | Backfill, incremental sync, locks and caps |
| 7 | **Endpoint 1: contributor metrics** | ~400 | `GET /api/contributors/{login}` works live |
| 8 | Fixture mode and seeding scripts | ~350 | Runs with no credentials |
| 9 | Peers, prompt input, grounding and confidence | ~450 | Deterministic narrative pipeline, no LLM yet |
| 10 | **Endpoint 2: Claude narrative** | ~450 | `GET /api/contributors/{login}/narrative` works |
| 11 | Docker, README and end-to-end test | ~300 | `docker compose up` demo, reviewer instructions |

---

## Step 1: Project setup and config
**Goal:** a runnable skeleton with secure configuration.
- `git init`, `pyproject.toml` (uv, Python 3.12), `uv.lock`, `.gitignore` (`.env`, `data/`), `.env.example`
- `app/config.py`:
  - `pydantic-settings`, with `SecretStr` for `GITHUB_TOKEN` and `ANTHROPIC_API_KEY`
  - `DATA_SOURCE` and `LLM_MODE` enums
  - fails fast when a required secret is missing in live mode
- `app/logging.py`: a redaction filter for `Authorization` and `x-api-key` headers
- `app/main.py`: app factory, lifespan hook and `GET /healthz`
- **Tests:** config validation (live vs fixture modes), secrets never appear in `repr` or logs, `/healthz` returns 200

**Verify:** `uv run uvicorn app.main:app`, then `curl localhost:8000/healthz`.
**Review focus:** dependency choices, how secrets are handled.

## Step 2: SQLite store
**Goal:** persistent storage as specified in design §4.
- `app/store/db.py`: schema for the `contributors`, `pull_requests`, `reviews` and `narratives` tables, plus indexes; `aiosqlite` connection setup and initialization.
- `app/store/repository.py`:
  - upsert PRs and reviews (reviews replaced per PR)
  - read and update contributor state and watermarks
  - fetch a contributor's PRs and reviews for a time window
  - delete data older than the retention period
  - get and put narrative cache entries
- All SQL uses parameter binding.
- **Tests:** upsert idempotency, window queries at the boundaries, retention deletion, cache lookup by hash.

**Review focus:** schema, indexes, SQL safety.

## Step 3: Metrics engine
**Goal:** pure, fully tested computations as specified in design §5.
- `app/analytics/periods.py`: the `1w|1m|3m` enum, current and previous windows, `HISTORY_DAYS` derived from the longest period.
- `app/analytics/metrics.py`:
  - the six metrics: `prs_merged, lines_changed, median_pr_size_lines, median_cycle_time_hours, reviews_given, active_weeks`
  - `previous_period` values and `pct_change`, with zero-division guards
  - `series` buckets: daily for 1w, weekly for 1m and 3m
  - self-reviews excluded
- `app/models.py`: Pydantic response models for the contributor endpoint.
- **Tests:** each metric on hand-built data, empty windows, `pct_change` when the previous value is 0, bucket boundaries.

**Review focus:** metric definitions and edge cases. This is the core logic.

## Step 4: GitHub client, transport layer
**Goal:** a safe, robust GraphQL client.
- `app/github/client.py`:
  - async `httpx` client with a hard-coded host and timeouts, sending the token as a Bearer header
  - every query carries a `rateLimit{cost remaining resetAt}` request, and the client sleeps when the budget is low
  - retries with backoff on 502, 503 and secondary-limit 403 responses
  - typed errors: `NotFound`, `RateLimited`, `Upstream`
- `app/github/validation.py`: the login regex.
- `user(login)` check: the user must exist and must not be a bot.
- **Tests (`respx`):** auth header present, retry then success, the client sleeps when `rateLimit` is low, 404 and bot handling, the token never appears in errors or logs.

**Review focus:** retry and rate-limit behaviour, error mapping.

## Step 5: GitHub client, contributor queries
**Goal:** fetch one contributor's PRs and reviews.
- `app/github/queries.py`: query documents. All inputs, including the full search string, are passed as **variables**.
- Search builders for authored PRs and reviewed PRs, using `created:` ranges for backfill and `updated:>` for incremental sync.
- **Date slicing:** monthly slices, split recursively when a slice hits the 1,000-result cap. Cursor pagination at 100 per page.
- Parsing into internal records. Bots and self-reviews are filtered out, and emails are never read.
- **Tests:** slicing logic (cap triggers a split), pagination, parsing, and a login carrying a qualifier-injection payload never reaches the query.

**Review focus:** query correctness, completeness of slicing.

## Step 6: Contributor sync
**Goal:** the persistence and incremental-update behaviour from design §4.
- `app/sync/contributor_sync.py`, `ensure_fresh(login)`:
  - **New login:** checks the new-contributor cap, starts a background 180-day backfill, records `status=fetching` and progress.
  - **Fresh** (within `SYNC_TTL`): no GitHub calls.
  - **Stale:** synchronous incremental sync using `updated:>watermark`, then upsert and advance the watermarks.
  - a per-login `asyncio.Lock`, a semaphore for `MAX_CONCURRENT_BACKFILLS`, retention cleanup after each sync
  - if GitHub fails and cached data exists, the data is marked `stale`
- **Tests (fake GitHub client):** the backfill moves the status from fetching to ready, a late-merged PR is picked up incrementally, no calls within the TTL, the lock prevents a double sync, the caps are enforced, the stale fallback.

**Review focus:** concurrency and state transitions.

## Step 7: Endpoint 1, contributor metrics
**Goal:** the first working endpoint, live against GitHub.
- `app/api/routes.py`: `GET /api/contributors/{login}?period=1w|1m|3m`.
- Wiring: validation → `ensure_fresh` → repository → metrics → response.
- Error mapping (design §11):
  - 422 for invalid input
  - 404 for an unknown user, a bot, or no activity
  - 202 with `Retry-After` while a backfill runs
  - 429 when a cap is reached
  - 503 when GitHub is down and nothing is cached
  - 200 with `stale` when GitHub is down but data is cached
- Per-IP rate limiting with `slowapi`. Error bodies are sanitized.
- **Tests (`TestClient` with a fake sync):** response shape, every error code, rate limit.

**Verify (needs your GitHub PAT in `.env`):** `curl localhost:8000/api/contributors/frenck?period=1m` returns 202, then 200.
**Review focus:** API contract, error responses.

## Step 8: Fixture mode and seeding scripts
**Goal:** run the service with no credentials.
- `app/github/fixture_client.py`: serves recorded GraphQL responses through the same interface as the real client.
- `scripts/record_fixtures.py`: records about 25 contributors, strips any author emails, and writes them to `fixtures/`.
- `scripts/seed_contributors.py`: backfills a given list of logins, or by default the top N from `/stats/contributors`.
- Generated `fixtures/*.json`. These are data, so they aren't counted toward the review limit; you only need to spot-check them.
- **Tests:** in fixture mode the app serves the contributor endpoint with no token set, and the recording script strips emails.

**Needs:** your GitHub PAT, once, to record the fixtures.
**Review focus:** fixture client parity with the real client, sanitization.

## Step 9: Peers, prompt input, grounding and confidence
**Goal:** everything around the LLM call, deterministic and testable before any model is involved.
- `app/analytics/peers.py`, `PeerResolver`:
  - parses the list (dedupe, drop self, `MAX_PEERS`)
  - resolves peers from the store only, giving `available` or `unavailable` with a reason
  - builds `peer_comparison` (rank and group median), or `null`
- `app/narrative/prompt.py`: the system prompt, and an input builder that produces a delimited JSON data block (metrics, top 5 PRs with length-capped titles, peer comparison).
- `app/narrative/grounding.py`:
  - resolves each evidence `ref` (a JSON path or `pr:#N`) and checks the value matches
  - drops peer refs when there are no peers, and forces the label to "Not available"
  - sets the hypothesis to null when no evidence survives
- Confidence: `min(llm, data_sufficiency)`.
- LLM output Pydantic schema in `app/models.py`.
- **Tests:** peer resolution cases, prompt input snapshot, grounding (valid, wrong path, value mismatch, peer ref without peers), confidence capping.

**Review focus:** prompt wording, grounding rules.

## Step 10: Endpoint 2, Claude narrative
**Goal:** the LLM endpoint.
- `app/llm/claude.py`:
  - official `anthropic` SDK, `claude-opus-5-5`
  - `client.messages.parse()` with the output schema
  - effort `low`, `max_tokens` capped, refusal fallback (`fallbacks: "default"`), `stop_reason` checked
- `app/llm/recorded.py`: returns recorded responses keyed by input hash.
- `app/narrative/service.py`: builds the input, checks the cache by hash, calls the LLM, applies grounding and confidence, stores the result.
- Route `GET /api/contributors/{login}/narrative?period=…&peers=…`. Returns 502 with the metrics included when the LLM fails or refuses.
- Recorded narratives for the fixture contributors (data, not counted toward the review limit).
- **Tests (stub LLM):** cache hit skips the LLM, refusal or error gives 502 with metrics, ungrounded evidence is dropped end to end, `peers_used` and `peers_unavailable` appear in the response.

**Needs:** your Anthropic key, once, to record the narratives for fixture mode.
**Review focus:** SDK usage, failure handling.

## Step 11: Docker, README and end-to-end test
**Goal:** a reviewer-ready package.
- A multi-stage `Dockerfile` that runs as a non-root user. `docker-compose.yml` defaults to fixture plus recorded mode.
- `README.md`:
  - quick start with no keys
  - live mode, including creating a fine-grained PAT and getting an Anthropic key
  - example calls for three cases: peers available, some unavailable, self only
  - a note on extending the periods
- An end-to-end test in fixture mode covering both endpoints and all three peer cases.

**Verify:** `docker compose up`, then the README's curl commands.
**Review focus:** whether someone else can reproduce it from the README alone.

---

## Credentials needed from you during implementation
| Step | What | Why |
|---|---|---|
| 7 | GitHub fine-grained PAT in `.env` | Live check of endpoint 1 |
| 8 | Same PAT | Record fixtures, once |
| 10 | Anthropic API key in `.env` | Live check of the narrative and recording responses for fixture mode |

`.env` is never committed. Steps 1–6 and 9 need no credentials.
