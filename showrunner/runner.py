"""The show runner: plays games, runs chat votes, answers chat, and drives Hubie, the game
view and the HUD. Deterministic code owns the loop; the brain is asked one bounded
question at a time, and every failure falls back to something that keeps the show going."""
import asyncio
import collections
import inspect
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

from . import brain as brain_mod
from . import clipper as clipper_mod, music, screenwatch, venice
from .config import LiveConfig
from .games import GameRegistry, IllegalAction
from .links import Broadcast, PuppetLink
from .safety import ChatMessage, Safety, normalize

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "puppet"))
import tts  # noqa: E402

log = logging.getLogger("runner")
VIEWERS_FILE = Path(__file__).resolve().parent / ".viewers.json"   # chat memory, gitignored
STATE_FILE = ROOT / "showrunner" / ".state.json"
START_BALANCE = 10_000.0
EMOTE = re.compile(r"\[emote:\d+:([\w-]+)\]")
FALLBACK_GAME = None            # a simulated game to switch to while a real-site game is offline; None = stay and wait
OFFLINE_RETRY_S = 30            # after a real-site failure, wait this long before trying it again
GAME_COOLDOWN_S = 600           # a game switched away from for failing is skipped (votes too) this long
BLACK_LEVEL = 8                 # average brightness below this = the captured game window is black
SCREEN_CHECK_S = 10
# Reload the game page this often, between rounds, so it never goes stale. (Housebets also reloads
# its own page at irregular times, 13-35 min apart on 2026-11-08; games retry through those.)
PAGE_REFRESH_S = 3 * 3600
CHECK_IN_ANGLES = ("how this session is going, in one vivid image", "something a chatter said earlier",
                   "chasing the stream record", "a small detail of life on the couch (the snacks, the cat, the remote)",
                   "the music that's playing", "this game itself and why you like or hate it",
                   "a ridiculous superstition you're trying right now", "something absurd you'd buy for Reginald or the couch",
                   "a fake sponsor you'd love to have", "your rival (an imaginary rich sloth named Reginald)",
                   "what your top hat has seen", "a dramatic movie-trailer voice description of this session",
                   "a one-line life lesson that is clearly terrible advice (about anything but gambling, money or risk)",
                   "a confession about your worst habit", "rating the last few minutes like a sports commentator",
                   "a weird thought you just had about the game's design", "what you'd name this session if it were an album")
CHAT_ANGLES = ("a question for chat about their night", "calling out the lurkers to say hi",
               "a would-you-rather for chat", "asking chat for a song recommendation", "roasting a recent chatter (playfully)",
               "asking chat to pick your next game with a vote", "asking chat about their best ever win at anything",
               "asking chat what they'd do with your record multiplier", "a this-or-that (two silly options) for chat",
               "asking chat to guess the next result", "asking chat for a nickname for your top hat",
               "asking chat what snack you should get", "asking chat what time it is where they are",
               "asking chat to rate the current track out of ten", "asking chat for their most controversial food opinion",
               "challenging chat to type a word in chat if they're actually awake",
               "asking chat what game they'd add to the rotation", "asking chat for a dare (a safe, silly one)",
               "asking a recent chatter a follow-up about something they said")


# Mines tiles chat can name: spots on the 5x5 board as (row, col), top-left is (0, 0).
_SPOTS = [
    (r"\b(?:all|four|4) corners\b", "all four corners", [(0, 0), (0, 4), (4, 0), (4, 4)]),
    (r"\btop corners\b", "top corners", [(0, 0), (0, 4)]),
    (r"\bbottom corners\b", "bottom corners", [(4, 0), (4, 4)]),
    (r"\bleft corners\b", "left corners", [(0, 0), (4, 0)]),
    (r"\bright corners\b", "right corners", [(0, 4), (4, 4)]),
    (r"\b(?:top|upper)[ -]left|\bleft[ -]top\b", "top left", [(0, 0)]),
    (r"\b(?:top|upper)[ -]right|\bright[ -]top\b", "top right", [(0, 4)]),
    (r"\b(?:bottom|lower)[ -]left|\bleft[ -]bottom\b", "bottom left", [(4, 0)]),
    (r"\b(?:bottom|lower)[ -]right|\bright[ -]bottom\b", "bottom right", [(4, 4)]),
    (r"\b(?:center|centre|middle)\b", "the middle", [(2, 2)]),
    (r"\bcorners\b", "the corners", [(0, 0), (0, 4), (4, 0), (4, 4)]),
]


_GAME_NAMES = {"mines": r"mines?", "tower": r"(?:dragon )?tower|dragon", "road": r"(?:hubie'?s )?road",
               "plinko": r"plinko", "limbo": r"limbo", "coinflip": r"coin ?flip|flip", "wheel": r"wheel",
               "hilo": r"hi[ -]?lo", "maxwin": r"max ?win", "dice": r"dice", "keno": r"keno"}
_GAME_ASK = re.compile(r"\b(?:play|switch(?: it)? to|go(?: back)? to|get on|move to|change to|let'?s do|do|try|hit|spin)"
                       r"(?: (?:the|those|that|some|a|on))? (" + "|".join(f"(?:{v})" for v in _GAME_NAMES.values()) + r")\b")


_SOFTER = [(r"\bfuck(?:ing|in')\b", "freaking"), (r"\bfucker(?:s)?\b", "menace"), (r"\bfuck(?:ed)?\b", "frick"),
           (r"\bmotherfucker\b", "menace"), (r"\bshit(?:ty)?\b", "crap"), (r"\bbastard(?:s)?\b", "rascal"),
           (r"\bcoward(?:s|ly)?\b", "chicken"), (r"\bgutless\b", "timid"), (r"\bass\b", "butt"),
           (r"\bbitch\b", "pest"), (r"\bdick\b", "jerk")]


def soften(line: str) -> str:
    """The voice's content filter refuses name-calling and some swears; the tamer word keeps the joke."""
    for pat, sub in _SOFTER:
        line = re.sub(pat, lambda m, sub=sub: sub.capitalize() if m.group(0)[:1].isupper() else sub, line, flags=re.I)
    return line


def game_request(text: str) -> str | None:
    """A game asked for in plain words ("get on those mines", "play plinko"): counted as a vote, so
    Hubie doesn't promise a switch only a vote can make."""
    m = _GAME_ASK.search(text.lower())
    if not m:
        return None
    return next((gid for gid, pat in _GAME_NAMES.items() if re.fullmatch(pat, m.group(1))), None)


