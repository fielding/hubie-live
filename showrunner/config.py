"""Live config: showrunner/config.toml, reloaded between rounds. A file that fails to
parse or validate is rejected and the previous config stays in effect, so a typo during
a live stream never takes Hubie down."""
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("config")
PATH = Path(__file__).resolve().parent / "config.toml"
EXPRESSIONS = {"neutral", "happy", "laugh", "surprised", "thinking", "focused", "smug", "grimace", "sad", "eyeroll"}
REACTIONS = {"lean_in", "pop_up", "slump", "shake", "hat_tip", "nod"}
SEGMENT_IMAGE_DIR = "assets/segments/"


@dataclass(frozen=True)
class Segment:
    id: str
    every_minutes: float
    seconds: float
    say: tuple[str, ...]
    image: str | None
    expression: str | None
    reaction: str | None
    chat: str | None = None          # posted in Kick chat by the bot when the segment plays (links)
    game: str | None = None          # only plays while this game is on (e.g. "mines" for pick-your-mines)


@dataclass(frozen=True)
class Config:
    game_models: tuple[str, ...]
    chat_models: tuple[str, ...]
    temperature: float
    primary_timeout: float    # seconds before a non-final model is abandoned for the next one
    voice_speed: float
    fast_play: bool                    # real-site games: plan once, play fast, talk only when it's worth it
    talk_from_x: float                 # fast play: runs/hits below this multiplier go by without commentary
    rotate_minutes: float              # switch games on his own after this long with no votes (0 = never)
    music_volume: float                # background music level, 0-1 (0 = off)
    music_duck: float                  # music level while Hubie talks, 0-1
    music_skip_votes: int              # chatters needed for !skip (mods skip alone)
    fallback_voice: bool               # say voice-refused lines in the unfiltered voice (else skip)
    chat_reply_every: float
    vote_window: int
    line_gap: float          # quiet seconds after each spoken line
    quiet_move_chance: float  # chance Hubie plays a mid-round move without commenting
    quiet_result_chance: float  # chance an ordinary one-shot result (< 3x) gets a face, no line
    quiet_early_steps: int      # first N steps of a multi-step round count as "early"
    quiet_early_chance: float   # chance an early step goes by without commentary
    grind_line_every: int       # grind games (Max Win): a line every N spins
    chat_safe: float
    chat_reply: float
    output_safe: float
    persona: str
    command_cooldown: float
    commands: dict = field(default_factory=dict)          # "!cmd" -> reply text
    segments: tuple[Segment, ...] = ()
    catchphrases: dict = None                # event kind -> fixed lines, e.g. {"bust": ("Not today, man.",)}
    pronunciations: dict = None              # word -> how the voice should say it, e.g. {"degen": "dee-jen"}
    waiting_lines: tuple = ()                # said when the site stalls and Hubie waits to retry
    kick_moderate: bool = True               # kick bot deletes rejected messages / times out repeaters
    ignore_users: frozenset = frozenset()    # chat accounts Hubie never shows or answers (his bot)
    bot_commands: dict = None                # "!cmd" -> text the Kick bot posts
    catchphrase_chance: float = 0.0


def _num(d, key, lo, hi, default=None):
    v = d.get(key, default)
    if not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValueError(f"{key} must be a number in [{lo}, {hi}], got {v!r}")
    return float(v)


def _models(v, key):
    if not isinstance(v, list) or not v or not all(isinstance(m, str) and m for m in v):
        raise ValueError(f"models.{key} must be a non-empty list of model ids")
    return tuple(v)


def _bot_commands(cmds: dict) -> dict:
    out = {}
    for k, v in cmds.items():
        if not (isinstance(k, str) and k.startswith("!") and k[1:].isalnum() and isinstance(v, str) and 0 < len(v) <= 400):
            raise ValueError(f"kickbot.commands {k!r}: needs !word = \"reply (<= 400 chars)\"")
        out[k.lower()] = v
    return out


