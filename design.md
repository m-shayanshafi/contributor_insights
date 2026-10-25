# Design: GitHub Contributor Velocity Insights

This service reports how an individual contributor's velocity in **`home-assistant/core`** changes over 1 week, 1 month or 3 months. Claude explains the numbers, compares them with the contributor's own history and, optionally, with peers the caller chooses. It gives a root-cause hypothesis, a confidence score and an evidence chain.

- **External API:** GitHub GraphQL v4
- **Stack:** Python, FastAPI, SQLite, Anthropic Claude (`claude-opus-5-5`)

---

## 1. Overview

The target repo is fixed by config (`TARGET_REPO=home-assistant/core`). It was chosen for very high activity: 3,824 merged PRs in the last 30 days, standard GitHub reviews and merges, and thousands of contributors.

| Endpoint | Purpose |
|---|---|
| `GET /api/contributors/{login}?period=1w\|1m\|3m` | Velocity metrics for the period, compared with the contributor's previous period of the same length |
| `GET /api/contributors/{login}/narrative?period=…&peers=alice,bob` | Claude narrative: self-comparison, optional peer comparison, root-cause hypothesis, confidence and evidence chain |
| `GET /healthz` | Liveness and sync status |

Periods map to fixed windows: **1w = 7 days, 1m = 30 days, 3m = 90 days**. Each period is compared with the window of the same length right before it, so up to **180 days** of history is kept per contributor.

> **Demo scope.** The demo is limited to these three periods. Longer periods (6 or 12 months) only need a larger history window (`HISTORY_DAYS` = 2 × the longest period) and new enum values. No other part of the design changes.

Data is fetched **per contributor, on demand**, persisted, and refreshed incrementally. Syncing the whole repo for 180 days would mean about 23k PRs and several hours of rate-limit budget, and much more if the design is extended to longer periods. A single contributor's history is small.

## 2. Architecture

```
Client ─▶ FastAPI routes ─▶ ContributorSync (per-login backfill / incremental) ─▶ GitHubClient (GraphQL)
                │                     │
                │                     ▼
                │            Repository (SQLite: contributors, pull_requests, reviews, narratives)
                ▼
          MetricsEngine ─▶ PeerResolver (user-provided list, store-only) ─▶ NarrativeService ─▶ ClaudeClient
```

Run modes:
- `DATA_SOURCE=github|fixtures`: live GitHub, or a bundled recorded snapshot
- `LLM_MODE=live|recorded`: live Claude, or recorded responses

## 3. GitHub integration (GraphQL v4, contributor-scoped)

Every query is scoped to one login in the target repo, using the GraphQL `search` connection (100 results per page).

| Data | Search query | Fields |
|---|---|---|
| PRs authored | `repo:home-assistant/core is:pr author:{login} created:{from}..{to}` | `number, title, createdAt, mergedAt, closedAt, updatedAt, state, additions, deletions` |
| Reviews given | `repo:home-assistant/core is:pr reviewed-by:{login} updated:{from}..{to}` | per PR: `reviews(author: {login}, first: 20){id, state, submittedAt}` |
| User check | `user(login)` | must exist and must not be a Bot |

- **Search caps results at 1,000 per query.** Date ranges are sliced by month, and any slice that hits the cap is split recursively.
- **Self-reviews are excluded.**
- **Rate limits:** each query also requests `rateLimit { cost remaining resetAt }`, and the client sleeps when the budget is low. 502, 503 and secondary-limit 403 responses are retried with exponential backoff. Every call has a timeout. At most 2 contributor backfills run at once.
- **Deliberately not tracked:**
  - **Issues closed:** GitHub search has no "closed-by" qualifier, so it would need a full-repo issue sync.
  - **Commits:** the repo squash-merges, so commits on `dev` ≈ merged PRs and add no signal.

## 4. Persistent store and incremental sync (SQLite)

### Tables
| Table | Columns |
|---|---|
| `contributors` | `login PK, covered_since, prs_watermark, reviews_watermark, last_sync_at, status (fetching\|ready\|error), progress` |
| `pull_requests` | `number PK, author_login, title, created_at, merged_at, closed_at, updated_at, state, additions, deletions` |
| `reviews` | `review_id PK, pr_number, reviewer_login, state, submitted_at` |
| `narratives` | `login, period, data_hash, response_json, created_at` |

Raw records stay in the database. The API returns only aggregates.

### Per-contributor sync: `ContributorSync.ensure_fresh(login)`
1. **First request for a login:** start a background **180-day backfill** and return `202 Accepted` with `{"status": "fetching", "progress": …}` and a `Retry-After` header. Later calls return data once the status is `ready`.
2. **Later requests:**
   - If `last_sync_at` is less than `SYNC_TTL` old (default 1 hour), serve from the database.
   - Otherwise run a synchronous **incremental** sync: search authored and reviewed PRs with `updated:>{watermark}`, then upsert. Searching by updated time picks up PRs merged after they were first stored, as well as new reviews.
