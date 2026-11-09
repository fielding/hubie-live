"""Hubie show runner.

    VENICE_API_KEY=... uv run --with websockets --with httpx --with requests \
        python -m showrunner.main --chat sim

Serves the browser sources on http://127.0.0.1:8000 (puppet, game view, HUD, banner, the
live stage), the puppet/game/HUD sockets on 8765/8766/8767, and a local admin API:

    curl -s -H "X-Admin-Token: $(cat showrunner/.admin_token)" 127.0.0.1:8000/admin/say -d '{"text":"hi"}'
    endpoints: say, pause, resume, game {"id"}, reload, chat {"user","text"} (inject a test
    message through the full safety pipeline), status
Every admin action counts as human input and restarts the HUD's autonomy clock (except status).
"""
import argparse
import asyncio
import functools
import http.server
import html
import json
import logging
import secrets
import threading
import urllib.parse
from pathlib import Path

from . import music
from .chat import KickChat, SimChat
from .games import base as games_base
from .kickbot import KickBot
from .links import BridgeLink, Broadcast, PuppetLink, serve
from .runner import ROOT, Runner
from .safety import ChatMessage

log = logging.getLogger("main")
TOKEN_FILE = ROOT / "showrunner" / ".admin_token"
MUSIC_DIR = ROOT / "assets" / "music"


def admin_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(tok)
    TOKEN_FILE.chmod(0o600)
    return tok


class Handler(http.server.SimpleHTTPRequestHandler):
    runner: Runner = None
    loop: asyncio.AbstractEventLoop = None
    token = ""

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/admin/") or self.path.startswith("/showrunner/.") :
            return self.send_error(404)
        if self.path.startswith("/kick/"):
            return self._kick()
        if self.path.split("?")[0] == "/music/playlist.json":
            cfg = self.runner.config.current
            return self._json(200, {"volume": cfg.music_volume, "duck_to": cfg.music_duck,
                                    "tracks": music.tracks(MUSIC_DIR)})
        super().do_GET()

    def _kick(self):
        """One-time Kick bot sign-in: /kick/login sends the browser to Kick, /kick/callback finishes."""
        bot = getattr(self.runner, "kick", None)
        if not bot or not bot.configured:
            return self._page(500, "Kick bot not configured (KICK_CLIENT_ID / KICK_CLIENT_SECRET missing).")
        url = urllib.parse.urlparse(self.path)
        if url.path == "/kick/login":
            self.send_response(302)
            self.send_header("Location", bot.login_url())
            self.end_headers()
            return
        if url.path == "/kick/callback":
            q = dict(urllib.parse.parse_qsl(url.query))
            if "error" in q:
                return self._page(400, f"Kick said: {q.get('error')} {q.get('error_description', '')}")
            try:
                asyncio.run_coroutine_threadsafe(bot.finish_login(q.get("code", ""), q.get("state", "")),
                                                 self.loop).result(30)
            except Exception as e:
                return self._page(400, f"Sign-in failed: {e}")
            return self._page(200, "Hubie's Kick bot is signed in. You can close this tab.")
        self.send_error(404)

    def _page(self, code: int, text: str):
        data = f"<!doctype html><meta charset=utf-8><body style='font:16px system-ui;padding:40px'>{html.escape(text)}".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/music/now":
            # From the stage page: which track just started. Only a file in the music folder is
            # accepted (any web page could POST to localhost; this way it can't put text in chat).
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                name, nxt = str(body.get("file", "")), str(body.get("next", ""))
            except (ValueError, AttributeError):
                return self._json(400, {"ok": False})
            known = {t["file"]: t for t in music.tracks(MUSIC_DIR)}
            track = known.get(name)
            asyncio.run_coroutine_threadsafe(self.runner.set_now_playing(track, known.get(nxt)), self.loop)
            return self._json(200, {"ok": bool(track)})
        if not self.path.startswith("/admin/"):
            return self.send_error(404)
        if not secrets.compare_digest(self.headers.get("X-Admin-Token", ""), self.token):
            return self._json(403, {"ok": False, "error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(min(n, 16384)) or b"{}")
            result = asyncio.run_coroutine_threadsafe(self._admin(self.path[7:], body), self.loop).result(90)
            self._json(200, {"ok": True, **(result or {})})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)[:200]})

    async def _admin(self, cmd: str, body: dict):
        r = self.runner
        if cmd == "status":
            return {"game": r.game.id, "balance": r.game.balance, "paused": not r.paused.is_set(),
                    "puppets": len(r.puppet.clients), "views": len(r.view.clients), "huds": len(r.hud.clients),
                    "games": sorted(r.games.classes), "puppet_heartbeat": r.puppet.last_heartbeat,
                    "bridge": len(r.bridge.clients), "bridge_frames": sorted(r.bridge.snapshots)}
        if cmd == "act":                     # dev: one move of a real-site game, no speech, no stats
            gid = str(body.get("game"))
            for changed in r.games.reload():         # edited plugins: fresh test instances
                r.dev_games.pop(changed, None)
            g = r.dev_games.get(gid) or r.games.create(gid, 0)
            r.dev_games[gid] = g
            ev = g.act(body.get("action") or g.default_action())
            ev = await ev if asyncio.iscoroutine(ev) else ev
            return {"event": ev, "state": g.state()}
        # Upkeep of the browser/extension doesn't steer the show, so it doesn't reset the "no human
        # input" clock. Anything that acts on the game or on what Hubie says still does.
        if cmd == "bridge" and body.get("type") in ("probe", "query", "status", "stream_mode",
                                                    "stream_window", "reload_extension"):
            if body.get("type") == "probe":
                return {"sent": await r.bridge.command(body)}
            return {"result": await r.bridge.command(body)}
        r.human_touched()
        if cmd == "say":
            asyncio.create_task(r.speak(str(body.get("text", ""))[:300], body.get("expression")))
        elif cmd == "pause":
            r.paused.clear()
        elif cmd == "resume":
            r.paused.set()
        elif cmd == "game":
            r.pending_game = str(body.get("id"))
        elif cmd == "reload":
            return {"reloaded": r.games.reload()}
        elif cmd == "bridge":                   # raw click/set on the Housebets page
            return {"result": await r.bridge.command(body)}
        elif cmd == "chat":
            r.chat_q.put_nowait(ChatMessage(str(body.get("user", "tester"))[:25], str(body.get("text", ""))[:300], "admin"))
        else:
            raise ValueError(f"unknown admin command {cmd}")

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class QuietHTTPServer(http.server.ThreadingHTTPServer):
    """Browsers drop connections mid-file all the time (an audio element reading ahead, a page
    reload); that's not an error worth a traceback in the show log."""
    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


