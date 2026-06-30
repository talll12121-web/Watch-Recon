# WatchExchange Tracker — Project Handoff (for Claude Code)

> Context file summarizing a build done in a separate Claude chat. Hand this to
> Claude Code in the terminal to continue. You can rename it to `CLAUDE.md` to
> have Claude Code auto-load it as project memory.

## TL;DR
A small Flask web app that tracks r/WatchExchange via Reddit's **public RSS**
(no Reddit API / credentials), matches watches I'm hunting and users I follow,
and sends **Discord** notifications (which reach my iPhone through the Discord
app). It has an interactive UI to add/remove watches live. **Built and tested;
not deployed yet.** The remaining job is: run locally to verify, then deploy to
a 24/7 host (Railway or Render).

## Goal
- Monitor r/WatchExchange for specific watch models/refs (e.g. `submariner 16610`).
- Also alert on every post by specific followed users.
- Notify via Discord → lands on my iPhone.
- Web UI to add/remove watches and users live (no restart, no editing files).

## Key decision: NO Reddit API
Reddit's API app creation (`reddit.com/prefs/apps`) is now gated behind a new
(June 2026) "Responsible Builder" registration at
`developers.reddit.com/app-registration`, and the create-app step kept silently
failing. We **pivoted to Reddit's public RSS feeds**, which need zero credentials:
- Subreddit: `https://www.reddit.com/r/{sub}/new/.rss`
- User: `https://www.reddit.com/user/{name}/submitted/.rss`

If RSS ever returns HTTP 429 (rate limit), the code backs off and retries. An
authenticated PRAW "API edition" exists as a fallback only if credentials are
ever obtained — not needed for now.

## Architecture
- **app.py** — Flask app. A background daemon thread polls the RSS feeds every
  `POLL_INTERVAL_SECONDS`, matches against the DB watch/user lists, sends the
  Discord webhook, and records each alert. Serves the UI and a JSON API.
- **SQLite** (`tracker.db`) — tables: `watches`, `users`, `seen` (dedup),
  `alerts` (last ~200, shown in UI).
- **templates/index.html** — interactive UI (vanilla JS, same-origin fetch).
- Only secret in the whole system = `DISCORD_WEBHOOK_URL`.

## Files
| file | purpose |
|------|---------|
| `app.py` | backend, RSS poller, matching, Discord, JSON API |
| `templates/index.html` | interactive control-panel UI |
| `requirements.txt` | flask, gunicorn, requests, feedparser, python-dotenv |
| `Procfile` | `web: gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT app:app` |
| `Dockerfile` | container build (single worker) |
| `.env.example` | env var template |
| `README.md` | setup + deploy guide |

## Matching rules
- A watch = list of `terms` (ALL must appear in title+body) + optional `exclude`
  terms (skip if any appear). UI input `submariner 16610` → terms; `submariner -date`
  → term `submariner`, exclude `date`.
- Watch matches only count for posts tagged `WTS`/`WTT` in the title
  (`WATCH_REQUIRE_TAGS`). Followed-user alerts fire on ANY of their posts in the sub.
- Dedup by RSS entry id. First run "primes" the `seen` table silently to avoid a
  notification storm; set `NOTIFY_ON_FIRST_RUN=true` once to test on existing posts.

## Environment variables
| var | required | default | notes |
|-----|----------|---------|-------|
| `DISCORD_WEBHOOK_URL` | yes | — | **secret — never commit**; user will provide |
| `APP_PASSWORD` | strongly recommended | empty | enables Basic Auth on the UI (any username) |
| `SUBREDDIT` | no | `Watchexchange` | |
| `POLL_INTERVAL_SECONDS` | no | `180` | |
| `WATCH_REQUIRE_TAGS` | no | `WTS,WTT` | |
| `MAX_POST_AGE_MINUTES` | no | `720` | ignore older posts |
| `USER_AGENT` | no | descriptive default | reduces 429s |
| `DB_PATH` | no | `./tracker.db` | **point at a persistent volume in the cloud** |

## Status
✅ Built. ✅ API + matching + exclusions + dedup + UI verified via Flask test client.
❌ Not run against live Reddit yet (was tested offline). ❌ Not deployed.

## NEXT ACTIONS (do these)
1. `cd` into the project. Create venv and `pip install -r requirements.txt`.
2. `cp .env.example .env`. Ask the user for `DISCORD_WEBHOOK_URL` and have them
   choose an `APP_PASSWORD`; write them into `.env`. **Do not hardcode the webhook
   anywhere that gets committed.**
3. Run locally: `python app.py` → open `http://localhost:8000`. Click
   **Send test alert**; confirm a message appears in their Discord channel.
4. Add a watch (`submariner 16610`) and a user via the UI; confirm they persist
   and the status line shows recent polls. Optionally set `NOTIFY_ON_FIRST_RUN=true`
   once to confirm a real match fires, then unset it.
5. Deploy for 24/7:
   - **Railway**: deploy from repo (auto-detects `Procfile`); set env vars; add a
     **Volume** at `/data` and set `DB_PATH=/data/tracker.db`.
   - **Render**: Web Service; start cmd `gunicorn -w 1 --threads 8 app:app`; set env
     vars; add a **Disk** at `/data` and `DB_PATH=/data/tracker.db`.
   - Keep **one worker** so only one poller runs.
6. iPhone: install Discord, enable notifications for the channel. Optionally open
   the app URL in Safari → Add to Home Screen to manage watches like an app.

## Optional follow-ups (not done yet)
- Generate `render.yaml` / `railway.json` for one-click config.
- Add native iOS push via **ntfy** or **Pushover** alongside Discord.
- Swap to the authenticated PRAW "API edition" if Reddit credentials are obtained.

## Gotchas / constraints
- Single gunicorn worker only (multiple workers = multiple pollers = duplicate alerts).
- Free cloud filesystems are ephemeral → without a mounted volume, the watch list
  resets on redeploy. `seen` resetting is harmless (it re-primes, no spam).
- The webhook is server-side only and is never sent to the browser.
- iPhone cannot host the poller (iOS kills background tasks) — it only receives
  alerts via Discord.
- `.env` and `*.db` must not be committed (see `.gitignore`).