3. **Retention:** a rolling 180 days (`HISTORY_DAYS`).
4. **Concurrency:** a per-login lock stops duplicate syncs of the same contributor.

## 5. Contributor endpoint

`GET /api/contributors/{login}?period=3m`

**Validation**
- `login` must match `^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$`, otherwise **422**.
- An unknown user or a bot returns **404**.
- A contributor with no PRs or reviews in the repo in the last 180 days returns **404** `"no activity in home-assistant/core"`.

### Velocity metrics
| Metric | Definition |
|---|---|
| `prs_merged` | PRs authored and merged in the window |
| `lines_changed` | Additions plus deletions across their merged PRs |
| `median_pr_size_lines` | Median lines changed per merged PR |
| `median_cycle_time_hours` | Median time from PR creation to merge |
| `reviews_given` | Reviews submitted on other people's PRs |
| `active_weeks` | Weeks with at least one PR or review, a measure of consistency |

- Each metric has `value`, `previous_period` and `pct_change`.
- A compact `series` shows the trend: daily buckets for `1w`, weekly buckets for `1m` and `3m`, each with `prs_merged` and `reviews_given`.

### Response
```json
{
  "repo": "home-assistant/core",
  "login": "someuser",
  "period": "3m",
  "window": {"from": "2026-07-27", "to": "2026-10-25"},
  "metrics": {
    "prs_merged": {"value": 14, "previous_period": 31, "pct_change": -54.8},
    "lines_changed": {"value": 5880, "previous_period": 2945, "pct_change": 99.7},
    "median_pr_size_lines": {"value": 420, "previous_period": 95, "pct_change": 342.1},
    "median_cycle_time_hours": {"value": 30.5, "previous_period": 18.0, "pct_change": 69.4},
    "reviews_given": {"value": 52, "previous_period": 40, "pct_change": 30.0},
    "active_weeks": {"value": 11, "previous_period": 12, "pct_change": -8.3},
    "series": [{"bucket": "2026-W31", "prs_merged": 2, "reviews_given": 5}]
  },
  "meta": {"last_synced_at": "2026-10-25T14:00:00Z", "stale": false}
}
```

## 6. Narrative endpoint (Claude) and optional peer comparison

`GET /api/contributors/{login}/narrative?period=3m&peers=alice,bob,carol`

The narrative uses the same sync and metrics pipeline, so its numbers always match the contributor endpoint.

### Peers are chosen by the caller (`PeerResolver`, reads the store only and never fetches)
1. `peers` is an optional comma-separated list of up to `MAX_PEERS` (default 5) logins. Each is regex-validated (422 on a bad login). The list is deduplicated, and the contributor themselves is removed.
2. Each peer is checked against the store. A peer is **available** if their status is `ready` and their stored data covers both the current and the previous window.
3. Unavailable peers are **not fetched**. They are reported as `peers_unavailable: [{login, reason: "not_fetched" | "insufficient_history" | "no_activity"}]`. Calling `GET /api/contributors/{peer}` fetches and stores that peer, so they're available next time.
4. If **at least one** peer is available, a `peer_comparison` block goes into the LLM input. It has each available peer's current-window and previous-window values for each metric, the contributor's rank within the group, and the group median for each metric. Otherwise `peer_comparison: null`.

### LLM input
- The contributor metrics from §5
- The contributor's top 5 merged PRs in the window: `number, title, lines_changed, cycle_time_hours`
- `peer_comparison`, or `null`

No raw diffs or comments are sent.

### Prompt
The system prompt describes an engineering-analytics reviewer. Claude explains how the contributor's velocity compares with their own previous period. **If `peer_comparison` is null, it compares only with the contributor's own history and says peer data wasn't available.** It proposes a root cause only when the data supports one. Patterns it should recognize:
- **Fewer but much larger PRs:** the work shifted to bigger items rather than output dropping.
- **Fewer PRs while reviews rise:** time moved to reviewing.
- **Longer cycle times while PR size stays flat:** a likely review bottleneck.
- **Activity concentrated in a few weeks:** bursty contribution.
- **Very little data:** calls for low confidence.
- **With peers:** whether the change is group-wide (for example, everyone slowed down over the holidays) or specific to this contributor.

### Model call
- `claude-opus-5-5` through the official `anthropic` Python SDK.
- **Structured outputs** (`client.messages.parse()` with a Pydantic schema). Forced tool use isn't supported on this model.
- Effort `low` (a short classification-style task), `max_tokens` capped.
- Server-side refusal fallback (`fallbacks: "default"`) enabled. `stop_reason` is checked before the output is parsed.

