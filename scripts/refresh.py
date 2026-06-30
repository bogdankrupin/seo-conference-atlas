#!/usr/bin/env python3
"""
Conference Atlas — data refresh pipeline.

What it does, in order:
  1. Loads the current data/conferences.json (your curated seed survives every run).
  2. (Optional) DISCOVERY: asks Claude + web_search for upcoming SEO/marketing/AI
     conferences in the window that aren't already in the file, and merges them in.
  3. ENRICHMENT: for every conference, asks SE Ranking AI Search how visible its
     site/brand is inside LLM answers (prompts-by-target -> `total`), per engine.
  4. SCORING: normalizes those totals into a 0-100 AI-visibility score (`ai`).
  5. Writes data/conferences.json back with a fresh `generated_at` timestamp.

No third-party packages — standard library only, so it runs on a bare runner.

Endpoints used (SE Ranking Data API, base https://api.seranking.com/v1):
  GET /ai-search/prompts-by-target   -> {"total": int, "prompts":[...]}   (200 credits / returned prompt)
  GET /ai-search/prompts-by-brand    -> {"total": int, "prompts":[...]}   (200 credits / returned prompt)
We request limit=1 everywhere, so each call returns at most ONE prompt = ~200 credits.
Docs: https://seranking.com/api/data/ai-search#get-prompts-by-target

Cost math: (conferences) x (engines) x 200 credits.
  31 confs x 1 engine (ai-overview)  ~ 6,200 credits / run.
  31 confs x 2 engines               ~12,400 credits / run.
Pick ENGINES and the schedule with that in mind.

Environment variables:
  SERANKING_API_KEY   required for enrichment (skip enrichment if unset)
  ANTHROPIC_API_KEY   optional; enables discovery when DISCOVER=1
  SOURCE              regional prompt DB, default "us"
  ENGINES             comma list, default "ai-overview" (also: ai-mode,chatgpt,perplexity,gemini)
  WINDOW_MONTHS       discovery horizon, default "12"
  DISCOVER            "1" to run Claude discovery, default "0"
  MAX_NEW             cap on newly discovered events per run, default "12"
  MAX_REQUESTS        hard cap on SE Ranking calls (credit guard), default "120"
  DATA_PATH           default "data/conferences.json"
"""

import os, sys, json, time, math, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone

SR_BASE   = "https://api.seranking.com/v1"
ANTH_URL  = "https://api.anthropic.com/v1/messages"
ANTH_VER  = "2023-06-01"
ANTH_MODEL = "claude-sonnet-4-6"

SR_KEY    = os.environ.get("SERANKING_API_KEY", "").strip()
ANTH_KEY  = os.environ.get("ANTHROPIC_API_KEY", "").strip()
SOURCE    = os.environ.get("SOURCE", "us").strip() or "us"
ENGINES   = [e.strip() for e in os.environ.get("ENGINES", "ai-overview").split(",") if e.strip()]
WINDOW    = int(os.environ.get("WINDOW_MONTHS", "12"))
DISCOVER  = os.environ.get("DISCOVER", "0").strip() == "1"
MAX_NEW   = int(os.environ.get("MAX_NEW", "12"))
MAX_REQ   = int(os.environ.get("MAX_REQUESTS", "120"))
DATA_PATH = os.environ.get("DATA_PATH", "data/conferences.json")

req_count = 0


# ----------------------------------------------------------------------------- utils
def log(*a):
    print(*a, file=sys.stderr, flush=True)

def norm_name(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())

def http_get_json(url, headers, timeout=40):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_post_json(url, headers, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ----------------------------------------------------------------- SE Ranking calls
def sr_headers():
    return {"Authorization": f"Token {SR_KEY}", "Content-Type": "application/json"}

def prompts_total_by_target(domain, engine):
    """Total prompts where `domain` appears in `engine` results. limit=1 => ~200 credits."""
    global req_count
    if req_count >= MAX_REQ:
        return None
    q = urllib.parse.urlencode({
        "target": domain, "scope": "domain", "source": SOURCE,
        "engine": engine, "limit": 1,
    })
    url = f"{SR_BASE}/ai-search/prompts-by-target?{q}"
    req_count += 1
    try:
        data = http_get_json(url, sr_headers())
        return int(data.get("total", 0) or 0)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):      # no data for this target/engine -> treat as 0
            return 0
        log(f"   ! prompts-by-target {domain}/{engine}: HTTP {e.code}")
        return None
    except Exception as e:
        log(f"   ! prompts-by-target {domain}/{engine}: {e}")
        return None

def prompts_total_by_brand(brand, engine):
    """Fallback when a conference has no usable domain: count brand mentions."""
    global req_count
    if req_count >= MAX_REQ:
        return None
    q = urllib.parse.urlencode({
        "brand": brand, "source": SOURCE, "engine": engine, "limit": 1,
    })
    url = f"{SR_BASE}/ai-search/prompts-by-brand?{q}"
    req_count += 1
    try:
        data = http_get_json(url, sr_headers())
        return int(data.get("total", 0) or 0)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return 0
        log(f"   ! prompts-by-brand {brand}/{engine}: HTTP {e.code}")
        return None
    except Exception as e:
        log(f"   ! prompts-by-brand {brand}/{engine}: {e}")
        return None


