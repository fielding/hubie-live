"""Housebets Limbo on the real site (extension/), $0 bets only. Pick a target multiplier; the
round's crash point either reaches it (win) or not. Result chip: "Result 1.97x, lost. View bet
details". Selectors learned 2026-11-07."""
import logging
import re

from .housebets import HousebetsGame, SiteError

log = logging.getLogger("housebets")

SEL = {
    "bet": ".hl-p-betbtn",                   # first match is the Manual tab's Bet
    "quick": "button.hl-p-qt",                # target presets, text "1.5x" "2x" "5x" "10x" "100x"
    "chips": ".hl-g-chip",
}
TARGETS = {1.5: "1.5x", 2: "2x", 5: "5x", 10: "10x", 100: "100x"}   # the page's preset buttons
# Fielding's call (2026-11-08): limbo is played at 7x, which has no preset, so it's typed into the
# target box (found by its label; the preset 5x is the fallback if the page changes).
PLAY_TARGET = 7
RESULT = re.compile(r"^Result ([\d,]+(?:\.\d+)?)x, (won|lost)")   # "Result 13,803x, won"


class HousebetsLimbo(HousebetsGame):
    id = "limbo"
    slug = "limbo"
    title = "HOUSEBETS LIMBO"
    command = "!limbo"
    max_multiplier = 100       # his highest target
    ready_selector = SEL["quick"]
    brief = ("Housebets Limbo (real site, zero-value bets): pick a target multiplier; a random crash "
             "point either reaches it (win at that multiplier) or doesn't. Higher target = rarer win. "
             "One shot per round. Talk multipliers casually, never money.")

    def in_round(self) -> bool:
        return False

    def legal_actions(self) -> list[dict]:
        return [{"type": "launch", "target": [PLAY_TARGET]}]

    def win_chance(self, action: dict) -> float:
        return 0.99 / float(action.get("target") or 2)

    def default_action(self) -> dict:
        return {"type": "launch", "target": PLAY_TARGET}

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "last": self.round}

    async def _type_target(self, target: float) -> bool:
        """Type a custom target into the target box (any input labelled target/multiplier/payout,
        never the bet amount). True once the box reads back the value."""
        for inp in await self.query("input"):
            label = " ".join(str(inp["attrs"].get(k, "")) for k in
                             ("aria-label", "aria-labelledby", "placeholder", "name", "id")).lower()
            if "bet" in label or not re.search(r"target|multiplier|payout|cash", label):
                continue
            key = next((k for k in ("id", "aria-labelledby", "aria-label", "name") if inp["attrs"].get(k)), None)
            if not key:
                continue
            sel = f'#{inp["attrs"]["id"]}' if key == "id" else f'input[{key}="{inp["attrs"][key]}"]'
            await self.set_value(sel, target)
            got = await self.query(sel)
            try:
                if got and abs(float(str(got[0]["value"]).replace("x", "")) - target) < 0.01:
                    return True
            except ValueError:
                pass
        return False

    async def _chips(self) -> list[str]:
        return [c["attrs"].get("aria-label", "") for c in await self.query(SEL["chips"])]

    async def play(self, kind: str, action: dict) -> dict:
        target = action.get("target")
        if target not in TARGETS and target != PLAY_TARGET:
            raise SiteError("bad limbo target")
        await self.ensure_page()
        if target in TARGETS:
            await self.click(SEL["quick"], TARGETS[target])
            picked = [q for q in await self.query(SEL["quick"]) if q["attrs"].get("aria-pressed") == "true"]
            if not picked or picked[0]["text"] != TARGETS[target]:
                raise SiteError("target did not stick")
        elif not await self._type_target(target):
            log.warning("limbo: no target box found; playing the 5x preset instead of %sx", target)
            target = 5
            await self.click(SEL["quick"], TARGETS[target])
        await self.assert_zero_bet()
        chip = await self.bet_and_read(SEL["bet"], SEL["chips"])
        result = chip["attrs"].get("aria-label", "")
        m = RESULT.match(result)
        if not m:
            raise SiteError(f"unreadable result {result!r}")
        crash, won = float(m.group(1).replace(",", "")), m.group(2) == "won"
        # The result must fit the target we think we set (Dice once showed 6x while rolling 50:50).
        if won != (crash >= float(target)):
            await self.snapshot_page(f"limbo result doesn't fit a {target}x target: {result!r}")
            raise SiteError(f"limbo isn't playing a {target}x target (got {result!r})")
        self.round = {"target": target, "result": crash, "won": won}
        return {"kind": "win" if won else "loss", "result": crash, "target": target,
                "multiplier": target, "bet": 0, "payout": 0}
