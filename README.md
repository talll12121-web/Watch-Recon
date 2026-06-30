# WatchExchange Tracker — web app

An always-on tracker with a live control panel. Reads Reddit's **public RSS**
(no Reddit account, app, or credentials), matches the watches you're hunting and
the users you follow, and sends a **Discord** notification — which lands on your
phone through the Discord app. Add and remove watches from the UI and the next
poll picks them up immediately.

```
┌────────────┐   polls RSS    ┌─────────────┐   webhook    ┌─────────┐
│  Reddit    │ ─────────────▶ │  this app   │ ───────────▶ │ Discord │ ─▶ 📱
│  (public)  │                │ (cloud host)│              │ channel │
└────────────┘                └─────────────┘              └─────────┘
                                     ▲
                                 you open the
                                 UI to manage watches
```

## What you need

1. A **Discord webhook URL** — in your server: Channel → *Edit Channel* →
   *Integrations* → *Webhooks* → *New Webhook* → *Copy Webhook URL*.
2. An always-on place to run it (below). Your phone only **receives** alerts via
   the Discord app — it doesn't host anything.

## Run it locally first (optional sanity check)

```
pip install -r requirements.txt
cp .env.example .env          # paste your webhook; set APP_PASSWORD
python app.py                 # open http://localhost:8000
```

Click **Send test alert** — a message should hit your Discord channel. Then add a
watch and you're live.

## Deploy to the cloud (so your computer can be off)

### Railway
1. Push this folder to a GitHub repo (or use Railway's "deploy from local").
2. New Project → Deploy from repo. Railway auto-detects the `Procfile`.
3. In **Variables**, set `DISCORD_WEBHOOK_URL` and `APP_PASSWORD` (and any tuning vars).
4. Add a **Volume** mounted at `/data` and set `DB_PATH=/data/tracker.db` so your
   watch list survives redeploys.
5. Open the generated URL. Log in with any username + your `APP_PASSWORD`.

### Render
1. New → **Web Service** → connect the repo.
2. Build: `pip install -r requirements.txt` · Start: `gunicorn -w 1 --threads 8 app:app`
3. Set the same environment variables. Add a **Disk** mounted at `/data` and
   `DB_PATH=/data/tracker.db` for persistence.

Either way: keep it to **one worker** (`-w 1`) — the Procfile already does this —
so only one background poller runs.

## Add it to your phone

- **Get the alerts:** install **Discord** on your iPhone, open the channel, and
  turn on notifications for it. Every match buzzes your phone.
- **Manage watches on the go:** open the app's URL in Safari and *Add to Home
  Screen* — it behaves like an app for adding/removing watches.

## Using it

- **Hunting:** type a model or ref like `submariner 16610` (all words must appear).
  Exclude terms with a minus: `submariner -date`. Only `WTS`/`WTT` posts count
  toward a watch match (configurable via `WATCH_REQUIRE_TAGS`).
- **Following:** add a username to get *every* post they make in the subreddit,
  regardless of tag.
- Changes are live — no restart.

## Notes

- Unauthenticated RSS suits a personal poll every few minutes. If Reddit rate-limits
  you, you'll see `429` in the logs; the app backs off and retries.
- Protect the UI with `APP_PASSWORD` whenever it's reachable from the internet.
- The webhook lives only on the server — it's never exposed to the browser.