### Output schema
```json
{
  "narrative": "2–4 sentences",
  "self_comparison": "Faster|Slower|Steady",
  "peer_comparison": "Above peers|In line|Below peers|Not available",
  "root_cause_hypothesis": "string, or null if the signal doesn't support one",
  "confidence": 0.0,
  "evidence_chain": [
    {"claim": "...", "ref": "metrics.prs_merged.pct_change | peer_comparison.group_median.reviews_given | pr:#12345", "value": "-54.8"}
  ]
}
```

### Post-processing
- **Grounding check:** every `evidence_chain[].ref` has to resolve to a real path in the input JSON, or to a PR in the top-5 list, and its `value` has to match the input.
  - Items that fail are dropped and counted in `ungrounded_evidence_dropped`.
  - If `peer_comparison` was null, any peer ref is dropped and the peer label is forced to `"Not available"`.
  - If no grounded evidence remains, `root_cause_hypothesis` is set to `null`.
- **Confidence:** `final = min(llm_confidence, data_sufficiency)`, where `data_sufficiency = min(1, (PRs + reviews across both windows) / 20)`. Both values are returned.
- **Response extras:** `peers_used` and `peers_unavailable`.
- **Caching:** responses are stored under the SHA-256 of the input payload, which includes the resolved peers and their data. Unchanged inputs never trigger a second LLM call. A different peer list, or a peer becoming available, produces a fresh narrative.
- **Failure:** if the LLM call fails or is refused, the endpoint returns **502** with the metrics still included.

## 7. Security practices

**Secrets**
- No credentials in the repo. `.env` is in `.gitignore`, and `.env.example` contains placeholders only.
- Secrets are loaded with `pydantic-settings` as `SecretStr`, so they never appear in reprs, logs or tracebacks. Live mode fails fast with a clear message when one is missing.
- Tokens are kept in memory only and never persisted. A logging filter redacts `Authorization` and `x-api-key` headers.

**Least privilege**
- GitHub: a **fine-grained personal access token** with "Public repositories (read-only)", **no permissions granted** and a short expiry. It can't write anything or read private data.
- Anthropic: a dedicated workspace key with a spend limit set in the Console.

**Input handling**
- `login` (and each peer login, although peers are only looked up in SQLite) goes into a GitHub **search string**. The strict regex (letters, digits and hyphens; no spaces, colons or quotes) prevents **search-qualifier injection** such as `x repo:other/repo`.
- The full search string is passed as a GraphQL **variable**, never interpolated into the query document.
- The repo comes from config, never from input. `period` is an enum. All SQL uses parameter binding.
- Outbound hosts are hard-coded (`api.github.com`, `api.anthropic.com`), so user input can't redirect requests (no SSRF). TLS verification stays on, and every call has a timeout.

**Abuse and cost control**
- Per-IP rate limiting (`slowapi`, 30/min).
- A cap on concurrent backfills and on new contributors per hour (`MAX_NEW_CONTRIBUTORS_PER_HOUR`).
- Narrative caching.

Together these bound both GitHub and Anthropic usage.

**LLM-specific**
- PR titles are untrusted input. They go into a delimited JSON data block, the system prompt says to treat that block as data rather than instructions, and they are length-capped.
- Output is schema-constrained, and the grounding check drops any evidence the model invents.

**Service hardening**
- Error responses never include stack traces or upstream response bodies.
- Pinned dependencies (`uv.lock`).
- The Docker image runs as a non-root user.
- CI and pre-commit tooling are out of scope. The scope is the security of the submitted code itself.

**Privacy**
- Only public GitHub activity and logins are stored. No emails.

## 8. Reproducibility without the author's credentials

1. **Fixture mode (no keys):** `DATA_SOURCE=fixtures LLM_MODE=recorded docker compose up` loads recorded 180-day data for about 25 `home-assistant/core` contributors (a mix of maintainers and external contributors) plus recorded Claude responses. The recorded data still goes through the real metrics, peer resolution, grounding and confidence code. The README gives example calls for three cases:
   - all peers available (`peers=` other fixture logins)
   - some unavailable (includes a non-fixture login)
   - self only (no `peers`)
