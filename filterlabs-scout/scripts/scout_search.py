#!/usr/bin/env python3
"""
FilterLabs Scout search client.

Calls scout.ubiquity.filterlabs.ai/api/v1/search with a valid FilterLabs
OAuth access token (auto-refreshed via filterlabs_auth.py, in this same
directory) and returns structured results.

As of the 2026 API revision:
  1. Request body is a SINGLE free-form "query" string -- there is no
     more topics/keywords/max_results structure on the wire. Put
     everything in the query text: subject matter, which networks/
     sources to search (e.g. "Reddit and Twitter/X"), how many results
     you want back, recency, language, etc. Scout parses all of that
     out of the natural-language text itself.
  2. The API is now ASYNC. POST /search returns immediately with
     {request_id, status: "queued", poll_url, wait_hint_seconds, ...}
     -- it does NOT block until results are ready. The client must poll
     GET https://scout.ubiquity.filterlabs.ai<poll_url> (Bearer auth)
     until status becomes "done" (or "error"). This module's
     scout_search() handles that polling loop internally, so callers
     still just get back a finished results dict -- but be aware the
     underlying HTTP exchange is now POST + repeated GET, not one POST.

First-time setup (once per user/machine):
    python3 filterlabs_auth.py --login "email@example.com" "password"

Usage as a library:
    from scout_search import scout_search
    data = scout_search("Find the 5 most recent news articles about AI safety")
    for r in data["results"]:
        print(r["title"], r["url"])

CLI usage (foreground, blocks until done):
    python3 scout_search.py --query "Find 10 recent articles about AI safety and interpretability"

    # JSON output only (for piping):
    python3 scout_search.py --query "..." --json

CLI usage (long-running searches, e.g. asking for many results):
    # Launches itself detached via nohup, returns immediately with a PID
    # and the output file path. Poll that file / check the PID to know
    # when it's done -- do NOT block a foreground shell on large requests.
    python3 scout_search.py --query "Find 300 articles about climate policy" \
        --out /tmp/scout_run.json --background --results-hint 300

    # Then, elsewhere:
    python3 scout_search.py --status /tmp/scout_run.json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filterlabs_auth import get_access_token  # noqa: E402

SCOUT_BASE = "https://scout.ubiquity.filterlabs.ai"
SCOUT_URL = SCOUT_BASE + "/api/v1/search"

# Observed behavior (2026-09, still true post-migration to the single
# `query` field + async poll_url flow): Scout does not reliably deliver
# as many results as the query asks for. Requests for large counts
# (e.g. 500) have come back with the structured `results` list far
# smaller (tens, not hundreds) even though the request succeeded (HTTP
# 200 + status "done"). Larger requested counts also increase job
# duration roughly linearly-ish because Scout is doing live multi-query
# research + synthesis, not a DB lookup. Plan accordingly: treat any
# count mentioned in the query as a hint/ceiling, not a guarantee, and
# always check len(data["results"]) before reporting a count to the user.

# `results_hint` is a LOCAL-ONLY value (never sent to the API) used only
# to scale this client's own overall poll timeout. Since the desired
# result count now lives inside the free-text query, callers should pass
# roughly the same number here as they put in the query text so the
# timeout is realistic.
_BASE_TIMEOUT = 600
_SECONDS_PER_RESULT = 5
_POLL_INTERVAL = 10


def _default_timeout(results_hint):
    return max(_BASE_TIMEOUT, int(results_hint) * _SECONDS_PER_RESULT)


def _authed_request(url, token, method="GET", body=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def scout_search(query, results_hint=10, timeout=None, poll_interval=_POLL_INTERVAL):
    """
    query: str (required) -- free-form natural-language description of
        what to find. Fold in everything: topic/subject, which
        networks/sources to search (news, Reddit, Twitter/X, etc.),
        how many results to return, recency window, language, etc.
        Example: "Find the 10 most recent Reddit and Twitter/X reactions
        to the latest Fed interest rate decision, English only."
    results_hint: int -- NOT sent to the API. Used only to scale this
        client's overall poll timeout; pass roughly the same count you
        asked for inside `query` text.
    timeout: int or None -- overall wall-clock budget in seconds to wait
        for the job to reach status "done"/"error". If None, auto-scales
        with results_hint (see _default_timeout). Pass an explicit value
        to override.
    poll_interval: seconds between polls of poll_url (default 10).

    Submits the query (POST /search), then polls the returned poll_url
    until status is "done" or "error", or until `timeout` elapses.

    Returns the parsed JSON response dict with keys:
      request_id, status, content, results, queries_used, usage
    plus, if the API's structured `results` came back empty while
    `content` clearly contains embedded result data (a known Scout
    failure mode on large/social-heavy queries), a best-effort
    `results` list salvaged from `content` and `results_salvaged=True`.
    If the poll times out before status is "done", returns the last
    poll response as-is with its status left at "queued"/"running" --
    callers should check data.get("status") rather than assuming success.

    Note: queries asking for many results (50+) can take several minutes
    and may not return anywhere near the requested count. Run this from
    a background process (see --background) rather than blocking
    synchronously for such queries.
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")

    if timeout is None:
        timeout = _default_timeout(results_hint)

    token = get_access_token()
    try:
        data = _authed_request(SCOUT_URL, token, method="POST", body={"query": query})
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"Scout search failed ({e.code}): {err_body}")

    poll_url = data.get("poll_url")
    deadline = time.monotonic() + timeout
    while data.get("status") in ("queued", "running") and poll_url:
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
        token = get_access_token()  # re-fetch in case it rotated mid-poll
        full_url = urljoin(SCOUT_BASE, poll_url)
        try:
            data = _authed_request(full_url, token, method="GET")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            raise RuntimeError(f"Scout poll failed ({e.code}): {err_body}")

    if not data.get("results") and data.get("content"):
        salvaged = _salvage_results_from_content(data["content"])
        if salvaged:
            data["results"] = salvaged
            data["results_salvaged"] = True

    return data


