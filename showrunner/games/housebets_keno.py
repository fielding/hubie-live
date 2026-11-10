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
       "secondary": "button.hl-btn-secondary", "chips": ".hl-g-chip",
       # Risk buttons, by analogy with Wheel/Plinko (.hp-p-riskbtn); plain buttons by text as a fallback.
       "risk": ("button.hl-p-riskbtn", "button")}
# Fielding (2026-11-10): not Classic. Higher risk = fewer paying hit counts, bigger top payouts.
RISKS = {"low": "Low", "medium": "Medium", "high": "High"}
COUNTS = (3, 5, 7, 10)
PLAY_COUNT = 10                 # degen keno: ten numbers, the biggest top payout
# He keeps "his numbers" for the whole session, like a real player. (A scheduled re-pick every 25
# draws broke live on 2026-11-08: the board ended up empty with Bet disabled, likely the clear
# animation finishing after the new picks.)


class HousebetsKeno(HousebetsGame):
    id = "keno"
    slug = "keno"
    title = "HOUSEBETS KENO"
    # Off the menu 2026-11-08 to 11-09 after a burst of site self-reloads broke its bets. Page-reload
    # recovery has since been added; a failure now saves a page snapshot for diagnosis.
    command = "!keno"
    max_multiplier = 1000       # 10 of 10 hits (approx.)
    settle_s = 6.0              # the result shows only after the animation finishes
    streak_talk = False         # what matters is how many of his numbers hit, not wins in a row
    big_hit_x = 10
    ready_selector = SEL["tiles"]
    brief = ("Housebets Keno (real site, zero-value bets): he plays ten numbers (of 40) and keeps them bet after bet; the "
             "draw marks hits and more hits pay more. Risk low/medium/high: higher risk pays nothing for a few hits "
             "but much more for many (he's a degen: mostly high, sometimes medium for a change). One draw per round. "
             "Talk hits and multipliers casually, never money.")

    def in_round(self) -> bool:
        return False

    def legal_actions(self) -> list[dict]:
        return [{"type": "draw", "count": [PLAY_COUNT], "risk": list(RISKS)}]

    def default_action(self) -> dict:
        return {"type": "draw", "count": PLAY_COUNT, "risk": "high"}

    async def _set_risk(self, risk: str):
        """Select the risk level. A setting that won't stick is logged (with a page snapshot), not fatal:
        Keno still plays, just at the risk the page already has."""
        label = RISKS[risk]
        for sel in SEL["risk"]:
            try:
                await self.click(sel, label)
            except SiteError:
                continue
            on = [b for b in await self.query(sel) if b.get("text", "").strip() == label and
                  (b["attrs"].get("aria-checked") == "true" or b["attrs"].get("aria-pressed") == "true" or
                   "active" in b.get("cls", "") or "selected" in b.get("cls", ""))]
            if on:
                self._risk = risk
                return
        if not getattr(type(self), "_risk_snapshot_taken", False):
            type(self)._risk_snapshot_taken = True
            await self.snapshot_page(f"keno risk {risk} didn't stick")
        log.warning("keno: couldn't confirm risk %s; playing on", risk)
        self._risk = risk                          # don't retry every draw

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
        risk = action.get("risk") if action.get("risk") in RISKS else "high"
        if getattr(self, "_risk", None) != risk:
            await self._set_risk(risk)
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
            await self.snapshot_page("keno bet failed")
            raise
        result = chip["text"]
        m = re.match(r"([\d,]+(?:\.\d+)?)", result)
        if not m:
            raise SiteError(f"unreadable result {result!r}")
        mult = float(m.group(1).replace(",", ""))
        hits = len([t for t in await self.query(SEL["tiles"]) if "hl-g-tile--hit" in t["cls"]])
        self.round = {"numbers": [i + 1 for i in picks], "hits": hits, "multiplier": mult, "risk": risk}
        return {"kind": "win" if mult > 1 else "loss", "picked": count, "hits": hits, "hits_of": f"{hits}/{count}", "risk": risk,
                "multiplier": mult, "bet": 0, "payout": 0}
