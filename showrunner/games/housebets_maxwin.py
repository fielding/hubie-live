"""Housebets Max Win on the real site (extension/), $0 bets only: pick a mode (Standard 1,000x at
~1 in 1,000, Silver 100x ~1 in 100, Gold 10x ~1 in 10, Diamond 5x ~1 in 5) and bet: it either hits
the mode's multiplier or nothing. Learned 2026-11-07:
  - mode dropdown `.hl-p-selbtn[aria-label="Mode"]`; options `.hl-p-buyopt` in that order
  - result chip `.hl-g-chip` aria-label "Result 10×, won. View bet details" (newest first)."""
import re

from .housebets import HousebetsGame, SiteError

SEL = {"bet": ".hl-p-betbtn", "mode": '.hl-p-selbtn[aria-label="Mode"]', "option": ".hl-p-buyopt",
       "chips": ".hl-g-chip"}
MODES = {"standard": (0, 1000), "silver": (1, 100), "gold": (2, 10), "diamond": (3, 5)}
RESULT = re.compile(r"^Result (.+?), (won|lost)")   # a loss reads "Result NOPE, lost."


class HousebetsMaxWin(HousebetsGame):
    id = "maxwin"
    slug = "max-win"
    title = "MAX WIN"
    command = "!maxwin"
    max_multiplier = 1000       # Standard mode, the most this game pays
    grind = True          # spin back to back until it hits (the runner's grind mode)
    ready_selector = SEL["mode"]
    settle_s = 6.0              # a hit plays a celebration before its result shows
    brief = ("Housebets Max Win (real site, zero-value bets): pick a mode and it's all or nothing: "
             "Standard hits 1,000x about 1 in 1,000, Silver 100x 1 in 100, Gold 10x 1 in 10, Diamond 5x "
             "1 in 5. You GRIND it: pick a mode once and spin until it hits; every so often you're told how "
             "many spins it's been, so build the tension (\'spin forty, it's coming\'). It's an all-or-nothing draw, like a lottery ticket: nothing to "
             "climb, no mountain, no levels. Call it Max Win. Pure lottery energy. Talk multipliers "
             "casually, never money.")

    def in_round(self) -> bool:
        return False

    def legal_actions(self) -> list[dict]:
        return [{"type": "bet", "mode": list(MODES)}]

    def default_action(self) -> dict:
        return {"type": "bet", "mode": "gold"}

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "last": self.round}

    async def _chips(self) -> list[str]:
        return [c["attrs"].get("aria-label", "") for c in await self.query(SEL["chips"])]

    async def play(self, kind: str, action: dict) -> dict:
        mode = action.get("mode")
        if mode not in MODES:
            raise SiteError("bad max win mode")
        index, target = MODES[mode]
        await self.ensure_page()
        current = await self.query(SEL["mode"])
        if not current or not current[0]["text"].lower().startswith(mode):
            await self.click(SEL["mode"])                       # open the dropdown
            await self.click(SEL["option"], index=index)
            current = await self.query(SEL["mode"])
            if not current or not current[0]["text"].lower().startswith(mode):
                raise SiteError(f"mode {mode} did not stick")
        await self.assert_zero_bet()
        chip = await self.bet_and_read(SEL["bet"], SEL["chips"])
        result = chip["attrs"].get("aria-label", "")
        m = RESULT.match(result)
        if not m:
            raise SiteError(f"unreadable result {result!r}")
        won = m.group(2) == "won"
        self.round = {"mode": mode, "won": won}
        return {"kind": "win" if won else "loss", "mode": mode, "multiplier": target, "bet": 0, "payout": 0}