async def amain(a):
    puppet, view, hud = PuppetLink(), Broadcast("game-view"), Broadcast("hud", ROOT / "showrunner" / ".hud_history.json")
    bridge = BridgeLink(ROOT / "showrunner" / ".probe")
    runner = Runner(puppet, view, hud, vote_seconds=a.vote_seconds)
    runner.bridge = bridge
    runner.kick = KickBot(a.kick_slug, ROOT / "showrunner" / ".kick_tokens.json",
                          f"http://localhost:{a.http_port}/kick/callback")
    if a.paused:                             # dev work on the site: connected, but Hubie stays idle
        runner.paused.clear()
    games_base.SITE_BRIDGE = bridge          # real-site games (games/housebets_*.py) play through it
    servers = [await serve(puppet, 8765), await serve(view, 8766), await serve(hud, 8767),
               await serve(bridge, 8768)]

    Handler.runner, Handler.loop, Handler.token = runner, asyncio.get_running_loop(), admin_token()
    httpd = QuietHTTPServer(("127.0.0.1", a.http_port),
                                            functools.partial(Handler, directory=str(ROOT)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("serving http://127.0.0.1:%d/stage/live.html (admin token in %s)", a.http_port,
             TOKEN_FILE.relative_to(ROOT))

    sources = []
    if a.chat == "sim":
        sources.append(SimChat(runner.chat_q, every=a.sim_every))
    elif a.chat == "kick":
        sources.append(KickChat(runner.chat_q, a.kick_channel, runner.follows))
    try:
        await runner.run(sources)
    finally:
        for s in servers:
            s.close()
        httpd.shutdown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", choices=["sim", "kick", "none"], default="sim")
    ap.add_argument("--kick-channel", type=int, default=949, help="Kick channel id (fielding = 949)")
    ap.add_argument("--sim-every", type=float, default=4.0)
    ap.add_argument("--vote-seconds", type=int, default=0, help="override config.toml pacing.vote_window_seconds")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--kick-slug", default="fielding", help="channel name the Kick bot posts to")
    ap.add_argument("--paused", action="store_true", help="start paused (resume via /admin/resume)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("websockets", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(amain(a))


if __name__ == "__main__":
    main()
