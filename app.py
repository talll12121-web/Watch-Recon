#!/usr/bin/env python3
"""
WatchExchange Tracker — web app edition
---------------------------------------
A small Flask app you host on an always-on machine (cloud worker, VPS, Pi).

  * Reads Reddit's PUBLIC RSS feeds — no Reddit account, app, or credentials.
  * Background thread polls r/WatchExchange + any followed users on a schedule.
  * Matches your tracked watches, sends a Discord notification (lands on your
    phone via the Discord app), and records the alert.
  * Interactive UI at "/" to add/remove watches and followed users LIVE — the
    next poll cycle picks up changes immediately, no restart.

The only secret is your Discord webhook URL (set in the environment).
"""

import os
import re
import json
import time
import sqlite3
import logging
import threading
from functools import wraps
from datetime import datetime, timezone

import requests
import feedparser
from flask import Flask, request, jsonify, Response, send_from_directory
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------------------- config
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SUBREDDIT = os.environ.get("SUBREDDIT", "Watchexchange")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "180"))
REQUIRE_TAGS = [t.strip().upper() for t in os.environ.get("WATCH_REQUIRE_TAGS", "WTS,WTT").split(",") if t.strip()]
MAX_AGE_MIN = int(os.environ.get("MAX_POST_AGE_MINUTES", "720"))
SCAN_USER_FEEDS = os.environ.get("SCAN_FOLLOWED_USER_FEEDS", "true").lower() == "true"
USER_AGENT = os.environ.get("USER_AGENT", "watchexchange-tracker/1.0 (personal RSS notifier)")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")          # if set, the UI requires it
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "tracker.db"))
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("tracker")

COLOR = {"watch": 3066993, "user": 3447003, "both": 15844367}

_state = {"last_poll": None, "last_error": None}

