"""Hubie's Kick chat bot (official Kick API): posts commands, vote and record updates in chat, and
moderates (deletes links / injection attempts / slurs, times out repeat offenders).

Account: a separate Kick account (homiehubie) that is a moderator on the channel. One-time sign-in:
open http://localhost:8000/kick/login in a browser logged into THAT account. Kick redirects back to
/kick/callback; tokens are saved to showrunner/.kick_tokens.json (private, never committed) and
refreshed automatically. Client id/secret come from 1Password via run.sh (KICK_CLIENT_ID/SECRET).

API (docs.kick.com, checked 2026-11-07): OAuth 2.1 + PKCE S256 at id.kick.com; POST
api.kick.com/public/v1/chat {type:"user", broadcaster_user_id, content}; DELETE /public/v1/chat/{id};
POST /public/v1/moderation/bans {broadcaster_user_id, user_id, duration (minutes), reason}.
Messages are sent as type "user": type "bot" always posts to the bot account's OWN channel.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path

import httpx

log = logging.getLogger("kickbot")

AUTH_URL = "https://id.kick.com/oauth/authorize"
TOKEN_URL = "https://id.kick.com/oauth/token"
API = "https://api.kick.com/public/v1"
SCOPES = "user:read channel:read chat:write moderation:ban moderation:chat_message:manage"
MIN_GAP_S = 1.5                  # between our chat messages (Kick rate-limits; be a polite bot)


def plain(text: str) -> str:
    """Kick refuses messages with too many special characters: keep bot posts plain."""
    for a, b in (("·", ","), ("—", "-"), ("–", "-"), ("…", "..."), ("×", "x")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


class KickBot:
    def __init__(self, channel_slug: str, token_file: Path, redirect_uri: str):
        # The API wants the broadcaster's USER id, which is not the channel/chatroom id (fielding:
        # user 957, channel 949; using 949 posted into some other streamer's chat). It's looked up from
        # the channel name at startup, never hard-coded.
        self.channel_slug = channel_slug
        self.channel_id: int | None = None
        self.token_file = token_file
        self.redirect_uri = redirect_uri
        self.client_id = os.environ.get("KICK_CLIENT_ID", "")
        self.client_secret = os.environ.get("KICK_CLIENT_SECRET", "")
        self.tokens: dict = {}
        self._pending: dict[str, str] = {}          # oauth state -> PKCE verifier
        self._out: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._client = httpx.AsyncClient(timeout=10)
        if token_file.exists():
            try:
                self.tokens = json.loads(token_file.read_text())
            except ValueError:
                log.warning("unreadable %s; sign in again at /kick/login", token_file.name)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def ready(self) -> bool:
        return self.configured and bool(self.tokens.get("refresh_token"))

    # ---- sign-in ------------------------------------------------------------------------
    def login_url(self) -> str:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        self._pending = {state: verifier}           # only the latest login attempt is valid
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
            "state": state, "scope": SCOPES, "code_challenge": challenge, "code_challenge_method": "S256"})

    async def finish_login(self, code: str, state: str):
        verifier = self._pending.pop(state, None)
        if not verifier:
            raise ValueError("unknown or expired sign-in attempt; start again at /kick/login")
        await self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                           "code_verifier": verifier})
        log.info("kick bot signed in")

    async def _token(self, form: dict):
        r = await self._client.post(TOKEN_URL, data={**form, "client_id": self.client_id,
                                                     "client_secret": self.client_secret})
        if r.status_code != 200:
            raise RuntimeError(f"kick token request failed: HTTP {r.status_code} {r.text[:160]}")
        t = r.json()
        t["expires_at"] = time.time() + int(t.get("expires_in", 3600)) - 120
        if "refresh_token" not in t and self.tokens.get("refresh_token"):
            t["refresh_token"] = self.tokens["refresh_token"]
        self.tokens = t
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(t))
        os.chmod(tmp, 0o600)                        # tokens act as the bot account: owner-only
        os.replace(tmp, self.token_file)

    async def _auth(self) -> dict:
        if time.time() >= self.tokens.get("expires_at", 0):
            await self._token({"grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"]})
        return {"Authorization": f"Bearer {self.tokens['access_token']}"}

    async def resolve_broadcaster(self) -> int | None:
        if self.channel_id is None:
            r = await self._client.get(f"https://kick.com/api/v2/channels/{self.channel_slug}", headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/140.0 Safari/537.36", "Accept": "application/json"})
            uid = (r.json() or {}).get("user_id") if r.status_code == 200 else None
            if uid:
                self.channel_id = int(uid)
                log.info("kick bot posting to %s (broadcaster user id %s)", self.channel_slug, uid)
        return self.channel_id

    async def _call(self, method: str, path: str, body: dict | None = None):
        if not self.ready:
            return None
        if body is not None and "broadcaster_user_id" in body:
            if not await self.resolve_broadcaster():
                log.warning("kick bot: broadcaster id unknown; not sending")   # never guess a channel
                return None
            body = {**body, "broadcaster_user_id": self.channel_id}
        r = await self._client.request(method, API + path, json=body, headers=await self._auth())
        if r.status_code == 401:                     # revoked/expired early: refresh once and retry
            self.tokens["expires_at"] = 0
            r = await self._client.request(method, API + path, json=body, headers=await self._auth())
        if r.status_code >= 400:
            log.warning("kick %s %s -> HTTP %s %s", method, path, r.status_code, r.text[:160])
            return None
        return r.json() if r.content else {}

    # ---- actions ------------------------------------------------------------------------
    def say(self, text: str):
        """Queue a chat message (dropped, not blocking, if the queue is full)."""
        if self.ready and not self._out.full():
            self._out.put_nowait(plain(text)[:480])

    async def delete(self, message_id: str) -> bool:
        return bool(message_id) and await self._call("DELETE", f"/chat/{message_id}") is not None

    async def timeout(self, user_id: int, minutes: int, reason: str) -> bool:
        return await self._call("POST", "/moderation/bans", {
            "broadcaster_user_id": 0, "user_id": int(user_id),
            "duration": max(1, min(10080, minutes)), "reason": reason[:100]}) is not None

    async def run(self):
        """Send queued messages, spaced out."""
        while True:
            text = await self._out.get()
            try:
                sent = await self._call("POST", "/chat", {"type": "user", "broadcaster_user_id": 0,
                                                          "content": text})
                if sent is None and re.search(r"[^\w\s.,']", text):   # e.g. MAX_SPECIAL_CHARS_ERROR
                    await self._call("POST", "/chat", {"type": "user", "broadcaster_user_id": 0,
                                                       "content": re.sub(r"[^\w\s.,']", " ", text)})
            except Exception as e:
                log.warning("kick bot send failed: %s", type(e).__name__)
            await asyncio.sleep(MIN_GAP_S)
