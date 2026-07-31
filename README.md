# twit-archive

A full-archive podcast feed for **This Week in Tech (TWiT)**.

The official TWiT feeds only carry the most recent ~10 episodes (they cite feed
size / bandwidth). But every episode — going back to episode 1 in April 2005 —
still has a public page on [twit.tv](https://twit.tv/shows/this-week-in-tech).
This repo scrapes those pages and builds **one RSS feed containing the entire
back catalogue**, with real titles, show notes, guests, artwork and durations,
so you can subscribe to the whole thing in any podcast player.

It's a personal-use archive tool. Audio is **not** rehosted — feed enclosures
point straight at TWiT's own CDN, so streaming an episode pulls it directly from
TWiT exactly like the official player would.

## How it works

`build_feed.py`:

1. Finds the latest episode number from the show page.
2. Fetches each episode page (`/shows/this-week-in-tech/episodes/N`) and parses:
   - title & episode number (from the page + embedded JSON‑LD `VideoObject`),
   - air date / publish timestamp, duration, per‑episode artwork,
   - guests, category, and the full HTML show notes,
   - the MP3 enclosure (ad/analytics tracking prefixes are stripped to a clean
     `cdn.twit.tv` URL).
3. Caches everything to `data/episodes.json` so re-runs only fetch new episodes
   (plus a re-check of the most recent few, since show notes get edited).
4. Writes `public/feed.xml` (RSS 2.0 + iTunes tags) and a small
   `public/index.html` landing page.

## Run it locally

```bash
pip install -r requirements.txt
python build_feed.py            # incremental update
python build_feed.py --full     # re-crawl everything
python build_feed.py --limit 20 # quick smoke test
```

Then either point your podcast app at the local `public/feed.xml`, or host it
(see below).

Useful flags: `--no-sizes` (skip enclosure byte-size lookups, faster),
`--workers N`, `--refresh N` (how many recent episodes to always re-fetch),
`--latest N` (override auto-discovery).

## Hosting — the subscribe URL

The feed is deployed to **GitHub Pages** by the included workflow
(`.github/workflows/build-feed.yml`), which rebuilds weekly and on demand.

Subscribe URL:

```
https://kyerussell.github.io/twit-archive/feed.xml
```

Human landing page (archive list + one-tap subscribe):

```
https://kyerussell.github.io/twit-archive/
```

### One-time Pages setup

The workflow tries to enable Pages automatically. If a run fails at the
**Configure GitHub Pages** step (the Actions token can't always create the
Pages site), enable it once by hand:

1. Repo **Settings → Pages**.
2. **Build and deployment → Source = GitHub Actions**.
3. Re-run the **Build TWiT archive feed** workflow (Actions tab).

After that every run deploys automatically.

### Fallbacks

The feed is also committed to the repo, so it's reachable directly:

```
https://raw.githubusercontent.com/kyerussell/twit-archive/main/public/feed.xml
https://cdn.jsdelivr.net/gh/kyerussell/twit-archive@main/public/feed.xml
```

### Notes & caveats

- **Big feed, by design.** It contains ~1,000+ items. Every mainstream podcast
  app handles this fine; some may take a moment on first load.
- **Enclosure sizes** are fetched best-effort (`Content-Length`); a `0` length
  just means the CDN didn't report one and players will range-request instead.
- If a scheduled run can't push (e.g. branch protection on `main`), loosen the
  protection or run the workflow manually — the Pages deploy still works.
- This scrapes public pages for personal use. TWiT also has an official
  [developer API](https://twit.tv/about/developer-program) (requires a free
  approved app key, rate-limited to 5 req/min) if you'd prefer that route.
