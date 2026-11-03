"""Import followed Spotify artists into artists.yaml (local CLI only, PKCE flow)."""
from __future__ import annotations

import base64
import hashlib
import http.server
import logging
import os
import secrets
import threading
import webbrowser
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from gigwatch.normalize import norm

log = logging.getLogger("gigwatch")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
FOLLOWING_URL = "https://api.spotify.com/v1/me/following"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-follow-read"


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)[:64]
    while len(verifier) < 64:
        verifier += secrets.token_urlsafe(8)
        verifier = verifier[:64]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_auth_url(client_id: str, challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPE, "code_challenge_method": "S256", "code_challenge": challenge,
        "state": state})


def _wait_for_code(expected_state: str, timeout: float = 180) -> str:
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlsplit(self.path).query)
            if q.get("state", [""])[0] == expected_state and "code" in q:
                result["code"] = q["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"gigwatch: you can close this tab.")
            done.set()

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        done.wait(timeout)
    finally:
        srv.shutdown()
        srv.server_close()
    if "code" not in result:
        raise RuntimeError("no authorisation code received")
    return result["code"]


def fetch_token(client_id: str, session: requests.Session | None = None) -> str:
    session = session or requests.Session()
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    url = build_auth_url(client_id, challenge, state)
    print("Open this URL if your browser does not open automatically:\n" + url)
    webbrowser.open(url)
    code = _wait_for_code(state)
    r = session.post(TOKEN_URL, data={
        "client_id": client_id, "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_followed(token: str, session: requests.Session | None = None) -> list[dict[str, str]]:
    session = session or requests.Session()
    out: list[dict[str, str]] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"type": "artist", "limit": 50}
        if after:
            params["after"] = after
        r = session.get(FOLLOWING_URL, params=params,
                        headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        block = r.json().get("artists", {})
        out += [{"name": a["name"], "spotify_id": a.get("id", "")} for a in block.get("items", [])]
        after = (block.get("cursors") or {}).get("after")
        if not after:
            return out


def merge_artists(existing: list[dict[str, Any]], incoming: list[dict[str, str]],
                  tier: str = "maybe") -> tuple[list[dict[str, Any]], int]:
    """Add incoming artists not already present (by norm(name)); existing entries untouched."""
    merged = list(existing)
    seen = {norm(e.get("name", "")) for e in merged}
    for e in merged:
        seen.update(norm(a) for a in e.get("aliases") or [])
    added = 0
    for a in incoming:
        k = norm(a["name"])
        if not k or k in seen:
            continue
        item: dict[str, Any] = {"name": a["name"], "tier": tier}
        if a.get("spotify_id"):
            item["spotify_id"] = a["spotify_id"]
        merged.append(item)
        seen.add(k)
        added += 1
    return merged, added


def import_followed(config_dir: str, tier: str = "maybe") -> int:
    from gigwatch.config import load_artists_raw, save_artists_raw
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise RuntimeError("SPOTIFY_CLIENT_ID is not set")
    token = fetch_token(client_id)
    incoming = fetch_followed(token)
    merged, added = merge_artists(load_artists_raw(config_dir), incoming, tier)
    save_artists_raw(config_dir, merged)
    return added
