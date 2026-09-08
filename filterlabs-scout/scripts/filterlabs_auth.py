#!/usr/bin/env python3
"""
FilterLabs / Ubiquity OAuth token manager.

Stores tokens in ~/.hermes/secrets/filterlabs_tokens.json and refreshes
the access token automatically when it's near expiry, using the refresh_token
grant against Keycloak (auth.filterlabs.ai).

First-time use (or re-auth after refresh_token expiry, ~8h idle):
    python3 filterlabs_auth.py --login "email@example.com" "password"

Library usage:
    from filterlabs_auth import get_access_token
    token = get_access_token()   # returns a valid access token, refreshing if needed

CLI:
    python3 filterlabs_auth.py            # prints a valid access token to stdout
    python3 filterlabs_auth.py --status   # prints expiry info
    python3 filterlabs_auth.py --login EMAIL PASSWORD   # initial login / re-login
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_PATH = os.path.expanduser("~/.hermes/secrets/filterlabs_tokens.json")
LEEWAY_SECONDS = 120  # refresh if less than this much time remains

REALM = "filter-labs-web"
CLIENT_ID = "web-app"
TOKEN_URL = f"https://auth.filterlabs.ai/realms/{REALM}/protocol/openid-connect/token"


def _load():
    with open(TOKEN_PATH) as f:
        return json.load(f)


def _save(d):
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        json.dump(d, f, indent=2)
    os.chmod(TOKEN_PATH, 0o600)


def _post_token(body_dict):
    body = urllib.parse.urlencode(body_dict).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"Token request failed ({e.code}): {err_body}")


def login(username, password):
    """Initial login via Resource Owner Password Credentials grant.
    Stores tokens; never persists the raw password."""
    data = _post_token({
        "client_id": CLIENT_ID,
        "grant_type": "password",
        "username": username,
        "password": password,
    })
    now = time.time()
    d = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "token_type": data.get("token_type", "Bearer"),
        "access_expires_at": now + data["expires_in"],
        "refresh_expires_at": now + data["refresh_expires_in"],
        "realm": REALM,
        "client_id": CLIENT_ID,
        "token_url": TOKEN_URL,
        "username": username,
    }
    _save(d)
    return d


def _refresh(d):
    data = _post_token({
        "client_id": d.get("client_id", CLIENT_ID),
        "grant_type": "refresh_token",
        "refresh_token": d["refresh_token"],
    })
    now = time.time()
    d["access_token"] = data["access_token"]
    d["refresh_token"] = data.get("refresh_token", d["refresh_token"])  # Keycloak rotates refresh tokens
    d["access_expires_at"] = now + data["expires_in"]
    d["refresh_expires_at"] = now + data.get("refresh_expires_in", d["refresh_expires_at"] - now)
    _save(d)
    return d


def get_access_token():
    try:
        d = _load()
    except FileNotFoundError:
        raise RuntimeError(
            f"No stored tokens at {TOKEN_PATH}. Run:\n"
            "  python3 filterlabs_auth.py --login <email> <password>"
        )
    now = time.time()
    if d["access_expires_at"] - now > LEEWAY_SECONDS:
        return d["access_token"]

    if d["refresh_expires_at"] - now <= LEEWAY_SECONDS:
        raise RuntimeError(
            "Refresh token has also expired. Re-run:\n"
            "  python3 filterlabs_auth.py --login <email> <password>"
        )
    d = _refresh(d)
    return d["access_token"]


def status():
    d = _load()
    now = time.time()
    return {
        "username": d.get("username"),
        "access_expires_in_s": round(d["access_expires_at"] - now, 1),
        "refresh_expires_in_s": round(d["refresh_expires_at"] - now, 1),
    }


if __name__ == "__main__":
    if "--login" in sys.argv:
        idx = sys.argv.index("--login")
        try:
            email, password = sys.argv[idx + 1], sys.argv[idx + 2]
        except IndexError:
            print("Usage: filterlabs_auth.py --login <email> <password>", file=sys.stderr)
            sys.exit(1)
        login(email, password)
        print(f"Login successful. Tokens saved to {TOKEN_PATH}")
    elif "--status" in sys.argv:
        print(json.dumps(status(), indent=2))
    else:
        print(get_access_token())
