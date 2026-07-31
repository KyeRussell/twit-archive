#!/usr/bin/env python3
"""Build a complete podcast RSS feed for TWiT's "This Week in Tech" archive.

The official TWiT feeds only carry the most recent ~10 episodes (citing size
and bandwidth). Every episode, however, still has a public page on twit.tv.
This tool scrapes those pages, extracts the audio enclosure + show notes +
metadata, and emits a single RSS 2.0 feed containing the full archive so you
can subscribe to the whole back-catalogue in any podcast player.

Notes
-----
* Audio is NOT rehosted. Enclosures point straight at TWiT's own CDN
  (cdn.twit.tv / Megaphone), so this uses none of your bandwidth and streams
  exactly what the official player would.
* Ad/analytics tracking prefixes (podtrac / podscribe / megaphone redirect)
  are stripped so enclosures are clean direct URLs.
* Results are cached in data/episodes.json so re-runs are incremental: only
  new (and the most recent few) episodes are re-fetched.

Usage
-----
    python build_feed.py                 # incremental update
    python build_feed.py --full          # re-crawl every episode
    python build_feed.py --limit 20      # quick smoke test (first 20)
    python build_feed.py --no-sizes      # skip enclosure byte-size lookups
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SHOW_SLUG = "this-week-in-tech"
BASE = "https://twit.tv"
SHOW_URL = f"{BASE}/shows/{SHOW_SLUG}"
EP_URL = SHOW_URL + "/episodes/{n}"

FEED_TITLE = "This Week in Tech — Complete Archive"
FEED_LINK = SHOW_URL
FEED_DESC = (
    "The complete archive of Leo Laporte's This Week in Tech (TWiT) — every "
    "episode from 2005 to today, with full show notes. Unofficial personal "
    "feed; audio is streamed directly from TWiT's own CDN."
)
FEED_AUTHOR = "Leo Laporte"
FEED_OWNER_NAME = os.environ.get("FEED_OWNER_NAME", "This Week in Tech Archive")
FEED_OWNER_EMAIL = os.environ.get("FEED_OWNER_EMAIL", "")
FEED_IMAGE = (
    "https://elroy.twit.tv/sites/default/files/images/shows/this_week_in_tech/"
    "album_art/twit_2022albumart_standard_2048.jpg"
)
FEED_LANG = "en-us"
FEED_CATEGORY = "Technology"
# Public URL the feed will be served from (for the atom:self link). Optional.
FEED_SELF_URL = os.environ.get("FEED_SELF_URL", "")

USER_AGENT = (
    "twit-archive-feed/1.0 (personal podcast archive; "
    "https://github.com/kyerussell/twit-archive)"
)

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "data" / "episodes.json"
OUT_DIR = ROOT / "public"

# Podcast download-tracking / dynamic-ad prefixes that wrap the real audio URL.
# They stack, so we strip repeatedly until we reach the underlying host.
_TRACKER_PREFIXES = [
    r"(?:https?://)?pdst\.fm/e/",
    r"(?:https?://)?pscrb\.fm/rss/p/",
    r"(?:https?://)?mgln\.ai/e/\d+/",
    r"(?:https?://)?chrt\.fm/track/[^/]+/",
    r"(?:https?://)?dts\.podtrac\.com/redirect\.mp3/",
    r"(?:https?://)?www\.podtrac\.com/pts/redirect\.mp3/",
    r"(?:https?://)?op3\.dev/e[^/]*/",
    r"(?:https?://)?verifi\.podscribe\.com/rss/p/",
    r"(?:https?://)?claritaspod\.com/measure/",
    r"(?:https?://)?arttrk\.com/p/[^/]+/",
]
_TRACKER_RE = re.compile("|".join(f"(?:{p})" for p in _TRACKER_PREFIXES))

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}

_NOTE_BOILERPLATE = (
    "download or subscribe",
    "join club twit",
    "bandwidth for this week in tech is provided",
    "sponsors:",
    "sponsor:",
)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en"})
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=32, pool_maxsize=32, max_retries=3
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def discover_latest(session: requests.Session) -> int:
    """Highest episode number currently listed on the show page."""
    r = session.get(SHOW_URL, timeout=30)
    r.raise_for_status()
    nums = [int(x) for x in re.findall(rf"/shows/{SHOW_SLUG}/episodes/(\d+)", r.text)]
    return max(nums) if nums else 0


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def clean_audio_url(url: str) -> str:
    """Strip stacked podcast tracking prefixes to reach the direct CDN URL."""
    u = url.strip()
    while True:
        m = _TRACKER_RE.match(u)
        if not m:
            break
        u = u[m.end():]
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def parse_upload_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_air_date(s: str | None) -> datetime | None:
    """Parse the human 'Apr 17th 2005' air-date string."""
    if not s:
        return None
    m = re.match(r"([A-Za-z]{3})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?[, ]+\s*(\d{4})", s)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).title())
    if not mon:
        return None
    # noon UTC keeps the calendar date stable across time zones
    return datetime(int(m.group(3)), mon, int(m.group(2)), 12, 0, tzinfo=timezone.utc)


def iso_duration_to_secs(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"P?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", s)
    if not m or not any(m.groups()):
        return None
    h, mn, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + sec


def secs_to_hms(secs: int | None) -> str | None:
    if secs is None:
        return None
    h, rem = divmod(secs, 3600)
    mn, sec = divmod(rem, 60)
    return f"{h}:{mn:02d}:{sec:02d}" if h else f"{mn}:{sec:02d}"


def _find_video_ld(soup: BeautifulSoup) -> dict:
    for s in soup.select('script[type="application/ld+json"]'):
        raw = s.string or s.get_text() or ""
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            continue
        cand = d if isinstance(d, list) else [d]
        for item in cand:
            if isinstance(item, dict) and item.get("@type") in ("VideoObject", "AudioObject", "PodcastEpisode"):
                return item
    return {}


def extract_notes(soup: BeautifulSoup) -> str:
    """Return the show-notes body as clean HTML (guests/tags/scripts removed)."""
    ed = soup.select_one(".episode-details")
    if not ed:
        return ""
    # operate on a detached copy
    ed = BeautifulSoup(str(ed), "html.parser").select_one(".episode-details")
    for sel in (".guests", ".tags", "script", "style", ".related-posts", ".ad", "ins"):
        for el in ed.select(sel):
            el.decompose()
    # drop stray "Transcripts" heading left behind by related-posts removal
    for h in ed.find_all(["h2", "h3", "h4"]):
        if h.get_text(strip=True).lower().startswith("transcript"):
            h.decompose()
    # drop promotional boilerplate paragraphs
    for p in ed.find_all("p"):
        t = p.get_text(" ", strip=True).lower()
        if t and any(b in t for b in _NOTE_BOILERPLATE):
            p.decompose()
    html = ed.decode_contents().strip()
    html = re.sub(r"(\s*\n\s*){2,}", "\n", html)
    return html.strip()


def parse_episode(n: int, html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    src = soup.select_one('source[type="audio/mpeg"]')
    if not src or not src.get("src"):
        return None  # no audio enclosure -> not a usable episode
    audio = clean_audio_url(src["src"])

    ld = _find_video_ld(soup)
    name = (ld.get("name") or "").strip()
    summary = (ld.get("description") or "").strip()
    thumb = ld.get("thumbnailUrl")
    if isinstance(thumb, list):
        thumb = thumb[0] if thumb else None

    title_el = soup.select_one("h1.title")
    page_title = title_el.get_text(strip=True) if title_el else f"This Week in Tech {n}"
    sub_el = soup.select_one("h2.subtitle")
    subtitle = sub_el.get_text(strip=True) if sub_el else ""

    air_el = soup.select_one("p.air-date")
    air_date = air_el.get_text(strip=True) if air_el else None

    dt = parse_upload_date(ld.get("uploadDate")) or parse_air_date(air_date)

    guests = []
    g = soup.select_one(".episode-details .guests")
    if g:
        links = [a.get_text(strip=True) for a in g.select("a")]
        if links:
            guests = links
        else:
            guests = [
                x.strip()
                for x in g.get_text().replace("Guests:", "").split(",")
                if x.strip()
            ]

    secs = iso_duration_to_secs(ld.get("duration"))

    # Nice, human episode title: "1094: Rest in Peace, Buzzkill"
    if name:
        title = f"{n}: {name}"
    else:
        title = page_title

    return {
        "n": n,
        "title": title,
        "name": name,
        "subtitle": (summary or subtitle or "")[:255],
        "summary": summary or subtitle,
        "date": dt.isoformat() if dt else None,
        "duration": secs_to_hms(secs),
        "duration_secs": secs,
        "audio": audio,
        "size": None,  # filled in later (best effort)
        "image": thumb or FEED_IMAGE,
        "guests": guests,
        "notes_html": extract_notes(soup),
        "page": EP_URL.format(n=n),
    }


def fetch_episode(session: requests.Session, n: int) -> tuple[str, object]:
    url = EP_URL.format(n=n)
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException as e:
        return ("error", (n, str(e)))
    if r.status_code == 404:
        return ("missing", n)
    if r.status_code != 200:
        return ("error", (n, f"HTTP {r.status_code}"))
    data = parse_episode(n, r.text)
    if not data:
        return ("missing", n)
    return ("ok", data)


def fetch_size(session: requests.Session, url: str) -> int:
    """Best-effort content length in bytes (0 if unknown)."""
    try:
        r = session.head(url, allow_redirects=True, timeout=20)
        cl = r.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > 1:
            return int(cl)
        # fall back to a ranged GET to read Content-Range total
        r = session.get(
            url, stream=True, timeout=20, headers={"Range": "bytes=0-0"}
        )
        cr = r.headers.get("Content-Range", "")
        r.close()
        if "/" in cr:
            total = cr.rsplit("/", 1)[-1]
            if total.isdigit():
                return int(total)
    except requests.RequestException:
        pass
    return 0


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def load_cache() -> dict:
    if CACHE_PATH.exists():
        with CACHE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("episodes", {})
        data.setdefault("missing", [])
        return data
    return {"episodes": {}, "missing": []}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache["generated"] = datetime.now(timezone.utc).isoformat()
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
    tmp.replace(CACHE_PATH)


# --------------------------------------------------------------------------- #
# RSS generation
# --------------------------------------------------------------------------- #
def rfc822(iso: str | None) -> str:
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


def cdata(s: str | None) -> str:
    s = s or ""
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_item(ep: dict) -> str:
    parts = ["  <item>"]
    parts.append(f"    <title>{xml_escape(ep['title'])}</title>")
    if ep.get("name"):
        parts.append(f"    <itunes:title>{xml_escape(ep['name'])}</itunes:title>")
    parts.append(f"    <itunes:episode>{ep['n']}</itunes:episode>")
    parts.append("    <itunes:episodeType>full</itunes:episodeType>")
    parts.append(f'    <guid isPermaLink="false">twit-archive-ep-{ep["n"]}</guid>')
    parts.append(f"    <link>{xml_escape(ep['page'])}</link>")
    if ep.get("date"):
        parts.append(f"    <pubDate>{rfc822(ep['date'])}</pubDate>")

    size = ep.get("size") or 0
    parts.append(
        f'    <enclosure url="{xml_escape(ep["audio"])}" '
        f'length="{size}" type="audio/mpeg"/>'
    )
    if ep.get("duration"):
        parts.append(f"    <itunes:duration>{ep['duration']}</itunes:duration>")
    if ep.get("image"):
        parts.append(f'    <itunes:image href="{xml_escape(ep["image"])}"/>')
    parts.append(f"    <itunes:author>{xml_escape(FEED_AUTHOR)}</itunes:author>")
    parts.append("    <itunes:explicit>false</itunes:explicit>")

    if ep.get("subtitle"):
        parts.append(f"    <itunes:subtitle>{xml_escape(ep['subtitle'])}</itunes:subtitle>")
    if ep.get("summary"):
        parts.append(f"    <itunes:summary>{cdata(ep['summary'])}</itunes:summary>")

    # rich description: guests line + show notes
    body = ""
    if ep.get("guests"):
        body += "<p><strong>Guests:</strong> " + xml_escape(", ".join(ep["guests"])) + "</p>\n"
    body += ep.get("notes_html") or (f"<p>{xml_escape(ep.get('summary',''))}</p>" if ep.get("summary") else "")
    if body.strip():
        parts.append(f"    <description>{cdata(body)}</description>")
        parts.append(f"    <content:encoded>{cdata(body)}</content:encoded>")
    parts.append("  </item>")
    return "\n".join(parts)


def build_rss(episodes: list[dict]) -> str:
    items = [e for e in episodes if e.get("audio")]
    items.sort(key=lambda e: (e.get("date") or "", e["n"]), reverse=True)

    now = format_datetime(datetime.now(timezone.utc))
    head = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"  <title>{xml_escape(FEED_TITLE)}</title>",
        f"  <link>{xml_escape(FEED_LINK)}</link>",
        f"  <language>{FEED_LANG}</language>",
        f"  <description>{xml_escape(FEED_DESC)}</description>",
        f"  <itunes:author>{xml_escape(FEED_AUTHOR)}</itunes:author>",
        f"  <itunes:summary>{xml_escape(FEED_DESC)}</itunes:summary>",
        "  <itunes:type>episodic</itunes:type>",
        "  <itunes:explicit>false</itunes:explicit>",
        f'  <itunes:image href="{xml_escape(FEED_IMAGE)}"/>',
        "  <image>",
        f"    <url>{xml_escape(FEED_IMAGE)}</url>",
        f"    <title>{xml_escape(FEED_TITLE)}</title>",
        f"    <link>{xml_escape(FEED_LINK)}</link>",
        "  </image>",
        f'  <itunes:category text="{xml_escape(FEED_CATEGORY)}"/>',
        "  <itunes:owner>",
        f"    <itunes:name>{xml_escape(FEED_OWNER_NAME)}</itunes:name>",
    ]
    if FEED_OWNER_EMAIL:
        head.append(f"    <itunes:email>{xml_escape(FEED_OWNER_EMAIL)}</itunes:email>")
    head.append("  </itunes:owner>")
    head.append(f"  <lastBuildDate>{now}</lastBuildDate>")
    head.append(f"  <generator>twit-archive</generator>")
    if FEED_SELF_URL:
        head.append(
            f'  <atom:link href="{xml_escape(FEED_SELF_URL)}" '
            'rel="self" type="application/rss+xml"/>'
        )

    body = [build_item(e) for e in items]
    tail = ["</channel>", "</rss>", ""]
    return "\n".join(head + body + tail)


def build_index(episodes: list[dict], feed_url: str) -> str:
    eps = sorted([e for e in episodes if e.get("audio")], key=lambda e: e["n"], reverse=True)
    n = len(eps)
    latest = eps[0] if eps else None
    oldest = eps[-1] if eps else None
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def yr(e):
        return (e.get("date") or "")[:4] if e else "?"

    rows = "\n".join(
        f'<tr><td>{e["n"]}</td><td>{xml_escape(e.get("name") or "")}</td>'
        f'<td>{(e.get("date") or "")[:10]}</td>'
        f'<td><a href="{xml_escape(e["page"])}">page</a></td></tr>'
        for e in eps[:60]
    )
    sub = feed_url or "feed.xml"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{xml_escape(FEED_TITLE)}</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:820px;
   margin:2rem auto;padding:0 1rem;line-height:1.5;color:#12212e;background:#f6f8fa}}
 a{{color:#0a58ca}}
 .card{{background:#fff;border:1px solid #d8dee4;border-radius:12px;padding:1.25rem 1.5rem;margin-bottom:1.5rem}}
 code{{background:#eef1f4;padding:.15rem .4rem;border-radius:6px;word-break:break-all}}
 .btn{{display:inline-block;background:#0a58ca;color:#fff;padding:.55rem 1rem;border-radius:8px;
   text-decoration:none;margin:.25rem .25rem 0 0}}
 table{{border-collapse:collapse;width:100%;font-size:.9rem}}
 td,th{{border-bottom:1px solid #e4e9ee;padding:.35rem .5rem;text-align:left}}
 .muted{{color:#5a6b7b;font-size:.9rem}}
 img.art{{float:right;width:120px;border-radius:10px;margin:0 0 1rem 1rem}}
</style></head><body>
<img class="art" src="{xml_escape(FEED_IMAGE)}" alt="show art">
<h1>{xml_escape(FEED_TITLE)}</h1>
<p class="muted">Unofficial full-archive podcast feed. Audio streams directly from TWiT's CDN.</p>
<div class="card">
  <h2>Subscribe</h2>
  <p>Add this URL in your podcast app:</p>
  <p><code>{xml_escape(sub)}</code></p>
  <p>
    <a class="btn" href="{xml_escape(sub)}">feed.xml</a>
    <a class="btn" href="{xml_escape(sub.replace('https://','podcast://').replace('http://','podcast://'))}">Open in podcast app</a>
  </p>
</div>
<div class="card">
  <h2>Archive</h2>
  <p><strong>{n}</strong> episodes &middot; {yr(oldest)}–{yr(latest)} &middot;
     updated {updated}</p>
  <table><thead><tr><th>#</th><th>Title</th><th>Date</th><th></th></tr></thead>
  <tbody>{rows}</tbody></table>
  <p class="muted">Showing the {min(60,n)} most recent; the feed contains all {n}.</p>
</div>
</body></html>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true",
                    help="re-crawl every episode (ignore cache hits)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only consider episodes 1..LIMIT (smoke test)")
    ap.add_argument("--latest", type=int, default=0,
                    help="override the highest episode number instead of discovering it")
    ap.add_argument("--refresh", type=int, default=5,
                    help="always re-fetch the N most recent episodes (default 5)")
    ap.add_argument("--workers", type=int, default=8, help="concurrent fetchers")
    ap.add_argument("--no-sizes", dest="sizes", action="store_false",
                    help="skip enclosure byte-size lookups")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="output directory")
    args = ap.parse_args(argv)

    session = make_session()

    latest = args.latest or discover_latest(session)
    if args.limit:
        latest = min(latest, args.limit) if latest else args.limit
    if not latest:
        print("Could not determine latest episode number.", file=sys.stderr)
        return 1
    print(f"Latest episode: {latest}")

    cache = load_cache()
    eps = cache["episodes"]
    missing = set(cache.get("missing", []))

    if args.full:
        todo = list(range(1, latest + 1))
    else:
        todo = [n for n in range(1, latest + 1)
                if str(n) not in eps and n not in missing]
        # always refresh the most recent handful (show notes get edited)
        todo += [n for n in range(max(1, latest - args.refresh + 1), latest + 1)]
        todo = sorted(set(todo))

    print(f"Episodes to fetch: {len(todo)} "
          f"(cached: {len(eps)}, known-missing: {len(missing)})")

    lock = threading.Lock()
    done = {"c": 0}
    n_new = 0

    def worker(n):
        return fetch_episode(session, n)

    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(worker, n): n for n in todo}
            for fut in as_completed(futs):
                status, payload = fut.result()
                with lock:
                    done["c"] += 1
                    c = done["c"]
                if status == "ok":
                    eps[str(payload["n"])] = payload
                    missing.discard(payload["n"])
                    n_new += 1
                elif status == "missing":
                    missing.add(payload)
                    eps.pop(str(payload), None)
                else:  # error
                    n, err = payload
                    print(f"  ! ep {n}: {err}", file=sys.stderr)
                if c % 50 == 0 or c == len(todo):
                    print(f"  fetched {c}/{len(todo)}")

    # enclosure sizes (best effort, cached)
    if args.sizes:
        need = [e for e in eps.values() if not e.get("size")]
        if need:
            print(f"Fetching enclosure sizes for {len(need)} episodes...")
            prog = {"c": 0}
            def size_worker(e):
                e["size"] = fetch_size(session, e["audio"])
                with lock:
                    prog["c"] += 1
                    if prog["c"] % 100 == 0 or prog["c"] == len(need):
                        print(f"  sizes {prog['c']}/{len(need)}")
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                list(ex.map(size_worker, need))

    cache["missing"] = sorted(missing)
    cache["latest"] = latest
    save_cache(cache)

    episodes = list(eps.values())
    args.out.mkdir(parents=True, exist_ok=True)
    feed_xml = build_rss(episodes)
    (args.out / "feed.xml").write_text(feed_xml, encoding="utf-8")
    (args.out / "index.html").write_text(
        build_index(episodes, FEED_SELF_URL), encoding="utf-8"
    )
    # avoid Jekyll processing on GitHub Pages
    (args.out / ".nojekyll").write_text("", encoding="utf-8")

    with_audio = sum(1 for e in episodes if e.get("audio"))
    print(f"\nDone. {with_audio} episodes in feed "
          f"(+{n_new} fetched this run). Wrote {args.out/'feed.xml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
