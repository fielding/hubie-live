"""WebSocket links to the browser sources: the puppet (8765), the game view (8766) and
the HUD (8767). The pages connect to us and reconnect on their own, so the show runner
can restart (or be swapped for a new version) without touching OBS."""
import asyncio
import base64
import collections
import itertools
import json
import time
import logging

import websockets

log = logging.getLogger("links")


class Broadcast:
    """Fan-out to every connected page of one kind; remembers the last message of each
    type so a page that (re)connects gets the current picture immediately."""

    def __init__(self, name: str, history_file=None):
        self.name = name
        self.clients: set = set()
        self.latest: dict[str, str] = {}
        self.history: collections.deque = collections.deque(maxlen=8)   # e.g. recent chat lines
        # Kept on disk too, so a restart doesn't wipe the chat panel on stream.
        self.history_file = history_file
        if history_file and history_file.exists():
            try:
                self.history.extend(json.loads(history_file.read_text())[-8:])
            except (ValueError, OSError):
                pass
        self.on_message = None
        self.max_incoming = 65536                # pages only send small control messages

    async def handler(self, ws):
        self.clients.add(ws)
        log.info("%s connected (%d)", self.name, len(self.clients))
        try:
            for msg in [*self.history, *self.latest.values()]:
                await ws.send(msg)
            async for raw in ws:
                if self.on_message and isinstance(raw, str) and len(raw) < self.max_incoming:
                    try:
                        self.on_message(json.loads(raw))
                    except ValueError:
                        pass
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            log.info("%s disconnected (%d)", self.name, len(self.clients))

    async def send(self, obj: dict, remember: str | None = None, keep: bool = False):
        """remember: replay only the latest message under this key to new clients.
        keep: add to the rolling history replayed to new clients (chat)."""
        msg = json.dumps(obj)
        if remember:
            self.latest[remember] = msg
        if keep:
            self.history.append(msg)
            if self.history_file:
                try:
                    self.history_file.write_text(json.dumps(list(self.history)))
                except OSError:
                    pass
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                self.clients.discard(ws)


class PuppetLink(Broadcast):
    """Drives the puppet and waits for each line to finish (speech_end)."""

    def __init__(self):
        super().__init__("puppet")
        self._waiting: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self.on_message = self._incoming
        self.last_heartbeat: dict = {}

    def _incoming(self, m: dict):
        if m.get("type") == "speech_end":
            fut = self._waiting.pop(m.get("id"), None)
            if fut and not fut.done():
                fut.set_result(bool(m.get("ok")))
        elif m.get("type") == "heartbeat":
            self.last_heartbeat = m

    async def say(self, clips: list[tuple[bytes, list | None, float]], expression: str | None = None) -> bool:
        """Play clips in order and wait for them. With no puppet connected, wait out the
        clips' length anyway so pacing stays the same."""
        ok = True
        for k, (mp3, visemes, duration) in enumerate(clips):
            sid = f"sr-{next(self._ids)}"
            msg = {"type": "say", "id": sid, "audio": base64.b64encode(mp3).decode()}
            if visemes:
                msg["visemes"] = visemes
            if expression and k == 0:
                msg["expression"] = expression
            if not self.clients:
                await asyncio.sleep(duration)
                continue
            fut = asyncio.get_running_loop().create_future()
            self._waiting[sid] = fut
            await self.send(msg)
            try:
                ok &= await asyncio.wait_for(fut, duration + 8)
            except asyncio.TimeoutError:
                self._waiting.pop(sid, None)
                log.warning("no speech_end for %s", sid)
                ok = False
        return ok

    async def expression(self, name: str):
        await self.send({"type": "expression", "name": name})

    async def reaction(self, name: str):
        await self.send({"type": "reaction", "name": name})


async def serve(link: Broadcast, port: int):
    return await websockets.serve(link.handler, "127.0.0.1", port, max_size=32 * 1024 * 1024)


class BridgeLink(Broadcast):
    """The Housebets browser extension (8768). It reports page snapshots, which are kept
    in memory and on disk (showrunner/.probe/) for learning and debugging the site's UI,
    and acknowledges the clicks it is sent."""

    def __init__(self, probe_dir):
        super().__init__("housebets-bridge")
        self.max_incoming = 8 * 1024 * 1024      # page snapshots carry the site's HTML
        self.probe_dir = probe_dir
        self.last_load = 0.0          # when a game page last loaded (top frame)
        self.last_nav_cmd = 0.0       # when we last asked the extension to navigate
        self.site_reloads = 0         # page loads we didn't ask for, this run
        self.snapshots: dict[str, dict] = {}       # frame url -> latest snapshot
        self._waiting: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self.on_message = self._incoming

    async def handler(self, ws):
        # Any web page can open a socket to localhost; only our extension may drive the site.
        origin = ws.request.headers.get("Origin", "") if ws.request else ""
        if not origin.startswith("chrome-extension://"):
            log.warning("bridge: refused connection from origin %r", origin)
            await ws.close(1008, "extension only")
            return
        await super().handler(ws)

    def _incoming(self, m: dict):
        kind = m.get("type")
        if kind == "snapshot":
            self.snapshots[m.get("url", "?")] = m
            self.probe_dir.mkdir(exist_ok=True)
            name = "top" if m.get("top") else f"frame-{abs(hash(m.get('url'))) % 10_000}"
            (self.probe_dir / f"{name}.json").write_text(json.dumps(m, indent=1))
        elif kind in ("status", "loaded", "error"):
            log.info("bridge %s: %s", kind, json.dumps({k: v for k, v in m.items() if k != "type"})[:600])
            if kind == "loaded" and m.get("top") and not m.get("reinjected"):
                self.last_load = time.time()        # any page load (ours, a switch, the site's own)
                # Ours = we asked for a navigation in the last 30 s. Anything else is the site (or a
                # person) reloading the page: logged so outages can be blamed on the right side.
                if time.time() - self.last_nav_cmd > 30:
                    self.site_reloads += 1
                    log.warning("site reloaded the page itself (nav=%s, #%d this run): %s",
                                m.get("nav"), self.site_reloads, m.get("url"))
        elif kind == "done":
            fut = self._waiting.pop(m.get("id"), None)
            if fut and not fut.done():
                fut.set_result(m)

    async def request(self, msg: dict, timeout: float = 5.0) -> dict | None:
        """Send one command to the page and wait for its reply ({"ok":..., "data":...})."""
        if not self.clients:
            return None
        if msg.get("type") == "navigate":
            self.last_nav_cmd = time.time()
        cid = f"b-{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._waiting[cid] = fut
        await self.send({**msg, "id": cid})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._waiting.pop(cid, None)
            return None

    async def command(self, msg: dict, timeout: float = 5.0):
        if msg.get("type") == "probe":
            if self.clients:
                await self.send(msg)
            return bool(self.clients)
        r = await self.request(msg, timeout)
        return r if r is None or msg.get("type") in ("query", "navigate", "stream_window", "stream_mode") else bool(r.get("ok"))
