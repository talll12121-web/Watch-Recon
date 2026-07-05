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
import html
import json
import time
import random
import calendar
import hashlib
import sqlite3
import logging
import threading
from functools import wraps
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from flask import (Flask, request, jsonify, Response, send_from_directory,
                   session, redirect, url_for)
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------------------- config
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SUBREDDIT = os.environ.get("SUBREDDIT", "Watchexchange")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
REQUIRE_TAGS = [t.strip().upper() for t in os.environ.get("WATCH_REQUIRE_TAGS", "WTS,WTT").split(",") if t.strip()]
MAX_AGE_MIN = int(os.environ.get("MAX_POST_AGE_MINUTES", "720"))
SCAN_USER_FEEDS = os.environ.get("SCAN_FOLLOWED_USER_FEEDS", "true").lower() == "true"
# A followed user's post lands in /new too, so /new is the fast path for them. The slower,
# more-throttled per-user feeds only backstop posts that scrolled out of /new — scan them
# every Nth cycle instead of every cycle so they don't drag out each poll.
USER_FEED_EVERY = max(1, int(os.environ.get("USER_FEED_EVERY_CYCLES", "5")))
USER_AGENT = os.environ.get("USER_AGENT", "watchexchange-tracker/1.0 (personal RSS notifier)")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")          # if set, the UI requires it
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "tracker.db"))
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"
# Price-history feature: historical asking prices from pullpush.io (Pushshift mirror).
PRICE_SINCE = os.environ.get("PRICE_SINCE", "2025-01-01")   # earliest month to chart
PRICE_CACHE_TTL = int(os.environ.get("PRICE_CACHE_TTL_SECONDS", "43200"))  # 12h
PRICE_MAX_PAGES = int(os.environ.get("PRICE_MAX_PAGES", "6"))   # pullpush pages (100 each) per lookup
DEAL_THRESHOLD_PCT = float(os.environ.get("DEAL_THRESHOLD_PCT", "12"))  # flag listings this % under median
PULLPUSH_API = "https://api.pullpush.io/reddit/search/submission/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("tracker")

