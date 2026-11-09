"""Housebets Dragon Tower on the real site (extension/), $0 bets only: climb 9 floors, each with 3
tiles (Medium: 1 trap per floor); a trap ends the round, cash out any time after a floor, the top
floor pays out on its own. Learned 2026-11-07:
  - tiles `button.tw-tile`, aria-label "Pick floor 2, tile 1" when pickable; data-state
    next/ahead/idle/safe/trap (+ reveal-safe/reveal-trap once the round is over)
  - `.tw-floor[data-f=N-1]` aria-label "Floor N, pays 1.48×"
  - Bet/Cash out = `.hl-p-betbtn` (filter out "Start Autobet"); `--cashout` while a round is open."""
import random
import re

from .housebets import HousebetsGame, SiteError

SEL = {"bet": ".hl-p-betbtn", "tiles": "button.tw-tile", "floor": '.tw-floor[data-f="{i}"]',
       "floors": ".tw-floor", "dd": ".hl-p-ddbtn", "ddval": ".hl-p-ddval", "option": '[role="option"]',
       "cap": ".hl-p-diffcap"}
FLOORS, TILES = 9, 3
# Difficulty options aren't in the page until the dropdown opens; these are the expected names until
# the real list has been read once (DragonTower.difficulties).
GUESSED = ("easy", "medium", "hard", "expert", "master")


