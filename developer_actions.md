# Developer Actions

These are the things only you can do: creating credentials and putting them where the service can read them. Claude never needs to see the actual values, and you should never paste them into the chat.

## Summary

| # | Action | Needed by | Time |
|---|---|---|---|
| 1 | Check local tools | Step 1 | Done ✅ |
| 2 | Create a GitHub fine-grained token | Steps 7–8 | ~3 min |
| 3 | Create an Anthropic API key, with a spend limit | Step 10 | ~3 min |
| 4 | Create `.env` and fill in the tokens | Step 7, then again at step 10 | ~1 min |
| 5 | Run the token check | Step 7 | ~1 min |
| 6 | Revoke the tokens when you're done | After submission | ~1 min |

Steps 1–6 and 9 of the implementation plan need **no credentials**. You can do actions 2–5 just before step 7.

---

## 1. Local tools ✅
Everything is already installed on this machine:

| Tool | Installed | Used for |
|---|---|---|
| Python | 3.14.7 | uv installs the project's Python 3.12 automatically |
| uv | 0.11.3 | dependencies and running the app |
| Docker | 27.4.0 | `docker compose up` demo (step 11) |
| git | 2.39.3 | one commit per approved step |

No action needed.

## 2. GitHub fine-grained personal access token
Used to read public data from `home-assistant/core`. The token is read-only and can't change anything.

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens** (https://github.com/settings/personal-access-tokens/new).
2. Fill in:
   - **Token name:** `contributor-insights-dev`
   - **Expiration:** 30 days, or the shortest that covers your review
   - **Resource owner:** your own account
   - **Repository access:** **Public repositories (read-only)**
   - **Permissions:** leave **everything unset**. Reading public repos needs none.
3. Click **Generate token** and copy it. It starts with `github_pat_`, and GitHub shows it only once.

No approval is needed. The token works immediately.

## 3. Anthropic API key (needed from step 10)
Used for the narrative endpoint.

1. Go to the **Anthropic Console** (https://console.anthropic.com), then **Settings → Workspaces**. Create a workspace named `contributor-insights`.
2. Under that workspace, set a **monthly spend limit**, for example $10. Each narrative is one short call, and responses are cached, so development should cost very little.
3. Go to **API Keys → Create Key**, select the `contributor-insights` workspace, and copy the key. It starts with `sk-ant-` and is shown only once.

## 4. Create `.env`
The repo will include `.env.example` (created in step 1). `.env` is in `.gitignore`, so it will never be committed.

Once step 1 is done, run:
```bash
cd /Users/mshayan/loops
cp .env.example .env
chmod 600 .env
code .env
```
Fill in the values and save:
```bash
DATA_SOURCE=github
LLM_MODE=recorded                 # switch to "live" at step 10
GITHUB_TOKEN=github_pat_...
ANTHROPIC_API_KEY=                # fill in at step 10
```
The app refuses to start when a mode's credential is missing: `DATA_SOURCE=github` needs `GITHUB_TOKEN`, and `LLM_MODE=live` needs `ANTHROPIC_API_KEY`. Keep `LLM_MODE=recorded` until you have the Anthropic key.

How Claude uses it:
- The app reads `.env` itself when Claude runs `uv run …` commands, so Claude never opens or prints the file.
- Until you fill in `.env`, Claude works in `DATA_SOURCE=fixtures` and `LLM_MODE=recorded` modes, or with mocked tests, which need no tokens.

## 5. Check the GitHub token (before step 7)
Run this yourself. It prints only your login and the remaining rate limit, never the token:
```bash
cd /Users/mshayan/loops
set -a; source .env; set +a
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/graphql \
  -d '{"query":"{ viewer { login } rateLimit { limit remaining } }"}'
```
Expected output: `{"data":{"viewer":{"login":"<you>"},"rateLimit":{"limit":5000,"remaining":4999}}}`. A `401` means the token was copied wrong or has expired.

For the Anthropic key, Claude runs a check at step 10 using the app's own client, and the check prints only success or failure.

## 6. After submission
- Revoke the GitHub token: Settings → Developer settings → Fine-grained tokens → **Delete**.
- Disable or delete the Anthropic key in the Console. Lower or remove the workspace spend limit.
- Optionally delete `.env` and `data/insights.db` locally.

---

## Rules of thumb
- **Never paste a token into the chat.** Put it in `.env` only.
- If a token gets exposed (pasted, committed or logged), revoke it right away and create a new one. The app's log redaction is a safety net, not a guarantee.
- Reviewers of your submission don't need your tokens. Fixture mode (`docker compose up`) runs with none, and the README explains how they can create their own.
