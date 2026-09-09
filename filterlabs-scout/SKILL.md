---
name: filterlabs-scout
description: Use for FilterLabs Scout API. Login, token refresh, search.
---

# FilterLabs Scout: login, token management, and search

Self-contained skill for FilterLabs/Ubiquity services:
- Ubiquity (ubiquity.filterlabs.ai) frontend, secured by Keycloak at
  auth.filterlabs.ai (realm `filter-labs-web`, public client `web-app`).
- Scout search API at scout.ubiquity.filterlabs.ai/api/v1/search — takes
  topics (+ optional keywords), returns curated news/social/report results.

Everything needed (auth + search) is bundled in this skill's scripts/
directory and works on any machine — no hardcoded paths. Requires only a
FilterLabs account (email + password) and Python 3 with stdlib.

## First-time setup (per user/machine)
No tokens exist yet until someone logs in once. Direct Access Grants
(Resource Owner Password Credentials) is enabled on the `web-app` client,
so a single call gets both tokens — no browser/PKCE flow required:

  python3 scripts/filterlabs_auth.py --login "email@example.com" "password"

This stores tokens at ~/.hermes/secrets/filterlabs_tokens.json (chmod 600,
never the raw password). All later calls use this file automatically.

IMPORTANT for agents: do NOT try to drive a headless browser through the
login UI to get tokens — the browser tool's Chrome is typically headless
with no visible window for the user to type into. Just ask the user for
their email+password directly and run the --login command above.

## Token lifecycle
- access_token: ~30 min TTL
- refresh_token: ~8 hr TTL, rotates on every refresh (old one becomes invalid)
- scripts/filterlabs_auth.py get_access_token() (or `python3 filterlabs_auth.py`
  with no args) always returns a currently-valid access token, silently
  refreshing via the refresh_token grant if <120s remain on the current one.
- If the refresh_token has ALSO expired (>8h since last login/refresh), it
  raises RuntimeError — at that point re-run --login with fresh credentials.

CLI:
  python3 scripts/filterlabs_auth.py                # prints a valid access token
  python3 scripts/filterlabs_auth.py --status        # prints expiry countdowns
  python3 scripts/filterlabs_auth.py --login E P     # first-time / re-login

## Scout search
Request: POST https://scout.ubiquity.filterlabs.ai/api/v1/search
  Headers: Authorization: Bearer <access_token>, Content-Type: application/json
  Body: {"max_results": <int, required>, "topics": ["..."], "keywords": ["..."] (optional)}

Response: {request_id, content (raw LLM narration), results:
[{title,url,source,language,published_at,snippet}, ...], queries_used
(internal expanded queries Scout actually ran), usage}

### Foreground (small requests, max_results <~30)
  python3 scripts/scout_search.py --topics "topic one" "topic two" \
      --keywords kw1 kw2 --max-results 10
  python3 scripts/scout_search.py --topics "topic" --max-results 10 --json   # raw JSON

Timeout auto-scales with max_results (min 600s, +5s per requested
result) — no need to pass --timeout manually unless overriding. Even a
small request (e.g. max_results=30) has taken ~9 minutes end-to-end in
practice, so the floor is intentionally generous.

### Background (long-running requests — USE THIS for max_results >= 50,
or whenever you'd otherwise block a shell/terminal call waiting)
  # Launch: returns immediately with a PID and files to poll
  python3 scripts/scout_search.py --topics "topic" --max-results 300 \
      --out /tmp/scout_run.json --background

  # Poll (repeat until status is done/error; safe to call anytime)
  python3 scripts/scout_search.py --status /tmp/scout_run.json
  # -> "status: running" | "status: done\nresults: N" | "status: error\nsee log: ..."

When driving this from the agent's own terminal tool, launch with
--background (returns instantly), then use the terminal tool's own
background+notify mechanism (or repeated --status polls) rather than
blocking a single foreground call on a 5-10+ minute Scout request.

### Library usage
  import sys, os
  sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
  from scout_search import scout_search
  data = scout_search(topics=["AI safety"], keywords=["alignment"], max_results=5)
  for r in data["results"]:
      print(r["title"], r["url"])
  # timeout=None (default) auto-scales with max_results; pass an int to override.

## Usage notes
- topics is required (non-empty list); keywords is optional.
- Map the user's core subject(s) to topics, narrower filter terms to
  keywords. Scout expands these into several internal search queries
  (see `queries_used`) — keep topics/keywords short/conceptual, not full
  sentences. To bias toward social platforms, say so explicitly in the
  topic/keywords (e.g. topics=["Reddit and Twitter/X reactions to ..."],
  keywords=["reddit","twitter","X","tweet"]) — Scout does not default to
  social sources.
- max_results is a hint/ceiling, NOT a guarantee. Scout has returned far
  fewer structured results than requested (e.g. asked for 500, got 56)
  even on a successful HTTP 200. ALWAYS check len(data["results"]) and
  report the actual count to the user — never assume you got what you
  asked for. The CLI prints `requested: X  received: Y` and warns on
  stderr when received < 50% of requested.
- `content` is free-text LLM narration. Normally ignore it and parse
  `results`. EXCEPTION: if `results` comes back empty (seen on
  social/Reddit-heavy queries where Reddit access gets rate-limited
  server-side), scout_search() automatically tries to salvage a results
  list out of an embedded ```json block inside `content` and sets
  `results_salvaged=True` on the returned dict. Treat salvaged results
  as lower-confidence/approximate (the source narration may itself note
  reconstruction from partial data) and mention the caveat to the user.
- 401 errors usually mean the refresh_token itself expired (>8h idle) —
  re-run --login with fresh credentials.
