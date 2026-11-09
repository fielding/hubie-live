"""Hubie's Road on the real Housebets site (extension/), $0 bets only: Hubie surfs across up to 22
shark lanes to an island. Pick a difficulty, Bet, then Go lane by lane; cash out any time after
the first lane. Learned 2026-11-07:
  - main button `.hs-p-mainbtn`: "Bet" (no round) / "Go" (in a round; disabled while he moves)
  - `.hs-p-cashbtn` cash out; `.hs-g-foot-mult` current multiplier ("13.80x")
  - `.hs-g-hubie[data-pose]`: stand/jump/land/crouch/fall while moving, "celebrate" on a cash out
  - the game animates, so its window must not be covered (Chrome pauses covered windows)."""
import re

from .housebets import HousebetsGame, SiteError

SEL = {
    "main": ".hs-p-mainbtn",
    "cash": ".hs-p-cashbtn",
    "mult": ".hs-g-foot-mult",
    "hubie": ".hs-g-hubie",
    "difficulty": 'button.hs-p-qt[aria-label="{d}"]',
}
DIFFICULTIES = {"easy": "Easy", "medium": "Medium", "hard": "Hard", "daredevil": "Daredevil"}
LANES = 22


def _mult(text: str) -> float:
    m = re.match(r"([\d,]+(?:\.\d+)?)\s*(k?)", text or "")
    return float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1) if m else 1.0


class HubiesRoad(HousebetsGame):
    id = "road"
    slug = "hubie-s-road"
    title = "HUBIE'S ROAD"
    command = "!road"
    max_multiplier = None       # Daredevil deep runs: effectively unlimited
    ready_selector = SEL["main"]
    brief = ("Hubie's Road (real Housebets game starring YOU, zero-value bets): you surf across up to 22 "
             "shark lanes to an island. Each lane crossed raises the multiplier; a shark ends the round. "
             "Pick a difficulty (harder = sharks more likely, multipliers climb faster). Cash out any time "
             "after one lane. This is your own game, so ham it up. Degen, but not a goldfish: on Hard or "
             "Daredevil a 5-10x is a real moment, so bank one now and then and gloat about it; busting every "
             "single run is boring TV. Talk multipliers casually, never money.")

    # ---- the Game interface ---------------------------------------------------------------
    def in_round(self) -> bool:
        return bool(self.round) and self.round["status"] == "playing"

    def legal_actions(self) -> list[dict]:
        if not self.in_round():
            return [{"type": "start", "bet": [0], "difficulty": list(DIFFICULTIES)}]
        acts = [{"type": "go"}]
        if self.round["lanes"]:
            acts.append({"type": "cashout"})
        return acts

    def default_action(self) -> dict:
        if not self.in_round():
            return {"type": "start", "bet": 0, "difficulty": "medium"}
        return {"type": "cashout"} if self.round["lanes"] >= 3 else {"type": "go"}

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "round": self.round,
                "lanes_total": LANES}

    # Rough shark odds per lane (not published by the site; from play): decides what gets voiced ahead.
    BUST = {"easy": 0.05, "medium": 0.12, "hard": 0.2, "daredevil": 0.35}

    def bust_chance(self, action: dict) -> float:
        if action.get("type") != "go" or not self.round:
            return 0.0
        return self.BUST.get(self.round.get("difficulty"), 0.2)

    # ---- page --------------------------------------------------------------------------------
    async def _page(self) -> dict:
        main = await self.query(SEL["main"])
        if not main:
            raise SiteError("Hubie's Road: no main button")
        mult = await self.query(SEL["mult"])
        hubie = await self.query(SEL["hubie"])
        return {"label": main[0]["text"].strip(), "busy": main[0]["disabled"],
                "mult": _mult(mult[0]["text"]) if mult else 1.0,
                "pose": hubie[0]["attrs"].get("data-pose") if hubie else None}

    @staticmethod
    def _in_round(p) -> bool:
        return p["label"].startswith("Go")

    # ---- moves -------------------------------------------------------------------------------
    async def play(self, kind: str, action: dict) -> dict:
        if kind == "start":
            return await self._start(action)
        if kind == "go":
            return await self._go()
        return await self._cashout()

    async def _start(self, action: dict) -> dict:
        diff = action.get("difficulty")
        if diff not in DIFFICULTIES or action.get("bet", 0) != 0:
            raise SiteError("bad road start")
        await self.ensure_page()
        p = await self._page()
        if self._in_round(p):                 # a round left open (restart, reload): adopt it
            # The lane count isn't shown; the multiplier is what matters (and > 1 means he can cash out).
            self.round = {"status": "playing", "difficulty": diff, "lanes": 1 if p["mult"] > 1 else 0,
                          "multiplier": p["mult"]}
            return {"kind": "resume", "multiplier": p["mult"]}
        await self.click(SEL["difficulty"].format(d=DIFFICULTIES[diff]))
        await self.assert_zero_bet()
        await self.click_until(SEL["main"], self._page, lambda p: self._in_round(p) and not p["busy"], "the bet")
        self.round = {"status": "playing", "difficulty": diff, "lanes": 0, "multiplier": 1.0}
        return {"kind": "start", "bet": 0, "difficulty": diff}

    async def _go(self) -> dict:
        r = self.round
        before = (await self._page())["mult"]
        p = await self.click_until(
            SEL["main"], self._page,
            lambda p: (self._in_round(p) and not p["busy"] and p["mult"] != before) or not self._in_round(p),
            "the jump")
        if self._in_round(p):
            r["lanes"] += 1
            r["multiplier"] = p["mult"]
            return {"kind": "safe", "lane": r["lanes"], "multiplier": p["mult"]}
        if p["pose"] == "celebrate":          # made it to the island: the round pays out on its own
            r["lanes"] += 1
            r["status"], r["multiplier"] = "cashed", p["mult"]
            return {"kind": "cashout", "multiplier": p["mult"], "payout": 0, "bet": 0, "perfect": True}
        r["status"] = "busted"
        return {"kind": "bust", "lane": r["lanes"] + 1, "lost": 0, "revealed": r["lanes"]}

    async def _cashout(self) -> dict:
        r = self.round
        p = await self.click_until(SEL["cash"], self._page, lambda p: not self._in_round(p), "the cash out")
        r["status"], r["multiplier"] = "cashed", p["mult"]
        return {"kind": "cashout", "multiplier": p["mult"], "payout": 0, "bet": 0, "perfect": False}