def parse(raw: dict) -> Config:
    m, voice, pace, safety = raw.get("models", {}), raw.get("voice", {}), raw.get("pacing", {}), raw.get("safety", {})
    persona = raw.get("persona", {}).get("prompt", "")
    if not isinstance(persona, str) or len(persona) < 50:
        raise ValueError("persona.prompt is missing or too short")
    cmds = raw.get("commands", {})
    replies = cmds.get("replies", {})
    for k, v in replies.items():
        if not (isinstance(k, str) and k.startswith("!") and k[1:].isalnum() and isinstance(v, str) and 0 < len(v) <= 220):
            raise ValueError(f"bad command {k!r}: needs !word and a reply of 1-220 chars")
    segments = []
    for s in raw.get("segments", []):
        if not s.get("enabled", True):
            continue
        sid = s.get("id")
        say = s.get("say", [])
        if not isinstance(sid, str) or not sid:
            raise ValueError("every segment needs an id")
        if not isinstance(say, list) or not all(isinstance(x, str) and 0 < len(x) <= 220 for x in say):
            raise ValueError(f"segment {sid}: say must be a list of lines (1-220 chars)")
        image = s.get("image")
        if image is not None and (not isinstance(image, str) or not image.startswith(SEGMENT_IMAGE_DIR) or ".." in image):
            raise ValueError(f"segment {sid}: image must live under {SEGMENT_IMAGE_DIR}")
        expr, react = s.get("expression"), s.get("reaction")
        if expr is not None and expr not in EXPRESSIONS:
            raise ValueError(f"segment {sid}: unknown expression {expr}")
        if react is not None and react not in REACTIONS:
            raise ValueError(f"segment {sid}: unknown reaction {react}")
        chat = s.get("chat")
        if chat is not None and (not isinstance(chat, str) or not 0 < len(chat) <= 400):
            raise ValueError(f"segment {sid}: chat must be a message (1-400 chars)")
        segments.append(Segment(sid, _num(s, "every_minutes", 1, 1440), _num(s, "seconds", 1, 120, 6),
                                tuple(say), image, expr, react, chat, s.get("game") or None))
    cp = raw.get("catchphrases", {})
    phrases = {}
    for kind, lines in cp.items():
        if kind == "chance":
            continue
        if kind not in ("safe", "bust", "cashout", "win", "loss") or not isinstance(lines, list) or \
                not all(isinstance(x, str) and 0 < len(x) <= 120 for x in lines):
            raise ValueError(f"catchphrases.{kind}: needs a list of short lines for safe/bust/cashout/win/loss")
        phrases[kind] = tuple(lines)
    waiting = raw.get("lines", {}).get("waiting", ["Hold on a second, chat."])
    if not isinstance(waiting, list) or not waiting or not all(isinstance(x, str) and 0 < len(x) <= 120 for x in waiting):
        raise ValueError("lines.waiting: needs a list of short lines")
    pron = raw.get("pronunciations", {})
    if not isinstance(pron, dict) or not all(isinstance(k, str) and k.strip() and isinstance(v, str) and 0 < len(v) <= 60
                                             for k, v in pron.items()):
        raise ValueError("pronunciations: needs word = \"spoken form\" pairs")
    return Config(
        game_models=_models(m.get("game"), "game"), chat_models=_models(m.get("chat"), "chat"),
        temperature=_num(m, "temperature", 0, 2, 0.9),
        primary_timeout=_num(m, "primary_timeout_seconds", 2, 30, 7), voice_speed=_num(voice, "speed", 0.7, 1.3, 1.0),
        fallback_voice=bool(voice.get("fallback_voice", False)),
        rotate_minutes=_num(pace, "rotate_minutes", 0, 1440, 25),
        fast_play=bool(pace.get("fast_play", True)),
        talk_from_x=_num(pace, "talk_from_x", 1.5, 1000, 5),
        music_volume=_num(raw.get("music", {}), "volume", 0, 1, 0.25),
        music_duck=_num(raw.get("music", {}), "duck_to", 0, 1, 0.08),
        music_skip_votes=int(_num(raw.get("music", {}), "skip_votes", 1, 50, 2)),
        chat_reply_every=_num(pace, "chat_reply_every_seconds", 5, 600, 20),
        vote_window=int(_num(pace, "vote_window_seconds", 30, 3600, 150)),
        line_gap=_num(pace, "line_gap_seconds", 0, 20, 2.0),
        quiet_move_chance=_num(pace, "quiet_move_chance", 0, 1, 0.3),
        quiet_result_chance=_num(pace, "quiet_result_chance", 0, 1, 0.5),
        quiet_early_steps=int(_num(pace, "quiet_early_steps", 0, 20, 2)),
        quiet_early_chance=_num(pace, "quiet_early_chance", 0, 1, 0.85),
        grind_line_every=int(_num(pace, "grind_line_every", 2, 100, 10)),
        chat_safe=_num(safety, "chat_safe", 0.5, 0.99, 0.75), chat_reply=_num(safety, "chat_reply", 0, 2, 0.9),
        output_safe=_num(safety, "output_safe", 0.5, 0.99, 0.8), persona=persona,
        command_cooldown=_num(cmds, "cooldown_seconds", 0, 3600, 60), commands=dict(replies),
        segments=tuple(segments), catchphrases=phrases, pronunciations=dict(pron),
        waiting_lines=tuple(waiting),
        kick_moderate=bool(raw.get("kickbot", {}).get("moderate", True)),
        ignore_users=frozenset(str(u).lower() for u in raw.get("kickbot", {}).get("ignore_users", [])),
        bot_commands=_bot_commands(raw.get("kickbot", {}).get("commands", {})),
        catchphrase_chance=_num(cp, "chance", 0, 1, 0.3))


class LiveConfig:
    def __init__(self, path: Path = PATH):
        self.path = path
        self._mtime = None
        self.current: Config = parse(tomllib.loads(path.read_text()))   # must be valid at startup
        self._mtime = path.stat().st_mtime

    def maybe_reload(self) -> bool:
        """Re-read the file if it changed. Returns True when a new config was applied."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            self.current = parse(tomllib.loads(self.path.read_text()))
        except Exception as e:
            log.error("config.toml rejected, keeping the previous config: %s", e)
            return False
        log.info("config.toml reloaded: game=%s chat=%s, %d commands, %d segments",
                 self.current.game_models[0], self.current.chat_models[0], len(self.current.commands),
                 len(self.current.segments))
        return True
