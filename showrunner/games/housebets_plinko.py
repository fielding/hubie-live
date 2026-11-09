"""Housebets Plinko on the real site (extension/), $0 bets only: pick a risk level and drop a
ball; it lands in a multiplier bucket. Result chip `.hp-g-chip` text "0.48×" (newest first).
Learned 2026-11-07."""
import asyncio
import logging
import re

from .housebets import HousebetsGame, SiteError

log = logging.getLogger("housebets")

SEL = {"bet": ".hp-p-betbtn", "risk": "button.hp-p-riskbtn", "chips": ".hp-g-chip", "rows": "input.hp-p-range"}
RISKS = {"low": "Low", "medium": "Medium", "high": "High"}
# Fielding's call (2026-11-08): full degen, high risk only, balls dropped in bursts.
PLAY_RISK = "high"
ROWS = 16                       # as many rows as the page allows: bigger edge buckets (slider max is read)
BURST = 5                       # balls per drop; the page lists only the last 5 results, so no more


class HousebetsPlinko(HousebetsGame):
    id = "plinko"
    slug = "plinko"
    title = "HOUSEBETS PLINKO"
    command = "!plinko"
    streak_talk = False         # bursts in a row mean nothing here; big balls do
    big_hit_x = 10              # a ball this big gets a comment
    clip_x = 1000               # and this big gets auto-clipped (500x is common enough on Plinko)
    max_multiplier = 1000       # high risk at max rows, edge bucket (approx.)
    settle_s = 6.0              # the result shows only after the animation finishes
    ready_selector = SEL["risk"]
    brief = ("Housebets Plinko (real site, zero-value bets): high risk, five balls dropped at once through "
             "the pegs at max rows; each lands in a multiplier bucket (the middle pays almost nothing, the "
             "edges pay hundreds of x). Talk about the best ball of the burst. Talk multipliers casually, never money.")

    def in_round(self) -> bool:
        return False

    def legal_actions(self) -> list[dict]:
        return [{"type": "drop", "risk": [PLAY_RISK]}]

    def default_action(self) -> dict:
        return {"type": "drop", "risk": PLAY_RISK}

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "last": self.round}

    async def _chips(self) -> list[str]:
        return [c["text"] for c in await self.query(SEL["chips"])]

    async def play(self, kind: str, action: dict) -> dict:
        risk = action.get("risk")
        if risk not in RISKS:
            raise SiteError("bad plinko risk")
        await self.ensure_page()
        await self.click(SEL["risk"], RISKS[risk])
        on = [b for b in await self.query(SEL["risk"]) if b["attrs"].get("aria-checked") == "true"]
        if not on or on[0]["text"] != RISKS[risk]:
            raise SiteError(f"risk {risk} did not stick")
        await self._max_rows()
        await self.assert_zero_bet()
        landed = await self._burst()
        best, back = max(landed), sum(landed) / len(landed)
        self.round = {"risk": risk, "landed": landed}
        # A burst "wins" only if it paid back more than it cost (average over the balls): with five
        # balls there's nearly always one above 1x, which is no win at all.
        return {"kind": "win" if back > 1 else "loss", "balls": landed, "best_ball": best,
                "burst_return_x": round(back, 2), "risk": risk, "multiplier": best, "bet": 0, "payout": 0}

    async def _max_rows(self):
        """Push the rows slider to its top (more rows = bigger edge multipliers). Best effort: if the
        page doesn't take it, play on at whatever it shows."""
        got = await self.query(SEL["rows"])
        if not got:
            return
        top = int(float(got[0]["attrs"].get("max") or ROWS))
        if int(float(got[0]["value"] or 0)) == top:
            return
        await self.set_value(SEL["rows"], top)
        got = await self.query(SEL["rows"])
        if not got or int(float(got[0]["value"] or 0)) != top:
            log.warning("plinko: rows slider didn't move to %s (shows %s)", top, got[0]["value"] if got else "?")

    async def _burst(self) -> list[float]:
        """Drop BURST balls back to back, wait until they've all landed, read the newest results.
        Plinko redraws its whole results strip on every ball (any marks we put on the chips are
        wiped), so "landed" means: the strip changed and then stayed the same for a moment."""
        async def strip():
            return [c["text"] for c in await self.query(SEL["chips"])]
        # Stamp the current chips: the page redraws the strip when balls land, so unstamped chips
        # mean new results even when they read exactly like the old ones (five 0.2x in a row).
        await self.bridge.request({"type": "mark", "scope": "game", "selector": SEL["chips"]})

        async def redrawn():
            return any("data-hubie-seen" not in c["attrs"] for c in await self.query(SEL["chips"]))
        before = await strip()
        dropped = 0
        for _ in range(BURST):
            if not [b for b in await self.query(SEL["bet"]) if not b.get("disabled")]:
                break                       # the page won't take another ball right now
            await self.click(SEL["bet"])    # index 0 is "Bet" (the other match is "Start Autobet")
            dropped += 1
            await asyncio.sleep(0.25)
        if not dropped:
            raise SiteError("plinko wouldn't take a ball")
        await asyncio.sleep(2.0)            # balls take a couple of seconds to fall
        loop = asyncio.get_running_loop()
        deadline, last, steady_since = loop.time() + self.settle_s + dropped, None, 0.0
        while loop.time() < deadline:
            now = await strip()
            if now != last:
                last, steady_since = now, loop.time()
            elif now != before and loop.time() - steady_since >= 1.0:
                break
            await asyncio.sleep(0.25)
        if not last or (last == before and not await redrawn()):
            raise SiteError("plinko results never showed")
        out = []
        for text in last[:dropped]:
            m = re.match(r"([\d,]+(?:\.\d+)?)", text)
            if m:
                out.append(float(m.group(1).replace(",", "")))
        if not out:
            raise SiteError("unreadable plinko results")
        return out
