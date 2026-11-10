"""Housebets Dice on the real site (extension/), $0 bets only. Selectors learned from page
snapshots on 2026-11-07: the result appears as a history chip "Win|Loss, rolled 72.10"."""
import asyncio
import logging
import re

from .housebets import HousebetsGame, SiteError

log = logging.getLogger("housebets")

SEL = {
    "bet": ".hd-p-betbtn",                       # first match is the Manual tab's Bet
    # The inputs lost their ids (site update, seen 2026-11-08); they're named by their labels now.
    "chance": 'input[aria-labelledby="hd-g-lbl-chance"]',
    "mult": 'input[aria-labelledby="hd-g-lbl-mult"]',
    "mode": 'button[data-mode="{side}"]',
    "chips": ".hd-g-chip",
    "target": 'input[aria-labelledby="hd-g-lbl-target"]',
    # The game's own state: the box values show whatever was typed even when the game ignores it,
    # but the slider handle's aria-valuenow is the roll target it will actually play.
    "thumb": 'div[role="slider"][aria-label="Roll target"]',
}
# Win chance %. Fielding's call (2026-11-08): dice is played at 6x (99/6 = 16.5%), which makes
# streaks rare enough to be exciting under the auto-session ladder.
CHANCES = (16.5,)
RESULT = re.compile(r"^(Win|Loss), rolled (\d+(?:\.\d+)?)")


class HousebetsDice(HousebetsGame):
    id = "dice"
    slug = "dice"
    title = "HOUSEBETS DICE"
    # 2026-11-08: after a site update the game stopped taking the typed win chance (payout stuck at
    # 2x). It can't be debugged off-stream (Cloudflare blocks fresh browsers), so Dice tries several
    # ways in, keeps the one the payout readout confirms, and snapshots the page if none works.
    command = "!dice"
    _method: str | None = None    # the input method that worked last (class-wide: survives new instances)
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

    async def _enter_chance(self, chance: float, side: str, want: float, payout):
        """Get the game to take the win chance, trying each way in until its payout readout agrees."""
        target = {"over": 100 - chance, "under": chance}[side]
        methods = {
            "target box": lambda: self.set_value(SEL["target"], f"{target:g}"),
            "target box, typed": lambda: self.set_value(SEL["target"], f"{target:g}", typed=True),
            "slider keys": lambda: self._step_slider(target),
            "chance box": lambda: self.set_value(SEL["chance"], chance),
            "payout box": lambda: self.set_value(SEL["mult"], f"{want:g}"),
            "chance box, typed": lambda: self.set_value(SEL["chance"], chance, typed=True),
        }
        order = ([type(self)._method] if type(self)._method in methods else []) + \
            [m for m in methods if m != type(self)._method]
        # Right after a page load the game ignores the first input or two: a second pass retries all.
        for rnd in range(2):
            for name in order:
                try:
                    await methods[name]()
                    await self.until(payout, lambda m: abs(m - want) <= 0.02, f"the {want:g}x payout", timeout=2)
                except SiteError:
                    continue
                if type(self)._method != name:
                    log.info("dice: the win chance took via the %s", name)
                type(self)._method = name
                return
            await asyncio.sleep(1.0)
        await self.snapshot_page(f"dice ignored every way of setting a {chance}% win chance (payout {await payout()})")
        raise SiteError(f"dice won't take a {chance}% win chance")

    async def _step_slider(self, target: float):
        """Walk the slider handle to the target with arrow keys (big steps first), reading its value."""
        async def now():
            got = await self.query(SEL["thumb"])
            return float(got[0]["attrs"].get("aria-valuenow"))
        for _ in range(60):
            gap = target - await now()
            if abs(gap) < 0.005:
                return
            key = "ArrowRight" if gap > 0 else "ArrowLeft"
            before = await now()
            r = await self.bridge.request({"type": "key", "scope": "game", "selector": SEL["thumb"], "key": key,
                                           "times": 1})
            if not r or not r.get("ok"):
                raise SiteError("slider didn't take keys")
            step = abs(await now() - before)
            if step == 0:
                raise SiteError("slider didn't move")
            n = int(abs(gap) // step)
            if n > 1:                                 # jump most of the way in one message
                await self.bridge.request({"type": "key", "scope": "game", "selector": SEL["thumb"], "key": key,
                                           "times": n - 1})
            elif abs(gap) < step:
                return                                # as close as this slider's step allows
        raise SiteError("slider never reached the target")

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

        async def box(sel):
            got = await self.query(sel)
            try:
                return float(got[0]["value"]) if got else 0.0
            except (TypeError, ValueError):
                return 0.0

        async def payout():
            """The multiplier the game will really play, from its slider (not the boxes)."""
            got = await self.query(SEL["thumb"])
            try:
                t = float(got[0]["attrs"].get("aria-valuenow"))
            except (IndexError, TypeError, ValueError):
                return 0.0
            real = 100 - t if side == "over" else t
            return round(99 / real, 2) if real > 0 else 0.0
        if abs(await payout() - want) > 0.02:
            await self._enter_chance(chance, side, want, payout)
        await self.assert_zero_bet()
        chip = await self.bet_and_read(SEL["bet"], SEL["chips"])
        result = chip["attrs"].get("aria-label", "")
        m = RESULT.match(result)
        if not m:
            raise SiteError(f"unreadable result {result!r}")
        won, roll = m.group(1) == "Win", float(m.group(2))
        # The result itself must fit the odds we think we're playing: a win needs a roll past the target.
        target = 100 - chance if side == "over" else chance
        if won != (roll > target if side == "over" else roll < target):
            type(self)._method = None
            await self.snapshot_page(f"dice result doesn't fit a {chance}% {side} roll: {result!r}")
            raise SiteError(f"dice isn't playing {chance}% (got {result!r})")
        mult = round(99 / chance, 2)
        broken = 0 if won else self.streak
        self.streak = self.streak + 1 if won else 0
        self.best_streak = max(self.best_streak, self.streak)
        self.round = {"chance": chance, "side": side, "roll": roll, "won": won, "multiplier": mult}
        return {"kind": "win" if won else "loss", "roll": roll, "chance": chance, "side": side,
                "multiplier": mult, "win_streak": self.streak, "streak_broken_at": broken or None,
                "bet": 0, "payout": 0}
