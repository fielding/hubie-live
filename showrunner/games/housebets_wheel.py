"""Housebets Wheel on the real site (extension/), $0 bets only: pick a risk level and spin; the
wheel lands on a multiplier segment (0x loses). Result chip `.hp-g-chip` text "1.50×" (newest
first). Learned 2026-11-07."""
import re

from .housebets import HousebetsGame, SiteError

SEL = {"bet": ".hp-p-betbtn", "risk": "button.hp-p-riskbtn", "chips": ".hp-g-chip"}
RISKS = {"low": "Low", "medium": "Medium", "high": "High"}


class HousebetsWheel(HousebetsGame):
    id = "wheel"
    slug = "wheel"
    title = "HOUSEBETS WHEEL"
    command = "!wheel"
    max_multiplier = 50         # high-risk top segment (approx.)
    settle_s = 6.0              # the result shows only after the animation finishes
    ready_selector = SEL["risk"]
    brief = ("Housebets Wheel (real site, zero-value bets): pick a risk level (higher risk = more blank "
             "segments, bigger top multipliers) and spin. Lands on a multiplier, or 0x for nothing. "
             "Talk multipliers casually, never money.")

    def in_round(self) -> bool:
        return False

    def legal_actions(self) -> list[dict]:
        return [{"type": "spin", "risk": list(RISKS)}]

    def default_action(self) -> dict:
        return {"type": "spin", "risk": "medium"}

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "last": self.round}

    async def _chips(self) -> list[str]:
        return [c["text"] for c in await self.query(SEL["chips"])]

    async def play(self, kind: str, action: dict) -> dict:
        risk = action.get("risk")
        if risk not in RISKS:
            raise SiteError("bad wheel risk")
        await self.ensure_page()
        await self.click(SEL["risk"], RISKS[risk])
        on = [b for b in await self.query(SEL["risk"]) if b["attrs"].get("aria-checked") == "true"]
        if not on or on[0]["text"] != RISKS[risk]:
            raise SiteError(f"risk {risk} did not stick")
        await self.assert_zero_bet()
        chip = await self.bet_and_read(SEL["bet"], SEL["chips"])
        result = chip["text"]
        m = re.match(r"([\d,]+(?:\.\d+)?)", result)
        if not m:
            raise SiteError(f"unreadable result {result!r}")
        mult = float(m.group(1).replace(",", ""))
        self.round = {"risk": risk, "landed": mult}
        return {"kind": "win" if mult > 1 else "loss", "landed": mult, "risk": risk,
                "multiplier": mult, "bet": 0, "payout": 0}
