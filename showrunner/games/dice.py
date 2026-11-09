"""Dice: roll 0.00-99.99; win if the roll lands over (or under) the target."""
from .base import Game, IllegalAction

BETS = (5, 10, 25, 50)
TARGETS = (10, 25, 50, 75, 90)        # chance of winning, in percent


class Dice(Game):
    id = "mock_dice"
    title = "DICE (PRACTICE)"
    command = ""          # not votable for now: the stream stays on Mines (still the offline fallback)
    brief = ("Dice: pick a win chance (10-90%) and a side (over or under). Lower chance pays more: "
             "payout = 99 / chance. One roll per round.")

    def __init__(self, balance: float):
        super().__init__(balance)
        self.last = None

    def in_round(self) -> bool:
        return False                    # a roll is a whole round

    def legal_actions(self) -> list[dict]:
        return [{"type": "roll", "bet": list(BETS), "chance": list(TARGETS), "side": ["over", "under"]}]

    def default_action(self) -> dict:
        return {"type": "roll", "bet": 10, "chance": 50, "side": "over"}

    def state(self) -> dict:
        return {"game": self.id, "balance": round(self.balance, 2), "fair": self.fair.public(), "last": self.last}

    def act(self, action: dict) -> dict:
        self._require(action)
        bet, chance, side = action.get("bet"), action.get("chance"), action.get("side")
        if bet not in BETS or chance not in TARGETS or side not in ("over", "under"):
            raise IllegalAction("dice: bad bet, chance or side")
        if bet > self.balance:
            raise IllegalAction("dice: insufficient demo balance")
        self.fair.next_round()
        roll = round(self.fair.floats(1)[0] * 100, 2)
        line = 100 - chance if side == "over" else chance
        win = roll > line if side == "over" else roll < line
        mult = round(99 / chance, 2)
        payout = round(bet * mult, 2) if win else 0.0
        self.balance += payout - bet
        self.last = {"roll": roll, "line": line, "side": side, "chance": chance, "win": win,
                     "multiplier": mult, "bet": bet, "payout": payout}
        return {"kind": "win" if win else "loss", **self.last}
