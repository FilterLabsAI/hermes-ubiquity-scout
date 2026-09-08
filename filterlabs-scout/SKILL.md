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

Response: {request_id, content (raw LLM narration — ignore, parse `results`
instead), results: [{title,url,source,language,published_at,snippet}, ...],
queries_used (internal expanded queries Scout actually ran), usage}

CLI (auto-handles token fetch/refresh):
  python3 scripts/scout_search.py --topics "topic one" "topic two" \
      --keywords kw1 kw2 --max-results 10
  python3 scripts/scout_search.py --topics "topic" --max-results 10 --json   # raw JSON

Library usage:
  import sys, os
  sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
  from scout_search import scout_search
  data = scout_search(topics=["AI safety"], keywords=["alignment"], max_results=5)
  for r in data["results"]:
      print(r["title"], r["url"])

## Usage notes
- topics is required (non-empty list); keywords is optional.
- Map the user's core subject(s) to topics, narrower filter terms to
  keywords. Scout expands these into several internal search queries
  (see `queries_used`) — keep topics/keywords short/conceptual, not full
  sentences.
- Large max_results (e.g. 50) can take several minutes to generate — run
  as a background process with a generous timeout (200s+), don't block
  synchronously waiting on a single foreground call.
- `content` is free-text LLM narration; always parse `results` for facts,
  never `content`.
- 401 errors usually mean the refresh_token itself expired (>8h idle) —
  re-run --login with fresh credentials.
