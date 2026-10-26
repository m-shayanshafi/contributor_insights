# Submission notes

## 1. How to run it locally

**Prerequisite:** Docker with Compose v2.24 or later (Docker Desktop is fine). No API keys are
needed for the default mode.

```bash
git clone https://github.com/m-shayanshafi/contributor_insights.git
cd contributor_insights
docker compose up --build
```

Then:

```bash
curl -s localhost:8000/healthz
curl -s "localhost:8000/api/contributors/joostlek?period=3m"
curl -s "localhost:8000/api/contributors/zweckj/narrative?period=1m"
```

This runs on recorded GitHub data for 25 contributors and recorded Gemini narratives, so it
works offline and without credentials. API docs are at http://localhost:8000/docs.

**Live mode** (real GitHub data, live Gemini): copy `.env.example` to `.env` and set

| Variable | Value |
|---|---|
| `DATA_SOURCE` | `github` |
| `LLM_MODE` | `live` |
| `GITHUB_TOKEN` | fine-grained PAT, *Public repositories (read-only)*, no permissions |
| `GEMINI_API_KEY` | free key from Google AI Studio |

then run `docker compose up --build` again. The [README](README.md) has step-by-step
instructions for both credentials and the full list of optional settings.

**Tests** (needs [uv](https://docs.astral.sh/uv/)): `uv run pytest`. No credentials or network
needed.

## 2. Architecture tour

A request to `GET /api/contributors/{login}` goes through four layers. The route validates the
login against GitHub's username rules and asks `ContributorSync` to make sure that
contributor's data is fresh. The first time a login is seen, the sync starts a background
load of their last 180 days from the GitHub GraphQL API and the route returns `202` with
progress; after that, data is served from SQLite and refreshed at most once an hour with an
incremental search for everything updated since the last sync. The GitHub client handles the
awkward parts of the API: search results are capped at 1,000, so date ranges are cut into
slices that are split again when they hit the cap; the rate-limit budget is tracked; and
transient failures are retried. Once the data is stored, the metrics engine (pure functions)
computes six metrics for the current and previous windows. A fixture mode replaces the GitHub
client with recorded data behind the same interface, with the clock frozen at the recording
time, so reviewers can run everything without credentials.

`GET /api/contributors/{login}/narrative` reuses the same sync and metrics, so the numbers
always match the first endpoint. It builds a compact JSON input (the metrics, the weekly
series and the contributor's five largest PRs), sends it to Gemini with a schema the output
must follow, and then checks the result instead of trusting it: every evidence item must
point at a real value in the input and match it, unsupported evidence is dropped, the
hypothesis is cleared if nothing survives, and confidence is capped by how much data there
is. Results are cached by a hash of the prompt and input, so unchanged data never calls the
model twice. If the model fails or refuses, the endpoint returns `502` with the metrics still
in the body.

**Main decisions and trade-offs**

- **One contributor over time, not a leaderboard.** I found "how has this person's work
  changed, and why?" more interesting than a top-N list, and it suits a root-cause narrative.
  The cost is that the caller needs to know a username, and there's no team-level view.
- **Fetch per contributor on demand, not the whole repo.** `home-assistant/core` merges
  thousands of PRs a month, and syncing 180 days of all of them would take hours of rate-limit
  budget. One contributor's history takes seconds to a couple of minutes.
- **Fixed rolling windows (1w, 1m, 3m), not arbitrary dates.** Each period is compared with
  the one before it, so the stored history is bounded at twice the longest period and caching
  stays simple. Longer periods are an enum change; arbitrary ranges would need a check that
  the requested range is covered by stored history.
- **Compare a contributor only with their own past.** Comparing different people mixes in
  differences in role and project area that this data can't explain.
- **Ground the LLM instead of trusting it.** The evidence check and the confidence cap mean
  the narrative can't cite numbers that don't exist or sound confident on thin data. Across
  the 75 recorded narratives, 237 evidence items passed and 2 were dropped.
- **Fixture and recorded modes.** This makes the demo reproducible without my credentials.
  The trade-off is about 4.5 MB of recorded data in the repo and a frozen clock in that mode.
- **SQLite, single process.** Enough for this scope. The per-contributor locks and the rate
  limiter live in memory, so running several instances would need Redis or Postgres.
- **Python and Gemini.** Python is the stack I'm fastest in. Gemini's free tier keeps the
  project free to run and lets reviewers use their own free key.

## 3. What I'd do next with another day

1. **Eval harness.** Replay the recorded narratives (or call Gemini live) and report schema
   validity, how much evidence survives the check, how often a hypothesis is given and the
   confidence spread, so prompt or model changes can be compared before they ship.
2. **A team view.** List the stored contributors with their metrics, so there's an entry
   point that doesn't require knowing a username.
3. **Periodic background refresh** for known contributors, so requests never wait on a sync.
4. **Small React frontend** with a contributor picker, the trend series and the narrative.
5. **Arbitrary date ranges**, with a coverage check against stored history.


## 4. How I used AI

I built this with Claude Code. I set the requirements and constraints, had it draft a design
document and an 11-step implementation plan with each step capped at 500 lines including
tests, and reviewed every step before it was committed. Each commit in the history is one
approved step, so the history shows how the project came together.

I made the product and design decisions and pushed back where the output wasn't what I
wanted. I chose GitHub and a single high-activity repo, cut the periods to 1w/1m/3m for the
demo, removed a peer-comparison feature I decided didn't belong, raised the sync TTL from 15
minutes to an hour, switched the LLM to Gemini's free tier, and asked for medians to be whole
numbers and for README changes.

Reviewing and testing each step caught real problems:
- The off-the-shelf rate limiter (`slowapi`) silently did nothing on our routes with this
  FastAPI version; a test caught it and I replaced it with a small dependency.
- The first version of the log redaction broke uvicorn's access logs.
- Fixture seeding initially tripped the new-contributor cap and returned 429 instead of 404.

I also checked the numbers against GitHub directly: merged-PR counts for several contributors
and periods match a live GitHub search exactly. The fixture data and recorded narratives were
generated by the scripts in `scripts/`.
