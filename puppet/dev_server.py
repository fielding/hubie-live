#!/usr/bin/env python3
"""Dev server for the Hubie puppet: static files + a demo orchestrator on ws://127.0.0.1:8765.

    uv run --with websockets python puppet/dev_server.py
    open http://127.0.0.1:8000/puppet/index.html?debug=1

Hubie's voice comes from Venice TTS (Theo Silk via ElevenLabs, see tts.py) when
VENICE_API_KEY is set; otherwise espeak-ng, otherwise amplitude-modulated noise.

Make him say anything (pushed to every connected puppet):

    curl -s 127.0.0.1:8000/say -d '{"text": "Hello chat!", "expression": "happy"}'

The demo loop plays the orchestrator's part: it cycles every expression and reaction,
sends speech (base64 and audio_url), and mixes in hostile input (bad JSON, unknown
types, undecodable audio, a mid-speech stop) to prove the page shrugs it off. It waits
for speech_end exactly like the real orchestrator does, so a missing speech_end shows
up as a timeout in the log.
"""
import argparse
import asyncio
import base64
import functools
import http.server
import io
import json
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import websockets

import tts

LOOP: asyncio.AbstractEventLoop | None = None
SESSIONS: set = set()          # connected puppets, for POST /say

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "puppet" / ".cache"
EXPRESSIONS = ["neutral", "happy", "laugh", "surprised", "thinking"]
REACTIONS = ["lean_in", "pop_up", "slump", "shake", "hat_tip", "nod"]
LINES = [
    ("Welcome back, high rollers. Hubie is in the house and the reels are warm.", "happy"),
    ("Okay, okay. Three cherries and a lemon. That is basically a win if you squint.", "thinking"),
    ("Jackpot! I told you the hat was lucky. Never doubt the hat.", "laugh"),
]


def am_noise_wav(seconds: float, rate: int = 16000) -> bytes:
    """Speech-shaped stand-in when espeak-ng isn't installed: low-passed noise in
    syllable-rate bursts with pauses between words."""
    n = int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        lp, frames = 0.0, bytearray()
        for i in range(n):
            t = i / rate
            syl = max(0.0, math.sin(2 * math.pi * 4.2 * t)) ** 0.7
            word = 1.0 if math.sin(2 * math.pi * 0.7 * t) > -0.6 else 0.05
            lp += 0.25 * (random.uniform(-1, 1) - lp)
            frames += int(max(-1, min(1, lp * syl * word * 0.9)) * 32767).to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def make_clips(engine: str) -> list[tuple[Path, str, str]]:
    CACHE.mkdir(parents=True, exist_ok=True)
    espeak = shutil.which("espeak-ng")
    clips, used = [], set()
    for i, (text, expr) in enumerate(LINES):
        if engine == "venice":
            try:
                path = CACHE / f"line{i}.mp3"
                path.write_bytes(tts.speak(text)[0])
                clips.append((path, text, expr))
                used.add("venice")
                continue
            except tts.TTSError as e:
                print(f"[dev] venice tts failed ({e}); falling back", flush=True)
        path = CACHE / f"line{i}.wav"
        if espeak:
            subprocess.run([espeak, "-w", str(path), "-s", "150", text], check=True)
            used.add("espeak-ng")
        else:
            path.write_bytes(am_noise_wav(0.36 * len(text.split()) + 0.5))
            used.add("AM noise")
        clips.append((path, text, expr))
    print(f"[dev] demo audio: {', '.join(sorted(used))}", flush=True)
    return clips


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != "/say":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(min(n, 64 * 1024)) or b"{}")
            text = str(req.get("text", "")).strip()
            if not text:
                raise ValueError("text is required")
            expression = req.get("expression")
            mp3s = tts.speak(text)
        except (ValueError, tts.TTSError) as e:
            return self._json(400, {"ok": False, "error": str(e)})
        ids = []
        for k, mp3 in enumerate(mp3s):
            sid = f"http-{int(time.time() * 1000)}-{k}"
            msg = {"type": "say", "id": sid, "audio": base64.b64encode(mp3).decode()}
            cues = tts.visemes(mp3)
            if cues:
                msg["visemes"] = cues
            if k == 0 and isinstance(expression, str):
                msg["expression"] = expression
            for s in list(SESSIONS):
                asyncio.run_coroutine_threadsafe(s.send(msg), LOOP)
            ids.append(sid)
        print(f"[dev] /say {len(text)} chars -> {len(ids)} clip(s), {len(SESSIONS)} puppet(s)", flush=True)
        self._json(200, {"ok": True, "ids": ids, "puppets": len(SESSIONS)})

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def serve_static(port: int):
    handler = functools.partial(Quiet, directory=str(ROOT))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[dev] static: http://127.0.0.1:{port}/puppet/index.html?debug=1", flush=True)