COLOR = {"watch": 3066993, "user": 3447003, "both": 15844367, "deal": 13215543}   # deal = gold

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
        CREATE TABLE IF NOT EXISTS price_cache (k TEXT PRIMARY KEY, ts REAL, json TEXT);
        """)
        for table, col, decl in (("watches", "max_price", "REAL"),
                                 ("alerts", "price_val", "REAL"),
                                 ("alerts", "market_median", "REAL"),
                                 ("alerts", "deal_pct", "REAL")):
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    log.info("DB ready at %s", DB_PATH)


def load_watches():
    with db() as c:
        rows = c.execute("SELECT * FROM watches ORDER BY created").fetchall()
    out = []
    for r in rows:
        keys = r.keys()
        out.append({
            "id": r["id"], "label": r["label"],
            "terms": json.loads(r["terms"] or "[]"),
            "exclude": json.loads(r["exclude"] or "[]"),
            "max_price": (r["max_price"] if "max_price" in keys else None),
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
        # A changing "_" param busts Reddit's edge cache so we're less likely to get a
        # stale copy that's missing the newest posts.
        r = requests.get(url, headers={"User-Agent": USER_AGENT},
                         params={"_": int(time.time())}, timeout=20)
        if r.status_code == 429:
            # Don't block the whole cycle sleeping — just skip this feed. The next poll
            # (POLL_INTERVAL later) retries, and every feed queued behind it stays on time.
            log.warning("Rate-limited (429), skipping this cycle: %s", url); return []
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


def flair_of(entry):
    # Reddit RSS puts the post's link flair (e.g. "$9000-$11999") in entry.tags as a
    # category term. Return the one that looks like a price-range flair, if any.
    for t in getattr(entry, "tags", []) or []:
        term = (getattr(t, "term", None) or (t.get("term") if isinstance(t, dict) else None) or "")
        if FLAIR_RANGE_RE.search(term) or FLAIR_PLUS_RE.search(term):
            return term
    return None


def watch_hits(title, body, watches):
    # Full matched watch dicts (so callers can read max_price/terms for deal scoring).
    if REQUIRE_TAGS and not any(t in tags_of(title) for t in REQUIRE_TAGS):
        return []
    hay = (title + " " + re.sub("<[^>]+>", " ", body)).lower()
    hits = []
    for w in watches:
        terms = [t.lower() for t in w["terms"]]
        if terms and all(t in hay for t in terms) and not any(x.lower() in hay for x in w["exclude"]):
            hits.append(w)
    return hits


def match_watches(title, body, watches):
    return [w["label"] for w in watch_hits(title, body, watches)]


# ----------------------------------------------------------------------------- price history
# WatchExchange sell posts carry a price-range flair (e.g. "$9000-$11999", "$15500+"), but
# that bucket is too wide to trend on. Sellers almost always write the *exact* asking price
# in the title ("… 126000 … - $7,500"), so we prefer that precise number and use the flair
# range only to validate it (rejecting a stray "$50 shipping") or as a coarse fallback.
SELL_RE = re.compile(r"\bWT[ST]\b", re.I)                       # WTS or WTT (not WTB)
FLAIR_RANGE_RE = re.compile(r"\$\s?([\d,]+)\s*[-–]\s*\$?\s?([\d,]+)")
FLAIR_PLUS_RE = re.compile(r"\$\s?([\d,]+)\s*\+")
DOLLAR_RE = re.compile(r"\$\s?(\d[\d,]{1,7})")


def _num(s): return int(str(s).replace(",", "").replace("$", "").strip())


def flair_bounds(flair):
    # (low, high) price bounds from a range flair; high is None for an open "$X+" flair.
    if not flair:
        return None
    m = FLAIR_RANGE_RE.search(flair)
    if m:
        return (_num(m.group(1)), _num(m.group(2)))
    m = FLAIR_PLUS_RE.search(flair)
    if m:
        return (_num(m.group(1)), None)
    return None


def listing_price(title, body, flair):
    # Prefer the exact price written in the title/body, validated against the flair range;
    # fall back to the flair midpoint when the text has no corroborating number.
    cands = [_num(x) for x in DOLLAR_RE.findall(f"{title} {body}")]
    cands = [v for v in cands if 300 <= v <= 500000]        # drop shipping/fees & junk
    b = flair_bounds(flair)
    if b:
        lo, hi = b
        cap = hi if hi is not None else lo * 3
        within = [v for v in cands if lo * 0.85 <= v <= cap * 1.15]
        if within:
            mid = (lo + (hi if hi is not None else lo)) / 2.0
            return float(min(within, key=lambda v: abs(v - mid)))   # the flair-corroborated ask
        return float((lo + hi) / 2.0) if hi is not None else float(lo)
    return float(max(cands)) if cands else None                 # no flair — best-effort


def _median(vals):
    v = sorted(vals); n = len(v); m = n // 2
    return float(v[m]) if n % 2 else (v[m - 1] + v[m]) / 2.0


def fetch_page(q, after_ts, before_ts):
    # Newest up to 100 listings in [after_ts, before_ts). Returns (data, ok);
    # ok=False means rate-limited/errored out → caller stops and keeps partial data.
    params = {"subreddit": SUBREDDIT, "q": q, "after": int(after_ts), "before": int(before_ts),
              "size": 100, "sort": "desc", "sort_type": "created_utc"}
    for attempt in range(5):
        try:
            r = requests.get(PULLPUSH_API, headers={"User-Agent": USER_AGENT},
                             params=params, timeout=40)
        except Exception as e:
            log.warning("pullpush error: %s", e); time.sleep(3 * (attempt + 1) + random.random()); continue
        if r.status_code == 200:
            return r.json().get("data", []), True
        if r.status_code in (429, 500, 502, 503):        # transient — back off and retry
            time.sleep(4 * (attempt + 1) + random.random() * 2); continue
        log.warning("pullpush HTTP %s", r.status_code); return [], False
    return [], False


def price_series(watch, force=False):
    terms = watch["terms"]
    tl = [t.lower() for t in terms]
    exclude = [x.lower() for x in watch.get("exclude", [])]
    key = "||".join(sorted(tl)) + "##" + "|".join(sorted(exclude))
    with db() as c:
        row = c.execute("SELECT ts, json FROM price_cache WHERE k=?", (key,)).fetchone()
    prev = json.loads(row["json"]) if row else None
    if prev and not force and (time.time() - row["ts"]) < PRICE_CACHE_TTL:
        prev["cached"] = True
        return prev

    # Query pullpush on the wordy (non-numeric) terms for a broad-but-relevant net, then
    # client-filter on ALL terms by substring so reference variants (116610 -> 116610LN) match.
    # Walk backward in time one page (100) at a time; a few pages keeps us under rate limits.
    alpha = [t for t in terms if not any(c.isdigit() for c in t)]
    q = " ".join(alpha or terms)
    since_ts = calendar.timegm(time.strptime(PRICE_SINCE, "%Y-%m-%d"))
    before = int(time.time())
    raw, scanned, truncated = [], 0, False
    for _ in range(PRICE_MAX_PAGES):
        data, ok = fetch_page(q, since_ts, before)
        if not ok:
            truncated = True; break
        if not data:
            break
        scanned += len(data)
        raw.extend(data)
        oldest = min(int(p.get("created_utc", before)) for p in data)
        if len(data) < 100 or oldest <= since_ts:
            break
        before = oldest - 1
        time.sleep(1.5)

    buckets = {}
    for p in raw:
        title = html.unescape(p.get("title", "") or "")
        body = html.unescape(p.get("selftext", "") or "")
        if not SELL_RE.search(title):
            continue
        hay = (title + " " + body).lower()
        if not all(t in hay for t in tl) or any(x in hay for x in exclude):
            continue
        pr = listing_price(title, body, p.get("link_flair_text"))
        if not pr:
            continue
        label = time.strftime("%Y-%m", time.gmtime(int(p.get("created_utc", 0))))
        buckets.setdefault(label, []).append(pr)

    points = [{"month": lbl, "count": len(v), "median": round(_median(v)),
               "min": round(min(v)), "max": round(max(v))}
              for lbl, v in sorted(buckets.items())]
    total = sum(len(v) for v in buckets.values())
    # A single "market median" for deal-scoring: median over the most recent ≤3 months'
    # monthly medians, so a one-off spike month doesn't skew the bargain threshold.
    recent = [p["median"] for p in points[-3:]]
    market_median = round(_median(recent)) if recent else None

    result = {
        "label": watch["label"], "terms": terms, "since": PRICE_SINCE,
        "source": "r/WatchExchange price-range flair (midpoint), via pullpush.io",
        "note": "Asking prices from listings — not verified sold prices.",
        "points": points, "listings": total, "scanned": scanned, "truncated": truncated,
        "market_median": market_median,
        "generated": time.time(), "cached": False, "stale": False,
    }
    if points:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO price_cache (k, ts, json) VALUES (?,?,?)",
                      (key, time.time(), json.dumps(result)))
        return result

    # No usable data this run. Prefer a previously-fetched series over an empty
    # "rate-limited" screen, and don't let a transient 429 poison the cache for 12h —
    # only cache an empty answer when the archive actually responded (not truncated).
    if prev and prev.get("points"):
        prev["cached"] = True
        prev["stale"] = True
        return prev
    if not truncated:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO price_cache (k, ts, json) VALUES (?,?,?)",
                      (key, time.time(), json.dumps(result)))
    return result


def _watch_key(watch):
    tl = [t.lower() for t in watch["terms"]]
    exclude = [x.lower() for x in watch.get("exclude", [])]
    return "||".join(sorted(tl)) + "##" + "|".join(sorted(exclude))


def cached_market_median(watch):
    # The model's market median from the price cache ONLY — never triggers a fetch, so the
    # poll loop stays fast and never hits pullpush. Returns None until the chart is warmed
    # (open the $ chart once, or add_watch warms it in the background).
    with db() as c:
        row = c.execute("SELECT json FROM price_cache WHERE k=?", (_watch_key(watch),)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["json"]).get("market_median")
    except Exception:
        return None


# ----------------------------------------------------------------------------- discord
def send_discord(entry, reasons, kind, est=None, market_median=None, deal_pct=0.0):
    if not DISCORD_WEBHOOK_URL:
        log.error("No DISCORD_WEBHOOK_URL set — cannot send."); return False
    title = getattr(entry, "title", "(no title)")
    body = body_of(entry)
    price = price_of(title) or price_of(body)
    fields = [{"name": "Why", "value": "\n".join(reasons), "inline": False},
              {"name": "Author", "value": f"u/{author_of(entry)}", "inline": True}]
    if price: fields.append({"name": "Price", "value": price, "inline": True})
    elif est: fields.append({"name": "Est. asking", "value": f"~${est:,.0f}", "inline": True})
    if market_median:
        fields.append({"name": "Market median", "value": f"${market_median:,.0f}", "inline": True})
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


def record_alert(entry, reasons, kind, est=None, market_median=None, deal_pct=0.0):
    with db() as c:
        c.execute("INSERT INTO alerts (post_id,title,link,author,price,reason,kind,ts,"
                  "price_val,market_median,deal_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (getattr(entry, "id", "") or getattr(entry, "link", ""),
                   getattr(entry, "title", ""), getattr(entry, "link", ""),
                   author_of(entry), price_of(getattr(entry, "title", "")) or price_of(body_of(entry)),
                   " · ".join(reasons), kind, time_of(entry),
                   est, market_median, (round(deal_pct, 1) if deal_pct else None)))
        c.execute("DELETE FROM alerts WHERE id NOT IN (SELECT id FROM alerts ORDER BY ts DESC LIMIT 200)")


# ----------------------------------------------------------------------------- poll cycle
def iter_entries(users, scan_users=True):
    # /new catches everything fresh — including followed users, whose posts show up here
    # too and get matched by author in scan_once. This is the fast path every cycle.
    yield from fetch_feed(f"https://www.reddit.com/r/{SUBREDDIT}/new/.rss?limit=50")
    if SCAN_USER_FEEDS and scan_users:
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
    _state["cycle"] = _state.get("cycle", 0) + 1
    scan_users = (_state["cycle"] % USER_FEED_EVERY == 0)   # backstop only every Nth cycle
    for entry in iter_entries(users, scan_users):
        pid = getattr(entry, "id", None) or getattr(entry, "link", None)
        if not pid: continue
        with db() as c:
            if seen(c, pid): continue
        if max_age and (now - time_of(entry)) > max_age:
            with db() as c: c.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (pid, time.time()))
            continue
        title, body = getattr(entry, "title", "") or "", body_of(entry)
        followed = author_of(entry).lower() in user_set
        hits = watch_hits(title, body, watches)

        # Effective asking price for this listing (precise title price, or flair midpoint).
        flair = flair_of(entry)
        est = listing_price(title, body, flair)

        # Per-watch budget: drop matches whose asking price is over that watch's cap.
        # (Keep matches with no price estimate — better to over-alert than miss silently.)
        kept = [w for w in hits if not (w.get("max_price") and est and est > w["max_price"])]

        # Deal score: compare est to the best (highest) market median among matched models,
        # so a real bargain on ANY matched model still flags. Cached medians only — no fetch.
        best_pct, best_med = 0.0, None
        for w in kept:
            med = cached_market_median(w)
            if med and est and est < med:
                pct = (med - est) / med * 100.0
                if pct > best_pct:
                    best_pct, best_med = pct, med
        is_deal = best_pct >= DEAL_THRESHOLD_PCT

        reasons = []
        if followed: reasons.append(f"👤 Followed user u/{author_of(entry)}")
        if kept: reasons.append("🎯 " + ", ".join(w["label"] for w in kept))
        if is_deal:
            reasons.append(f"🔥 ~{round(best_pct)}% under median (${best_med:,.0f})")

        if reasons:
            kind = "deal" if is_deal else ("both" if (followed and kept) else ("user" if followed else "watch"))
            if suppress:
                log.info("[priming] would alert: %s", title[:70])
            else:
                if send_discord(entry, reasons, kind, est, best_med, best_pct):
                    record_alert(entry, reasons, kind, est, best_med, best_pct); sent += 1
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

# Signed session cookie so the home-screen web app can stay logged in (iOS standalone
# mode can't show the HTTP Basic Auth dialog). Key is derived from APP_PASSWORD so it
# stays stable across restarts — no re-login on every deploy — unless SECRET_KEY is set.
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("watchexchange-tracker::" + APP_PASSWORD).encode()).hexdigest()
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure by default (Railway is HTTPS); set COOKIE_SECURE=false for local http testing.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
)


def check_auth(u, p): return p == APP_PASSWORD


def _authed():
    if not APP_PASSWORD:
        return True
    if session.get("authed"):
        return True
    auth = request.authorization          # keep Basic Auth working for curl/API clients
    return bool(auth and check_auth(auth.username, auth.password))


def protected(f):
    @wraps(f)
    def w(*a, **k):
        if _authed():
            return f(*a, **k)
        if request.path.startswith("/api"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login", next=request.path))
    return w


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD or _authed():
        return redirect(url_for("index"))
    if request.method == "POST":
        if check_auth(None, (request.form.get("password") or "").strip()):
            session.permanent = True
            session["authed"] = True
            dest = request.form.get("next") or "/"
            return redirect(dest if dest.startswith("/") else "/")
        # Re-show the form with an error flag (GET, so refresh won't resubmit).
        return redirect(url_for("login", err=1, next=request.form.get("next") or ""))
    return send_from_directory("templates", "login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    try:
        max_price = float(d["max_price"]) if d.get("max_price") not in (None, "") else None
    except (TypeError, ValueError):
        max_price = None
    with db() as c:
        cur = c.execute("INSERT INTO watches (label,terms,exclude,created,max_price) VALUES (?,?,?,?,?)",
                        (label, json.dumps(terms), json.dumps(exclude), time.time(), max_price))
        wid = cur.lastrowid
    # Warm the price cache so deal-scoring has a market median without waiting for the
    # user to open the chart. Best-effort, off the request thread.
    watch = {"id": wid, "label": label, "terms": terms, "exclude": exclude, "max_price": max_price}
    threading.Thread(target=lambda: _safe_warm(watch), daemon=True).start()
    return jsonify({"ok": True})


def _safe_warm(watch):
    try:
        price_series(watch)
    except Exception as e:
        log.warning("cache warm failed for %s: %s", watch.get("label"), e)


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


@app.route("/api/price-history")
@protected
def api_price_history():
    wid = request.args.get("watch_id", type=int)
    w = next((x for x in load_watches() if x["id"] == wid), None)
    if not w:
        return jsonify({"error": "Unknown watch."}), 404
    try:
        return jsonify(price_series(w, force=bool(request.args.get("refresh"))))
    except Exception as e:
        log.exception("price-history failed")
        return jsonify({"error": f"Price lookup failed: {e}"}), 502


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
