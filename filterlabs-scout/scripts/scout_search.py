#!/usr/bin/env python3
"""
FilterLabs Scout search client.

Calls scout.ubiquity.filterlabs.ai/api/v1/search with a valid FilterLabs
OAuth access token (auto-refreshed via filterlabs_auth.py, in this same
directory) and returns structured results.

First-time setup (once per user/machine):
    python3 filterlabs_auth.py --login "email@example.com" "password"

Usage as a library:
    from scout_search import scout_search
    data = scout_search(topics=["AI safety"], keywords=["alignment"], max_results=5)
    for r in data["results"]:
        print(r["title"], r["url"])

CLI usage:
    python3 scout_search.py --topics "AI safety" "climate policy" \
        --keywords alignment interpretability --max-results 5

    # JSON output only (for piping):
    python3 scout_search.py --topics "AI safety" --max-results 5 --json
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filterlabs_auth import get_access_token  # noqa: E402

SCOUT_URL = "https://scout.ubiquity.filterlabs.ai/api/v1/search"


def scout_search(topics, keywords=None, max_results=10, timeout=280):
    """
    topics: list[str] (required)
    keywords: list[str] or None (optional)
    max_results: int

    Returns the parsed JSON response dict with keys:
      request_id, content, results, queries_used, usage

    Note: large max_results (e.g. 50) can take several minutes to generate.
    Run this from a background process with a generous timeout rather than
    blocking synchronously if max_results is large.
    """
    if not topics:
        raise ValueError("topics must be a non-empty list of strings")

    body = {"max_results": max_results, "topics": topics}
    if keywords:
        body["keywords"] = keywords

    token = get_access_token()
    req = urllib.request.Request(
        SCOUT_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"Scout search failed ({e.code}): {err_body}")


def _main():
    p = argparse.ArgumentParser(description="Query FilterLabs Scout search")
    p.add_argument("--topics", nargs="+", required=True, help="one or more topic strings")
    p.add_argument("--keywords", nargs="*", default=None, help="optional keyword strings")
    p.add_argument("--max-results", type=int, default=10)
    p.add_argument("--json", action="store_true", help="print raw JSON only")
    args = p.parse_args()

    data = scout_search(topics=args.topics, keywords=args.keywords, max_results=args.max_results)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"request_id: {data.get('request_id')}")
    print(f"queries_used: {data.get('queries_used')}")
    print()
    for i, r in enumerate(data.get("results", []), 1):
        print(f"{i}. {r.get('title')}")
        print(f"   source: {r.get('source')}  published: {r.get('published_at')}")
        print(f"   url: {r.get('url')}")
        snippet = r.get("snippet", "")
        print(f"   {snippet[:300]}")
        print()


if __name__ == "__main__":
    _main()