class Session:
    def __init__(self, ws, clips, http_port, log):
        self.ws, self.clips, self.http_port, self.log = ws, clips, http_port, log
        self.ended: dict[str, dict] = {}       # speech_end by id; ends can arrive out of order
        self.changed = asyncio.Condition()
        self.stats = {"speech_ok": 0, "speech_fail": 0, "timeouts": 0, "pongs": 0}

    async def reader(self):
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            t = msg.get("type")
            if t == "heartbeat":
                rec = {"t": round(time.time(), 1), **msg}
                print("[hb]", json.dumps(rec), flush=True)
                if self.log:
                    with open(self.log, "a") as f:
                        f.write(json.dumps(rec) + "\n")
            elif t == "pong":
                self.stats["pongs"] += 1
            else:
                print("[in]", json.dumps(msg), flush=True)
            if t == "speech_end" and isinstance(msg.get("id"), str):
                if len(self.ended) > 200:
                    self.ended.clear()
                async with self.changed:
                    self.ended[msg["id"]] = msg
                    self.changed.notify_all()

    async def send(self, obj):
        await self.ws.send(obj if isinstance(obj, str) else json.dumps(obj))

    async def wait_end(self, sid, timeout):
        try:
            async with self.changed:
                await asyncio.wait_for(self.changed.wait_for(lambda: sid in self.ended), timeout)
            msg = self.ended.pop(sid)
            self.stats["speech_ok" if msg.get("ok") else "speech_fail"] += 1
            return msg
        except asyncio.TimeoutError:
            pass
        self.stats["timeouts"] += 1
        print(f"[dev] TIMEOUT waiting for speech_end {sid}", flush=True)
        return None

    async def say(self, sid, clip, via_url=False, expression=None, wait=True):
        path, text, expr = clip
        msg = {"type": "say", "id": sid, "text": text, "expression": expression or expr}
        if via_url:
            msg["audio_url"] = f"http://127.0.0.1:{self.http_port}/puppet/.cache/{path.name}"
        else:
            msg["audio"] = base64.b64encode(path.read_bytes()).decode()
        cues = tts.visemes(path.read_bytes())
        if cues:
            msg["visemes"] = cues
        await self.send(msg)
        return await self.wait_end(sid, 60) if wait else None

    async def demo(self):
        loop_n = 0
        while True:
            loop_n += 1
            for e in EXPRESSIONS:
                await self.send({"type": "expression", "name": e})
                await asyncio.sleep(2.5)
            await self.send({"type": "expression", "name": "neutral"})
            for r in REACTIONS:
                await self.send({"type": "reaction", "name": r})
                await asyncio.sleep(6 if r == "slump" else 2.5)

            await self.say(f"l{loop_n}-a", self.clips[0])
            await self.say(f"l{loop_n}-b", self.clips[1], via_url=True)

            # Hostile input: none of this may crash the page or stall the queue.
            await self.send("{not json")
            await self.send({"type": "explode", "name": "now"})
            await self.send({"type": "reaction", "name": 12})
            await self.send({"type": "expression", "name": "<img src=x onerror=alert(1)>"})
            await self.send({"type": "say", "id": f"l{loop_n}-bad", "audio": "!!!not-base64!!!",
                             "text": "<b>should never render as html</b>"})
            await self.wait_end(f"l{loop_n}-bad", 20)
            await self.send({"type": "say", "id": f"l{loop_n}-404",
                             "audio_url": f"http://127.0.0.1:{self.http_port}/puppet/.cache/missing.wav"})
            await self.wait_end(f"l{loop_n}-404", 20)

            # Queue two, stop during the first: both must still get speech_end.
            await self.say(f"l{loop_n}-c", self.clips[2], wait=False)
            await self.say(f"l{loop_n}-d", self.clips[0], wait=False)
            await asyncio.sleep(1.2)
            await self.send({"type": "stop"})
            await self.wait_end(f"l{loop_n}-c", 20)
            await self.wait_end(f"l{loop_n}-d", 20)

            await self.send({"type": "ping"})
            await self.say(f"l{loop_n}-e", self.clips[2], expression="laugh")
            await self.send({"type": "reaction", "name": "pop_up"})
            await asyncio.sleep(2)
            print(f"[dev] loop {loop_n} done {self.stats}", flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--ws-port", type=int, default=8765)
    ap.add_argument("--log", help="append heartbeats here as JSON lines")
    ap.add_argument("--no-demo", action="store_true", help="serve only; drive the page yourself")
    ap.add_argument("--tts", choices=["venice", "local"],
                    default="venice" if os.environ.get("VENICE_API_KEY") else "local",
                    help="demo voice: Venice (Theo Silk) or espeak-ng/noise")
    a = ap.parse_args()

    global LOOP
    LOOP = asyncio.get_running_loop()
    clips = make_clips(a.tts)
    serve_static(a.http_port)

    async def handler(ws):
        s = Session(ws, clips, a.http_port, a.log)
        SESSIONS.add(s)
        print("[dev] puppet connected", flush=True)
        reader = asyncio.create_task(s.reader())
        demo = None if a.no_demo else asyncio.create_task(s.demo())
        try:
            await reader
        except websockets.ConnectionClosed:
            pass
        finally:
            SESSIONS.discard(s)
            if demo:
                demo.cancel()
            print("[dev] puppet disconnected", s.stats, flush=True)

    async with websockets.serve(handler, "127.0.0.1", a.ws_port, max_size=32 * 1024 * 1024):
        print(f"[dev] ws: ws://127.0.0.1:{a.ws_port}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
