"""Mines: 5x5 board, N mines. Reveal tiles to grow the multiplier; cash out before a mine."""
import random

from .base import Game, IllegalAction

_player_rng = random.SystemRandom()

TILES = 25
BETS = (5, 10, 25, 50)
MINE_COUNTS = (1, 3, 5, 7, 10)


def multiplier(mines: int, revealed: int, edge: float) -> float:
    m = 1.0
    for i in range(revealed):
        m *= (TILES - i) / (TILES - mines - i)
    return round(m * (1 - edge), 2) if revealed else 1.0


class Mines(Game):
    # The real Housebets Mines (housebets_mines.py) owns "mines" and !mines; this mock stays
    # for offline testing (switch to it from the admin API).
    id = "mock_mines"
    title = "MINES (PRACTICE)"
    command = ""
    brief = ("Mines: a 5x5 board hides N mines. Each safe reveal raises the multiplier; hitting a mine "
             "loses the bet. More mines = faster growth, more risk. Cash out any time after one reveal.")

    def __init__(self, balance: float):
        super().__init__(balance)
        self.round = None          # dict while a round is live, or the last finished round

    def in_round(self) -> bool:
        return bool(self.round) and self.round["status"] == "playing"

    def legal_actions(self) -> list[dict]:
        if not self.in_round():
            return [{"type": "start", "bet": list(BETS), "mines": list(MINE_COUNTS)}]
        acts = [{"type": "reveal"}]
        if self.round["revealed"]:
            acts.append({"type": "cashout"})
        return acts

    def default_action(self) -> dict:
        if not self.in_round():
            return {"type": "start", "bet": 10, "mines": 3}
        return {"type": "cashout"} if len(self.round["revealed"]) >= 3 else {"type": "reveal"}

    def state(self) -> dict:
        r = self.round
        s = {"game": self.id, "balance": round(self.balance, 2), "fair": self.fair.public(), "round": None}
        if r:
            s["round"] = {
                "status": r["status"], "bet": r["bet"], "mines": r["mines"],
                "revealed": r["revealed"],
                "multiplier": multiplier(r["mines"], len(r["revealed"]), self.house_edge),
                "next_multiplier": multiplier(r["mines"], len(r["revealed"]) + 1, self.house_edge),
                # Mine positions are only public once the round is over.
                "mine_tiles": sorted(r["mine_tiles"]) if r["status"] != "playing" else None,
                "hit": r.get("hit"),
            }
        return s

    def act(self, action: dict) -> dict:
        kind = self._require(action)
        if kind == "start":
            bet, mines = action.get("bet"), action.get("mines")
            if bet not in BETS or mines not in MINE_COUNTS:
                raise IllegalAction("mines: bad bet or mine count")
            if bet > self.balance:
                raise IllegalAction("mines: insufficient demo balance")
            self.fair.next_round()
            order = sorted(range(TILES), key=lambda i, f=self.fair.floats(TILES): f[i])
            self.round = {"status": "playing", "bet": bet, "mines": mines,
                          "mine_tiles": set(order[:mines]), "revealed": []}
            self.balance -= bet
            return {"kind": "start", "bet": bet, "mines": mines}
        r = self.round
        if kind == "reveal":
            hidden = [t for t in range(TILES) if t not in r["revealed"]]
            tile = action.get("tile")
            if tile is None:
                # The tile choice is the player's, so it must not come from the house's seeded
                # stream: that stream also placed the mines, and reusing it biases picks onto them.
                tile = _player_rng.choice(hidden)
            if tile not in hidden:
                raise IllegalAction("mines: tile already revealed")
            if tile in r["mine_tiles"]:
                r["status"], r["hit"] = "busted", tile
                return {"kind": "bust", "tile": tile, "lost": r["bet"], "revealed": len(r["revealed"])}
            r["revealed"].append(tile)
            m = multiplier(r["mines"], len(r["revealed"]), self.house_edge)
            if len(r["revealed"]) == TILES - r["mines"]:
                return self._cashout(perfect=True)
            return {"kind": "safe", "tile": tile, "multiplier": m,
                    "next_multiplier": multiplier(r["mines"], len(r["revealed"]) + 1, self.house_edge)}
        return self._cashout()

    def _cashout(self, perfect=False) -> dict:
        r = self.round
        m = multiplier(r["mines"], len(r["revealed"]), self.house_edge)
        payout = round(r["bet"] * m, 2)
        self.balance += payout
        r["status"] = "cashed"
        return {"kind": "cashout", "multiplier": m, "payout": payout, "bet": r["bet"], "perfect": perfect}