def tile_requests(text: str) -> list[tuple[str, tuple[int, int]]]:
    """Tiles a chat message asks for, in the order it names them: "top corners", "right top corner
    then left top", "middle", "tile 7" / "tiles 1 5 25" (1-25, left to right, top to bottom).
    Fixed phrases, not the model: chat can steer which tile, never anything else."""
    low = text.lower()
    found, taken = [], []
    for pattern, name, spots in _SPOTS:
        for m in re.finditer(pattern, low):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue                                   # "top corners" already covers "corners"
            taken.append((m.start(), m.end()))
            found += [(m.start(), name, s) for s in spots]
    for m in re.finditer(r"\btiles? ((?:\d{1,2}[ ,]*)+)", low):
        for n in re.findall(r"\d{1,2}", m.group(1)):
            if 1 <= int(n) <= 25:
                found.append((m.start(), f"tile {n}", ((int(n) - 1) // 5, (int(n) - 1) % 5)))
    found.sort(key=lambda f: f[0])
    return [(name, spot) for _, name, spot in found][:6]


# What people actually type for a game, mapped to its real command.
COMMAND_ALIASES = {"!dragontower": "!tower", "!dragon": "!tower", "!hubiesroad": "!road", "!hubieroad": "!road",
                   "!coinflip": "!flip", "!coin": "!flip", "!hi-lo": "!hilo", "!higherlower": "!hilo",
                   "!max": "!maxwin", "!max-win": "!maxwin", "!mine": "!mines", "!limb": "!limbo"}


class _Bag:
    """Draws every item once, in random order, before any repeats (random.choice repeated itself a lot)."""
    def __init__(self):
        self._left: dict[tuple, list] = {}

    def draw(self, items):
        key = tuple(items)
        if not self._left.get(key):
            self._left[key] = random.sample(key, len(key))
        return self._left[key].pop()


BAG = _Bag()


def _either(*events: asyncio.Event) -> asyncio.Event:
    """An event that is set once any of these is (checked when a line is ready, so a cheap view)."""
    class Either:
        def is_set(self):
            return any(e.is_set() for e in events)
    return Either()


PICK_WINDOW_S = 20             # a chat pick vote (!mines N) stays open this long
PICK_HOLD_S = 180              # a chat pick (!mines 20) holds this long, or until the next pick
BLURT_GAP_S = 45               # least time between instant interjections
CLIP_MIN_X = 500               # hits this big get auto-clipped (a game can raise its own bar: clip_x)
CLIP_HYPE = ("this moment just got clipped for the highlight reel: hype it up, tell chat to share the clip, "
             "act like a streamer who knows this one's going viral")
MUSIC_COMMENT_CHANCE = 0.35     # share of new tracks Hubie says something about
MUSIC_COMMENT_GAP_S = 480       # at most one music comment per 8 minutes


class Runner:
    def __init__(self, puppet: PuppetLink, view: Broadcast, hud: Broadcast, vote_seconds=150):
        self.puppet, self.view, self.hud = puppet, view, hud
        self.config = LiveConfig()
        self.brain = brain_mod.Brain(self.config)
        self.games = GameRegistry()
        self.games.reload()
        self.safety = Safety(commands=set(self.games.commands()) | set(COMMAND_ALIASES))
        self.safety.apply(self.config.current)
        self.command_last: dict[str, float] = {}
        self.chat_q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.candidates: list = []               # (score, at, msg, verdict, reply task)
        self.recent: collections.deque = collections.deque(maxlen=12)
        # Chat memory for replies: each viewer's recent exchange with Hubie, and the room's last lines.
        self.threads: collections.OrderedDict[str, collections.deque] = self._load_threads()
        self.chat_log: collections.deque = collections.deque(maxlen=8)
        self._page_loaded_at = time.time()
        self._catch_at = 0.0
        self._last_line_at = time.time()
        self.clipper = clipper_mod.Clipper(Path.home() / "Movies" / "Hubie Clips")
        self._screen_reloads: list[float] = []
        self._handled_site_reload = 0.0
        self._face_at = 0.0
        self._muted: bool | None = None              # game tab muted? (unknown at startup)
        self._bg_talk: asyncio.Task | None = None    # a comment playing while the bets keep going
        self._last_start: dict[str, dict] = {}       # fast play: last round's start settings per game
        self._ladder: dict | None = None            # auto session's pretend bet size, for the HUD
        self._game_since = time.time()
        self._blurts: dict[str, tuple] = {}         # interjection text -> voiced clips, ready to play
        self._recent_blurts: collections.deque = collections.deque(maxlen=6)
        self._blurt_at = 0.0
        self._act_done = 0.0
        self._grind_action: dict[str, dict] = {}    # grind games: the mode picked for this stint
        self._tease_at = 0.0
        self._sticky_pick: dict[str, tuple[dict, float]] = {}   # game -> (chat's pick, until when)
        self._pick_open: dict[str, float] = {}      # game -> when its chat-pick vote opened
        self._ad_clear = asyncio.Event()              # cleared while an ad break plays: play holds
        self._ad_clear.set()
        self._bg_filler = False                       # the background line is filler (gives way to replies)
        self._reply_task: asyncio.Task | None = None  # the chat reply in progress (its own slot)
        self._tile_asks: collections.deque = collections.deque(maxlen=10)   # Mines: chat's (row, col) picks, until when
        self._last_turn: dict[str, dict] = {}       # step games: the plan last used, per game
        self._plan_ahead: tuple[str, asyncio.Task] | None = None   # step games: next round's plan, written during this one
        self.now_playing: dict | None = None                       # set by the stage page's music player
        self.up_next: dict | None = None
        self._skip_votes: set[str] = set()
        self._music_comment: dict | None = None    # a new track Hubie may say something about
        self._music_comment_at = 0.0
        self.follows: asyncio.Queue = asyncio.Queue(maxsize=200)   # filled by the Kick chat source
        self._thanked: set[str] = set()                            # follow/unfollow spam gets one thanks
        self.speak_lock = asyncio.Lock()
        self._next_round: asyncio.Task | None = None   # next round's first move, planned early
        self._offline_game, self._offline_until = None, 0.0  # a real-site game that just failed
        self._fell_back_from: str | None = None
        self._waiting_since = 0.0                # when the current real-site game went offline
        self._fail_count: dict[str, int] = {}    # consecutive failures per real-site game
        self._cooldown: dict[str, float] = {}    # game id -> until when it's skipped after failing
        self.dev_games: dict = {}                # admin "act" test instances (main.py)
        self.kick = None                         # KickBot (main.py): chat posts + moderation
        self._strikes: dict[str, list] = {}      # user -> times their messages were removed
        self._botcmd_at: dict[str, float] = {}   # bot command -> last answered
        self.paused = asyncio.Event()
        self.paused.set()                       # set = running
        self.vote_seconds = vote_seconds or self.config.current.vote_window
        self.votes: dict[str, set] = {}
        self.picks: dict[str, dict] = {}         # game id -> user -> (value, at): chat-chosen settings
        self.vote_ends = 0.0
        self.pending_game: str | None = None
        self.pending_reason: tuple[str, int | None] = ("admin", None)   # why the next switch happens
        self.last_chat_reply = 0.0
        self.st = self._load_state()
        self.game = self.games.create(self.st["game"], self.st["balances"].get(self.st["game"], START_BALANCE))

    # ---- persistence ----------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            st = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            st = {}
        default_game = "mines" if "mines" in self.games.classes else next(iter(self.games.classes))
        st.setdefault("game", default_game)
        if st["game"] not in self.games.classes:
            st["game"] = default_game
        st.setdefault("balances", {})
        st.setdefault("stats", {})              # per game: rounds, best, streak
        st.setdefault("autonomy_since", time.time() * 1000)
        st.setdefault("segments_at", {})         # segment id -> last time it played
        return st

    def _save_state(self):
        self.st["balances"][self.game.id] = self.game.balance
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st))
        os.replace(tmp, STATE_FILE)            # atomic: a crash never leaves half a file

    def human_touched(self):
        """Admin actions are human input; the HUD's autonomy clock restarts from here."""
        self.st["autonomy_since"] = time.time() * 1000
        self._save_state()
        asyncio.create_task(self.hud.send({"type": "autonomy", "since": self.st["autonomy_since"]}, "autonomy"))

    # ---- speaking -------------------------------------------------------------------
    async def prepare(self, line: str, checked: bool = False):
        """Safety-check and render a line without playing it, so it can be made ready while
        Hubie is still talking. Returns (line, clips), or None when there is nothing to say."""
        line = normalize(line or "").lstrip(":;,.-\u2013\u2014 ")   # models sometimes lead with ": "
        if not line:
            return None
        spoken = self._pronounce(line)

        async def voice():
            async with venice.slot():
                return await asyncio.to_thread(self._render, spoken, self.config.current.voice_speed)
        # The safety check and the voice render run side by side (each takes a second or more); a
        # blocked line's audio is simply never played.
        render = asyncio.create_task(voice())
        if not checked and not await self.safety.check_output(line):
            render.cancel()
            log.info("output check blocked: %r", line)
            return None
        try:
            clips = await render
        except tts.TTSError as e:
            if "content policy" not in str(e):
                log.warning("tts failed: %s | line: %r", e, line)
                return None
            # ElevenLabs' filter refuses name-calling aimed at someone ("you bastard") and violent
            # phrasing, not swearing. Optionally say those in the unfiltered voice; otherwise skip.
            if not self.config.current.fallback_voice:
                # Usually one word sinks it ("...Fucker.", "bastard of a tile"): retry once softened
                # instead of losing the line.
                soft = soften(spoken)
                if soft != spoken:
                    try:
                        async with venice.slot():
                            clips = await asyncio.to_thread(self._render, soft, self.config.current.voice_speed)
                        log.info("voice refused %r; said it softened", line)
                        return soften(line), clips
                    except tts.TTSError:
                        pass
                log.warning("voice refused %r; skipped", line)
                return None
            try:
                async with venice.slot():
                    clips = await asyncio.to_thread(self._render, spoken, self.config.current.voice_speed,
                                                    tts.UNFILTERED_MODEL, tts.UNFILTERED_VOICE)
            except tts.TTSError as e2:
                log.warning("voice refused %r; unfiltered voice failed too (%s); skipped", line, e2)
                return None
            log.info("voice refused %r; said in the unfiltered voice", line)
        return line, clips

    async def play(self, ready, expression: str | None = None, reaction: str | None = None,
                   noted: bool = False) -> bool:
        if reaction:
            await self.puppet.reaction(reaction)
        if not ready:
            if expression:
                await self.puppet.expression(expression)
                self._relax_face(expression)
            return False
        line, clips = ready
        async with self.speak_lock:
            log.info("HUBIE: %s", line)
            if not noted:
                self.recent.append(f"Hubie said: {line}")
            self.brain.said.append(line)
            ok = await self.puppet.say(clips, expression)
            self._last_line_at = time.time()
            self._relax_face(expression)
            await asyncio.sleep(self.config.current.line_gap)   # a breath before anything else
            return ok

    async def _face(self, name: str, force: bool = False):
        """Set a face during play without flicker: at most one change every couple of seconds
        (a big moment can force it), and never over a line being spoken."""
        now = time.time()
        if self.speak_lock.locked() or (not force and now - self._face_at < 2.5):
            return
        self._face_at = now
        await self.puppet.expression(name)
        self._relax_face(name)

    def _relax_face(self, expression: str | None):
        """Surprise (the little "o" mouth) and thinking are moments, not resting faces: with fast
        play he can go a long while between lines, and holding them looked like a frozen pucker."""
        hold = {"surprised": 1.5, "thinking": 1.5, "grimace": 2.0, "eyeroll": 2.5, "sad": 4.0}.get(expression)
        if hold is None:
            return

        async def relax():
            await asyncio.sleep(hold)
            if not self.speak_lock.locked():
                await self.puppet.expression(random.choice(("neutral", "happy")))
        asyncio.create_task(relax())

    async def speak(self, line: str, expression: str | None = None, reaction: str | None = None,
                    checked: bool = False) -> bool:
        return await self.play(await self.prepare(line, checked), expression, reaction)

    def _pronounce(self, line: str) -> str:
        """Respell words the voice gets wrong (config [pronunciations]); whole words, any case."""
        for word, said in (self.config.current.pronunciations or {}).items():
            line = re.sub(rf"\b{re.escape(word)}\b", said, line, flags=re.I)
        return line

    @staticmethod
    def _render(line: str, speed: float = 1.0, model: str = tts.MODEL, voice: str = tts.VOICE):
        out = []
        for mp3 in tts.speak(line, speed=speed, model=model, voice=voice):
            out.append((mp3, tts.visemes(mp3), max(1.0, tts.duration(mp3))))
        return out

    # ---- game loop ------------------------------------------------------------------
    async def push_game_state(self, event: dict | None = None):
        await self.view.send({"type": "state", "game": self.game.id, "title": self.game.title,
                              "state": self.game.state(), "event": event}, "state")

    def _stream_stats(self) -> dict:
        """Stream-wide numbers for the console (per-game ones jumped around on every game switch).
        Seeded once from the per-game history so the stream doesn't start from zero."""
        st = self.st["stats"]
        if "_stream" not in st:
            games = [v for k, v in st.items() if not k.startswith("_")]
            st["_stream"] = {"rounds": sum(g["rounds"] for g in games),
                             "best": max((g["best"] for g in games), default=0.0), "streak": 0}
        return st["_stream"]

    def _update_goal(self):
        """The storyline line in every game decision: chase the stream record, but only where this
        game can actually pay that much; elsewhere it's about the game's own top hit and streaks."""
        best = self._stream_stats()["best"]
        top = getattr(self.game, "max_multiplier", None)
        if top is not None and top <= best:
            self.brain.goal = (
                f"This game tops out at {top:g}x, so the stream record ({best:g}x) can't be beaten here: don't "
                f"talk about breaking the record in this game. Play for this game's big hit ({top:g}x), hot "
                f"streaks and chaos. If chat wants a record attempt, Hubie's Road, Mines or HiLo can get there.")
        else:
            self.brain.goal = (
                f"Stream record to beat: {best:g}x, and it's reachable in this game. Chase it: pick settings and "
                f"pushes that can beat it (more mines, harder modes, push past it) and hype it when you're close. "
                f"Mixing in safer rounds is fine, but the record is the storyline.")

    async def push_stats(self):
        self._update_goal()
        s = self._stream_stats()
        balance = None if getattr(self.game, "live", False) else self.game.balance   # real site: $0 bets, no balance
        await self.hud.send({"type": "stats", "game": self.game.title, "rounds": s["rounds"], "best": s["best"],
                             "streak": s["streak"], "balance": balance, "ladder": self._ladder}, "stats")

    def _record(self, event: dict) -> bool:
        """Count the round; True if it set a new stream-wide record multiplier."""
        won = event["kind"] in ("cashout", "win")
        before = self._stream_stats()["best"]
        for s in (self.st["stats"].setdefault(self.game.id, {"rounds": 0, "best": 0.0, "streak": 0}),
                  self._stream_stats()):
            s["rounds"] += 1
            if won:
                s["best"] = max(s["best"], float(event.get("multiplier") or 0))
                s["streak"] = s["streak"] + 1 if s["streak"] >= 0 else 1
            else:
                s["streak"] = s["streak"] - 1 if s["streak"] <= 0 else -1
        return won and float(event.get("multiplier") or 0) > before > 0

    async def _think(self, coro, speculative: bool = False, voice: bool = True):
        """A brain call plus its rendered line, run as one background task. Speculative work (lines
        for outcomes that may not happen) waits behind urgent Venice calls; voice=False writes the
        line but leaves voicing until it's actually needed (see _voiced)."""
        if speculative:
            venice.speculative()
        t0 = time.time()
        out = await coro
        t1 = time.time()
        ready = None
        if voice and out and out.get("say"):
            ready = await self.prepare(out["say"]) or False   # False: tried and refused, don't voice it again
        log.info("timing: line written in %.1fs, voiced in %.1fs%s", t1 - t0, time.time() - t1,
                 " (ahead)" if speculative else "")
        return out, ready

    async def _voiced(self, out, ready):
        """Voice a line that was written ahead without audio (the less likely branch)."""
        if ready is None and out and out.get("say"):
            ready = await self.prepare(out["say"])
        return ready

    # ---- fast play ------------------------------------------------------------------
    # One brain call plans a round (or a whole auto session); the moves then go out back to back
    # with no thinking between them. Hubie speaks only when it's worth hearing: a run past
    # talk_from_x, a big hit, a streak, a record. Ordinary busts get a face (sometimes a catchphrase).
    async def _talk(self, g, event: dict, pre: asyncio.Task | None = None, stale: asyncio.Event | None = None,
                    unless: asyncio.Task | None = None) -> bool:
        """A line about a moment. A big one gets an instant pre-voiced blurt while the real line is
        written and voiced (a few seconds), so the reaction lands with the hit, not after it; the line
        then follows on from the blurt. pre: the line already being written ahead (see _auto_session).
        stale: set once the moment has passed (drop the line if it isn't ready by then). unless: skip
        this line if that earlier one got said (a bust right after he hyped the run)."""
        if unless:
            said = await asyncio.gather(unless, return_exceptions=True)
            if said[0] is True:
                return False
        log.info("talk: %s on %s", json.dumps({k: event.get(k) for k in ("kind", "multiplier", "hits_of", "streak") if k in event}), g.id)
        blurt = None if pre and pre.done() else self._pick_blurt(self._blurt_kind(event))
        if pre is None:
            pre = asyncio.create_task(self._think(self.brain.react(
                g, {**event, "you_just_said": blurt} if blurt else event, list(self.recent))))
        if blurt:
            await self._say_blurt(blurt, event)
        r, ready = await pre
        ready = await self._voiced(r, ready)
        if stale and stale.is_set():
            return False                               # the round moved on while it was being written
        if r and ready:
            return await self.play(ready, r["expression"], r["reaction"] or self._default_reaction(event))
        return False

    # ---- interjections --------------------------------------------------------------
    async def _voice_blurts(self):
        """Voice every interjection once (the voice cache keeps them across restarts)."""
        for lines in (self.config.current.interjections or {}).values():
            for line in lines:
                if line not in self._blurts:
                    ready = await self.prepare(line, checked=True)
                    if ready:
                        self._blurts[line] = ready

    @staticmethod
    def _blurt_kind(event: dict) -> str | None:
        kind = event.get("kind")
        if event.get("clip") or kind in ("win", "cashout"):
            return "hit"
        if kind == "win_streak":
            return None if str(event.get("mood", "")).startswith("tease") else "streak"   # play a tease cool
        if kind == "deep_run":
            return "tension"
        if kind in ("bust", "loss", "streak_broken"):
            return "bust"
        return None                                    # check-ins, chat time, music: no rush

    def _pick_blurt(self, kind: str | None) -> str | None:
        """A voiced blurt for this moment, or None (none ready, or he's mid-sentence: a blurt that
        waits for the lock isn't instant any more)."""
        # A blurt is for the moment that stands out: on a hot board 8x comes every round, and a blurt on
        # every one became a tic. At most one every BLURT_GAP_S, never one of the last few again.
        if not kind or self.speak_lock.locked() or time.time() - self._blurt_at < BLURT_GAP_S:
            return None
        lines = [x for x in (self.config.current.interjections or {}).get(kind, ()) if x not in self._recent_blurts]
        ready = [x for x in lines if x in self._blurts]
        if len(ready) < len(lines):
            asyncio.create_task(self._voice_blurts())  # lines added to config while live
        if not ready:
            return None
        line = random.choice(ready)
        self._recent_blurts.append(line)
        self._blurt_at = time.time()
        return line

    async def _say_blurt(self, line: str, event: dict):
        face = {"hit": "laugh", "streak": "surprised", "tension": "surprised", "bust": "grimace"}[self._blurt_kind(event)]
        async with self.speak_lock:
            log.info("HUBIE (blurt): %s", line)
            await self.puppet.say(self._blurts[line][1], face)
            self._last_line_at = time.time()

    async def _announce_record(self, g, m: float):
        if self.kick:
            self.kick.say(f"NEW STREAM RECORD: {m:g}x on {g.title.title()}!")
        line = asyncio.create_task(self.prepare(f"NEW STREAM RECORD! {m:g} x! Somebody clip that!", checked=True))
        blurt = self._pick_blurt("hit")
        if blurt:
            await self._say_blurt(blurt, {"kind": "win"})
        await self.play(await line, "laugh", "pop_up")

    def _step_action(self, g) -> dict:
        while g.id == "mines" and self._tile_asks:
            pos, until = self._tile_asks.popleft()
            if time.time() < until:
                return {"type": "reveal", "pos": pos}
        if hasattr(g, "step_action"):
            return g.step_action()
        return {"type": next(a["type"] for a in g.legal_actions() if a["type"] != "cashout")}

    def _recent_chatters(self, minutes: float = 10) -> list[str]:
        """Who's actually around: chatters from the last few minutes, each with their latest line.
        Old names (a viewer who left an hour ago) got called out like they were still here."""
        cutoff, seen = time.time() - minutes * 60, {}
        for user, line, at in reversed(self.chat_log):
            if at >= cutoff and user not in seen:
                seen[user] = line
        return list(seen.values())[:4]

    async def _hold(self):
        """Wait while play is held: an admin pause, or an ad break."""
        await self.paused.wait()
        await self._ad_clear.wait()

    def _ad_due(self) -> bool:
        ab = self.config.current.ad_break
        if not ab:
            return False
        last = self.st["segments_at"].setdefault("ad_break", time.time())
        return time.time() - last >= ab["every_minutes"] * 60

    async def ad_break(self):
        """Hubie throws to a commercial: one of the videos in the ad folder plays on his monitor, the
        music goes quiet and play holds, then he comes back."""
        ab = self.config.current.ad_break
        self.st["segments_at"]["ad_break"] = time.time()
        self._save_state()
        folder = ROOT / ab["folder"]
        videos = sorted(p.name for p in folder.glob("*") if p.suffix.lower() in (".mp4", ".webm", ".mov")) \
            if folder.is_dir() else []
        if not videos:
            return
        name = BAG.draw(videos)
        level = await asyncio.to_thread(clipper_mod.leveled, folder / name)   # made once, then reused
        play = f"leveled/{level.name}" if level else name
        secs = await asyncio.to_thread(clipper_mod.video_seconds, folder / play)
        if not secs:
            log.warning("ad break: couldn't read %s; skipped", name)
            return
        secs = min(secs, 120.0)
        log.info("ad break: %s (%.0fs)", name, secs)
        self._ad_clear.clear()
        try:
            await self.speak(BAG.draw(ab["intro"]), "happy", "hat_tip", checked=True)
            await self.hud.send({"type": "music_mute", "seconds": secs + 1.5})
            await self.view.send({"type": "segment", "video": f"/{ab['folder'].strip('/')}/{play}", "seconds": secs})
            await asyncio.sleep(secs + 0.5)
            await self.speak(BAG.draw(ab["outro"]), "happy", "pop_up", checked=True)
        finally:
            self._ad_clear.set()

    def _business(self):
        """Between-move business (chat replies, ads and reminders, a word on a new track) runs in the
        background while play goes on, one thing at a time."""
        if self.config.maybe_reload():                 # a cheap mtime check: edits apply mid-session too
            self.safety.apply(self.config.current)
            self.vote_seconds = self.config.current.vote_window
        if self.config.maybe_reload():                 # a cheap mtime check: edits apply mid-session too
            self.safety.apply(self.config.current)
            self.vote_seconds = self.config.current.vote_window
        # Replies have their own slot: a viewer waits for the line being spoken, not for the queue of
        # game lines behind it (on Mines that queue made replies 20-50 s late, which read as ignoring).
        if self.candidates and time.time() - self.last_chat_reply >= self.config.current.chat_reply_every \
                and not (self._reply_task and not self._reply_task.done()):
            self._reply_task = asyncio.create_task(self.maybe_answer_chat())
            if self._bg_talk and not self._bg_talk.done() and self._bg_filler and not self.speak_lock.locked():
                self._bg_talk.cancel()                 # filler not yet spoken gives way
            return
        if self._bg_talk and not self._bg_talk.done():
            return
        if self._ad_due():
            self._in_background(self.ad_break())
        elif self._segment_due():
            self._in_background(self.maybe_run_segment())
        elif self._music_comment is not None:
            self._in_background(self.maybe_comment_music(), filler=True)

    def _then(self, coro):
        """Queue a line behind the one in flight (instead of dropping it like _in_background)."""
        prev = self._bg_talk

        async def chained():
            if prev and not prev.done():
                await asyncio.gather(prev, return_exceptions=True)
            return await coro
        self._bg_talk = asyncio.create_task(chained())
        return self._bg_talk

    async def _opener(self, out):
        ready = await self.prepare(out["say"])
        await self.play(ready, out["expression"], out["reaction"])

    async def _fast_steps(self, g):
        cfg = self.config.current
        tfx = getattr(g, "talk_from_x", cfg.talk_from_x)   # a game where 5x is routine (HiLo) sets its own
        target, peak = tfx, 1.0
        if not g.in_round() and self._picks_ready(g.id):
            self._apply_picks(g)                       # chat's "!mines 10"
            self._plan_ahead = None                    # planned before chat picked
        elif not g.in_round() and g.id in self._sticky_pick:
            pick, until = self._sticky_pick[g.id]
            if time.time() < until:
                g.chat_pick = dict(pick)               # still chat's call
            else:
                del self._sticky_pick[g.id]
        if not g.in_round():
            # The plan for this round was written during the last one, so a round starts right away.
            # Words only: the opener is voiced (in the background) only when it's actually said.
            ahead, self._plan_ahead = self._plan_ahead, None
            last = self._last_turn.get(g.id)
            if ahead and ahead[0] == g.id and not ahead[1].done() and last:
                turn, self._plan_ahead = last, ahead       # a round shorter than a plan: same again, keep that one coming
            else:
                if ahead and ahead[0] == g.id and not ahead[1].cancelled():
                    turn, _ = await ahead[1]
                else:
                    turn, _ = await self._think(self.brain.plan_round(g, list(self.recent), "steps"), voice=False)
                self._plan_ahead = (g.id, asyncio.create_task(self._think(
                    self.brain.plan_round(g, list(self.recent), "steps"), speculative=True, voice=False)))
            if turn and getattr(g, "chat_pick", None):
                turn = {**turn, "action": {**turn["action"], **g.chat_pick}}   # chat's number beats the plan
            if turn:
                self._last_turn[g.id] = turn
            await self._face(random.choice(("focused", "neutral")), force=True)
            if turn:
                target = turn["cash_out_at"]
                # An opener only when the plan changes (new difficulty...): same settings, just go.
                start = {k: v for k, v in turn["action"].items() if k != "bet"}
                if turn.get("say") and start != self._last_start.get(g.id):
                    self._in_background(self._opener(turn))
                self._last_start[g.id] = start
            start_action = turn["action"] if turn else g.default_action()
            log.info("round: %s %s", g.id, json.dumps({k: v for k, v in start_action.items() if k != "type"}))
            ev = await self._act(g, start_action)
            self.recent.append(f"{g.title}: {json.dumps(ev)} (cash out at {target:g}x)")
            await self.push_game_state(ev)
        next_talk, run_talk = tfx, None
        stale = asyncio.Event()                        # set when the round ends: run lines still being written are dropped
        try:
            return await self._climb(g, cfg, target, peak, next_talk, run_talk, stale, tfx)
        finally:
            stale.set()                                # also on a site error: no lines about a dead round

    async def _climb(self, g, cfg, target, peak, next_talk, run_talk, stale, tfx):
        for _ in range(60):
            await self._hold()
            self._business()
            can_cash = any(a["type"] == "cashout" for a in g.legal_actions())
            action = {"type": "cashout"} if can_cash and peak >= target else self._step_action(g)
            await self.puppet.send({"type": "typing", "seconds": 0.35})
            ev = await self._act(g, action)
            await self.push_game_state(ev)
            if ev["kind"] == "safe":
                peak = max(peak, float(ev.get("multiplier") or peak))
                await self._face("happy" if peak < tfx else random.choice(("happy", "focused", "smug")))
                if peak >= next_talk:                   # a run worth talking about: 5x, 10x, 20x...
                    next_talk = peak * 2
                    self.recent.append(f"{g.title}: {json.dumps(ev)}")
                    # Talk over the play like a streamer: the blurt lands now, the line follows while
                    # the clicks go on (a beat slower, so the run doesn't blow past the comment).
                    # Only the newest milestone gets a line: an older one not yet said would be stale (10x
                    # announced at 40x), so it's dropped when a bigger one comes.
                    line_stale = asyncio.Event()
                    if run_talk and not run_talk.done():
                        getattr(run_talk, "stale", line_stale).set()
                    run_talk = self._then(self._talk(g, {**ev, "kind": "deep_run", "cash_out_at": target},
                                                     stale=_either(stale, line_stale)))
                    run_talk.stale = line_stale
                    await asyncio.sleep(1.5)
                await asyncio.sleep(0.25)
                continue
            if ev["kind"] not in ("bust", "cashout", "win", "loss"):
                continue
            if ev["kind"] == "bust":
                ev["reached"] = peak
            stale.set()
            self.recent.append(f"{g.title}: {json.dumps(ev)}")
            await self.puppet.reaction(self._default_reaction(ev))
            # One line per round: a bust right after his run line got said just gets a face (unless the
            # run was huge); a cash-out (the payoff) always gets its line once it's big.
            if ev["kind"] != "bust":
                return await self._fast_result(g, ev, big=float(ev.get("multiplier") or 0) >= tfx)
            # On a hot board a bust just past the line (8x on 16 mines) happens every round, and a line
            # on each one became "8x and I got greedy" on repeat: a bust needs a real run (or quiet air).
            quiet = cfg.max_quiet_seconds and time.time() - self._last_line_at > cfg.max_quiet_seconds
            return await self._fast_result(g, ev, big=peak >= tfx * 2 or (peak >= tfx and quiet),
                                           unless=None if peak >= tfx * 4 else run_talk)

    async def _fast_result(self, g, ev: dict, big: bool, unless: asyncio.Task | None = None):
        record = self._record(ev)
        self._save_state()
        await self.push_stats()
        clipped = await self._maybe_clip(g, ev, record)
        if record:
            m = float(ev["multiplier"])
            await self._announce_record(g, m)
        elif big or clipped:
            # Said over the start of the next round, not in front of it.
            self._then(self._talk(g, {**ev, "clip": CLIP_HYPE} if clipped else ev, unless=None if clipped else unless))
            await asyncio.sleep(1.5)
        else:
            cfg = self.config.current
            lines = (cfg.catchphrases or {}).get(ev["kind"])
            # Fast play busts a lot (Master tower: most rounds end on the first pick), so the chance
            # alone would make it a running gag every few seconds: at most one every few minutes.
            if lines and random.random() < cfg.catchphrase_chance and time.time() - self._catch_at > 180:
                self._catch_at = time.time()
                self._in_background(self.speak(BAG.draw(lines), "laugh", self._default_reaction(ev), checked=True))
            await asyncio.sleep(0.6)

    def _in_background(self, coro, filler: bool = False):
        """Start a comment (or chat reply) without stopping play; at most one runs at a time, so
        lines can't pile up behind each other while the bets keep going. filler: a check-in or chat
        prompt, which gives way to a chat reply (see _business)."""
        if self._bg_talk and not self._bg_talk.done():
            coro.close()
            return
        self._bg_talk = asyncio.create_task(coro)
        self._bg_filler = filler

    async def _auto_session(self, g):
        """One-shot games (dice, limbo, flip...): bets back to back without pausing, with pretend
        bet sizing (it grows on each win, resets on a loss). Hubie comments while the bets keep
        rolling: back-to-back wins, a big streak breaking, huge single hits, a check-in now and then.
        The session lasts until the game changes (vote or rotation)."""
        cfg = self.config.current
        turn, _ = await self._think(self.brain.plan_round(g, list(self.recent), "auto"), voice=False)
        # A relaxed face while the bets run (the "thinking" pucker held for a whole session looked odd).
        await self._face(random.choice(("neutral", "focused")), force=True)
        action = turn["action"] if turn else g.default_action()
        inc = (turn["increase_on_win_pct"] if turn else 100) / 100
        start = {**{k: v for k, v in action.items() if k != "bet"}, "inc": inc}
        if turn and turn.get("say") and start != self._last_start.get(g.id):
            self._then(self._opener(turn))               # voiced and said over the first bets
        self._last_start[g.id] = start
        self.recent.append(f"{g.title}: rolling continuously, {json.dumps(action)}, pressing +{inc:.0%} on wins")
        units, streak, wins, peak_units, played, dry, best_hit = 1.0, 0, 0, 1.0, 0, 0, 0.0

        def streak_event(n: int, m: float, units: float) -> dict:
            # How special a streak is depends on the odds: two 6x dice wins in a row (~3%) is a tease,
            # two coin flips (25%) is nothing. Tiers by how unlikely this streak is.
            rare = p_win ** n
            mood = ("huge: this is the run chat came for, full hype" if rare <= 0.0002 else
                    "building: getting real, tension, hint at how rare a few more would be" if rare <= 0.005
                    else "tease: barely anything yet, play it cool, hint at what it could become")
            return {"kind": "win_streak", "streak": n, "multiplier": m, "mood": mood, "bet_size_units": round(units, 2)}
        # (streak, task): the line for the next win, written and voiced while that bet is still in the air,
        # so a streak line lands right on the hit. Fixed-target games only (the next win pays the same).
        ahead: tuple[int, asyncio.Task] | None = None
        p_win = g.win_chance(action) if hasattr(g, "win_chance") else 0.5
        try:
            while played < 400:
                await self._hold()
                if self.pending_game and self.pending_game != g.id:
                    break
                if self.config.current.rotate_minutes and \
                        time.time() - self._game_since > self.config.current.rotate_minutes * 60:
                    break                                    # the game loop rotates to another game
                if time.time() - self._page_age_from() > PAGE_REFRESH_S:
                    break                                    # the game loop refreshes the page between rounds
                if played and played % 20 == 0 and g.id in self.games.reload():
                    log.info("%s code changed; restarting the session with it", g.id)
                    self.game = self.games.create(g.id, g.balance)   # the edited version, from the next bet
                    break
                self._business()
                await self.puppet.send({"type": "typing", "seconds": 0.3})
                ev = await self._act(g, dict(action))
                played += 1
                ev["bet_size_units"] = round(units, 2)
                await self.push_game_state(ev)
                won = ev["kind"] in ("win", "cashout")
                record = self._record(ev)
                broken = streak
                if won:
                    wins, streak, dry = wins + 1, streak + 1, 0
                    units *= 1 + inc
                    peak_units = max(peak_units, units)
                else:
                    streak, units, dry = 0, 1.0, dry + 1
                self._ladder = {"units": units, "streak": streak, "left": None}
                await self.push_stats()
                # Not every bet goes into his short memory (12 items): it pushed his own recent lines
                # out, so he couldn't see he was repeating himself. Wins and big moments only.
                if won or float(ev.get("multiplier") or 0) >= cfg.talk_from_x:
                    self.recent.append(f"{g.title}: {json.dumps({k: ev[k] for k in ('kind', 'multiplier') if k in ev})}")
                m = float(ev.get("multiplier") or 0)
                best_hit = max(best_hit, m)
                p_win = g.win_chance(action) if hasattr(g, "win_chance") else (wins + 1) / (played + 2)
                await self.puppet.reaction("nod" if won else "shake")
                # Faces follow the run: smug on a hit, focused on a streak, an eye-roll or a sad face
                # now and then in a long drought, otherwise drift between resting faces.
                if won:
                    await self._face("focused" if streak >= 2 else random.choice(("smug", "happy")), force=True)
                elif dry >= 10 and random.random() < 0.25:
                    await self._face(random.choice(("eyeroll", "sad")))
                elif time.time() - self._face_at > 8:
                    await self._face(random.choice(("neutral", "focused", "happy", "smug")))
                clipped = await self._maybe_clip(g, ev, record)
                if record:
                    await self._announce_record(g, m)
                elif clipped:                               # a monster hit: always worth the moment
                    await self._talk(g, {**ev, "clip": CLIP_HYPE})
                elif won and streak >= 2 and getattr(g, "streak_talk", True) and p_win ** streak <= 0.1 and \
                        (p_win ** streak <= 0.005 or time.time() - self._tease_at > 120):
                    # A tease (4 flips in a row) comes often: one every couple of minutes. Real runs always talk.
                    if p_win ** streak > 0.005:
                        self._tease_at = time.time()
                    pre, ahead = (ahead[1], None) if ahead and ahead[0] == streak else (None, ahead)
                    self._in_background(self._talk(g, streak_event(streak, m, units), pre=pre))
                elif m >= getattr(g, "big_hit_x", max(20.0, cfg.talk_from_x * 4)):    # a huge single hit
                    self._in_background(self._talk(g, ev))
                elif not won and broken >= 2 and p_win ** broken <= 0.005 and getattr(g, "streak_talk", True):
                    self._in_background(self._talk(g, {"kind": "streak_broken", "streak_was": broken}))
                elif played % 30 == 0 or (self.config.current.max_quiet_seconds and
                                          time.time() - self._last_line_at > self.config.current.max_quiet_seconds):
                    # A check-in every 30 bets, and on slow games (Wheel) whenever he's gone quiet. Each
                    # gets a different angle so the filler doesn't become "N spins, nothing" on repeat.
                    self._last_line_at = time.time()          # don't re-trigger while the line is written
                    if played % 30 != 0 and random.random() < 0.7:
                        # Chat time: no spin counts to narrate, a nudge to get chat talking instead.
                        names = self._recent_chatters()
                        check = {"kind": "chat_time", "recent_chatters": names,
                                 "talk_about": BAG.draw(CHAT_ANGLES),
                                 "note": "Don't recap spins, droughts or the wheel. Talk TO chat: ask something, "
                                         "call out a chatter from recent_chatters by name (only about what they actually said), or start "
                                         "a bit they can answer. If recent_chatters is empty, chat is quiet: talk to the room."}
                    else:
                        check = {"kind": "check_in", "bets_so_far": played, "best_hit_so_far": best_hit,
                                 "talk_about": BAG.draw(CHECK_IN_ANGLES)}
                        if getattr(g, "streak_talk", True):
                            check.update(wins=wins, biggest_bet_units=round(peak_units, 2), bets_since_last_win=dry)
                    if not self.candidates:                 # a viewer waiting comes first
                        self._in_background(self._talk(g, check), filler=True)
                if ahead and ahead[0] != streak + 1:
                    ahead[1].cancel()                          # that streak is over (or its line was used)
                    ahead = None
                if won and ahead is None and hasattr(g, "win_chance") and getattr(g, "streak_talk", True) \
                        and p_win ** (streak + 1) <= 0.1:
                    nxt = streak_event(streak + 1, m, units * (1 + inc))
                    # Most of these never happen (a tease is a win or two away all session): write the
                    # words ahead, but only spend a voice render on the rarer tiers.
                    ahead = (streak + 1, asyncio.create_task(self._think(
                        self.brain.react(g, nxt, list(self.recent)), speculative=True,
                        voice=p_win ** (streak + 1) <= 0.005)))
                await asyncio.sleep(0.35)
        finally:
            if ahead:
                ahead[1].cancel()
            self._ladder = None
            self._save_state()
            await self.push_stats()

    async def _mute_game(self, on: bool):
        bridge = getattr(self, "bridge", None)
        if self._muted is on:
            return
        if bridge and bridge.clients:
            self._muted = on
            try:
                await bridge.request({"type": "mute", "on": on}, timeout=5)
            except Exception as e:
                log.warning("could not %smute the game tab: %s", "" if on else "un", type(e).__name__)

    async def _grind(self, g):
        """Grind games (Max Win): pick a mode once, then spin back to back until it hits, with a
        line every few spins to build tension. Chat replies still come first; a pending game switch
        ends the grind."""
        # The mode is picked once per stint on the game; after a hit he goes straight back in (re-planning
        # and an opener after every hit left half a minute of dead air).
        action = self._grind_action.get(g.id)
        if action is None:
            turn = await self.brain.game_turn(g, list(self.recent))
            action = turn["action"] if turn else g.default_action()
            self._grind_action[g.id] = action
            if turn and turn.get("say"):
                self._in_background(self._opener(turn))
        every = self.config.current.grind_line_every
        for spin in range(1, 501):
            await self._hold()
            self._business()
            if self.pending_game and self.pending_game != g.id:
                return                                   # chat voted something else: stop here
            if self.config.current.rotate_minutes and \
                    time.time() - self._game_since > self.config.current.rotate_minutes * 60:
                return                                   # a long drought can't hold the rotation hostage
            await self.puppet.send({"type": "typing", "seconds": 0.4})
            event = await self._act(g, dict(action))
            event["spin"] = spin
            self.recent.append(f"{g.title}: {json.dumps(event)}")
            record = self._record(event)
            self._save_state()
            await self.push_stats()
            await self.push_game_state(event)
            if event["kind"] == "win":
                clipped = await self._maybe_clip(g, event, record)
                if clipped and not record:
                    event = {**event, "clip": CLIP_HYPE}
                if record:
                    m = float(event["multiplier"])
                    await self._announce_record(g, m)
                else:
                    self._then(self._talk(g, event))       # blurt now, the line over the next spins
                return
            if spin % every == 0 and time.time() - self._last_line_at > 25:   # a check-in now and then
                # Said over the spinning, not between spins. The drought is only one of the topics:
                # a line about it every ten spins was the same joke for half an hour.
                roll = random.random()
                if roll < 0.35:
                    check = {"kind": "drought", "spins_without_a_hit": spin}
                elif roll < 0.7:
                    names = self._recent_chatters()
                    check = {"kind": "chat_time", "recent_chatters": names, "talk_about": BAG.draw(CHAT_ANGLES),
                             "note": "Don't mention the spins or the drought. Talk TO chat."}
                else:
                    check = {"kind": "check_in", "spins_so_far": spin, "talk_about": BAG.draw(CHECK_IN_ANGLES)}
                if not self.candidates:
                    self._in_background(self._talk(g, check), filler=True)
            else:
                await asyncio.sleep(0.3)

    async def play_round(self):
        g = self.game
        if getattr(g, "grind", False):
            return await self._grind(g)
        if getattr(g, "live", False) and self.config.current.fast_play:
            steps = g.in_round() or g.default_action().get("type") == "start"
            return await (self._fast_steps(g) if steps else self._auto_session(g))
        refilled = g.balance < 50
        if refilled:                            # demo credits never run out on stream
            g.balance = START_BALANCE
            await self.speak("Demo bank refilled. The sloth economy is very forgiving.", "happy")
        await self.puppet.expression("thinking")
        if not g.in_round() and self._picks_ready(g.id):
            self._apply_picks(g)
            if self._next_round:
                self._next_round.cancel()
                self._next_round = None
        nxt, self._next_round = self._next_round, None
        if nxt and (refilled or getattr(nxt, "game", None) is not g):   # planned against a stale state
            nxt.cancel()
            nxt = None
        # Dead air came from running brain -> safety -> TTS -> playback strictly in series.
        # Instead the move is decided as soon as the brain answers, and the NEXT brain call
        # (and its TTS) runs while the current line plays; the board only reveals the
        # result after the line, so viewers still see think -> say -> act -> reveal.
        if nxt is None:
            nxt = asyncio.create_task(self._think(self.brain.game_turn(g, list(self.recent))))
        try:
            # A live game (the real site) shows each move the instant it's made, so there the
            # line comes first and the next turn is planned after the move; a simulated game
            # resolves the move up front and plans ahead while the line plays.
            live = getattr(g, "live", False)
            hype = None
            for _ in range(40):                 # a round never takes more than 40 actions
                await self._hold()
                await self.maybe_answer_chat()  # chatters come first, even mid-round
                turn, ready = await nxt
                ready = await self._voiced(turn, ready)
                nxt = None
                if hype:                        # a catchphrase replaces this move's comment
                    ready, hype = await self.prepare(hype, checked=True), None
                action = turn["action"] if turn else g.default_action()
                # Any move but starting a multi-step round or cashing out can go by without commentary;
                # the first steps of a round (lane 1-2, floor 1-2...) are low stakes and mostly silent.
                cfg = self.config.current
                early = action.get("type") in ("go", "climb", "reveal", "guess") and self._progress(g) < cfg.quiet_early_steps
                quiet = cfg.quiet_early_chance if early else cfg.quiet_move_chance
                if turn and action.get("type") not in ("start", "cashout") and random.random() < quiet:
                    ready = None
                spec = spec_safe = None
                if live:
                    # The real site only reveals the outcome after the click, so a reaction written
                    # afterwards leaves Hubie frozen for a few seconds. The likely finishing outcome
                    # is reacted to ahead of time, while this line plays, and used if it happens.
                    # Both branches get written ahead; only the likelier one is voiced ahead (voicing
                    # is the slow, costly part), the other is voiced only if it happens.
                    p_bust = g.bust_chance(action) if hasattr(g, "bust_chance") else 0.5
                    guess = self._likely_finish(g, action)
                    if guess:
                        voice = guess["kind"] != "bust" or p_bust >= 0.5
                        spec = (guess["kind"], asyncio.create_task(
                            self._think(self.brain.react(g, guess, list(self.recent)), speculative=True, voice=voice)))
                    # ...and the other branch too: the next move as if this pick finds a gem.
                    preview = g.preview_safe(action) if hasattr(g, "preview_safe") else None
                    if preview:
                        pg, pev = preview
                        spec_safe = (pev, asyncio.create_task(self._think(self.brain.game_turn(
                            pg, list(self.recent) + [f"{g.title}: {json.dumps(pev)}"]),
                            speculative=True, voice=p_bust < 0.5)))
                    if turn:
                        await self.play(ready, turn["expression"], turn["reaction"])
                    await self.puppet.send({"type": "typing", "seconds": 0.6})
                    await asyncio.sleep(0.25)
                event = await self._act(g, action)
                done = event["kind"] in ("bust", "cashout", "win", "loss")
                if live and done:
                    await self.puppet.reaction(self._default_reaction(event))   # flinch right away
                elif live and event["kind"] == "safe":
                    await self.puppet.expression("laugh")                       # big grin on a gem
                    await self.puppet.reaction("nod")
                if event["kind"] == "safe":
                    cfg = self.config.current
                    lines = (cfg.catchphrases or {}).get("safe")
                    if lines and random.random() < cfg.catchphrase_chance:
                        hype = random.choice(lines)
                if ready and not live:          # the next turn is planned before this line plays
                    self.recent.append(f"Hubie said: {ready[0]}")
                self.recent.append(f"{g.title}: {json.dumps(event)}")
                if spec and done and spec[0] == event["kind"]:
                    nxt = spec[1]
                elif done:
                    nxt = asyncio.create_task(self._think(self.brain.react(g, event, list(self.recent))))
                if spec and nxt is not spec[1]:
                    spec[1].cancel()
                if spec_safe and not done and event.get("kind") == "safe" and event.get("tile") == spec_safe[0]["tile"]:
                    nxt = spec_safe[1]                    # planned while the line played
                elif spec_safe:
                    spec_safe[1].cancel()
                if done:
                    # The next round's opening move is planned now too, so it's ready the moment
                    # the reaction line ends (dropped if the game switches or credits refill first).
                    self._next_round = asyncio.create_task(self._think(self.brain.game_turn(g, list(self.recent)),
                                                                       speculative=True))
                    self._next_round.game = g
                elif nxt is None:
                    nxt = asyncio.create_task(self._think(self.brain.game_turn(g, list(self.recent))))
                if not live:
                    if turn:
                        await self.play(ready, turn["expression"], turn["reaction"], noted=True)
                    # Hubie "types" the move: a short burst of hand taps, and the action lands mid-burst.
                    await self.puppet.send({"type": "typing", "seconds": 0.6})
                    await asyncio.sleep(0.25)
                await self.push_game_state(event)
                await asyncio.sleep(0.35)       # let the view animate
                if not done:
                    # A gem keeps the grin until he speaks again; anything else goes back to thinking.
                    await self.puppet.expression("happy" if event["kind"] == "safe" else "thinking")
                if done:
                    record = self._record(event)
                    self._save_state()
                    await self.push_stats()
                    if record:                      # a new stream record always gets its moment
                        if nxt:
                            nxt.cancel()
                        m = float(event["multiplier"])
                        await self._announce_record(g, m)
                        return
                    cfg = self.config.current
                    # One-shot games (dice, limbo, wheel...) would talk after every single roll; an
                    # ordinary result often just gets a face, and the next roll comes sooner.
                    ordinary = event["kind"] in ("win", "loss") and float(event.get("multiplier") or 0) < 3
                    if ordinary and not event.get("streak_broken_at") and (event.get("win_streak") or 0) < 3 \
                            and random.random() < cfg.quiet_result_chance:
                        nxt.cancel()
                        nxt = None
                        return
                    lines = (cfg.catchphrases or {}).get(event["kind"])
                    if lines and random.random() < cfg.catchphrase_chance:
                        nxt.cancel()
                        nxt = None
                        await self.speak(random.choice(lines), "laugh", self._default_reaction(event), checked=True)
                        return
                    r, ready = await nxt
                    ready = await self._voiced(r, ready)
                    nxt = None
                    if r:
                        await self.play(ready, r["expression"], r["reaction"] or self._default_reaction(event))
                    else:
                        await self.puppet.reaction(self._default_reaction(event))
                    return
        finally:
            if nxt and not nxt.done():
                nxt.cancel()

    @staticmethod
    def _progress(g) -> int:
        """Steps taken in the current multi-step round (lanes, floors, gems, right guesses)."""
        r = (g.state() or {}).get("round") or {}
        if not isinstance(r, dict):
            return 0
        return int(r.get("lanes") or r.get("floor") or len(r.get("revealed") or []) or r.get("wins") or 0)

    @staticmethod
    def _likely_finish(g, action: dict) -> dict | None:
        """The round-ending event this action could produce, as far as it's knowable before the
        click: a reveal may bust; a cashout ends at the current multiplier."""
        st = (g.state() or {}).get("round") or {}
        if action.get("type") in ("reveal", "go", "climb"):
            return {"kind": "bust", "lost": 0, "revealed": len(st.get("revealed") or [])}
        if action.get("type") == "roll" and action.get("chance"):     # Dice: react to the likelier result
            c = float(action["chance"])
            return ({"kind": "win", "multiplier": round(99 / c, 2), "payout": 0, "bet": 0} if c >= 50
                    else {"kind": "loss", "lost": 0})
        if action.get("type") == "launch" and action.get("target"):    # Limbo: 2x+ targets lose more often
            t = float(action["target"])
            return {"kind": "loss", "lost": 0} if t >= 2 else {"kind": "win", "multiplier": t, "payout": 0, "bet": 0}
        if action.get("type") == "cashout" and st:
            return {"kind": "cashout", "multiplier": st.get("multiplier"), "payout": 0, "bet": 0, "perfect": False}
        return None

    async def _act(self, g, action: dict) -> dict:
        """One move; real-site games are async (they click and wait for the page)."""
        async def run(a):
            ev = g.act(a)
            return await ev if inspect.isawaitable(ev) else ev
        idle = time.time() - self._act_done
        if 3 < idle < 600:                        # dead air on screen: nothing moving in the game
            log.info("timing: game idle %.1fs before %s", idle, action.get("type"))
        try:
            return await run(action)
        except IllegalAction as e:
            log.warning("illegal action %s (%s); using default", action, e)
            return await run(g.default_action())
        finally:
            self._act_done = time.time()

    @staticmethod
    def _default_reaction(event: dict) -> str:
        m = event.get("multiplier", 0)
        if event["kind"] in ("bust", "loss"):
            return "slump" if event.get("bet", 0) >= 25 else "shake"
        return "pop_up" if m >= 5 else "hat_tip" if m >= 2 else "nod"

    def _available(self, game_id: str) -> bool:
        """Real-site games can be offline (browser closed, page changed); simulated ones never are."""
        if game_id == self._offline_game and time.time() < self._offline_until:
            return False
        if time.time() < self._cooldown.get(game_id, 0):     # switched away from it for failing
            return False
        probe = self.games.create(game_id, 0)
        return getattr(probe, "available", lambda: True)()

    async def switch_game(self, game_id: str, announce=True, reason="admin", votes=None):
        if game_id == self.game.id or game_id not in self.games.classes:
            return
        self._grind_action.pop(game_id, None)            # a new stint on a grind game picks its mode afresh
        self.st.setdefault("played_at", {})[self.game.id] = time.time()   # rotation fairness, kept across restarts
        if not self._available(game_id):
            log.info("not switching to %s: offline", game_id)
            return
        self._save_state()
        self.game = self.games.create(game_id, self.st["balances"].get(game_id, START_BALANCE))
        self.st["game"] = game_id
        self._game_since = time.time()
        self._save_state()
        # The view shows a "CHAT VOTED: <game>" card off this event.
        await self.push_game_state({"kind": "switch", "reason": reason, "votes": votes, "title": self.game.title})
        await self.push_stats()
        if announce:
            why = {"vote": "chat voted, switching to", "fallback": "that game is acting up on the site, so for now switching to",
                   "restored": "the real site is back, switching to",
                   "rotate": "getting restless, so on your own whim you're switching to"}.get(reason, "switching to")
            title = self.game.title

            async def say_it():
                a = await self.brain.announce(f"{why} {title}")
                await self.speak(a["say"] if a else f"Chat has spoken. {title} it is.",
                                 a["expression"] if a else "happy", "hat_tip")
            # Said while the new game loads (in that order it was 15-30 s of nothing on screen).
            self._then(say_it())
        if getattr(self.game, "live", False):
            try:
                await self.game.ensure_page()
            except Exception as e:
                log.info("preloading %s: %s (the first move retries)", game_id, type(e).__name__)

    async def keep_game_online(self) -> bool:
        """True when the current game can play now. A real-site game that failed or lost its
        browser waits (Hubie keeps talking to chat) and is retried every OFFLINE_RETRY_S; with a
        FALLBACK_GAME set, the stream plays that meanwhile instead."""
        g = self.game
        if getattr(g, "broken", None) and time.time() >= self._offline_until:
            # The retry window passed: forget the failure and re-read the board on the next move
            # (a round left open on the page is adopted, not restarted).
            log.info("retrying %s after: %s", g.id, g.broken)
            g.broken, g.round = None, None
        if self._available(g.id):
            if self._waiting_since:
                log.info("%s back online after %.0fs", g.id, time.time() - self._waiting_since)
                self._waiting_since = 0.0
                await self.view.send({"type": "offline", "on": False}, "offline")
            return True
        if FALLBACK_GAME and g.id != FALLBACK_GAME:
            self._fell_back_from = self._fell_back_from or g.id
            log.warning("%s offline; falling back to %s", g.id, FALLBACK_GAME)
            await self.switch_game(FALLBACK_GAME, reason="fallback")
            return True
        # One stuck game (e.g. a round the site won't resolve) shouldn't stall the stream: after it
        # fails twice in a row, play another real-site game and come back to it later.
        others = [gid for gid, cls in self.games.classes.items()
                  if gid != g.id and getattr(cls, "live", False) and cls.command and self._available(gid)]
        if self._fail_count.get(g.id, 0) >= 2 and others:
            alt = random.choice(others)
            log.warning("%s keeps failing; switching to %s, retrying it in %d min", g.id, alt, GAME_COOLDOWN_S // 60)
            self._fail_count[g.id] = 0
            self._cooldown[g.id] = time.time() + GAME_COOLDOWN_S
            self._waiting_since = 0.0
            await self.view.send({"type": "offline", "on": False}, "offline")
            await self.switch_game(alt, reason="fallback")
            return True
        if not self._waiting_since:
            self._waiting_since = time.time()
            # The site's game area is an empty dark box now: cover it until play resumes.
            await self.view.send({"type": "offline", "on": True}, "offline")
            log.warning("%s offline; waiting for it (retry every %ss)", g.id, OFFLINE_RETRY_S)
            await self.speak(random.choice(self.config.current.waiting_lines), "thinking", checked=True)
        return False

    async def game_loop(self):
        # Browser sources and the site extension reconnect a few seconds after a restart; don't
        # declare a real-site game offline before they've had the chance.
        bridge = getattr(self, "bridge", None)
        for _ in range(30):
            if not bridge or bridge.clients or not getattr(self.game, "live", False):
                break
            await asyncio.sleep(0.5)
        await self.push_game_state()
        await self.push_stats()
        while True:
            try:
                await self._hold()                 # paused: no playing, no talking
                if self.config.maybe_reload():           # config.toml edited while live
                    self.safety.apply(self.config.current)
                    self.vote_seconds = self.config.current.vote_window
                for gid in self.games.reload():          # hot-reload new or edited game plugins
                    log.info("game plugin loaded: %s", gid)
                    self.safety.commands = set(self.games.commands()) | set(COMMAND_ALIASES)
                if self.pending_game:
                    gid, self.pending_game = self.pending_game, None
                    reason, votes = self.pending_reason
                    self.pending_reason = ("admin", None)
                    await self.switch_game(gid, reason=reason, votes=votes)
                if not await self.keep_game_online():
                    await self.maybe_answer_chat()
                    await asyncio.sleep(3)
                    continue
                await self.maybe_rotate_game()
                if time.time() - self._page_age_from() > PAGE_REFRESH_S:
                    await self.reload_game_page("routine refresh")
                await self.play_round()
                self._fail_count.pop(self.game.id, None)   # a clean round clears its failure count
                if getattr(self.game, "live", False) and self.config.current.fast_play:
                    self._business()
                    await asyncio.sleep(0.3)
                    continue
                await self.maybe_run_segment()
                await self.maybe_comment_music()
                await self.maybe_answer_chat()
                await asyncio.sleep(random.uniform(0.3, 0.9))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("game loop error; continuing")
                if getattr(self.game, "broken", None):
                    self._offline_game, self._offline_until = self.game.id, time.time() + OFFLINE_RETRY_S
                    # One strike per real failed attempt; the second switches games (a one-off page
                    # reload or network blip gets a retry on the same game first).
                    self._fail_count[self.game.id] = self._fail_count.get(self.game.id, 0) + 1
                await asyncio.sleep(3)

    # ---- chat -----------------------------------------------------------------------
    async def chat_loop(self):
        while True:
            msg: ChatMessage = await self.chat_q.get()
            asyncio.create_task(self._handle_chat(msg))

    BOT_COMMANDS = ("!commands", "!games", "!record", "!uptime", "!song", "!track", "!music", "!next")
    # Verdicts that mean "this shouldn't be in chat at all": the bot removes them (never "steering",
    # whose false positives would delete normal messages).
    REMOVE = ("link_or_contact", "prompt_injection", "harassment_hate", "sexual_minors", "spam")

    def _bot_reply(self, cmd: str):
        if not self.kick or time.time() - self._botcmd_at.get(cmd, 0) < 20:
            return
        self._botcmd_at[cmd] = time.time()
        custom = (self.config.current.bot_commands or {}).get(cmd)
        if custom:
            self.kick.say(custom)
            return
        if cmd == "!next":
            nxt = self.up_next
            self.kick.say(f"Up next: {nxt['artist']} - {nxt['title']}" if nxt and nxt.get("artist") else
                          f"Up next: {nxt['title']}" if nxt else "Nothing queued up yet.")
            return
        if cmd in ("!song", "!track", "!music"):
            self.kick.say(music.credit(self.now_playing) if self.now_playing else "No music playing right now.")
            return
        if cmd == "!uptime":
            mins = int((time.time() * 1000 - self.st["autonomy_since"]) / 60000)
            self.kick.say(f"Hubie has been running on his own for {mins // 60}h {mins % 60}m with no human input.")
            return
        # Kick rejects messages with many special characters (MAX_SPECIAL_CHARS_ERROR), so game names
        # are listed as plain words, with a single "!" explaining the syntax.
        # Listed with their "!" as viewers type them; if Kick refuses the message for too many special
        # characters (MAX_SPECIAL_CHARS_ERROR), the bot resends it stripped (kickbot.run).
        cmds = [c.command for c in self.games.classes.values() if c.command]
        games = " ".join(cmds)
        best = self._stream_stats()["best"]
        # Kick rejects messages with many "!" (MAX_SPECIAL_CHARS_ERROR), so game names are plain words.
        order = ["tower", "road", "maxwin", "plinko", "wheel", "dice", "flip", "keno", "limbo", "hilo"]
        names = [c.lstrip("!") for c in cmds]
        names = [n for n in order if n in names] + [n for n in names if n not in order]
        if cmd == "!commands":
            # (Kick strips "<...>" as HTML, so "!<game>" would arrive as "!")
            # Kick chat has no line breaks: two messages read as two lines.
            self.kick.say("Vote for a game by typing !game, like !dice.")
            self.kick.say(f"Games: {', '.join(names)}")
            self.kick.say("Also: !record, !uptime, !song, !next, !skip, !housebets, !selv, !socials, !rules. Say Hubie's name and he talks back.")
            return
        self.kick.say({
            "!games": f"Hubie plays real Housebets games: {games}. The first vote opens a 60 second poll.",
            "!record": f"Stream record: {best:g}x. Hubie is chasing it on Hubie's Road and HiLo.",
        }[cmd])

    async def _moderate(self, msg: ChatMessage, reason: str):
        """Remove what the filters reject from Kick's own chat; repeat offenders get a timeout."""
        if not (self.kick and self.kick.ready and self.config.current.kick_moderate) or msg.staff:
            return
        if not reason.startswith(self.REMOVE):
            return
        if await self.kick.delete(msg.id):
            log.info("kick bot removed %s's message (%s)", msg.user, reason)
        now = time.time()
        hits = [t for t in self._strikes.get(msg.user, []) if now - t < 600] + [now]
        self._strikes[msg.user] = hits
        if len(hits) >= 3 and msg.user_id:
            if await self.kick.timeout(msg.user_id, 10, f"Hubie's filter: {reason.split(':')[0]}"):
                log.info("kick bot timed out %s for 10 min", msg.user)
                self._strikes[msg.user] = []

    async def _handle_chat(self, msg: ChatMessage):
        # The bot account's own posts (commands, vote/record updates) aren't viewer chat: not shown
        # on stream, not answered, not counted as votes.
        if msg.user.lower() in self.config.current.ignore_users:
            return
        msg.text = EMOTE.sub(r"\1", msg.text)          # Kick emotes "[emote:37226:KEKW]" -> "KEKW"
        first = msg.text.strip().split()[0].lower() if msg.text.strip() else ""
        if first == "!skip":
            await self._skip(msg)
            return
        if first in self.BOT_COMMANDS or first in (self.config.current.bot_commands or {}):
            self._bot_reply(first)
            return
        if not msg.id:
            msg.id = f"m{time.time_ns()}"        # lets the HUD highlight this line later
        try:
            v = await self.safety.check_chat(msg)
        except Exception:
            log.exception("safety check crashed; dropping message")
            return
        log.info("chat %-10s %-22s %s: %s", v.action, v.reason, msg.user, msg.text[:60])
        if v.action == "ignore":
            asyncio.create_task(self._moderate(msg, v.reason))
        if v.action == "command":
            cmd, _, arg = v.reason.partition(" ")
            cmd = COMMAND_ALIASES.get(cmd, cmd)
            self._vote(msg.user, cmd)
            self._pick(msg.user, cmd, arg)
            await self.hud.send({"type": "chat", "id": msg.id, "user": msg.user, "text": v.reason,
                                 "vote": bool(self.games.commands().get(cmd))}, keep=True)
            await self._push_votes()
        elif v.action in ("reply", "show"):
            self.chat_log.append((msg.user.lower(), f"{msg.user}: {normalize(msg.text)[:120]}", time.time()))
            self._remember(msg.user, f"{msg.user}: {normalize(msg.text)[:160]}")
            await self.hud.send({"type": "chat", "id": msg.id, "user": msg.user, "text": normalize(msg.text)}, keep=True)
            wanted = game_request(msg.text)
            cmd = getattr(self.games.classes.get(wanted), "command", "") if wanted else ""
            if cmd and wanted != self.game.id:
                self._vote(msg.user, cmd)                 # the vote opens (and is announced) as if typed
                await self._push_votes()
                log.info("chat game ask: %s from %s", wanted, msg.user)
                return
            spots = tile_requests(msg.text) if self.game.id == "mines" else []
            if spots:
                # "Click the top corners first": he does it (the next reveals) and says so, instead of
                # just talking about it.
                until = time.time() + 60
                self._tile_asks.extend((pos, until) for _, pos in spots)
                said = " then ".join(dict.fromkeys(name for name, _ in spots))
                self._then(self.speak(f"{said[:1].upper()}{said[1:]}, {msg.user}'s call.", "focused", "nod"))
                log.info("chat tiles: %s from %s", [pos for _, pos in spots], msg.user)
                return
            if v.action == "reply":
                self._queue_reply(msg, v)
        elif v.action == "info":
            await self.hud.send({"type": "chat", "id": msg.id, "user": msg.user, "text": v.reason}, keep=True)
            now = time.time()
            reply = self.config.current.commands.get(v.reason)
            if reply and now - self.command_last.get(v.reason, 0) >= self.config.current.command_cooldown:
                self.command_last[v.reason] = now
                if self.kick:
                    self.kick.say(reply)
                await self.speak(reply, "happy", checked=True)   # our own fixed text
        elif v.action == "canned":
            async with self.speak_lock:
                pass                                       # wait for any current line to finish
            await self.speak(v.canned, "neutral", checked=True)

    def _segment_due(self) -> bool:
        now = time.time()
        return any(now - self.st["segments_at"].get(seg.id, now) >= seg.every_minutes * 60
                   for seg in self.config.current.segments if not seg.game or seg.game == self.game.id)

    async def maybe_run_segment(self):
        """Between rounds, play the first segment that is due (ads, reminders, bits)."""
        now = time.time()
        for seg in self.config.current.segments:
            if seg.game and seg.game != self.game.id:
                continue                                 # e.g. "pick my mines" only during Mines
            # A newly added segment first plays one interval later, not the moment it appears.
            if now - self.st["segments_at"].setdefault(seg.id, now) < seg.every_minutes * 60:
                continue
            self.st["segments_at"][seg.id] = now
            self._save_state()
            log.info("segment: %s", seg.id)
            if seg.image:
                await self.view.send({"type": "segment", "image": "/" + seg.image, "seconds": seg.seconds})
            if seg.chat and self.kick:
                self.kick.say(seg.chat)
            started = time.time()
            await self.speak(BAG.draw(seg.say), seg.expression, seg.reaction, checked=True)
            await asyncio.sleep(max(0.0, seg.seconds - (time.time() - started)))
            return

    def _queue_reply(self, msg, v):
        """Start writing (and voicing) the reply as soon as a message qualifies, so it's ready by
        Hubie's next gap instead of costing a few seconds of dead air then."""
        key = msg.user.lower()
        thread = list(self.threads.get(key, ()))[:-1]       # [-1] is this message, passed on its own
        others = [line for _, line, _ in list(self.chat_log)[:-1]][-6:]   # [-1] is this message
        task = asyncio.create_task(self._think(self.brain.chat_reply(
            msg.user if v.speak_name else None, normalize(msg.text), self.game.title,
            list(thread), others, list(self.recent))))
        self.candidates.append((v.score, time.time(), msg, v, task))
        if len(self.candidates) > 12:                       # a busy chat: keep the best dozen
            worst = min(self.candidates, key=lambda c: (c[0], c[1]))
            worst[4].cancel()
            self.candidates.remove(worst)

    @staticmethod
    def _load_threads() -> collections.OrderedDict:
        """Viewers' conversations survive restarts, so a regular is still a regular."""
        out = collections.OrderedDict()
        try:
            for user, lines in json.loads(VIEWERS_FILE.read_text()).items():
                out[user] = collections.deque(lines, maxlen=30)
        except (OSError, ValueError, AttributeError):
            pass
        return out

    def _save_threads(self):
        try:
            tmp = VIEWERS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({u: list(t) for u, t in self.threads.items()}))
            os.replace(tmp, VIEWERS_FILE)
        except OSError as e:
            log.warning("couldn't save viewer memory: %s", e)

    def _remember(self, user: str, *lines: str):
        """This viewer's whole conversation with Hubie: everything they said (answered or not) and
        everything he said back, with the time, so a reply can pick up the thread."""
        key = user.lower()
        thread = self.threads.pop(key, None) or collections.deque(maxlen=30)
        stamp = time.strftime("%a %H:%M")             # the day too: memory now outlives a restart
        thread.extend(f"[{stamp}] {line}" for line in lines)
        self.threads[key] = thread                         # most recent last
        while len(self.threads) > 300:                     # forget the longest-quiet viewers
            self.threads.popitem(last=False)
        self._save_threads()

    async def maybe_answer_chat(self):
        """Answer the best waiting chat message (between moves and between rounds), at most every
        N seconds. Unanswered messages wait their turn for up to 90 s."""
        if time.time() - self.last_chat_reply < self.config.current.chat_reply_every:
            return
        now = time.time()
        for c in [c for c in self.candidates if now - c[1] >= 90]:
            c[4].cancel()
            self.candidates.remove(c)
        if not self.candidates:
            return
        best = max(self.candidates, key=lambda c: (c[0], c[1]))
        self.candidates.remove(best)
        _, _, msg, v, task = best
        self.last_chat_reply = time.time()
        await self.hud.send({"type": "answering", "id": msg.id})     # highlight the line already shown
        try:
            r, ready = await asyncio.wait_for(task, 15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return
        if r:
            self.recent.append(f"Chat {msg.user}: {normalize(msg.text)[:80]}")
            if await self.play(ready, r["expression"]):
                self._remember(msg.user, f"You (out loud, replying to that): {ready[0]}")

    # ---- votes ----------------------------------------------------------------------
    def _pick(self, user: str, command: str, arg: str):
        """Chat choosing a game setting, e.g. "!mines 10" -> next Mines round uses 10 mines.
        Games opt in with chat_setting = (param, command, lo, hi)."""
        gid = self.games.commands().get(command)
        spec = getattr(self.games.classes.get(gid), "chat_setting", None) if gid else None
        if not spec or not arg.isdigit() or not spec[2] <= int(arg) <= spec[3]:
            return
        self.picks.setdefault(gid, {})[user] = (int(arg), time.time())   # one vote per viewer: their latest
        # The first request opens a short vote instead of winning outright (a Mines round can be over in
        # two seconds, so "whatever came in since the last round" was just whoever typed last).
        if gid not in self._pick_open:
            self._pick_open[gid] = time.time()
            if self.game.id == gid:
                self._pick_open[gid] = float("inf")     # the clock starts when he announces it (may queue)

                async def announce():
                    self._pick_open[gid] = time.time()
                    await self.speak(f"{spec[0].capitalize()} vote! Type bang {spec[0]} and a number. "
                                     f"{PICK_WINDOW_S} seconds.", "happy", "lean_in", checked=True)
                self._then(announce())
        if self.game.id == gid and self._next_round:     # the planned next round didn't know this yet
            self._next_round.cancel()
            self._next_round = None

    def _picks_ready(self, gid: str) -> bool:
        opened = self._pick_open.get(gid)
        return bool(self.picks.get(gid)) and opened is not None and time.time() - opened >= PICK_WINDOW_S

    def _apply_picks(self, g):
        """At a round's start: the most-requested value wins (ties: the latest request)."""
        picks = self.picks.pop(g.id, None)
        self._pick_open.pop(g.id, None)
        spec = getattr(g, "chat_setting", None)
        if not picks or not spec:
            return
        counts: dict[int, list] = {}
        for value, at in picks.values():
            counts.setdefault(value, []).append(at)
        value = max(counts, key=lambda v: (len(counts[v]), max(counts[v])))
        g.chat_pick = {spec[0]: value}
        # A pick holds for a few minutes (one round was gone in a click at 20 mines, so it looked
        # ignored), and it's always announced by name, queued so it can't be dropped.
        who = [u for u, (v, _) in picks.items() if v == value]
        self._sticky_pick[g.id] = (g.chat_pick, time.time() + PICK_HOLD_S)
        if len(who) == 1:
            line = f"{value} {spec[0]}, {who[0]}'s call. Let's go."   # names: safety-checked (not checked=True)
        else:
            line = f"Chat has spoken: {value} {spec[0]}, {len(who)} votes. Let's go."
        self._then(self.speak(line, "happy", "lean_in"))
        self.recent.append(f"Chat picked {value} {spec[0]} for this round ({len(counts[value])} requests). "
                           f"Call it out and play it.")
        log.info("chat pick: %s=%s %s", spec[0], value, {v: len(a) for v, a in counts.items()})

    def _vote(self, user: str, command: str):
        gid = self.games.commands().get(command)
        if not gid:
            return
        for voters in self.votes.values():
            voters.discard(user)                           # one vote per user per window
        self.votes.setdefault(gid, set()).add(user)

    async def vote_loop(self):
        """Voting opens with the first !<game> vote: a countdown starts, Hubie announces it, the HUD
        shows the live tally, and the winner plays next. (Fixed rolling windows left voters guessing.)"""
        while True:
            self.votes = {gid: set() for gid in self.games.classes}
            self.vote_ends = 0.0
            await self._push_votes()
            while not any(self.votes.values()):
                await asyncio.sleep(0.5)
            self.vote_ends = time.time() + self.vote_seconds
            first = next(g for g, v in self.votes.items() if v)
            title = self.games.classes[first].title.title()
            secs = int(self.vote_seconds)
            when = f"{secs // 60} minute{'s' if secs >= 120 else ''}" if secs >= 60 else f"{secs} seconds"
            line = (f"A vote to stay on {title}. Anyone want something else? Voting closes in {when}."
                    if first == self.game.id else
                    f"Ooh, a vote for {title}! Anyone else? Voting closes in {when}.")
            asyncio.create_task(self.speak(line, "happy", checked=True))
            if self.kick:
                self.kick.say(f"Voting is open for {secs} seconds. {self.games.classes[first].title.title()} "
                              f"has the first vote. Type ! and a game name to vote.")
            while time.time() < self.vote_ends:
                await self._push_votes()
                await asyncio.sleep(1)
            tally = {g: len(v) for g, v in self.votes.items()}
            winner = max(tally, key=tally.get)
            log.info("vote winner: %s %s", winner, {g: n for g, n in tally.items() if n})
            if winner != self.game.id:
                self.pending_game = winner
                self.pending_reason = ("vote", tally[winner])
            if self.kick:
                self.kick.say(f"Chat picked {self.games.classes[winner].title.title()} "
                              f"({tally[winner]} vote{'s' if tally[winner] != 1 else ''}).")
            self.vote_ends = 0.0
            await self._push_votes(result=winner)
            await asyncio.sleep(3)

    async def _push_votes(self, result: str | None = None):
        await self.hud.send({"type": "vote", "open": self.vote_ends > 0,
                             "ends_in": max(0, round(self.vote_ends - time.time())) if self.vote_ends else None,
                             "result": self.games.classes[result].title if result else None,
                             "options": [{"cmd": self.games.classes[g].command, "votes": len(v)}
                                         for g, v in self.votes.items() if self.games.classes[g].command]}, "vote")

    async def maybe_rotate_game(self):
        """Variety without chat: after a while on one game (and no vote in progress), pick another."""
        mins = self.config.current.rotate_minutes
        if not mins or time.time() - self._game_since < mins * 60 or any(self.votes.values()):
            return
        others = [gid for gid, cls in self.games.classes.items()
                  if gid != self.game.id and getattr(cls, "live", False) and cls.command and self._available(gid)]
        if others:
            # The least recently played few, so every game comes around (pure random left some out for hours).
            played = self.st.get("played_at", {})
            others.sort(key=lambda gid: (played.get(gid, 0), random.random()))
            alt = random.choice(others[:3])
            log.info("rotating games: %s -> %s", self.game.id, alt)
            await self.switch_game(alt, reason="rotate")
        else:
            self._game_since = time.time()

    def _page_age_from(self) -> float:
        """When the game page last loaded: our own reloads, or any load the extension reported."""
        return max(self._page_loaded_at, getattr(getattr(self, "bridge", None), "last_load", 0.0))

    async def reload_game_page(self, why: str):
        """Reload the real-site game page in place (what a manual refresh does); a round left open
        on it is adopted when play resumes."""
        g = self.game
        bridge = getattr(self, "bridge", None)
        if not getattr(g, "live", False) or not bridge or not bridge.clients:
            return
        log.warning("reloading the %s page: %s", g.id, why)
        self._page_loaded_at = time.time()
        try:
            r = await bridge.request({"type": "navigate", "url": g.url}, timeout=30)
            if not r or not r.get("ok"):
                log.warning("page reload failed: %s", (r or {}).get("error"))
            await asyncio.sleep(3)
            await bridge.request({"type": "stream_mode", "on": True})
        except Exception as e:
            log.warning("page reload failed: %s", type(e).__name__)

    async def screen_watch_loop(self):
        """Catch the game window going dead on stream and refresh the page, like a human would.
        A dead window gives byte-identical captures; a quiet live game (Plinko between balls) still
        differs a little, so only near-zero change counts as frozen.
        Two symptoms: the capture turns black, or it freezes (Chrome stops painting the window: OBS
        shows black or a stuck frame while the game plays on, and a live game is never pixel-still
        for a minute and a half)."""
        black = still = 0
        last = None
        while True:
            await asyncio.sleep(SCREEN_CHECK_S)
            if not getattr(self.game, "live", False) or not self.paused.is_set():
                black = still = 0
                last = None
                continue
            # After Housebets reloads its own page, the game is usually left half-drawn until it's
            # reloaded once more (seen twice 2026-11-09): do that now instead of waiting ~90 s for
            # the frozen-screen check to notice.
            bridge = getattr(self, "bridge", None)
            if bridge and bridge.site_reload_at and bridge.site_reload_at > self._handled_site_reload:
                self._handled_site_reload = bridge.site_reload_at
                if time.time() - bridge.site_reload_at < 120:
                    await self.reload_game_page("the site reloaded the page itself; settling it with a clean reload")
                    black = still = 0
                    last = None
                    continue
            snap = await asyncio.to_thread(screenwatch.snapshot, getattr(bridge, "stream_bounds", None))
            if snap is None:
                continue
            level, thumb = snap
            black = black + 1 if level < BLACK_LEVEL else 0
            still = still + 1 if last is not None and screenwatch.changed(thumb, last) < 0.01 else 0
            last = thumb
            if black >= 6 or still >= 9:              # ~60 s black / ~90 s frozen at 10 s checks
                why = (f"the captured window is black (brightness {level:.1f})" if black >= 2 else
                       "the captured window froze (no change in 90 s while playing)")
                black = still = 0
                last = None
                # A reload fixes a stalled page, not a window Chrome has stopped drawing: after two
                # reloads that didn't help, back off (each one interrupts play) and say so in the log.
                now = time.time()
                self._screen_reloads = [t for t in self._screen_reloads if now - t < 900]
                if len(self._screen_reloads) >= 2:
                    log.warning("game window still dead after %d reloads; not reloading again for a while "
                                "(Chrome may have stopped drawing it: restart its GPU process)", len(self._screen_reloads))
                    await asyncio.sleep(600)
                    continue
                self._screen_reloads.append(now)
                await self.reload_game_page(why)

    def _short_title(self, g) -> str:
        return re.sub(r"^HOUSEBETS\s+", "", g.title).title().replace("'S", "'s")

    async def _maybe_clip(self, g, ev: dict, record: bool = False) -> bool:
        """A huge hit (or a new stream record): save the moment as clips, put "CLIPPED" on screen, tell
        chat. Returns True when it clipped, so the reaction line can hype it."""
        m = float(ev.get("multiplier") or 0)
        won = ev.get("kind") in ("win", "cashout")
        if not (record or (won and m >= getattr(g, "clip_x", CLIP_MIN_X))) or not self.clipper.ready():
            return False
        caption = f"Hubie hits {m:,.0f}x on {self._short_title(g)}!" if m >= 10 else f"Hubie hits {m:g}x on {self._short_title(g)}!"
        log.info("clipping: %s", caption)
        await self.hud.send({"type": "follow", "kicker": "Clipped", "user": caption.rstrip("!")})
        if self.kick:
            self.kick.say(f"Clipped: {caption}")
        self.recent.append(f"Clipped for the highlight reel: {caption}")
        asyncio.create_task(self.clipper.clip(caption))
        return True

    async def maybe_comment_music(self):
        """Between rounds, now and then: a word about the track that just came on."""
        t, self._music_comment = self._music_comment, None
        if not t or t is not self.now_playing or time.time() - self._music_comment_at < MUSIC_COMMENT_GAP_S:
            return
        self._music_comment_at = time.time()
        name = f"{t['title']} by {t['artist']}" if t.get("artist") else t["title"]
        genre = f" ({t['genre']})" if t.get("genre") else ""
        try:
            r = await asyncio.wait_for(self.brain.announce(
                f"The stream music just switched to {name}{genre}. React to the track in one short, casual line, "
                "like a streamer vibing to it (it's a NoCopyrightSounds track; don't read the full credit)."), 15)
        except Exception:
            return
        if r and r.get("say"):
            await self.speak(r["say"], r.get("expression") or "happy")

    async def _skip(self, msg: ChatMessage):
        """!skip: a vote among chatters (mods and the broadcaster skip alone). Works even before the
        player has reported a track (e.g. right after a restart)."""
        self._skip_votes.add(msg.user.lower())
        need = self.config.current.music_skip_votes
        if msg.staff or len(self._skip_votes) >= need:
            log.info("music: skipped by %s", ", ".join(sorted(self._skip_votes)))
            self._skip_votes.clear()
            await self.hud.send({"type": "music_skip"})
            if self.kick:
                self.kick.say("Chat skipped the track.")
        elif self.kick and len(self._skip_votes) == 1:
            self.kick.say(f"{msg.user} wants to skip this track. {need - 1} more !skip to skip it.")

    async def set_now_playing(self, track: dict | None, up_next: dict | None = None):
        self.up_next = up_next or self.up_next
        if track and self.now_playing and track["file"] == self.now_playing["file"]:
            return                                       # the page re-announcing the same track
        self.now_playing = track
        self._skip_votes.clear()
        if track and random.random() < MUSIC_COMMENT_CHANCE:
            self._music_comment = track
        if track:
            log.info("music: %s - %s", track["artist"], track["title"])
            self.recent.append(f"Music now playing: {track['artist']} - {track['title']}")
        await self.hud.send({"type": "music", "artist": (track or {}).get("artist", ""),
                             "title": (track or {}).get("title", "")}, "music")

    async def follow_loop(self):
        """Thank new followers out loud, on screen and in chat. Follows that land close together
        are thanked in one line. A named follow also bumps the follower count, so `credit` keeps a
        count-only follow from being thanked twice."""
        credit = 0
        while True:
            items = [await self.follows.get()]
            await asyncio.sleep(6)                              # gather a burst
            while not self.follows.empty():
                items.append(self.follows.get_nowait())
            names, anon = [], 0
            for kind, val in items:
                if kind == "name":
                    credit += 1
                    name = re.sub(r"[^A-Za-z0-9_]", "", str(val))[:25]
                    if name and name.lower() not in self._thanked:
                        self._thanked.add(name.lower())
                        names.append(name)
                else:
                    used = min(credit, val)
                    credit -= used
                    anon += val - used
            if not names and not anon:
                continue
            log.info("new followers: %s%s", names, f" +{anon} unnamed" if anon else "")
            for n in names:
                await self.hud.send({"type": "follow", "user": n})
            if anon and not names:
                await self.hud.send({"type": "follow", "user": None, "count": anon})
            who = ", ".join(names) if names else None
            if self.kick:
                self.kick.say(f"Thanks for the follow, {who}!" if who else "Thanks for the follow!")
            self.recent.append(f"New follower{'s' if len(names) + anon > 1 else ''}: {who or 'someone'}")
            what = (f"{who} just followed the stream. Thank them by name, in your style." if who else
                    "Someone just followed the stream. Thank the new follower, in your style.")
            try:
                r = await asyncio.wait_for(self.brain.announce(what), 15)
            except Exception:
                r = None
            line = r["say"] if r and r.get("say") else (f"Thanks for the follow, {who}!" if who else "Thanks for the follow!")
            await self.speak(line, (r or {}).get("expression") or "happy")

    async def run(self, sources: list):
        # A fresh start isn't waiting on the site: clear a "be right back" card a previous run left up.
        await self.view.send({"type": "offline", "on": False}, "offline")
        asyncio.get_running_loop().call_later(15, lambda: asyncio.ensure_future(self._mute_game(False)))
        if self.clipper.enabled:
            asyncio.get_running_loop().call_later(5, lambda: asyncio.ensure_future(self.clipper.ensure_buffer()))
        else:
            log.info("auto-clips off (no OBS_WS_PASSWORD)")
        await self.hud.send({"type": "autonomy", "since": self.st["autonomy_since"]}, "autonomy")
        asyncio.create_task(self._voice_blurts())
        tasks = [asyncio.create_task(t) for t in
                 (self.game_loop(), self.chat_loop(), self.vote_loop(), self.follow_loop(), self.screen_watch_loop(), *[s.run() for s in sources],
                  *[s.viewer_loop(self.hud) for s in sources if hasattr(s, "viewer_loop")],
                  *([self.kick.run()] if self.kick and self.kick.configured else []))]
        if self.kick and self.kick.configured and not self.kick.ready:
            log.warning("kick bot not signed in yet: open http://localhost:8000/kick/login as the bot account")
        await asyncio.gather(*tasks)
