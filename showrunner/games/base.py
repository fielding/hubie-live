"""Game plugin interface.

Every game Hubie can play is one module in this package exposing a `Game` subclass.
The show runner only ever talks to games through this interface, so a mock game and a
real Housebets adapter are interchangeable, and new games can be dropped in while the
stream runs (see GameRegistry.reload).

Rounds are provably fair in the standard originals way: a secret server seed is
committed by its SHA-256 hash before play, and each round's randomness is
HMAC-SHA256(server_seed, f"{client_seed}:{nonce}"), so any round can be re-derived
once the seed is revealed.
"""
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field


class IllegalAction(ValueError):
    pass


@dataclass
class Fairness:
    server_seed: str = field(default_factory=lambda: secrets.token_hex(32))
    client_seed: str = field(default_factory=lambda: secrets.token_hex(8))
    nonce: int = 0

    @property
    def server_seed_hash(self) -> str:
        return hashlib.sha256(self.server_seed.encode()).hexdigest()

    def floats(self, n: int) -> list[float]:
        """n uniform floats in [0, 1) for the current nonce (cursor-extended HMAC stream)."""
        out, cursor = [], 0
        while len(out) < n:
            digest = hmac.new(self.server_seed.encode(), f"{self.client_seed}:{self.nonce}:{cursor}".encode(),
                              hashlib.sha256).digest()
            for i in range(0, 32, 4):
                if len(out) == n:
                    break
                out.append(int.from_bytes(digest[i:i + 4], "big") / 2 ** 32)
            cursor += 1
        return out

    def next_round(self):
        self.nonce += 1

    def public(self) -> dict:
        return {"server_seed_hash": self.server_seed_hash, "client_seed": self.client_seed, "nonce": self.nonce}


class Game:
    """Subclass per game. All methods are synchronous and fast; no I/O."""
    id = ""            # stable key, e.g. "mines"
    title = ""         # shown on the HUD
    command = ""       # chat vote command, e.g. "!mines"
    brief = ""         # rules and strategy notes for the brain (plain text, short)
    house_edge = 0.01

    def __init__(self, balance: float):
        self.balance = balance
        self.fair = Fairness()

    def state(self) -> dict:
        """Everything the renderer and the brain need, JSON-serializable."""
        raise NotImplementedError

    def legal_actions(self) -> list[dict]:
        """Closed list of actions allowed right now, e.g. [{"type": "reveal"}, {"type": "cashout"}]."""
        raise NotImplementedError

    def act(self, action: dict) -> dict:
        """Apply one action; return an event dict with at least {"kind": ...}. Raises IllegalAction."""
        raise NotImplementedError

    def in_round(self) -> bool:
        raise NotImplementedError

    def default_action(self) -> dict:
        """A sensible move without the brain, so the stream keeps playing if the LLM is down."""
        raise NotImplementedError

    def _require(self, action: dict) -> str:
        kind = action.get("type") if isinstance(action, dict) else None
        if kind not in {a["type"] for a in self.legal_actions()}:
            raise IllegalAction(f"{self.id}: {kind!r} not legal now")
        return kind