# ----------------------------------------------------------------------------- database
def db():
    # A cloud volume (e.g. Railway /data) may be absent or mount after boot; create the
    # parent dir so SQLite can't hard-fail with "unable to open database file".
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT, terms TEXT, exclude TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE, created REAL);
        CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts REAL);
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT, title TEXT, link TEXT, author TEXT,
            price TEXT, reason TEXT, kind TEXT, ts REAL);
        """)
    log.info("DB ready at %s", DB_PATH)


def load_watches():
    with db() as c:
        rows = c.execute("SELECT * FROM watches ORDER BY created").fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "label": r["label"],
            "terms": json.loads(r["terms"] or "[]"),
            "exclude": json.loads(r["exclude"] or "[]"),
        })
    return out


def load_users():
    with db() as c:
        return [r["username"] for r in c.execute("SELECT username FROM users ORDER BY created").fetchall()]


# ----------------------------------------------------------------------------- RSS + matching
TAG_RE = re.compile(r"\[([A-Za-z]{2,4})\]")
PRICE_RE = re.compile(r"\$\s?\d[\d,]*")
IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


def fetch_feed(url):
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 429:
            log.warning("Rate-limited (429): %s", url); time.sleep(30); return []
        if r.status_code != 200:
            log.warning("Feed %s -> HTTP %s", url, r.status_code); return []
        return feedparser.parse(r.content).entries
    except Exception as e:
        log.warning("Fetch failed %s: %s", url, e); return []


def author_of(e):
    a = (getattr(e, "author", "") or "").replace("/u/", "").replace("u/", "").strip()
    return a or "[unknown]"


def body_of(e):
    try: return e.content[0].value
    except Exception: return getattr(e, "summary", "") or ""


def time_of(e):
    t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    return time.mktime(t) if t else time.time()


def tags_of(title): return [t.upper() for t in TAG_RE.findall(title or "")]
def price_of(text):
    m = PRICE_RE.search(text or ""); return m.group(0).replace(" ", "") if m else None


def match_watches(title, body, watches):
    if REQUIRE_TAGS and not any(t in tags_of(title) for t in REQUIRE_TAGS):
        return []
    hay = (title + " " + re.sub("<[^>]+>", " ", body)).lower()
    hits = []
    for w in watches:
        terms = [t.lower() for t in w["terms"]]
        if terms and all(t in hay for t in terms) and not any(x.lower() in hay for x in w["exclude"]):
            hits.append(w["label"])
    return hits


# ----------------------------------------------------------------------------- discord
def send_discord(entry, reasons, kind):
    if not DISCORD_WEBHOOK_URL:
        log.error("No DISCORD_WEBHOOK_URL set — cannot send."); return False
    title = getattr(entry, "title", "(no title)")
    body = body_of(entry)
    price = price_of(title) or price_of(body)
    fields = [{"name": "Why", "value": "\n".join(reasons), "inline": False},
              {"name": "Author", "value": f"u/{author_of(entry)}", "inline": True}]
    if price: fields.append({"name": "Price", "value": price, "inline": True})
    embed = {"title": title[:250], "url": getattr(entry, "link", ""), "color": COLOR[kind],
             "fields": fields,
             "timestamp": datetime.fromtimestamp(time_of(entry), tz=timezone.utc).isoformat(),
             "footer": {"text": "WatchExchange Tracker"}}
    m = IMG_RE.search(body or "")
    if m and re.search(r"\.(jpg|jpeg|png|gif)", m.group(1), re.I):
        embed["image"] = {"url": m.group(1).replace("&amp;", "&")}
    for _ in range(4):
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        if resp.status_code in (200, 204): return True
        if resp.status_code == 429:
            time.sleep(float(resp.json().get("retry_after", 2)) + 0.5); continue
        log.error("Discord failed (%s): %s", resp.status_code, resp.text[:200]); return False
    return False


def record_alert(entry, reasons, kind):
    with db() as c:
        c.execute("INSERT INTO alerts (post_id,title,link,author,price,reason,kind,ts) VALUES (?,?,?,?,?,?,?,?)",
                  (getattr(entry, "id", "") or getattr(entry, "link", ""),
                   getattr(entry, "title", ""), getattr(entry, "link", ""),
                   author_of(entry), price_of(getattr(entry, "title", "")) or price_of(body_of(entry)),
                   " · ".join(reasons), kind, time_of(entry)))
        c.execute("DELETE FROM alerts WHERE id NOT IN (SELECT id FROM alerts ORDER BY ts DESC LIMIT 200)")


# ----------------------------------------------------------------------------- poll cycle
def iter_entries(users):
    yield from fetch_feed(f"https://www.reddit.com/r/{SUBREDDIT}/new/.rss?limit=50")
    if SCAN_USER_FEEDS:
        sub = SUBREDDIT.lower()
        for u in users:
            time.sleep(1)
            for e in fetch_feed(f"https://www.reddit.com/user/{u}/submitted/.rss?limit=25"):
                if f"/r/{sub}/" in getattr(e, "link", "").lower():
                    yield e


def seen(c, pid): return c.execute("SELECT 1 FROM seen WHERE id=?", (pid,)).fetchone() is not None


def scan_once(suppress=False):
    watches, users = load_watches(), load_users()
    user_set = {u.lower() for u in users}
    max_age = MAX_AGE_MIN * 60
    now = time.time()
    sent = 0
    for entry in iter_entries(users):
        pid = getattr(entry, "id", None) or getattr(entry, "link", None)
        if not pid: continue
        with db() as c:
            if seen(c, pid): continue
        if max_age and (now - time_of(entry)) > max_age:
            with db() as c: c.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (pid, time.time()))
            continue
        title, body = getattr(entry, "title", "") or "", body_of(entry)
        reasons, followed, matched = [], author_of(entry).lower() in user_set, match_watches(title, body, watches)
        if followed: reasons.append(f"👤 Followed user u/{author_of(entry)}")
        if matched: reasons.append("🎯 " + ", ".join(matched))
        if reasons:
            kind = "both" if (followed and matched) else ("user" if followed else "watch")
            if suppress:
                log.info("[priming] would alert: %s", title[:70])
            else:
                if send_discord(entry, reasons, kind):
                    record_alert(entry, reasons, kind); sent += 1
                    log.info("ALERT: %s", title[:70])
        with db() as c:
            c.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (pid, time.time()))
    return sent


def poller():
    init_db()
    with db() as c:
        primed = c.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is not None
    if not primed and not NOTIFY_ON_FIRST_RUN:
        log.info("First run: priming (no notifications)...")
        try: scan_once(suppress=True)
        except Exception as e: log.warning("Prime failed: %s", e)
        log.info("Primed.")
    while True:
        try:
            n = scan_once()
            _state["last_poll"] = time.time(); _state["last_error"] = None
            if n: log.info("Sent %d alert(s).", n)
        except Exception as e:
            _state["last_error"] = str(e); log.exception("Poll failed: %s", e)
        time.sleep(POLL_INTERVAL)


# ----------------------------------------------------------------------------- flask
app = Flask(__name__, static_folder="templates", static_url_path="")


def check_auth(u, p): return p == APP_PASSWORD
def need_auth():
    return Response("Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="Tracker"'})


def protected(f):
    @wraps(f)
    def w(*a, **k):
        if APP_PASSWORD:
            auth = request.authorization
            if not auth or not check_auth(auth.username, auth.password):
                return need_auth()
        return f(*a, **k)
    return w


@app.route("/")
@protected
def index():
    return send_from_directory("templates", "index.html")


@app.route("/api/state")
@protected
def api_state():
    return jsonify({
        "subreddit": SUBREDDIT,
        "poll_interval": POLL_INTERVAL,
        "require_tags": REQUIRE_TAGS,
        "watches": load_watches(),
        "users": load_users(),
        "last_poll": _state["last_poll"],
        "last_error": _state["last_error"],
        "webhook_set": bool(DISCORD_WEBHOOK_URL),
    })


@app.route("/api/watches", methods=["POST"])
@protected
def add_watch():
    d = request.get_json(force=True)
    terms = d.get("terms") or [t for t in (d.get("query", "").split()) if t]
    if not terms: return jsonify({"error": "Enter at least one term, e.g. 'submariner 16610'."}), 400
    label = (d.get("label") or " ".join(terms)).strip()
    exclude = d.get("exclude") or []
    with db() as c:
        c.execute("INSERT INTO watches (label,terms,exclude,created) VALUES (?,?,?,?)",
                  (label, json.dumps(terms), json.dumps(exclude), time.time()))
    return jsonify({"ok": True})


@app.route("/api/watches/<int:wid>", methods=["DELETE"])
@protected
def del_watch(wid):
    with db() as c: c.execute("DELETE FROM watches WHERE id=?", (wid,))
    return jsonify({"ok": True})


@app.route("/api/users", methods=["POST"])
@protected
def add_user():
    name = (request.get_json(force=True).get("username") or "").replace("u/", "").replace("/u/", "").strip()
    if not name: return jsonify({"error": "Enter a username."}), 400
    try:
        with db() as c: c.execute("INSERT INTO users (username,created) VALUES (?,?)", (name, time.time()))
    except sqlite3.IntegrityError:
        return jsonify({"error": "Already following that user."}), 400
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@protected
def del_user(uid):
    with db() as c: c.execute("DELETE FROM users WHERE id=?", (uid,))
    return jsonify({"ok": True})


# users delete by name (UI passes id from /api/state via a parallel list) — provide name route too
@app.route("/api/users/by-name/<name>", methods=["DELETE"])
@protected
def del_user_by_name(name):
    with db() as c: c.execute("DELETE FROM users WHERE username=?", (name,))
    return jsonify({"ok": True})


@app.route("/api/alerts")
@protected
def api_alerts():
    limit = min(int(request.args.get("limit", 40)), 200)
    with db() as c:
        rows = c.execute("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/test", methods=["POST"])
@protected
def api_test():
    if not DISCORD_WEBHOOK_URL: return jsonify({"error": "No webhook configured."}), 400
    r = requests.post(DISCORD_WEBHOOK_URL, json={"content": "✅ WatchExchange Tracker test — your webhook works."}, timeout=15)
    return (jsonify({"ok": True}) if r.status_code in (200, 204)
            else (jsonify({"error": f"Discord returned {r.status_code}"}), 400))


# ----------------------------------------------------------------------------- start poller (once)
def start_poller_once():
    if getattr(start_poller_once, "_started", False): return
    start_poller_once._started = True
    threading.Thread(target=poller, daemon=True).start()
    log.info("Poller thread started (every %ss, r/%s).", POLL_INTERVAL, SUBREDDIT)


start_poller_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