def _salvage_results_from_content(content):
    """
    Best-effort recovery of result entries when the API's structured
    `results` array is empty but `content` (the model's free-text
    narration) contains an embedded ```json ... ``` block with the real
    data. That block is sometimes itself invalid JSON (unescaped quotes
    inside quoted strings), so a strict json.loads() can fail entirely
    even though the data is mostly there. Strategy:

      1. Try strict json.loads() on the fenced ```json block first --
         cheap and correct when it works.
      2. If that fails, fall back to splitting the raw text on
         `"title":` boundaries and regex-extracting title/url/source/
         published_at/snippet per chunk. This recovers partial/approximate
         data even from malformed JSON and is intentionally lenient.

    Returns a list of dicts (possibly empty) -- never raises.
    """
    m = re.search(r"```json\s*(\{.*\})\s*```", content, re.DOTALL)
    blob = m.group(1) if m else content

    try:
        parsed = json.loads(blob)
        results = parsed.get("results")
        if isinstance(results, list) and results:
            return results
    except (json.JSONDecodeError, AttributeError):
        pass

    # Fallback: regex salvage per-entry from raw text.
    chunks = re.split(r'\{\s*\n?\s*"title"\s*:', blob)[1:]
    salvaged = []
    for chunk in chunks:
        def _field(name):
            fm = re.search(rf'"{name}"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
            return fm.group(1).replace('\\"', '"') if fm else None

        # chunk starts right after `"title":`, so its value is the first
        # quoted string (may itself contain escaped inner quotes).
        title_m = re.match(r'\s*"((?:[^"\\]|\\.)*)"', chunk)
        title = title_m.group(1).replace('\\"', '"') if title_m else None

        entry = {
            "title": title,
            "url": _field("url"),
            "source": _field("source"),
            "language": _field("language"),
            "published_at": _field("published_at"),
            "snippet": _field("snippet"),
        }
        if entry["url"] or entry["title"]:
            salvaged.append(entry)
    return salvaged


def _launch_background(args_list, out_path):
    """
    Re-exec this script's foreground search, detached via nohup, writing
    JSON straight to out_path. Returns immediately with the child PID.
    A companion <out_path>.status file is written "running" -> "done"/
    "error" so callers can poll without parsing process state.
    """
    status_path = out_path + ".status"
    with open(status_path, "w") as f:
        f.write("running")

    cmd = [sys.executable, os.path.abspath(__file__)] + args_list + [
        "--json", "--out", out_path, "--status-file", status_path,
    ]
    with open(out_path + ".log", "w") as logf:
        proc = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return proc.pid, status_path


def _main():
    p = argparse.ArgumentParser(description="Query FilterLabs Scout search")
    p.add_argument("--query", "-q", help="free-form natural-language search request "
                    "(subject, sources/networks, desired result count, recency, etc.)")
    p.add_argument("--results-hint", type=int, default=10,
                    help="LOCAL ONLY (not sent to the API) -- rough number of results "
                         "you asked for inside --query, used to scale the poll timeout")
    p.add_argument("--timeout", type=int, default=None,
                    help="overall poll timeout in seconds (default: auto-scaled with --results-hint)")
    p.add_argument("--json", action="store_true", help="print raw JSON only")
    p.add_argument("--out", help="write full JSON response to this file")
    p.add_argument("--background", action="store_true",
                    help="run detached (nohup) and return immediately; requires --out")
    p.add_argument("--status-file", help=argparse.SUPPRESS)  # internal, set by --background
    p.add_argument("--status", metavar="OUT_FILE",
                    help="check status of a --background run given its --out path; prints "
                         "running/done/error and, if done, a result-count summary, then exits")
    args = p.parse_args()

    if args.status:
        status_path = args.status + ".status"
        state = open(status_path).read().strip() if os.path.exists(status_path) else "unknown"
        print(f"status: {state}")
        if state == "done" and os.path.exists(args.status):
            data = json.load(open(args.status))
            n = len(data.get("results", []))
            print(f"results: {n}" + (" (salvaged from narration text)" if data.get("results_salvaged") else ""))
        elif state == "error":
            errpath = args.status + ".log"
            if os.path.exists(errpath):
                print(f"see log: {errpath}")
        return

    if not args.query:
        p.error("--query is required unless using --status")

    if args.background:
        if not args.out:
            p.error("--background requires --out")
        argv = ["--query", args.query, "--results-hint", str(args.results_hint)]
        if args.timeout:
            argv += ["--timeout", str(args.timeout)]
        pid, status_path = _launch_background(argv, args.out)
        eta = _default_timeout(args.results_hint)
        print(f"started pid={pid} out={args.out} status={status_path}")
        print(f"expect up to ~{eta}s; poll with: python3 {os.path.basename(__file__)} --status {args.out}")
        return

    try:
        data = scout_search(
            query=args.query, results_hint=args.results_hint, timeout=args.timeout,
        )
        state = "done"
    except Exception as e:
        state = "error"
        if args.status_file:
            with open(args.status_file, "w") as f:
                f.write("error")
            if args.out:
                with open(args.out + ".log", "a") as f:
                    f.write(f"\n{e}\n")
        raise
    finally:
        if state == "done" and args.status_file:
            with open(args.status_file, "w") as f:
                f.write("done")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(data, f)

    if data.get("status") not in ("done", None):
        sys.stderr.write(
            f"NOTE: Scout job did not reach status 'done' within the poll timeout "
            f"(last status: {data.get('status')}). Results may be incomplete/empty.\n"
        )

    n_got = len(data.get("results", []))
    if n_got < args.results_hint * 0.5:
        sys.stderr.write(
            f"NOTE: results-hint was {args.results_hint} but Scout returned only "
            f"{n_got} results. This is a known Scout limitation -- treat any count "
            f"mentioned in the query as a hint/ceiling, not a guarantee.\n"
        )

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"request_id: {data.get('request_id')}")
    print(f"job status: {data.get('status')}")
    print(f"queries_used: {data.get('queries_used')}")
    print(f"results_hint: {args.results_hint}  received: {n_got}"
          + (" (salvaged from narration text)" if data.get("results_salvaged") else ""))
    print()
    for i, r in enumerate(data.get("results", []), 1):
        print(f"{i}. {r.get('title')}")
        print(f"   source: {r.get('source')}  published: {r.get('published_at')}")
        print(f"   url: {r.get('url')}")
        snippet = r.get("snippet", "") or ""
        print(f"   {snippet[:300]}")
        print()


if __name__ == "__main__":
    _main()
