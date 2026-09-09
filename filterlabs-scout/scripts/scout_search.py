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

CLI usage (foreground, blocks until done):
    python3 scout_search.py --topics "AI safety" "climate policy" \
        --keywords alignment interpretability --max-results 5

    # JSON output only (for piping):
    python3 scout_search.py --topics "AI safety" --max-results 5 --json

CLI usage (long-running searches, e.g. max_results >= 50):
    # Launches itself detached via nohup, returns immediately with a PID
    # and the output file path. Poll that file / check the PID to know
    # when it's done -- do NOT block a foreground shell on large requests.
    python3 scout_search.py --topics "AI safety" --max-results 300 \
        --out /tmp/scout_run.json --background

    # Then, elsewhere:
    python3 scout_search.py --status /tmp/scout_run.json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filterlabs_auth import get_access_token  # noqa: E402

SCOUT_URL = "https://scout.ubiquity.filterlabs.ai/api/v1/search"

# Observed behavior (2026-09): Scout does not reliably deliver max_results
# as requested. Large requests (e.g. 500) have come back with the
# structured `results` list far smaller (tens, not hundreds) even though
# the request succeeded (HTTP 200). Larger max_results also increases
# response time roughly linearly-ish because Scout is doing live
# multi-query research + synthesis, not a DB lookup. Plan accordingly:
# treat max_results as an upper bound / hint, not a guarantee, and always
# check len(data["results"]) against what was asked before reporting a
# count to the user.

# Rough floor for read timeout, scaled by how many results were asked
# for. Empirically: max_results=5-10 has taken anywhere from ~30s to
# ~4 minutes; max_results=500 took ~9 minutes end-to-end (and still only
# returned 56 results). This is intentionally generous -- it's a
# ceiling, not an expected duration.
_BASE_TIMEOUT = 600
_SECONDS_PER_RESULT = 5


def _default_timeout(max_results):
    return max(_BASE_TIMEOUT, int(max_results) * _SECONDS_PER_RESULT)


def scout_search(topics, keywords=None, max_results=10, timeout=None):
    """
    topics: list[str] (required)
    keywords: list[str] or None (optional)
    max_results: int
    timeout: int or None -- read timeout in seconds. If None, auto-scales
        with max_results (see _default_timeout). Pass an explicit value
        to override.

    Returns the parsed JSON response dict with keys:
      request_id, content, results, queries_used, usage
    plus, if the API's structured `results` came back empty while
    `content` clearly contains embedded result data (a known Scout
    failure mode on large/social-heavy queries), a best-effort
    `results` list salvaged from `content` and `results_salvaged=True`.

    Note: large max_results (e.g. 50+) can take several minutes and may
    not return anywhere near the requested count. Run this from a
    background process (see --background) rather than blocking
    synchronously when max_results is large (roughly >= 50).
    """
    if not topics:
        raise ValueError("topics must be a non-empty list of strings")

    if timeout is None:
        timeout = _default_timeout(max_results)

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
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"Scout search failed ({e.code}): {err_body}")

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
    p.add_argument("--topics", nargs="+", help="one or more topic strings")
    p.add_argument("--keywords", nargs="*", default=None, help="optional keyword strings")
    p.add_argument("--max-results", type=int, default=10)
    p.add_argument("--timeout", type=int, default=None,
                    help="read timeout in seconds (default: auto-scaled with --max-results)")
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

    if not args.topics:
        p.error("--topics is required unless using --status")

    if args.background:
        if not args.out:
            p.error("--background requires --out")
        argv = ["--topics"] + args.topics
        if args.keywords:
            argv += ["--keywords"] + args.keywords
        argv += ["--max-results", str(args.max_results)]
        if args.timeout:
            argv += ["--timeout", str(args.timeout)]
        pid, status_path = _launch_background(argv, args.out)
        eta = _default_timeout(args.max_results)
        print(f"started pid={pid} out={args.out} status={status_path}")
        print(f"expect up to ~{eta}s; poll with: python3 {os.path.basename(__file__)} --status {args.out}")
        return

    try:
        data = scout_search(
            topics=args.topics, keywords=args.keywords,
            max_results=args.max_results, timeout=args.timeout,
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

    n_got = len(data.get("results", []))
    if n_got < args.max_results * 0.5:
        sys.stderr.write(
            f"NOTE: requested max_results={args.max_results} but Scout returned only "
            f"{n_got} results. This is a known Scout limitation -- treat max_results as "
            f"a hint/ceiling, not a guarantee.\n"
        )

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"request_id: {data.get('request_id')}")
    print(f"queries_used: {data.get('queries_used')}")
    print(f"requested: {args.max_results}  received: {n_got}"
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
