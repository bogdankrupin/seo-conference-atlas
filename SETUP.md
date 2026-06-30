# Conference Atlas — live AI-visibility + route planner

This upgrade turns the static globe into a self-refreshing one. Every conference now
carries an **AI-visibility score** (how often its site/brand surfaces in LLM answers,
from SE Ranking AI Search), you can filter to **the next few months**, and you can
**build a travel route** across the events worth your time — with hop distances,
flight-time estimates, gaps between dates and a rough cost band.

## What's in the repo now

```
index.html                         # the app (loads data/conferences.json, falls back to a built-in seed)
data/conferences.json              # the data the globe reads; refreshed by the Action
scripts/refresh.py                 # discovery + SE Ranking enrichment + scoring (stdlib only)
.github/workflows/atlas-refresh.yml# scheduled + manual refresh, commits the JSON
```

The app still works as a single file: if `data/conferences.json` is missing it uses the
embedded seed, so nothing breaks before the first refresh.

## How the data flows

```
GitHub Action (weekly cron, or manual)
  -> [optional] Claude + web_search  : discover NEW upcoming events
  -> SE Ranking AI Search            : prompts-by-target `total` per event = AI visibility
  -> normalize to a 0-100 score
  -> commit data/conferences.json
GitHub Pages serves index.html, which fetches that JSON on load
```

"Real-time" in practice = a scheduled refresh. A public static page can't call SE Ranking
live (the API key must stay secret), so the Action does it on a schedule and bakes the
results into the JSON.

## Setup (GitHub web UI only — no terminal)

1. **Add the files** (Add file → Create new file, paste, Commit) at these exact paths:
   `index.html`, `data/conferences.json`, `scripts/refresh.py`,
   `.github/workflows/atlas-refresh.yml`.
2. **Add secrets**: Settings → Secrets and variables → Actions → New repository secret:
   - `SERANKING_API_KEY` — required (enrichment). Find it in your SE Ranking API dashboard.
   - `ANTHROPIC_API_KEY` — optional, only needed if you turn discovery on.
3. **Run it once**: Actions tab → *Refresh Conference Atlas* → Run workflow. Leave
   defaults (engines `ai-overview`, discover `0`) for the cheapest first run.
4. Pages is already serving the root, so the live atlas picks up the new JSON automatically.

## Credit cost — read before scheduling

AI Search **prompts-by-target** costs **200 credits per returned prompt**. The script asks
for `limit=1`, so each call is ~200 credits regardless of how visible the event is.

```
cost ≈ (events) × (engines) × 200
31 events × 1 engine (ai-overview)  ≈  6,200 credits / run
31 events × 2 engines               ≈ 12,400 credits / run
```

`MAX_REQUESTS` in the workflow caps calls per run as a hard credit guard (default 120).
The weekly cron is deliberate — AI Search data refreshes monthly, so weekly is already
generous; switch the cron to monthly (`0 6 1 * *`) if you want to spend less.

## Tuning (all via the manual "Run workflow" form)

- **engines** — start with `ai-overview` (it carries volume data). Add `chatgpt`,
  `perplexity`, `gemini`, `ai-mode` for a fuller picture at proportionally more credits.
- **discover** — set to `1` to let Claude web-search for new events and merge them in
  (needs `ANTHROPIC_API_KEY`). `MAX_NEW` caps how many get added per run.
- **source** — regional prompt database, default `us`. Change for a different market.
- **window_months** — how far ahead discovery looks.

## Editing events by hand

`data/conferences.json` stays yours — manual edits and the `domain` you set survive every
run. Add or fix an event with this shape (the `ai`/`ai_prompts` fields fill themselves):

```json
{ "n":"Your Event", "city":"Berlin", "country":"Germany", "region":"Europe",
  "lat":52.52, "lng":13.405, "d":"Sep 12-13, 2026", "iso":"2026-09-12",
  "tbc":false, "cat":"SEO", "domain":"yourevent.com" }
```

Getting `domain` right matters most — it's what SE Ranking measures. A handful of seed
events have `""` (couldn't confirm a domain); fill those in and they'll start scoring.
When a domain is blank the script falls back to a brand-name lookup (`prompts-by-brand`),
which is noisier.

## SE Ranking endpoints used

- Get prompts by target (the AI-visibility signal) — https://seranking.com/api/data/ai-search#get-prompts-by-target
- Get prompts by brand (fallback when no domain) — https://seranking.com/api/data/ai-search#get-prompts-by-brand
- Regional database codes (the `source` values) — https://seranking.com/api/data/reference#regional-database-codes

Heavier options you can layer on later if you want richer scoring (both cost more):
AI search overview (brand/link presence, trend; 800 cr/req) and the leaderboard
(share-of-voice across up to 10 domains; 7,500 cr/req, POST) —
https://seranking.com/api/data/ai-search#overview · https://seranking.com/api/data/ai-search#leaderboard
