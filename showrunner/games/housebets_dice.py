"""Housebets Dice on the real site (extension/), $0 bets only. Selectors learned from page
snapshots on 2026-11-07: the result appears as a history chip "Win|Loss, rolled 72.10"."""
import asyncio
import re

from .housebets import HousebetsGame, SiteError

SEL = {
    "bet": ".hd-p-betbtn",                       # first match is the Manual tab's Bet
    # The inputs lost their ids (site update, seen 2026-11-08); they're named by their labels now.
    "chance": 'input[aria-labelledby="hd-g-lbl-chance"]',
    "mult": 'input[aria-labelledby="hd-g-lbl-mult"]',
    "mode": 'button[data-mode="{side}"]',
    "chips": ".hd-g-chip",
}
# Win chance %. Fielding's call (2026-11-08): dice is played at 6x (99/6 = 16.5%), which makes
# streaks rare enough to be exciting under the auto-session ladder.
CHANCES = (16.5,)
RESULT = re.compile(r"^(Win|Loss), rolled (\d+(?:\.\d+)?)")


class HousebetsDice(HousebetsGame):
    id = "dice"
    slug = "dice"
    title = "HOUSEBETS DICE"
    # Off the menu 2026-11-08 20:30: after a fresh page load the game stopped taking the typed win
    # chance (payout never left 2x, even with retries). Debug off-stream, then restore "!dice".
    command = ""
    max_multiplier = 9.9        # his lowest win chance is 10% -> 9.9x
    ready_selector = SEL["chance"]
    brief = ("Housebets Dice (real site, zero-value bets): pick a win chance and roll over or under. Lower "
             "chance = bigger multiplier (about 99 divided by the chance). One roll per round, so play it "
             "as a STREAK hunt: the state shows your current win streak and your best this session. Call "
             "out the streak ('that's three in a row, can we make it four?'), push the odds when you're "
             "hot, chase a long shot to cap a streak, and make a moment of it when a streak breaks. Vary "
             "rolls and sides; never the same safe roll twice in a row. Talk multipliers casually, never money.")

    def in_round(self) -> bool:
        return False                             # every roll is its own round

    def legal_actions(self) -> list[dict]:
        return [{"type": "roll", "chance": list(CHANCES), "side": ["over", "under"]}]

    def win_chance(self, action: dict) -> float:
        return float(action.get("chance") or 49.5) / 100

    def default_action(self) -> dict:
        return {"type": "roll", "chance": 16.5, "side": "over"}

    def __init__(self, balance: float):
        super().__init__(balance)
        self.streak = 0                          # consecutive wins (resets on a loss)
        self.best_streak = 0

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "last": self.round,
                "win_streak": self.streak, "best_streak_this_session": self.best_streak}

    async def _chips(self) -> list[str]:
        return [c["attrs"].get("aria-label", "") for c in await self.query(SEL["chips"])]

    async def play(self, kind: str, action: dict) -> dict:
        chance, side = action.get("chance"), action.get("side")
        if chance not in CHANCES or side not in ("over", "under"):
            raise SiteError("bad dice action")       # not reachable via legal_actions
        await self.ensure_page()
        mode = await self.query(SEL["mode"].format(side=side))
        if mode and mode[0]["attrs"].get("aria-pressed") != "true":
            await self.click(SEL["mode"].format(side=side))
        # Check what the GAME took, not just the box: its payout readout must match the chance
        # (a value typed into the box but never committed left it rolling 50:50).
        want = round(99 / chance, 2)

        async def payout():
            got = await self.query(SEL["mult"])
            try:
                return float(got[0]["value"]) if got else 0.0
            except (TypeError, ValueError):
                return 0.0
        # Right after the page loads, the game ignores the first input or two (its controls aren't
        # wired yet): set it again until the payout shows it took.
        for attempt in range(4):
            if abs(await payout() - want) <= 0.02:
                break
            await self.set_value(SEL["chance"], chance)
            try:
                await self.until(payout, lambda m: abs(m - want) <= 0.02, f"the {want:g}x payout", timeout=2)
                break
            except SiteError:
                if attempt == 3:
                    raise
                await asyncio.sleep(1.0)
        await self.assert_zero_bet()
        chip = await self.bet_and_read(SEL["bet"], SEL["chips"])
        result = chip["attrs"].get("aria-label", "")
        m = RESULT.match(result)
        if not m:
            raise SiteError(f"unreadable result {result!r}")
        won, roll = m.group(1) == "Win", float(m.group(2))
        mult = round(99 / chance, 2)
        broken = 0 if won else self.streak
        self.streak = self.streak + 1 if won else 0
        self.best_streak = max(self.best_streak, self.streak)
        self.round = {"chance": chance, "side": side, "roll": roll, "won": won, "multiplier": mult}
        return {"kind": "win" if won else "loss", "roll": roll, "chance": chance, "side": side,
                "multiplier": mult, "win_streak": self.streak, "streak_broken_at": broken or None,
                "bet": 0, "payout": 0}
