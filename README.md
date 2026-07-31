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

## Hosting (GitHub Pages) — the subscribe URL

The included workflow (`.github/workflows/build-feed.yml`) rebuilds the feed
weekly and deploys it to GitHub Pages. **One-time setup:**

1. Push this repo to GitHub (branch merged into `main`).
2. In the repo, go to **Settings → Pages** and set **Source = GitHub Actions**.
3. Run the **Build TWiT archive feed** workflow once (Actions tab →
   *Run workflow*), or wait for the weekly schedule.

Your feed will then live at:

```
https://<your-username>.github.io/twit-archive/feed.xml
```

Subscribe to that URL in your podcast player. The landing page at
`https://<your-username>.github.io/twit-archive/` shows the archive and a
one-tap subscribe link.

The workflow also commits the refreshed `data/episodes.json` and
`public/feed.xml` back to the repo, so the feed is always available as a
fallback directly from the repository too.

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
