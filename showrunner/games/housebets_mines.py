"""Housebets Mines, played on the real site through the browser extension (extension/).

The extension only finds elements, reads them and clicks them; everything that knows the
site lives here, so a Housebets layout change is fixed in this file and hot-reloaded,
without touching the extension. Selectors were learned from page snapshots
(showrunner/.probe/) on 2026-11-06.

Money: Hubie only ever plays $0 bets. Before every Bet click the bet box is read back and
must be zero; anything else refuses the round and takes the game offline (fail closed).
"""
import copy
import random

from .base import IllegalAction
from .housebets import HousebetsGame, SiteError
from .mines import TILES, multiplier

SEL = {
    "mines": "#hl-p-mines",
    "grid_5x5": 'button[data-grid="25"]',
    "bet_button": ".hl-p-betbtn",
    "tiles": "button.hl-g-tile",
}
MINE_COUNTS = (5, 7, 10, 13, 16)    # what Hubie picks himself (20 busts nearly every round); chat can pick 1-24


class HousebetsMines(HousebetsGame):
    id = "mines"
    title = "HOUSEBETS MINES"
    command = "!mines"
    slug = "mines"
    max_multiplier = None       # lots of mines and a long run: millions of x in theory
    ready_selector = "button.hl-g-tile"
    chat_setting = ("mines", "!mines", 1, 24)   # "!mines 10" in chat sets the next round's mines
    brief = ("Housebets Mines (the real site, played for fun with zero-value bets): a 5x5 board hides N "
             "mines. Each gem raises the multiplier; a mine ends the round. More mines = faster growth, "
             "more risk. Cash out any time after one gem. Bets are always 0, so talk multipliers, not money. "
             "Play it like a showman degen: bold mine counts, push for big multipliers, chase the drama; "
             "a tiny cashout is boring TV, but so is busting every round, so mix it up. Chat can pick the mine count by typing !mines and a number.")

    def __init__(self, balance: float):
        super().__init__(balance)
        self.chat_pick: dict | None = None     # e.g. {"mines": 10} for the next round, set from chat

    # ---- the Game interface (sync parts read our cached view of the page) --------------------
    def in_round(self) -> bool:
        return bool(self.round) and self.round["status"] == "playing"

    def legal_actions(self) -> list[dict]:
        if not self.in_round():
            mines = [self.chat_pick["mines"]] if self.chat_pick else list(MINE_COUNTS)
            return [{"type": "start", "bet": [0], "mines": mines}]
        acts = [{"type": "reveal"}]
        if self.round["revealed"]:
            acts.append({"type": "cashout"})
        return acts

    def default_action(self) -> dict:
        if not self.in_round():
            return {"type": "start", "bet": 0, "mines": self.chat_pick["mines"] if self.chat_pick else 7}
        return {"type": "cashout"} if len(self.round["revealed"]) >= 3 else {"type": "reveal"}

    def state(self) -> dict:
        r = self.round
        s = {"game": self.id, "balance": round(self.balance, 2), "fair": None, "round": None, "site": "housebets"}
        if r:
            s["round"] = {
                "status": r["status"], "bet": 0, "mines": r["mines"], "revealed": r["revealed"],
                "multiplier": multiplier(r["mines"], len(r["revealed"]), self.house_edge),
                "next_multiplier": multiplier(r["mines"], len(r["revealed"]) + 1, self.house_edge),
                "mine_tiles": sorted(r.get("mine_tiles") or []) if r["status"] != "playing" else None,
                "hit": r.get("hit"),
            }
        return s

    def preview_safe(self, action: dict):
        """(game copy, event) as they'd be if this reveal finds a gem, so the runner can plan the
        next move while Hubie is still talking. Picks the tile now (written into the action) so
        the preview and the real click are the same tile."""
        if action.get("type") != "reveal" or not self.in_round():
            return None
        hidden = [t for t in range(TILES) if t not in self.round["revealed"]]
        if action.get("tile") not in hidden:
            action["tile"] = random.SystemRandom().choice(hidden)
        g = copy.copy(self)
        g.round = {**self.round, "revealed": self.round["revealed"] + [action["tile"]]}
        if len(g.round["revealed"]) == TILES - g.round["mines"]:
            return None                               # the last gem ends the round: not a "next move"
        n = len(g.round["revealed"])
        event = {"kind": "safe", "tile": action["tile"], "multiplier": multiplier(g.round["mines"], n, self.house_edge),
                 "next_multiplier": multiplier(g.round["mines"], n + 1, self.house_edge)}
        return g, event

    def bust_chance(self, action: dict) -> float:
        if action.get("type") != "reveal" or not self.in_round():
            return 0.0
        r = self.round
        return r["mines"] / max(1, TILES - len(r["revealed"]))

    async def play(self, kind: str, action: dict) -> dict:
        if kind == "start":
            return await self._start(action)
        if kind == "reveal":
            return await self._reveal(action)
        return await self._cashout()

    # ---- page access ------------------------------------------------------------------------
    async def _page(self) -> dict:
        """The board as the page shows it right now."""
        # The hidden Auto tab has a button with the same class ("Start Autobet"); never that one.
        btn = [b for b in await self.query(SEL["bet_button"]) if "autobet" not in b["text"].lower()]
        tiles = await self.query(SEL["tiles"])
        if not btn or len(tiles) != TILES:
            raise SiteError(f"unexpected page: {len(btn)} bet buttons, {len(tiles)} tiles (is Mines on 5x5?)")
        cls = [t["cls"] for t in tiles]
        return {
            "in_round": "hl-p-betbtn--cashout" in btn[0]["cls"],
            "gems": [i for i, c in enumerate(cls) if "hl-g-tile--gem" in c],
            "mines": [i for i, c in enumerate(cls) if "mine" in c],
            "hit": [i for i, c in enumerate(cls) if "mine" in c and "dim" not in c],
        }

    async def _click_until(self, selector: str, check, what: str) -> dict:
        return await self.click_until(selector, self._page, check, what)

    # ---- moves ------------------------------------------------------------------------------
    async def _start(self, action: dict) -> dict:
        mines = action.get("mines")
        if not isinstance(mines, int) or not 1 <= mines <= 24 or action.get("bet", 0) != 0:
            raise IllegalAction("housebets mines: bet must be 0 and mines 1-24")
        self.chat_pick = None                     # used up by this round
        await self.ensure_page()                  # right page, stream mode on (also how switching works)
        page = await self._page()
        if page["in_round"]:                      # e.g. after a restart mid-round: adopt it
            sel = await self.query(SEL["mines"])
            n = int(sel[0]["value"]) if sel and (sel[0]["value"] or "").isdigit() else mines
            self.round = {"status": "playing", "mines": n, "revealed": page["gems"]}
            return {"kind": "resume", "mines": n, "revealed": len(page["gems"])}
        grid = await self.query(SEL["grid_5x5"])
        if grid and grid[0]["attrs"].get("aria-checked") != "true":
            await self.click(SEL["grid_5x5"])
        await self.set_value(SEL["mines"], mines)
        await self.assert_zero_bet()
        sel = await self.query(SEL["mines"])
        if not sel or sel[0]["value"] != str(mines):
            raise SiteError("mine count did not stick")
        await self._click_until(SEL["bet_button"], lambda p: p["in_round"], "the bet")
        self.round = {"status": "playing", "mines": mines, "revealed": []}
        return {"kind": "start", "bet": 0, "mines": mines}

    async def _reveal(self, action: dict) -> dict:
        r = self.round
        hidden = [t for t in range(TILES) if t not in r["revealed"]]
        tile = action.get("tile")
        if tile is None:
            tile = random.SystemRandom().choice(hidden)
        if tile not in hidden:
            raise IllegalAction("housebets mines: tile already revealed")
        # The board sometimes ignores a click (mid-animation, a slow frame): retry, don't give up.
        p = await self._click_until(f'{SEL["tiles"]}[data-idx="{tile}"]',
                                    lambda p: tile in p["gems"] or not p["in_round"], "the tile flip")
        if tile in p["gems"] and p["in_round"]:
            r["revealed"].append(tile)
            m = multiplier(r["mines"], len(r["revealed"]), self.house_edge)
            return {"kind": "safe", "tile": tile, "multiplier": m,
                    "next_multiplier": multiplier(r["mines"], len(r["revealed"]) + 1, self.house_edge)}
        if tile in p["gems"]:                     # the last gem: the site ends the round itself
            r["revealed"].append(tile)
            return self._finish("cashed", p, perfect=True)
        r["hit"] = tile
        return self._finish("busted", p)

    async def _cashout(self) -> dict:
        p = await self._click_until(SEL["bet_button"], lambda p: not p["in_round"], "the cash out")
        return self._finish("cashed", p)

    def _finish(self, status: str, page: dict, perfect: bool = False) -> dict:
        r = self.round
        r["status"], r["mine_tiles"] = status, set(page["mines"])
        if status == "busted":
            return {"kind": "bust", "tile": r["hit"], "lost": 0, "revealed": len(r["revealed"])}
        m = multiplier(r["mines"], len(r["revealed"]), self.house_edge)
        return {"kind": "cashout", "multiplier": m, "payout": 0, "bet": 0, "perfect": perfect}