# ------------------------------------------------------------------- discovery (LLM)
DISCOVERY_PROMPT = """You are building a calendar of upcoming SEO, marketing, AI-search, content and growth conferences.

Use web search to find real, upcoming events in the next {window} months that are NOT already in this list of names:
{known}

Return ONLY a JSON array (no prose, no markdown) of NEW events. Each item MUST be:
{{"n": "Event name", "city": "City", "country": "Country",
  "region": "Americas" | "Europe" | "APAC",
  "lat": <number>, "lng": <number>,
  "d": "Human date label e.g. Sep 23-24, 2026",
  "iso": "YYYY-MM-DD" (start date, your best estimate if exact day unknown),
  "tbc": true|false (true if the date is not officially confirmed),
  "cat": "SEO"|"AI"|"Content"|"Social"|"Marketing"|"Digital"|"MarTech"|"GTM"|"Tech"|"Retail"|"Ecommerce",
  "domain": "official-site-domain.com (no protocol, no path)"}}

Rules: real events only; accurate lat/lng for the host city; up to {max_new} items; if unsure of a domain leave it "". Output the JSON array and nothing else."""

def discover_new(known_names):
    if not (DISCOVER and ANTH_KEY):
        return []
    log("-> discovery: asking Claude + web_search for new events")
    headers = {
        "x-api-key": ANTH_KEY,
        "anthropic-version": ANTH_VER,
        "content-type": "application/json",
    }
    payload = {
        "model": ANTH_MODEL,
        "max_tokens": 4000,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{
            "role": "user",
            "content": DISCOVERY_PROMPT.format(
                window=WINDOW, max_new=MAX_NEW,
                known="\n".join("- " + n for n in known_names),
            ),
        }],
    }
    try:
        data = http_post_json(ANTH_URL, headers, payload)
    except Exception as e:
        log(f"   ! discovery request failed: {e}")
        return []
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        log("   ! discovery: no JSON array in response")
        return []
    try:
        items = json.loads(text[start:end + 1])
    except Exception as e:
        log(f"   ! discovery: JSON parse error: {e}")
        return []
    out = []
    for it in items:
        if not it.get("n") or it.get("lat") is None or it.get("lng") is None:
            continue
        out.append({
            "n": it["n"], "city": it.get("city", ""), "country": it.get("country", ""),
            "region": it.get("region", "Americas"),
            "lat": float(it["lat"]), "lng": float(it["lng"]),
            "d": it.get("d", ""), "iso": it.get("iso", ""),
            "tbc": bool(it.get("tbc", True)),
            "cat": it.get("cat", "Marketing"),
            "domain": (it.get("domain") or "").replace("https://", "").replace("http://", "").strip("/"),
            "url": ("https://" + it["domain"]) if it.get("domain") else "",
            "ai": None, "ai_prompts": None,
        })
    log(f"   + discovered {len(out)} new events")
    return out[:MAX_NEW]


# ------------------------------------------------------------------------- scoring
def score(confs):
    raw = [c.get("ai_prompts") for c in confs]
    vals = [math.log1p(v) for v in raw if isinstance(v, (int, float))]
    if not vals:
        return
    lo, hi = min(vals), max(vals)
    for c in confs:
        v = c.get("ai_prompts")
        if not isinstance(v, (int, float)):
            c["ai"] = None
        elif hi <= lo:
            c["ai"] = 100 if v > 0 else 0
        else:
            c["ai"] = round(100 * (math.log1p(v) - lo) / (hi - lo))


# ---------------------------------------------------------------------------- main
def main():
    if not os.path.exists(DATA_PATH):
        log(f"! {DATA_PATH} not found"); sys.exit(1)
    doc = json.load(open(DATA_PATH, encoding="utf-8"))
    confs = doc["conferences"] if isinstance(doc, dict) else doc
    log(f"loaded {len(confs)} conferences from {DATA_PATH}")

    # 1. discovery
    known = {norm_name(c["n"]) for c in confs}
    for nc in discover_new([c["n"] for c in confs]):
        if norm_name(nc["n"]) not in known:
            confs.append(nc); known.add(norm_name(nc["n"]))
    log(f"total after discovery: {len(confs)}")

    # 2. enrichment
    if not SR_KEY:
        log("! SERANKING_API_KEY not set — skipping enrichment, keeping existing scores")
    else:
        log(f"-> enrichment via SE Ranking AI Search · engines={ENGINES} source={SOURCE}")
        for c in confs:
            total = None
            domain = (c.get("domain") or "").strip()
            for eng in ENGINES:
                t = prompts_total_by_target(domain, eng) if domain else prompts_total_by_brand(c["n"], eng)
                if t is not None:
                    total = (total or 0) + t
                time.sleep(0.25)
            c["ai_prompts"] = total
            log(f"   {c['n']:<32} {'('+domain+')' if domain else '[brand]':<28} -> {total}")
            if req_count >= MAX_REQ:
                log(f"   . hit MAX_REQUESTS={MAX_REQ}, stopping enrichment early"); break
        score(confs)

    # 3. write back
    out = {
        "schema": "conference-atlas/v2",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE,
        "engines": ENGINES,
        "window_months": WINDOW,
        "note": "ai = 0-100 AI-visibility score from SE Ranking AI Search prompts totals.",
        "conferences": sorted(confs, key=lambda c: c.get("iso", "")),
    }
    json.dump(out, open(DATA_PATH, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    log(f"wrote {DATA_PATH} · {len(confs)} conferences · {req_count} SE Ranking calls (~{req_count*200} credits)")

if __name__ == "__main__":
    main()
