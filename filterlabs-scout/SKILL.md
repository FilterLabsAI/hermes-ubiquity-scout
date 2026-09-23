---
name: filterlabs-scout
description: Use for FilterLabs Scout API. Login, token refresh, search.
---

# FilterLabs Scout: login, token management, and search

Self-contained skill for FilterLabs/Ubiquity services:
- Ubiquity (ubiquity.filterlabs.ai) frontend, secured by Keycloak at
  auth.filterlabs.ai (realm `filter-labs-web`, public client `web-app`).
- Scout search API at scout.ubiquity.filterlabs.ai/api/v1/search — takes a
  single free-form `query` string, returns curated news/social/report
  results.

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
  Headers: Authorization: Bearer *** Content-Type: application/json
  Body: {"query": "<free-form text, required>"}

As of the 2026 API revision, the request body has ONLY one field:
`query`, a free-form natural-language string. There is no more
separate topics/keywords/max_results structure — fold everything into
the text: subject/topic, which networks or sources to search (news,
Reddit, Twitter/X, etc.), how many results you want back, recency
window, language, etc. Scout parses all of that out of the query text
itself. Example query: "Find the 10 most recent Reddit and Twitter/X
reactions to the latest Fed interest rate decision, English only."

The API is now ASYNC: POST /search returns immediately with
{request_id, status: "queued", poll_url, wait_hint_seconds, ...} — it
does NOT block until results are ready. The client must GET
https://scout.ubiquity.filterlabs.ai<poll_url> (same Bearer auth)
repeatedly until status becomes "done" (or "error"). scout_search() in
scripts/scout_search.py handles this polling loop internally, so
callers still just get back a finished dict — but be aware the
underlying exchange is POST + repeated GET, not one blocking POST, if
you ever call the raw HTTP API directly.

Final response once status is "done": {request_id, status, content
(raw LLM narration), results: [{title,url,source,language,
published_at,snippet}, ...], queries_used (internal expanded queries
Scout actually ran), usage}

### Foreground (small requests, asking for <~30 results)
  python3 scripts/scout_search.py --query "Find 10 recent articles about topic X and topic Y"
  python3 scripts/scout_search.py --query "..." --json   # raw JSON

The overall poll timeout (how long the client will keep polling
poll_url before giving up) auto-scales with --results-hint (min 600s,
+5s per hinted result; default 10). --results-hint is LOCAL ONLY
(never sent to the API) — pass roughly the same count you put in the
query text so the timeout is realistic, e.g. --results-hint 30. Even a
small request has taken ~9 minutes end-to-end in practice (submit +
poll until status: done), so the floor is intentionally generous.

### Background (long-running requests — USE THIS when the query asks
for >= 50 results, or whenever you'd otherwise block a shell/terminal
call waiting)
  # Launch: returns immediately with a PID and files to poll
  python3 scripts/scout_search.py --query "Find 300 articles about topic X" \
      --results-hint 300 --out /tmp/scout_run.json --background

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
  data = scout_search("Find 5 recent articles about AI safety and alignment", results_hint=5)
  for r in data["results"]:
      print(r["title"], r["url"])
  # timeout=None (default) auto-scales with results_hint; pass an int to override.

## Usage notes
- query is required (non-empty string) and is the ONLY field sent to the
  API. Write it as a natural-language request, not keyword fragments —
  include subject matter, desired result count, source/network bias,
  recency, and language as plain text in the sentence.
- To bias toward social platforms, say so explicitly in the query text
  (e.g. "...search Reddit and Twitter/X reactions to...") — Scout does
  not default to social sources.
- Any count mentioned in the query is a hint/ceiling, NOT a guarantee.
  Scout has returned far fewer structured results than asked for (e.g.
  asked for 500, got 56) even on a successful HTTP 200. ALWAYS check
  len(data["results"]) and report the actual count to the user — never
  assume you got what you asked for. The CLI prints
  `results_hint: X  received: Y` and warns on stderr when received <
  50% of the hint.
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
- CONCURRENCY: jobs are fully isolated server-side — each
  concurrent POST /search gets its own distinct request_id and its own
  results, so firing off several --background launches at once (or
  calling scout_search() from parallel threads/processes) is safe.
  No staggering or one-at-a-time sequencing is required. (If you ever
  see duplicate request_ids across concurrent jobs, that would
  indicate a regression — treat it as a bug report, not expected
  behavior.)
