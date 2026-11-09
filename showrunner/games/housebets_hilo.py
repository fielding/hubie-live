"""Housebets HiLo on the real site (extension/), $0 bets only: a card is showing; guess whether the
next is higher-or-same or lower-or-same; each right guess multiplies the payout, a wrong one ends
the round; cash out any time after a win. Learned 2026-11-07:
  - main button `.hh-p-mainbtn`: "Bet" (no round) / "Cash out ..." (`--cash`) in a round
  - guesses `button.hh-g-tile`, aria-label "Higher / same — 3.22x, 30.77% chance" (or "Lower / same …")
  - current card text in `.hh-g-card`."""
import re

from .housebets import HousebetsGame, SiteError

# Buttons are fixed by side (data-side hi/lo); their labels change at the edges: a King offers
# "Same" (hi) and "Lower" (lo), an Ace "Higher" and "Same".
SIDE = {"higher": "hi", "lower": "lo"}
SEL = {"main": ".hh-p-mainbtn", "guess": 'button.hh-g-tile[data-side="{side}"]',
       "tiles": "button.hh-g-tile", "card": ".hh-g-card", "trail": ".hh-g-trail-row > *"}
ODDS = re.compile(r"^(.+?) — ([\d.]+)x, ([\d.]+)% chance")


class HousebetsHiLo(HousebetsGame):
    id = "hilo"
    slug = "hilo"
    title = "HOUSEBETS HILO"
    command = "!hilo"
    max_multiplier = None       # a run multiplies without a cap
    ready_selector = SEL["main"]
    brief = ("Housebets HiLo (real site, zero-value bets): a card is showing; guess if the next card is "
             "higher-or-same or lower-or-same (on a King the high side is 'Same', on an Ace the low side). The "
             "state shows each side's label, multiplier and chance. Each "
             "right guess multiplies the run; a wrong one ends it. Cash out any time after a win. Talk "
             "cards and multipliers casually, never money.")

    def in_round(self) -> bool:
        return bool(self.round) and self.round["status"] == "playing"

    def legal_actions(self) -> list[dict]:
        if not self.in_round():
            return [{"type": "start", "bet": [0]}]
        acts = [{"type": "guess", "side": ["higher", "lower"]}]
        if self.round["wins"]:
            acts.append({"type": "cashout"})
        return acts

    def step_action(self) -> dict:
        """Fast play: keep going, on the likelier side."""
        odds = (self.round or {}).get("odds") or {}
        return {"type": "guess", "side": max(odds, key=lambda s: odds[s]["chance"]) if odds else "higher"}

    def default_action(self) -> dict:
        if not self.in_round():
            return {"type": "start", "bet": 0}
        if self.round["wins"] >= 2:
            return {"type": "cashout"}
        odds = self.round.get("odds") or {}
        side = max(odds, key=lambda s: odds[s]["chance"]) if odds else "higher"
        return {"type": "guess", "side": side}

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "round": self.round}

    def bust_chance(self, action: dict) -> float:
        if action.get("type") != "guess" or not self.round:
            return 0.0
        pick = (self.round.get("odds") or {}).get(action.get("side"))
        return 1 - pick["chance"] / 100 if pick else 0.5

    async def _page(self) -> dict:
        main = await self.query(SEL["main"])
        if not main:
            raise SiteError("HiLo: no main button")
        odds = {}
        for t in await self.query(SEL["tiles"]):
            m = ODDS.match(t["attrs"].get("aria-label", ""))
            side = {"hi": "higher", "lo": "lower"}.get(t["attrs"].get("data-side"))
            if m and side and not t["disabled"]:
                odds[side] = {"label": m.group(1), "multiplier": float(m.group(2)), "chance": float(m.group(3))}
        card = await self.query(SEL["card"])
        # Cards drawn this round: the progress signal (a same-rank card looks unchanged otherwise).
        drawn = len(await self.query(SEL["trail"]))
        return {"in_round": "hh-p-mainbtn--cash" in main[0]["cls"], "busy": main[0]["disabled"],
                "card": card[0]["text"].strip() if card else "?", "odds": odds, "drawn": drawn}

    async def _total(self) -> float | None:
        """The run's current total multiplier as the page shows it ("13,188.31x" / "1.2kx"), if found."""
        for el in await self.query('[class*="foot-mult"], [class*="total-mult"], [class*="cur-mult"]'):
            m = re.search(r"([\d,]+(?:\.\d+)?)\s*(k?)\s*[x×]", el.get("text") or "")
            if m:
                return float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
        return None

    async def play(self, kind: str, action: dict) -> dict:
        if kind == "start":
            await self.ensure_page()
            p = await self._page()
            if not p["in_round"]:
                await self.assert_zero_bet()
                p = await self.click_until(SEL["main"], self._page, lambda p: p["in_round"] and p["odds"], "the bet")
            self.round = {"status": "playing", "wins": 0, "multiplier": 1.0, "card": p["card"], "odds": p["odds"]}
            return {"kind": "start", "bet": 0, "card": p["card"]}
        r = self.round
        if kind == "guess":
            side = action.get("side")
            pick = r["odds"].get(side)
            if not pick:
                raise SiteError(f"HiLo: {side} not available on {r['card']}")
            before, drawn = r["card"], (await self._page())["drawn"]
            p = await self.click_until(SEL["guess"].format(side=SIDE[side]), self._page,
                                       lambda p: (p["in_round"] and p["drawn"] > drawn and p["odds"]) or not p["in_round"],
                                       "the next card")
            if p["in_round"]:
                r["wins"] += 1
                # The page's own running total, when it shows one: multiplying the per-guess payouts
                # (shown rounded to 2 decimals) compounds the rounding and drifted ~3% low on a long
                # run (12,814x recorded for a 13,188x cash-out, 2026-11-08).
                r["multiplier"] = await self._total() or round(r["multiplier"] * pick["multiplier"], 2)
                r["card"], r["odds"] = p["card"], p["odds"]
                return {"kind": "safe", "side": side, "card": p["card"], "from": before, "multiplier": r["multiplier"]}
            r["status"] = "busted"
            return {"kind": "bust", "side": side, "from": before, "card": p["card"], "lost": 0, "revealed": r["wins"]}
        r["multiplier"] = await self._total() or r["multiplier"]   # what the page pays, read before it resets
        await self.click_until(SEL["main"], self._page, lambda p: not p["in_round"], "the cash out")
        r["status"] = "cashed"
        return {"kind": "cashout", "multiplier": r["multiplier"], "payout": 0, "bet": 0, "perfect": False}
