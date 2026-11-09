"""Housebets Coin Flip on the real site (extension/), $0 bets only: call heads or tails (1 coin,
1 flip, 1.98x). Result chip: "Result T, lost. View bet details". Learned 2026-11-07."""
import re

from .housebets import HousebetsGame, SiteError

SEL = {"bet": ".hl-p-betbtn", "side": ".hl-p-sidebtn--{side}", "chips": ".hl-g-chip"}
RESULT = re.compile(r"^Result ([HT]), (won|lost)")


class HousebetsCoinFlip(HousebetsGame):
    id = "coinflip"
    slug = "coin-flip"
    title = "COIN FLIP"
    command = "!flip"
    max_multiplier = 1.98
    ready_selector = ".hl-p-sidebtn--heads"
    brief = ("Housebets Coin Flip (real site, zero-value bets): call heads or tails; a win pays about "
             "double. Pure fifty-fifty, so have fun with superstition. Never talk money.")

    def in_round(self) -> bool:
        return False

    def legal_actions(self) -> list[dict]:
        return [{"type": "flip", "side": ["heads", "tails"]}]

    def win_chance(self, action: dict) -> float:
        return 0.495

    def default_action(self) -> dict:
        return {"type": "flip", "side": "heads"}

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "last": self.round}

    async def _chips(self) -> list[str]:
        return [c["attrs"].get("aria-label", "") for c in await self.query(SEL["chips"])]

    async def play(self, kind: str, action: dict) -> dict:
        side = action.get("side")
        if side not in ("heads", "tails"):
            raise SiteError("bad coin side")
        await self.ensure_page()
        b = await self.query(SEL["side"].format(side=side))
        if b and b[0]["attrs"].get("aria-pressed") != "true":
            await self.click(SEL["side"].format(side=side))
        await self.assert_zero_bet()
        chip = await self.bet_and_read(SEL["bet"], SEL["chips"])
        result = chip["attrs"].get("aria-label", "")
        m = RESULT.match(result)
        if not m:
            raise SiteError(f"unreadable result {result!r}")
        landed, won = ("heads" if m.group(1) == "H" else "tails"), m.group(2) == "won"
        self.round = {"call": side, "landed": landed, "won": won}
        return {"kind": "win" if won else "loss", "call": side, "landed": landed, "multiplier": 1.98,
                "bet": 0, "payout": 0}
