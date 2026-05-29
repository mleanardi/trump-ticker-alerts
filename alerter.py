#!/usr/bin/env python3
"""
Trump Ticker Alerter
====================
Polls Truth Social (via trumpstruth.org RSS) and optionally Factba.se for
remarks, scans new content for mentions of publicly traded companies, and
sends a Pushover push notification for each hit.

Designed to be run on a 5-minute cron (GitHub Actions, cron, etc.).
Deduplicates by post ID using a tiny on-disk state file (state.json).

Required env vars:
    PUSHOVER_TOKEN     Pushover application API token
    PUSHOVER_USER      Pushover user key

Optional env vars:
    FACTBASE_API_KEY   If set, also polls Factba.se remarks endpoint
    TICKERS_CSV        Path to tickers.csv (default: ./tickers.csv)
    STATE_FILE         Path to state file (default: ./state.json)
    LOOKBACK_MINUTES   Ignore posts older than this on first run (default: 60)
    DEBUG              If set to "1", prints verbose output
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
TICKERS_CSV = Path(os.environ.get("TICKERS_CSV", SCRIPT_DIR / "tickers.csv"))
STATE_FILE = Path(os.environ.get("STATE_FILE", SCRIPT_DIR / "state.json"))
LOOKBACK_MINUTES = int(os.environ.get("LOOKBACK_MINUTES", "60"))
DEBUG = os.environ.get("DEBUG") == "1"

# Daily heartbeat: once per day, at/after HEARTBEAT_HOUR (local), send a quiet
# "still alive" push summarizing activity since the previous heartbeat.
HEARTBEAT_ENABLED = os.environ.get("HEARTBEAT", "1") != "0"
HEARTBEAT_HOUR = int(os.environ.get("HEARTBEAT_HOUR", "9"))      # 0-23, local hour
HEARTBEAT_TZ_OFFSET = float(os.environ.get("HEARTBEAT_TZ_OFFSET", "0"))  # hours from UTC, e.g. -5 for US Eastern (EST)

TRUTH_RSS = "https://trumpstruth.org/feed"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
FACTBASE_REMARKS_URL = "https://api.factba.se/rest/json/trump/calendar"  # adjust per your tier

USER_AGENT = "trump-ticker-alerter/1.0 (+https://github.com/your/repo)"

# Tickers that look like common English words — require $ prefix or all-caps context.
AMBIGUOUS_TICKERS = {
    "A", "ALL", "ARE", "AS", "AT", "BE", "BY", "C", "DO", "FOR", "GO", "HAS",
    "HE", "IT", "IS", "M", "NEW", "NO", "ON", "ONE", "OR", "OUT", "RE", "SEE",
    "SO", "T", "TWO", "UP", "US", "V", "WE", "WHO", "X", "Y", "Z", "F", "K",
    "L", "O", "Q", "R", "S",
}

# Common-noun company names that need stricter matching to avoid false positives.
# Map of name -> required context word(s); if none of the context words appear
# within the same post, the match is dropped.
# NOTE: do NOT include the company name itself as its own context word
# (that would make the check circular).
COMMON_WORD_NAMES = {
    "Apple": ["iPhone", "iPad", "Mac", "Tim Cook", "Cupertino", "App Store", "Apple Inc", "AAPL"],
    "Target": ["Target store", "Target Corp", "Target Corporation", "retail", "boycott", "TGT"],
    "Block": ["Block Inc", "Jack Dorsey", "Cash App", "Square"],
    "Square": ["Square Inc", "Jack Dorsey", "Block Inc", "Cash App"],
    "Ford": ["Ford Motor", "Ford F-150", "Ford F150", "Ford Mustang", "F-150", "Jim Farley", "Detroit", "auto", "automaker", "car", "truck", "factory", "plant", "EV"],
    "Gap": ["Gap Inc", "Gap store", "Gap clothing", "Banana Republic", "Old Navy"],
    "Discover": ["Discover Financial", "Discover card", "credit card"],
    "Visa": ["Visa Inc", "Visa card", "credit card", "payment"],
    "Shell": ["Shell oil", "Shell plc", "Shell gas", "gasoline", "petroleum"],
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Company:
    ticker: str
    name: str
    aliases: list[str] = field(default_factory=list)


@dataclass
class Post:
    source: str          # "truthsocial" or "factbase"
    post_id: str         # unique stable ID
    url: str
    timestamp: datetime
    text: str
    title: str = ""


@dataclass
class Match:
    company: Company
    matched_term: str
    match_type: str      # "ticker", "name", "alias"


# ---------------------------------------------------------------------------
# State (dedup) management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen_ids": [], "first_run": True}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"seen_ids": [], "first_run": True}


def save_state(state: dict) -> None:
    # Cap seen_ids at last 500 to keep file small
    state["seen_ids"] = state["seen_ids"][-500:]
    state["first_run"] = False
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Ticker loading
# ---------------------------------------------------------------------------

def load_companies() -> list[Company]:
    companies: list[Company] = []
    with TICKERS_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row["ticker"].strip()
            name = row["name"].strip()
            aliases_raw = row.get("aliases", "").strip()
            aliases = [a.strip() for a in aliases_raw.split("|") if a.strip()]
            companies.append(Company(ticker=ticker, name=name, aliases=aliases))
    return companies


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url: str, headers: dict | None = None, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_post(url: str, data: dict, timeout: int = 15) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Source: Truth Social via trumpstruth.org RSS
# ---------------------------------------------------------------------------

def fetch_truth_social() -> list[Post]:
    """Fetch latest posts from trumpstruth.org RSS feed."""
    try:
        raw = http_get(TRUTH_RSS)
    except Exception as e:
        log(f"[truthsocial] fetch failed: {e}")
        return []

    posts: list[Post] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log(f"[truthsocial] XML parse error: {e}")
        return []

    # RSS 2.0: rss/channel/item
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        description = (item.findtext("description") or "").strip()
        guid = (item.findtext("guid") or link).strip()

        try:
            ts = parsedate_to_datetime(pub_date) if pub_date else datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)

        text = strip_html(description)
        if title and title not in text:
            text = f"{title}\n{text}"

        posts.append(Post(
            source="truthsocial",
            post_id=guid or link,
            url=link,
            timestamp=ts,
            text=text,
            title=title,
        ))
    return posts


# ---------------------------------------------------------------------------
# Source: Factba.se (optional, requires API key)
# ---------------------------------------------------------------------------

def fetch_factbase() -> list[Post]:
    key = os.environ.get("FACTBASE_API_KEY")
    if not key:
        return []
    # NOTE: Factba.se's API surface differs by subscription tier.
    # This is a placeholder using the public calendar endpoint as a stub.
    # Replace with whichever realtime-remarks endpoint your tier provides.
    try:
        raw = http_get(FACTBASE_REMARKS_URL, headers={"Authorization": f"Bearer {key}"})
        data = json.loads(raw)
    except Exception as e:
        log(f"[factbase] fetch failed: {e}")
        return []

    posts: list[Post] = []
    items = data if isinstance(data, list) else data.get("data", [])
    for item in items[:25]:
        post_id = str(item.get("id") or item.get("slug") or "")
        text = item.get("transcript") or item.get("description") or ""
        title = item.get("title") or item.get("event") or ""
        url = item.get("url") or ""
        date_str = item.get("date") or item.get("datetime") or ""
        try:
            ts = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)
        if not post_id or not text:
            continue
        posts.append(Post(
            source="factbase",
            post_id=f"factbase:{post_id}",
            url=url,
            timestamp=ts,
            text=text,
            title=title,
        ))
    return posts


# ---------------------------------------------------------------------------
# Text processing & matching
# ---------------------------------------------------------------------------

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(s: str) -> str:
    s = html.unescape(s)
    s = _HTML_TAG.sub(" ", s)
    s = _WS.sub(" ", s)
    return s.strip()


def find_matches(text: str, companies: list[Company]) -> list[Match]:
    """Two-pass: first collect strict matches; then re-evaluate ambiguous-name
    candidates allowing other unambiguous company matches in the same post to
    serve as disambiguating context.
    """
    matches: list[Match] = []
    seen_tickers: set[str] = set()
    deferred: list[Company] = []
    if not text:
        return matches

    # Pass 1: strict matching.
    for c in companies:
        if c.ticker in seen_tickers:
            continue

        # 1) $TICKER form is always a match.
        dollar = f"${c.ticker}"
        if dollar in text or dollar.replace(".", "") in text:
            matches.append(Match(c, dollar, "ticker"))
            seen_tickers.add(c.ticker)
            continue

        # 2) Bare ticker — must be whole word and not a common English word.
        if c.ticker not in AMBIGUOUS_TICKERS:
            pattern = rf"\b{re.escape(c.ticker)}\b"
            if re.search(pattern, text):
                matches.append(Match(c, c.ticker, "ticker"))
                seen_tickers.add(c.ticker)
                continue

        # 3) Company name (whole-word, case-insensitive).
        name_hit = _name_hit(text, c.name, strict=True)
        if name_hit:
            matches.append(Match(c, name_hit, "name"))
            seen_tickers.add(c.ticker)
            continue

        # 4) Aliases.
        alias_match = None
        for alias in c.aliases:
            alias_hit = _name_hit(text, alias, strict=True)
            if alias_hit:
                alias_match = alias_hit
                break
        if alias_match:
            matches.append(Match(c, alias_match, "alias"))
            seen_tickers.add(c.ticker)
            continue

        # Defer for pass 2 if it has a common-word name that was rejected.
        if c.name in COMMON_WORD_NAMES or any(a in COMMON_WORD_NAMES for a in c.aliases):
            deferred.append(c)

    # Pass 2: if at least one unambiguous match found, relax context for
    # common-word names. The presence of other named companies disambiguates.
    has_unambiguous = any(m.match_type in ("ticker", "name", "alias") for m in matches)
    if has_unambiguous:
        for c in deferred:
            if c.ticker in seen_tickers:
                continue
            for candidate in [c.name, *c.aliases]:
                relaxed_hit = _name_hit(text, candidate, strict=False)
                if relaxed_hit:
                    matches.append(Match(c, relaxed_hit, "name"))
                    seen_tickers.add(c.ticker)
                    break

    return matches


def _name_hit(text: str, name: str, strict: bool = True) -> str | None:
    """Whole-word, case-insensitive match for a company name.

    When strict=True, ambiguous common-word names require a context word
    from COMMON_WORD_NAMES to appear in the same text.
    When strict=False, the context check is skipped (used in pass 2 when
    other unambiguous companies have already matched).
    """
    if not name:
        return None
    base = re.sub(
        r"\s+(Inc\.?|Incorporated|Corp\.?|Corporation|Company|Co\.?|plc|PLC|NV|N\.V\.|"
        r"Holdings|Group|Ltd\.?|Limited|LLC|LP|& Co\.?|& Company)\s*$",
        "",
        name.strip(),
    ).strip()
    if not base:
        return None

    pattern = rf"\b{re.escape(base)}\b"
    if not re.search(pattern, text, re.IGNORECASE):
        return None

    if strict and base in COMMON_WORD_NAMES:
        context_words = COMMON_WORD_NAMES[base]
        if not any(re.search(rf"\b{re.escape(cw)}\b", text, re.IGNORECASE) for cw in context_words):
            return None

    return base


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------

def send_pushover(title: str, message: str, url: str = "", url_title: str = "View post",
                  priority: int = 0) -> bool:
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not token or not user:
        log("[pushover] missing PUSHOVER_TOKEN or PUSHOVER_USER; skipping")
        return False

    payload = {
        "token": token,
        "user": user,
        "title": title[:250],
        "message": message[:1024],
        "priority": str(priority),
    }
    if url:
        payload["url"] = url
        payload["url_title"] = url_title

    status, body = http_post(PUSHOVER_URL, payload)
    if status != 200:
        log(f"[pushover] HTTP {status}: {body}")
        return False
    return True


# ---------------------------------------------------------------------------
# Daily heartbeat
# ---------------------------------------------------------------------------

def maybe_send_heartbeat(state: dict, scanned_this_run: int, alerts_this_run: int) -> None:
    """Accumulate activity counters, and once per day (at/after HEARTBEAT_HOUR,
    local) send a quiet 'still alive' push summarizing activity since the last
    heartbeat. Robust to skipped cron runs: fires on the first run that occurs
    at/after the target time on a new day.
    """
    now_local = datetime.now(timezone.utc) + timedelta(hours=HEARTBEAT_TZ_OFFSET)

    # Accumulate rolling counters in state.
    stats = state.get("stats")
    if not isinstance(stats, dict):
        stats = {"scanned": 0, "alerts": 0, "window_start": now_local.isoformat()}
    stats["scanned"] = stats.get("scanned", 0) + scanned_this_run
    stats["alerts"] = stats.get("alerts", 0) + alerts_this_run
    state["stats"] = stats

    if not HEARTBEAT_ENABLED:
        return

    today_local = now_local.date().isoformat()
    last_heartbeat = state.get("last_heartbeat")
    target_today = now_local.replace(hour=HEARTBEAT_HOUR, minute=0, second=0, microsecond=0)

    if last_heartbeat == today_local or now_local < target_today:
        return  # already sent today, or not yet time

    window_start = stats.get("window_start", "")
    try:
        ws = datetime.fromisoformat(window_start)
        window_label = ws.strftime("%b %d %H:%M")
    except (TypeError, ValueError):
        window_label = "last heartbeat"

    tz_label = f"UTC{HEARTBEAT_TZ_OFFSET:+g}" if HEARTBEAT_TZ_OFFSET else "UTC"
    title = "Trump Ticker Alerter — daily heartbeat"
    message = (
        f"Monitor alive and running.\n"
        f"Since {window_label} ({tz_label}):\n"
        f"  - {stats['scanned']} new posts scanned\n"
        f"  - {stats['alerts']} company-mention alerts sent"
    )
    # priority -1 = quiet (no sound/vibration); it's a status ping, not urgent.
    sent = send_pushover(title, message, priority=-1)
    if sent:
        log(f"HEARTBEAT sent: scanned={stats['scanned']} alerts={stats['alerts']}")
        state["last_heartbeat"] = today_local
        state["stats"] = {"scanned": 0, "alerts": 0, "window_start": now_local.isoformat()}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def log(*args) -> None:
    if DEBUG or os.environ.get("CI"):
        print(*args, file=sys.stderr, flush=True)


def main() -> int:
    companies = load_companies()
    log(f"Loaded {len(companies)} companies")

    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    first_run = state.get("first_run", True)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)

    posts: list[Post] = []
    posts.extend(fetch_truth_social())
    posts.extend(fetch_factbase())
    log(f"Fetched {len(posts)} total posts")

    new_posts: list[Post] = []
    for p in posts:
        if p.post_id in seen_ids:
            continue
        if first_run and p.timestamp < cutoff:
            seen_ids.add(p.post_id)
            continue
        new_posts.append(p)

    log(f"{len(new_posts)} new posts to scan")

    alert_count = 0
    for post in new_posts:
        hits = find_matches(post.text, companies)
        seen_ids.add(post.post_id)
        if not hits:
            continue

        tickers_str = ", ".join(f"${m.company.ticker}" for m in hits)
        title = f"Trump mentioned {tickers_str}"
        body = (
            f"[{post.source}] {post.timestamp.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Matched: {', '.join(f'{m.company.ticker} ({m.matched_term})' for m in hits)}\n\n"
            f"\"{post.text[:600]}\""
        )
        sent = send_pushover(title, body, url=post.url)
        if sent:
            alert_count += 1
            log(f"ALERT sent: {tickers_str} -- {post.url}")

    maybe_send_heartbeat(state, scanned_this_run=len(new_posts), alerts_this_run=alert_count)

    state["seen_ids"] = list(seen_ids)
    save_state(state)
    log(f"Done. {alert_count} alerts sent. {len(seen_ids)} ids in state.")
    return 0


# ---------------------------------------------------------------------------
# Self-test mode
# ---------------------------------------------------------------------------

SAMPLE_POSTS = [
    # Expected matches: TSLA, F, GM
    "Tesla is doing GREAT things in America. Ford and General Motors better step up!",
    # Expected match: AAPL (via context "iPhone" disambiguating "Apple")
    "Tim Cook of Apple just called me. The iPhone is incredible!",
    # Expected NO matches (Apple alone with no context)
    "I love apple pie, the best in the world. Many people are saying it.",
    # Expected match: X (US Steel via alias)
    "We will not let Japan buy U.S. Steel. American jobs first!",
    # Expected match: DJT
    "Truth Social is exploding! TMTG is the future of free speech.",
    # Expected matches: BA, LMT
    "Boeing must do better. Lockheed Martin builds the greatest fighter jets in the world.",
    # Expected match: $NVDA (dollar prefix)
    "Big things happening with $NVDA. Jensen is a friend of mine.",
    # Expected NO matches (Target as verb with no retail context)
    "They will target us no matter what we do, but we keep winning.",
]


def selftest() -> int:
    companies = load_companies()
    print(f"Loaded {len(companies)} companies\n")
    total_hits = 0
    for i, text in enumerate(SAMPLE_POSTS, 1):
        hits = find_matches(text, companies)
        total_hits += len(hits)
        hit_strs = [f"{m.company.ticker}({m.matched_term})" for m in hits] or ["(none)"]
        print(f"{i}. {text}")
        print(f"   -> {', '.join(hit_strs)}\n")
    print(f"Total matches: {total_hits}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    sys.exit(main())
