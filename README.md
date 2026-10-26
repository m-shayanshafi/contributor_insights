# Contributor Insights

A small service that measures how an individual contributor's velocity in
[`home-assistant/core`](https://github.com/home-assistant/core) changes over time, and
asks an LLM (Google Gemini) to explain the change with a root-cause hypothesis, a
confidence score and an evidence chain.

- **External API:** GitHub GraphQL v4, fetched per contributor on demand and stored in SQLite
- **Insight endpoint:** six velocity metrics for a period, compared with the contributor's
  previous period of the same length
- **LLM endpoint:** a Gemini narrative whose evidence is checked against the metrics before
  it is returned

`home-assistant/core` was chosen for its very high activity (thousands of merged PRs a
month), which makes period-over-period changes meaningful.

Submission notes (architecture tour, trade-offs, next steps, AI usage) are in
[NOTES.md](NOTES.md).

## Quick start (no credentials)

The repo ships with recorded data for 25 active contributors and recorded Gemini
narratives, so it runs without any keys:

```bash
docker compose up --build
```

Then, in another terminal:

```bash
curl -s localhost:8000/healthz
curl -s "localhost:8000/api/contributors/joostlek?period=3m"
curl -s "localhost:8000/api/contributors/zweckj/narrative?period=1m"
```

Interactive API docs are at http://localhost:8000/docs.

In this mode the clock is frozen at the time the data was recorded, so windows always
line up with the data. Recorded contributors: `epenet, frenck, gjohansson-ST, erwindouna,
emontnemery, joostlek, balloob, tr4nt0r, Bre77, raman325, RaHehl, bieniu, zweckj,
chemelli74, darkrain-nl, mosandlt, jbouwh, abmantis, arturpragacz, Shulyaka, balloobbot,
rrooggiieerr, farmio, lackas, piitaya`. Any other login returns 404.

## Live mode (your own keys)

Live mode fetches real, current data from GitHub for any contributor to
`home-assistant/core` and has Gemini write a fresh narrative for each request (cached
until the data changes). It needs two free credentials of your own:

1. **GitHub token:** create a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new)
   with *Repository access: Public repositories (read-only)*, no permissions and a short
   expiry.
