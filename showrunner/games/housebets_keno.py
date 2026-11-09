"""Housebets Keno on the real site (extension/), $0 bets only: pick 1-10 of 40 numbers, the
draw marks hits; payout depends on how many hit. Learned 2026-11-07:
  - tiles `button.hl-g-tile[data-idx=0..39]` (aria-label "Number 8"), classes --sel/--hit/--miss
  - "Clear table" / "Auto pick" are `button.hl-btn-secondary` by text; Bet `.hl-p-betbtn`
  - result chip `.hl-g-chip` text "0.00x" (newest first)."""
import asyncio
import logging
import random
import re

from .housebets import HousebetsGame, SiteError

log = logging.getLogger("housebets")

SEL = {"bet": ".hl-p-betbtn", "tiles": "button.hl-g-tile", "tile": 'button.hl-g-tile[data-idx="{i}"]',
       "secondary": "button.hl-btn-secondary", "chips": ".hl-g-chip"}
COUNTS = (3, 5, 7, 10)
PLAY_COUNT = 10                 # degen keno: ten numbers, the biggest top payout
# He keeps "his numbers" for the whole session, like a real player. (A scheduled re-pick every 25
# draws broke live on 2026-11-08: the board ended up empty with Bet disabled, likely the clear
# animation finishing after the new picks.)


class HousebetsKeno(HousebetsGame):
    id = "keno"
    slug = "keno"
    title = "HOUSEBETS KENO"
    # Off the menu 2026-11-08 16:31: on stream the Keno page reloaded itself several times in a few
    # seconds and bets failed; debug off-stream (see the "keno bet failed" log line), then restore "!keno".
    command = ""
    max_multiplier = 1000       # 10 of 10 hits (approx.)
    settle_s = 6.0              # the result shows only after the animation finishes
    streak_talk = False         # what matters is how many of his numbers hit, not wins in a row
    big_hit_x = 10
    ready_selector = SEL["tiles"]
    brief = ("Housebets Keno (real site, zero-value bets): he plays ten numbers (of 40) and keeps them bet after bet; the "
             "draw marks hits and more hits pay more. One draw per round. Talk hits and multipliers "
             "casually, never money.")

    def in_round(self) -> bool:
        return False

    def legal_actions(self) -> list[dict]:
        return [{"type": "draw", "count": [PLAY_COUNT]}]

    def default_action(self) -> dict:
        return {"type": "draw", "count": PLAY_COUNT}

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "last": self.round}

    async def _pick(self, count: int):
        """Clear the board if it holds anything, then pick `count` fresh numbers."""
        if await self._pressed():
            # Clearing animates; picking before it finishes gets wiped, so wait for an empty board.
            await self.click(SEL["secondary"], "Clear table")
            await self.until(self._picked, lambda n: n == 0, "the table to clear", timeout=5)
            await asyncio.sleep(1.5)              # the board ignores picks for a moment after clearing
        picks = sorted(random.SystemRandom().sample(range(40), count))
        for attempt in range(3):
            missing = [i for i in picks if i not in await self._pressed()]
            if not missing:
                return
            for i in missing:                     # only ever click unpicked tiles: a second click unpicks
                await self.click(SEL["tile"].format(i=i))
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.5)
        log.warning("keno: picks didn't all stick (%s of %s)", len(await self._pressed()), count)

    async def _pressed(self) -> list[int]:
        return [int(t["attrs"].get("data-idx", -1)) for t in await self.query(SEL["tiles"])
                if t["attrs"].get("aria-pressed") == "true"]

    async def _picked(self) -> int:
        return len(await self._pressed())

    async def _chips(self) -> list[str]:
        return [c["text"] for c in await self.query(SEL["chips"])]

    async def play(self, kind: str, action: dict) -> dict:
        count = action.get("count")
        if count not in COUNTS:
            raise SiteError("bad keno count")
        await self.ensure_page()
        # Pick only when the board doesn't hold `count` numbers (first draw, after a reload), and
        # check again right before betting: a Bet click on an empty board does nothing.
        pressed = await self._pressed()
        for attempt in range(3):
            if len(pressed) == count:
                break
            await self._pick(count)
            await asyncio.sleep(0.6)
            pressed = await self._pressed()
        if len(pressed) != count:
            raise SiteError(f"keno board won't hold {count} numbers (has {len(pressed)})")
        picks = sorted(pressed)
        await self.assert_zero_bet()
        try:
            chip = await self.bet_and_read(SEL["bet"], SEL["chips"])
        except SiteError:
            # Evidence for the next failure: was the board wiped (a page reload) or is Bet stuck?
            bet = [b for b in await self.query(SEL["bet"]) if b.get("text", "").strip() == "Bet"]
            log.warning("keno bet failed: %d numbers picked (wanted %d), Bet %s, page %s",
                        len(await self._pressed()), count,
                        "disabled" if bet and bet[0].get("disabled") else "enabled" if bet else "missing",
                        (await self.bridge.request({"type": "status"}) or {}).get("result") and "up" or "?")
            raise
        result = chip["text"]
        m = re.match(r"([\d,]+(?:\.\d+)?)", result)
        if not m:
            raise SiteError(f"unreadable result {result!r}")
        mult = float(m.group(1).replace(",", ""))
        hits = len([t for t in await self.query(SEL["tiles"]) if "hl-g-tile--hit" in t["cls"]])
        self.round = {"numbers": [i + 1 for i in picks], "hits": hits, "multiplier": mult}
        return {"kind": "win" if mult > 1 else "loss", "picked": count, "hits": hits, "hits_of": f"{hits}/{count}",
                "multiplier": mult, "bet": 0, "payout": 0}
