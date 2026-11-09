"""Shared plumbing for games played on the real Housebets site through the extension.

Each game is a subclass that knows its page (slug, selectors, how to read a result). This base
handles what every one of them needs:
  - getting the stream tab onto the right game page (navigate + wait), with stream mode on,
    which is also how game switching works: the next round of a new game loads its own page;
  - reading and clicking only inside the live, logged-in copy of the game (scope "game");
  - the $0 bet check before every bet (fail closed);
  - clicks that sometimes don't land (retry after re-reading, never a double action).
"""
import asyncio
import time
import logging
import re

from . import base
from .base import Game

log = logging.getLogger("housebets")

SITE = "https://www.housebets.com/games/housegames-"
POLL_S = 0.25
SETTLE_TIMEOUT_S = 15.0     # last-attempt patience: a network blip (VPN) can stall the site for seconds
PAGE_LOAD_S = 25.0


class SiteError(RuntimeError):
    """The page didn't do what we expected; the game goes offline until it's retried."""


def zero(text: str | None) -> bool:
    digits = re.sub(r"[^\d.]", "", text or "")
    return digits == "" or float(digits or 0) == 0.0


class HousebetsGame(Game):
    live = True               # moves show on the page the instant they're made: say the line first
    slug = ""                 # e.g. "dice" -> https://www.housebets.com/games/housegames-dice
    ready_selector = ""       # an element that exists once the game is usable
    bet_input = 'input[aria-label="Bet Amount"]'
    max_multiplier: float | None = None   # the most one round can pay with Hubie's settings (None: no real cap)

    def __init__(self, balance: float):
        super().__init__(balance)
        self.round = None
        self.broken: str | None = None

    # ---- availability ----------------------------------------------------------------------
    @property
    def bridge(self):
        return getattr(base, "SITE_BRIDGE", None)

    @property
    def url(self) -> str:
        return SITE + self.slug

    def available(self) -> bool:
        return not self.broken and bool(self.bridge and self.bridge.clients)

    # ---- page access -----------------------------------------------------------------------
    async def query(self, selector: str) -> list[dict]:
        r = await self.bridge.request({"type": "query", "scope": "game", "selector": selector}) if self.bridge else None
        if not r or not r.get("ok"):
            raise SiteError(f"page did not answer ({selector})")
        return r.get("data") or []

    async def click(self, selector: str, text: str | None = None, index: int = 0):
        # Right after a page load the game's controls exist a moment before they respond, so a
        # "not found" is retried briefly before it counts as a failure.
        msg = {"type": "click", "scope": "game", "selector": selector, "index": index}
        if text is not None:
            msg["text"] = text
        for attempt in range(8):
            r = await self.bridge.request(msg)
            if r and r.get("ok"):
                return
            await asyncio.sleep(0.5)
        raise SiteError(f"could not click {selector}" + (f" ({text})" if text else ""))

    async def set_value(self, selector: str, value):
        r = await self.bridge.request({"type": "set", "scope": "game", "selector": selector, "value": str(value)})
        if not r or not r.get("ok"):
            raise SiteError(f"could not set {selector}")

    async def ensure_page(self):
        """Make sure the stream tab shows this game, ready to play (navigating there if not)."""
        if not self.bridge:
            raise SiteError("no browser extension connected")
        if not await self._ready():
            log.info("loading %s", self.url)
            r = await self.bridge.request({"type": "navigate", "url": self.url}, timeout=PAGE_LOAD_S + 5)
            if not r or not r.get("ok"):
                raise SiteError(f"could not open {self.url}: {(r or {}).get('error')}")
            deadline = asyncio.get_running_loop().time() + PAGE_LOAD_S
            while not await self._ready():
                if asyncio.get_running_loop().time() > deadline:
                    raise SiteError(f"{self.slug} page never became ready")
                await asyncio.sleep(0.5)
            await asyncio.sleep(1.5)       # let the freshly loaded game finish wiring up its controls
        # Idempotent; a reload or a manual toggle must not leave the stream showing the whole site.
        await self.bridge.request({"type": "stream_mode", "on": True})

    async def _ready(self) -> bool:
        # Class names are shared between games (Limbo's targets and Mines' grid buttons are both
        # .hl-p-qt), so first check the page really hosts THIS game, by the game element's own slug.
        hosts = await self.bridge.request({"type": "query", "selector": "[data-housegames-direct][data-game-slug]"})
        slugs = {h["attrs"].get("data-game-slug") for h in (hosts or {}).get("data") or []}
        if f"housegames-{self.slug}" not in slugs:
            return False
        r = await self.bridge.request({"type": "query", "scope": "game", "selector": self.ready_selector})
        return bool(r and r.get("ok") and r.get("data"))

    async def assert_zero_bet(self):
        """Fail closed on money: the manual bet box must read zero right before a bet."""
        bet = await self.query(self.bet_input)
        if not bet or not zero(bet[0]["value"]):
            raise SiteError(f"bet amount is not zero ({bet[0]['value'] if bet else 'missing'}); refusing to bet")

    async def until(self, read, check, what: str, timeout: float = SETTLE_TIMEOUT_S):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            p = await read()
            if check(p):
                return p
            if asyncio.get_running_loop().time() > deadline:
                raise SiteError(f"timed out waiting for {what}")
            await asyncio.sleep(POLL_S)

    async def click_until(self, selector: str, read, check, what: str, first_wait: float = 2.5):
        """Click, wait for the page to show the result; if it doesn't, re-read and click again only
        if the first click really didn't land (a late click must not become a second action)."""
        for attempt in range(3):
            if attempt:
                p = await read()
                if check(p):
                    return p
                log.warning("%s didn't register; clicking again (attempt %d)", what, attempt + 1)
            await self.click(selector)
            try:
                return await self.until(read, check, what, timeout=first_wait if attempt < 2 else SETTLE_TIMEOUT_S)
            except SiteError:
                if attempt == 2:
                    raise

    async def act(self, action: dict) -> dict:
        kind = self._require(action)
        started = time.time()
        try:
            return await self.play(kind, action)
        except SiteError as e:
            err = e
        # Housebets reloads its own page now and then, killing whatever was in flight. If the page
        # reloaded during this move, wait for the game to come back and try the move once more
        # (an open round survives the reload and gets adopted) instead of going offline.
        if getattr(self.bridge, "last_load", 0) >= started - 1:
            log.warning("%s: the page reloaded mid-move; retrying it once", self.id)
            try:
                await asyncio.sleep(2)
                await self.ensure_page()
                return await self.play(kind, action)
            except SiteError as e:
                err = e
        self.broken = str(err)
        log.error("%s offline: %s", self.id, err)
        raise err

    async def play(self, kind: str, action: dict) -> dict:
        raise NotImplementedError

    async def bet_and_read(self, bet_selector: str, result_selector: str) -> dict:
        """Place one bet and return the new result element. Results already on the page are stamped
        first, so the new one is found by the missing stamp, never by comparing text (identical
        results in a row look like nothing happened)."""
        r = await self.bridge.request({"type": "mark", "scope": "game", "selector": result_selector})
        if not r or not r.get("ok"):
            raise SiteError("could not mark previous results")

        async def fresh():
            return [c for c in await self.query(result_selector) if "data-hubie-seen" not in c["attrs"]]

        # Back-to-back bets (auto sessions) must not click while the last one is still animating:
        # the click is ignored and looks like a miss. Wait for the bet button to be pressable first.
        async def bet_ready():
            return [b for b in await self.query(bet_selector) if not b.get("disabled")]
        try:
            await self.until(bet_ready, lambda b: bool(b), "the bet button", timeout=self.settle_s + 2)
        except SiteError:
            pass                                      # click anyway; click_until reports a real miss
        new = await self.click_until(bet_selector, fresh, lambda n: bool(n), "the result", first_wait=self.settle_s)
        return new[0]

    settle_s = 2.5          # how long a bet's result takes to show (longer for animated games)

    def bust_chance(self, action: dict) -> float:
        """Rough chance this move ends the round in a loss: decides which outcome gets voiced ahead."""
        return 0.5