2. **Tests:** `pytest` uses `respx` to mock GitHub and a stub Claude client. No credentials or network needed.
3. **Live mode (reviewer's own keys):** the README walks through creating a fine-grained PAT (no approval required) and an Anthropic API key, then copying `.env.example` to `.env`.
4. **Helper scripts:**
   - `scripts/seed_contributors.py` backfills a list of logins, or by default the repo's top N contributors from `/stats/contributors`, so they're available as peers.
   - `scripts/record_fixtures.py` builds the fixture snapshot.

## 9. Configuration

| Variable | Default / notes |
|---|---|
| `TARGET_REPO` | `home-assistant/core` |
| `DATA_SOURCE` | `github` \| `fixtures` |
| `LLM_MODE` | `live` \| `recorded` |
| `GITHUB_TOKEN` | fine-grained PAT (live mode) |
| `ANTHROPIC_API_KEY` | live LLM mode |
| `CLAUDE_MODEL` | `claude-opus-5-5` |
| `DB_PATH` | `./data/insights.db` |
| `SYNC_TTL_SECONDS` | `3600` (1 hour) |
| `HISTORY_DAYS` | `180` (2 × the longest period) |
| `MAX_CONCURRENT_BACKFILLS` | `2` |
| `MAX_NEW_CONTRIBUTORS_PER_HOUR` | `20` |
| `MAX_PEERS` | `5` |
| `RATE_LIMIT` | `30/minute` |

## 10. Project layout

```
app/
  main.py               # app factory, lifespan (DB init, HTTP clients)
  config.py             # pydantic-settings, SecretStr
  models.py             # request/response + LLM output schemas
  api/routes.py
  github/client.py      # GraphQL client, rate limiting, retries
  github/queries.py     # query documents (variables only)
  github/fixture_client.py
  store/db.py           # schema + connection
  store/repository.py
  sync/contributor_sync.py
  analytics/metrics.py  # six metrics, deltas, series
  analytics/peers.py    # PeerResolver
  llm/claude.py
  llm/recorded.py
  narrative/prompt.py
  narrative/service.py  # grounding, confidence, cache
fixtures/
scripts/seed_contributors.py
scripts/record_fixtures.py
tests/
pyproject.toml  uv.lock  Dockerfile  docker-compose.yml  .env.example  .gitignore  README.md  design.md
```

Dependencies: `fastapi`, `uvicorn`, `httpx`, `pydantic-settings`, `aiosqlite`, `anthropic`, `slowapi`. Dev: `pytest`, `pytest-asyncio`, `respx`.

## 11. Error handling

| Case | Status |
|---|---|
| Invalid login, peer login, period, or more than `MAX_PEERS` peers | 422 |
| Unknown user, bot, or no activity in the repo | 404 |
| First request for a login, backfill in progress | 202 + progress + `Retry-After` |
| New-contributor cap reached | 429 |
| Client rate limit exceeded | 429 |
| GitHub unavailable, cached data available | 200 + `meta.stale: true` |
| GitHub unavailable, no cached data | 503 |
| LLM failure or refusal | 502 (metrics included) |

## 12. Testing strategy

- **Metrics:** the six metrics and previous-period deltas, series bucketing per period, search date slicing at the 1,000-result cap.
- **Peers:** list parsing (dedupe, drop self, max size), store-only resolution, unavailable reasons, `null` when none are available, and peer data never fetched.
- **Grounding:** path resolution, value matching, and peer refs dropped when there are no peers.
- **Sync:** backfill returns 202 then ready, an incremental `updated:>` search picks up a late merge, the per-login lock.
- **API:** response shapes and error codes, plus an end-to-end fixture run covering the with-peers and without-peers narratives.
- **Security:** logins carrying qualifier-injection payloads are rejected, and secrets never appear in logs or error bodies.

## 13. Verification

- `pytest` passes with no credentials set.
- **Fixture mode:**
  - `curl localhost:8000/api/contributors/{fixture_login}?period=3m` returns the documented shape and deltas.
  - `/narrative?period=3m&peers=a,b` with fixture peers returns a peer comparison.
  - With a non-fixture peer, that peer appears in `peers_unavailable`.
  - With no `peers`, the label is `"Not available"`.
  - Every evidence ref resolves.
- **Live mode:**
  - The first call to a new login returns 202, then 200 once ready.
  - A repeat call within the TTL makes no GitHub calls.
  - After the TTL, only `updated:>watermark` searches run (visible in the logs).

---

## Appendix: decision log

**Why GitHub.** A fine-grained token needs no approval, the API is stable, and the signals (PRs, sizes, cycle times, reviews) support real root-cause reasoning.

**Why per-contributor fetching.** Self-comparisons need twice the period in history: 180 days for the demo's 3-month period, and 2 years if the design is extended to 12 months. Fetching that per contributor is cheap, while a full-repo sync of `home-assistant/core` would not be.

**Why 1w / 1m / 3m for the demo.** These three periods keep backfills fast and the fixtures small. Extending to longer periods is a config change (see §1).

**Why caller-chosen peers.** The caller knows which comparison is meaningful. Peers are compared only from data already stored, which keeps the cost of each request bounded.