class DragonTower(HousebetsGame):
    id = "tower"
    slug = "dragon-tower"
    title = "DRAGON TOWER"
    command = "!tower"
    max_multiplier = 38.05      # top floor on Medium; re-read from the page whenever difficulty changes
    difficulties: tuple = ()    # read from the dropdown the first time it's opened (class-wide)
    ready_selector = SEL["tiles"]
    brief = ("Housebets Dragon Tower (real site, zero-value bets): climb 9 floors; each floor hides traps. "
             "Pick a difficulty when starting: harder means fewer safe tiles per floor and a much bigger top "
             "floor (Medium tops out around 38x; Hard and above go far higher). The state shows the current "
             "difficulty, the safe chance per pick and what the top floor pays. Cash out any time after a "
             "floor. Talk multipliers casually, never money.")

    def in_round(self) -> bool:
        return bool(self.round) and self.round["status"] == "playing"

    def legal_actions(self) -> list[dict]:
        if not self.in_round():
            return [{"type": "start", "bet": [0], "difficulty": list(type(self).difficulties or GUESSED)}]
        acts = [{"type": "climb"}]
        if self.round["floor"]:
            acts.append({"type": "cashout"})
        return acts

    def default_action(self) -> dict:
        if not self.in_round():
            return {"type": "start", "bet": 0, "difficulty": "medium"}
        return {"type": "cashout"} if self.round["floor"] >= 3 else {"type": "climb"}

    def state(self) -> dict:
        return {"game": self.id, "site": "housebets", "fair": None, "balance": 0, "round": self.round,
                "floors_total": FLOORS, "difficulty": self.difficulty, "safe_per_pick": self.safe,
                "top_floor_pays": self.max_multiplier}

    def __init__(self, balance: float):
        super().__init__(balance)
        self.difficulty = "medium"
        self.safe = 2 / 3                         # chance a pick is safe (read from the page)

    def bust_chance(self, action: dict) -> float:
        return 1 - self.safe if action.get("type") == "climb" else 0.0

    async def _set_difficulty(self, want: str):
        """Open the dropdown (between rounds only), read its options, pick `want` (or the nearest
        known one), then re-read the board: tiles per floor, safe chance, top payout."""
        cur = await self.query(SEL["ddval"])
        if not (cur and cur[0]["text"].strip().lower() == want):
            await self.click(SEL["dd"])
            opts = await self.until(lambda: self.query(SEL["option"]), lambda o: bool(o), "the difficulty list", timeout=4)
            names = [o["text"].strip().split()[0].lower() for o in opts if o["text"].strip()]
            if names:
                type(self).difficulties = tuple(names)
            if want not in names:
                want = "medium" if "medium" in names else names[0]
            await self.click(SEL["option"], index=names.index(want))
            cur = await self.until(lambda: self.query(SEL["ddval"]),
                                   lambda c: bool(c) and c[0]["text"].strip().lower() == want, "the difficulty", timeout=4)
        self.difficulty = want
        cap = await self.query(SEL["cap"])
        m = re.search(r"(\d+)% safe", cap[0]["text"]) if cap else None
        if m:
            self.safe = int(m.group(1)) / 100
        floors = await self.query(SEL["floors"])
        top = await self._pays(len(floors) or FLOORS)
        if top:
            self.max_multiplier = top

    async def _page(self) -> dict:
        btn = [b for b in await self.query(SEL["bet"]) if "autobet" not in b["text"].lower()]
        if not btn:
            raise SiteError("Dragon Tower: no bet button")
        tiles = await self.query(SEL["tiles"])
        picked = {}
        for t in tiles:
            m = re.search(r"floor (\d+), tile (\d+)", t["attrs"].get("aria-label", ""), re.I)
            if m and t["attrs"].get("data-state") in ("safe", "trap"):
                picked[int(m.group(1))] = (int(m.group(2)), t["attrs"]["data-state"])
        return {"in_round": "hl-p-betbtn--cashout" in btn[0]["cls"], "won": "btn-win" in btn[0]["cls"],
                "picked": picked}

    async def _pays(self, floor: int) -> float:
        f = await self.query(SEL["floor"].format(i=floor - 1))
        m = re.search(r"pays ([\d,]+(?:\.\d+)?)", f[0]["attrs"].get("aria-label", "")) if f else None
        return float(m.group(1).replace(",", "")) if m else 0.0

    async def play(self, kind: str, action: dict) -> dict:
        if kind == "start":
            await self.ensure_page()
            p = await self._page()
            if p["in_round"]:                  # left open by a restart or reload: adopt it
                floor = len([1 for _, s in p["picked"].values() if s == "safe"])
                self.round = {"status": "playing", "floor": floor, "multiplier": await self._pays(floor) if floor else 1.0}
                return {"kind": "resume", "floor": floor}
            await self._set_difficulty(str(action.get("difficulty") or "medium").lower())
            await self.assert_zero_bet()
            await self.click_until(SEL["bet"], self._page, lambda p: p["in_round"], "the bet")
            self.round = {"status": "playing", "floor": 0, "multiplier": 1.0, "difficulty": self.difficulty}
            return {"kind": "start", "bet": 0, "difficulty": self.difficulty, "top_floor_pays": self.max_multiplier}
        r = self.round
        if kind == "climb":
            floor = r["floor"] + 1
            # Tiles per floor depend on difficulty (Hard has 2): pick only among tiles the board actually
            # offers, waiting for it to draw the floor rather than guessing a count.
            async def offered():
                return [int(m.group(1)) for t in await self.query(SEL["tiles"])
                        if (m := re.match(rf"Pick floor {floor}, tile (\d+)", t["attrs"].get("aria-label", "")))]
            choices = await self.until(offered, lambda o: bool(o), f"floor {floor} to be pickable", timeout=6)
            tile = random.SystemRandom().choice(choices)
            p = await self.click_until(f'{SEL["tiles"]}[aria-label="Pick floor {floor}, tile {tile}"]', self._page,
                                       lambda p: floor in p["picked"] or not p["in_round"], "the pick")
            if p["picked"].get(floor, (0, ""))[1] == "safe":
                r["floor"], r["multiplier"] = floor, await self._pays(floor)
                if p["in_round"]:
                    return {"kind": "safe", "floor": floor, "multiplier": r["multiplier"]}
                r["status"] = "cashed"                       # top floor: paid out by the site
                return {"kind": "cashout", "multiplier": r["multiplier"], "payout": 0, "bet": 0, "perfect": True}
            r["status"] = "busted"
            return {"kind": "bust", "floor": floor, "lost": 0, "revealed": r["floor"]}
        p = await self.click_until(SEL["bet"], self._page, lambda p: not p["in_round"], "the cash out")
        r["status"] = "cashed"
        return {"kind": "cashout", "multiplier": r["multiplier"], "payout": 0, "bet": 0, "perfect": False}
