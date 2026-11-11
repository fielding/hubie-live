"""Auto-clips: when Hubie hits something huge, save OBS's replay buffer and cut it into a ready-to-post
16:9 clip and a 9:16 vertical one (caption, game monitor, Hubie at his desk).

OBS side (one time): Settings -> Output -> Replay Buffer on (60 s); Tools -> WebSocket Server Settings
on, with a password (1Password item HUBIE_OBS_WS / password, read by run.sh into OBS_WS_PASSWORD).
Talks obs-websocket v5 (built into OBS 28+) on ws://127.0.0.1:4455. Kick's API can't make clips.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import websockets

log = logging.getLogger("clipper")

# Where things sit in the 1920x1080 stage (stage/live.html): the game monitor's screen and Hubie at
# his desk. Used as fractions, so a scaled OBS output crops the same areas.
GAME = (880, 87, 843, 501)
HUBIE = (20, 330, 750, 750)
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


class OBS:
    """Minimal obs-websocket v5 client: one short connection per call."""

    def __init__(self, url: str, password: str):
        self.url, self.password = url, password

    async def call(self, *requests: tuple[str, dict | None]) -> list[dict]:
        async with websockets.connect(self.url, open_timeout=5) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
            ident = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth = hello["d"].get("authentication")
            if auth:
                secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
                ident["authentication"] = base64.b64encode(
                    hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
            await ws.send(json.dumps({"op": 1, "d": ident}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if msg.get("op") != 2:
                raise RuntimeError("OBS refused the connection (wrong websocket password?)")
            out = []
            for i, (kind, data) in enumerate(requests):
                await ws.send(json.dumps({"op": 6, "d": {"requestType": kind, "requestId": str(i),
                                                          "requestData": data or {}}}))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if m.get("op") == 7 and m["d"].get("requestId") == str(i):
                        break
                st = m["d"]["requestStatus"]
                if not st.get("result"):
                    raise RuntimeError(f"OBS {kind} failed: {st.get('comment') or st.get('code')}")
                out.append(m["d"].get("responseData") or {})
            return out


def video_seconds(path: Path) -> float | None:
    """A video's length in seconds (ffprobe), or None if it can't be read."""
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=15)
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _crop(box) -> str:
    x, y, w, h = box
    return f"crop=iw*{w}/1920:ih*{h}/1080:iw*{x}/1920:ih*{y}/1080"


def cut(replay: Path, folder: Path, caption: str, seconds: int = 45) -> tuple[Path, Path]:
    """The replay's last `seconds` as <name>_16x9.mp4 and a vertical <name>_9x16.mp4."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    folder.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y-%m-%d_%H%M%S_") + re.sub(r"[^\w]+", "-", caption).strip("-")[:60]
    wide, tall = folder / f"{stem}_16x9.mp4", folder / f"{stem}_9x16.mp4"
    subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-sseof", f"-{seconds}", "-i", str(replay),
                    "-c", "copy", "-movflags", "+faststart", str(wide)], check=True, timeout=120)
    # Homebrew's ffmpeg has no drawtext, so the caption is drawn as an image and overlaid.
    cap = _caption_png(caption)
    try:
        graph = (f"[0:v]{_crop(GAME)},scale=1080:-2[g];[0:v]{_crop(HUBIE)},scale=1080:1080[h];"
                 "color=c=0x14122a:s=1080x1920:r=30[bg];[bg][g]overlay=0:250:shortest=1[t];"
                 "[t][h]overlay=0:250+643+40[th];[th][1:v]overlay=0:60[v]")
        subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-sseof", f"-{seconds}", "-i", str(replay),
                        "-loop", "1", "-i", cap, "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
                        "-shortest", "-c:v", "h264_videotoolbox", "-b:v", "8M", "-c:a", "aac", "-b:a", "160k",
                        "-movflags", "+faststart", str(tall)], check=True, timeout=300)
    finally:
        os.unlink(cap)
    return wide, tall


def _caption_png(text: str) -> str:
    """The vertical clip's caption: gold, outlined, centered, up to two lines, 1080x170."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (1080, 170), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = 72
    while True:
        font = ImageFont.truetype(FONT, size)
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if d.textlength(trial, font=font) <= 1000:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        if len(lines) <= 2 or size <= 40:
            break
        size -= 6
    lh = size + 12
    y = (170 - lh * len(lines)) // 2
    for line in lines[:2]:
        x = (1080 - d.textlength(line, font=font)) / 2
        d.text((x, y), line, font=font, fill=(242, 179, 61), stroke_width=5, stroke_fill=(12, 11, 24))
        y += lh
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    img.save(path)
    return path


class Clipper:
    def __init__(self, folder: Path, min_gap_s: float = 60):
        password = os.environ.get("OBS_WS_PASSWORD", "")
        self.obs = OBS(os.environ.get("OBS_WS_URL", "ws://127.0.0.1:4455"), password) if password else None
        self.folder, self.min_gap_s, self.last = folder, min_gap_s, 0.0

    @property
    def enabled(self) -> bool:
        return self.obs is not None

    async def ensure_buffer(self):
        """Start OBS's replay buffer if it isn't running (needs it enabled in OBS's output settings)."""
        if not self.obs:
            return
        try:
            (status,) = await self.obs.call(("GetReplayBufferStatus", None))
            if not status.get("outputActive"):
                await self.obs.call(("StartReplayBuffer", None))
                log.info("OBS replay buffer started")
        except Exception as e:
            log.warning("clips: OBS replay buffer unavailable (%s)", e)

    def ready(self) -> bool:
        return self.enabled and time.time() - self.last >= self.min_gap_s

    async def clip(self, caption: str, after_s: float = 10, seconds: int = 45) -> tuple[Path, Path] | None:
        """Wait for the reaction, save the buffer, cut it. Returns (16:9, 9:16) paths, or None."""
        self.last = time.time()
        await asyncio.sleep(after_s)
        try:
            (before,) = await self.obs.call(("GetLastReplayBufferReplay", None))
        except Exception:
            before = {}
        try:
            await self.obs.call(("SaveReplayBuffer", None))
            path = None
            for _ in range(20):                    # the file lands a moment after the save
                await asyncio.sleep(0.5)
                (last,) = await self.obs.call(("GetLastReplayBufferReplay", None))
                p = last.get("savedReplayPath")
                if p and p != before.get("savedReplayPath") and Path(p).exists():
                    path = Path(p)
                    break
            if not path:
                raise RuntimeError("the saved replay never showed up")
            await asyncio.sleep(1.0)               # let OBS finish writing it
            out = await asyncio.to_thread(cut, path, self.folder, caption, seconds)
            log.info("clip saved: %s", out[1].name)
            return out
        except Exception as e:
            log.warning("clip failed: %s", e)
            return None