2. **Gemini key:** create a free API key in [Google AI Studio](https://aistudio.google.com)
   (no billing needed; limits are on its [rate-limit page](https://aistudio.google.com/rate-limit)).
3. Configure:

   ```bash
   cp .env.example .env && chmod 600 .env
   # then set in .env:
   # DATA_SOURCE=github
   # LLM_MODE=live
   # GITHUB_TOKEN=github_pat_...
   # GEMINI_API_KEY=...
   ```

4. Run `docker compose up --build` and call any login:

   ```bash
   curl -i "localhost:8000/api/contributors/emontnemery?period=1m"   # 202 while history loads
   curl -s "localhost:8000/api/contributors/emontnemery?period=1m"   # 200 once ready
   ```

The first request for a contributor starts a background load of their last 180 days and
returns 202. After that, data is refreshed at most once an hour with an incremental sync of
everything updated since the last one. The app refuses to start if a mode's key is missing.

Helper scripts (live mode):

```bash
uv run python -m scripts.seed_contributors [--top 10] [login ...]   # pre-load contributors
uv run python -m scripts.record_fixtures [--top 25] [login ...]     # re-record fixtures
uv run python -m scripts.record_narratives                          # re-record narratives
```

## API

| Endpoint | Returns |
|---|---|
| `GET /api/contributors/{login}?period=1w\|1m\|3m` | Metrics for the period vs the previous period |
| `GET /api/contributors/{login}/narrative?period=1w\|1m\|3m` | Gemini narrative, hypothesis, confidence, evidence, plus the metrics |
| `GET /healthz` | Liveness and run mode |

`period` defaults to `1m`. Periods are fixed windows (1w = 7 days, 1m = 30 days,
3m = 90 days) ending now, each compared with the window of the same length right before it.

### Metrics

| Metric | Definition |
|---|---|
| `prs_merged` | PRs the contributor authored that were merged in the window |
| `lines_changed` | Additions plus deletions across those PRs |
| `median_pr_size_lines` | Median lines changed per merged PR |
| `median_cycle_time_hours` | Median time from PR creation to merge |
| `reviews_given` | Reviews submitted on other people's PRs (self-reviews excluded) |
| `active_weeks` | Weeks with at least one merged PR or review |

Each metric has `value`, `previous_period` and `pct_change`. A `series` gives the trend:
daily buckets for `1w`, weekly for `1m` and `3m`.

**Why these metrics.** Each one answers a different question, and together they let a change
be explained rather than just reported. `prs_merged` and `lines_changed` measure output, but on
their own they can't tell "slowed down" from "took on bigger work", so `median_pr_size_lines`
separates the two. `median_cycle_time_hours` shows whether merges are waiting on review, which
matters most when PR size hasn't changed. `reviews_given` captures time spent unblocking other
people, which never shows up in PR counts. `active_weeks` and the `series` show whether the
work is steady or concentrated in bursts. Medians are used instead of means because a single
huge refactor would otherwise dominate. These are also the signals the narrative uses to form
a root-cause hypothesis: for example, fewer PRs with much larger sizes points to bigger work
items, and fewer PRs with more reviews points to time moving into reviewing.

### Narrative response (abridged)

`GET /api/contributors/zweckj/narrative?period=1m` in fixture mode:

```json
{
  "login": "zweckj",
  "period": "1m",
  "narrative": "During this period, zweckj significantly increased their productivity, merging 206.7 percent more pull requests and slashing their median cycle time from 33.0 hours down to 5.9 hours. Despite the surge in merged pull requests, their median pull request size dropped by 66.3 percent alongside a 53.5 percent increase in total lines changed. ...",
  "self_comparison": "Faster",
  "root_cause_hypothesis": "The work shifted toward a much higher volume of smaller pull requests, allowing faster merges and significantly reduced cycle times.",
  "confidence": 0.9,
  "llm_confidence": 0.9,
  "data_sufficiency": 1.0,
  "evidence_chain": [
    {"claim": "Merged pull requests increased by 206.7 percent", "ref": "metrics.prs_merged.pct_change", "value": "206.7"},
    {"claim": "Median cycle time dropped to 5.9 hours", "ref": "metrics.median_cycle_time_hours.value", "value": "5.9"},
    {"claim": "Median pull request size decreased by 66.3 percent", "ref": "metrics.median_pr_size_lines.pct_change", "value": "-66.3"}
  ],
  "ungrounded_evidence_dropped": 0,
  "metrics": {
    "prs_merged": {"value": 46, "previous_period": 15, "pct_change": 206.7},
    "median_pr_size_lines": {"value": 92, "previous_period": 273, "pct_change": -66.3},
    "median_cycle_time_hours": {"value": 5.9, "previous_period": 33.0, "pct_change": -82.1},
    "...": "three more metrics and the weekly series"
  },
  "meta": {"last_synced_at": "2026-10-26T01:42:05Z", "stale": false, "cached": false, "model": "recorded"}
}
```

When the data doesn't support a clear cause, `root_cause_hypothesis` is `null` rather than
a guess (for example `frenck?period=1m`, where output simply rose across the board).

How the narrative is kept honest:

- **Input:** only the metrics, the series and the contributor's five largest PRs (number,
  title, size, cycle time). The model compares the contributor only with their own history.
- **Structured output:** Gemini must return JSON matching a fixed schema, which is
  validated again on arrival.
- **Grounding:** every evidence `ref` must point at a real value in the input (a JSON path
  such as `metrics.prs_merged.pct_change`, or `pr:#12345`) and its `value` must match.
  Anything else is dropped and counted in `ungrounded_evidence_dropped`. If no evidence
  survives, the hypothesis is set to `null`.
- **Confidence:** `confidence = min(llm_confidence, data_sufficiency)`, where
  `data_sufficiency = min(1, PRs and reviews across both windows / 20)`, so thin data can't
  produce a confident story.
- **Caching:** narratives are cached by a hash of the prompt and input, so unchanged data
  never calls the LLM twice.

### Status codes

| Case | Status |
|---|---|
| Invalid login or period | 422 |
| Unknown GitHub user, or no activity in the repo in 180 days | 404 |
| First request for a contributor while their history loads | 202, with `progress` and `Retry-After` |
| Per-client rate limit or new-contributor hourly cap | 429 |
| GitHub down and nothing cached | 503 (with cached data: 200 and `meta.stale: true`) |
| LLM failed, refused or hit its quota | 502, with the metrics still included |

## Tests

```bash
uv run pytest
```

The suite needs no credentials or network: GitHub is mocked with `respx`, Gemini with a
stub, and an end-to-end test runs both endpoints against the committed fixtures.

## Security

- **Secrets** live only in `.env` (git- and docker-ignored) and are held as `SecretStr`, so
  they never appear in reprs or errors. A log filter redacts `Authorization` and
  `x-goog-api-key` headers and known token formats.
- **Least privilege:** a read-only, permission-less GitHub token; a free-tier Gemini key
  with no billing.
- **Injection:** logins must match GitHub's own rules (letters, digits, hyphens), which
  blocks GitHub search-qualifier injection such as `x repo:other/repo`. Search strings go
  in GraphQL variables, never into the query text. All SQL uses bound parameters.
- **Prompt injection:** PR titles are untrusted; control characters are stripped, length is
  capped, and the data block is escaped so a title can't close it early. The model output is
  schema-checked and grounded before it is returned.
- **Abuse and cost:** per-client rate limit (30/minute), at most 20 new contributors an hour,
  at most 2 concurrent history loads, narrative caching.
- **Service:** fixed outbound hosts, error bodies without stack traces or upstream details,
  a non-root, read-only container with all capabilities dropped. Only public GitHub data is
  stored; emails are never requested.

## Extending the periods

The demo is limited to `1w`, `1m` and `3m`. Adding longer periods (for example 6 or 12
months) means adding values to the `Period` enum in `app/analytics/periods.py`; the
required history (`2 x` the longest period) follows automatically, and `HISTORY_DAYS` must be
raised to match. Nothing else changes.

## Project layout

```
app/
  main.py, config.py, services.py, models.py, logging_setup.py
  api/            routes and per-client rate limiting
  github/         GraphQL client, contributor queries, fixture source, login validation
  sync/           per-contributor backfill and incremental sync
  store/          SQLite schema and repository
  analytics/      periods, metrics, window records
  narrative/      prompt, grounding and confidence, narrative service
  llm/            Gemini client and recorded responses
fixtures/         recorded GitHub data and narratives
scripts/          recording and seeding scripts
tests/
```

## Configuration

All settings come from environment variables or `.env` (see `.env.example`):
`DATA_SOURCE`, `LLM_MODE`, `GITHUB_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL`
(default `gemini-3.5-flash-lite`), `TARGET_REPO`, `DB_PATH`, `SYNC_TTL_SECONDS` (3600),
`HISTORY_DAYS` (180), `MAX_CONCURRENT_BACKFILLS` (2), `MAX_NEW_CONTRIBUTORS_PER_HOUR` (20),
`RATE_LIMIT` (`30/minute`).
