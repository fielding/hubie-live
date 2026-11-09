"""Venice text-to-speech for Hubie's voice.

Default voice: "Theo Silk" from the ElevenLabs voice library, through Venice's
tts-elevenlabs-turbo-v2-5 model. That model accepts raw ElevenLabs voice IDs, so no
ElevenLabs account is needed; billing is on the Venice key.

    VENICE_API_KEY=... python puppet/tts.py "Hello chat" > hello.mp3

Overrides: HUBIE_TTS_MODEL, HUBIE_TTS_VOICE, HUBIE_TTS_SPEED. Results are cached on
disk by (model, voice, speed, text), since a streamer repeats lines.

visemes() turns a clip into a mouth-shape timeline with Rhubarb Lip Sync (A-H, X;
https://github.com/DanielSWolf/rhubarb-lip-sync). It uses Rhubarb's phonetic recognizer,
which takes ~0.1x the clip length on one core; the default recognizer is slower than
real time. Needs ffmpeg and a rhubarb binary (HUBIE_RHUBARB, tools/, or PATH).
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

API = "https://api.venice.ai/api/v1/audio/speech"
MODEL = os.environ.get("HUBIE_TTS_MODEL", "tts-elevenlabs-turbo-v2-5")
THEO_SILK = "UmQN7jS1Ee8B1czsUtQh"       # ElevenLabs library voice ID, verified against Venice
VOICE = os.environ.get("HUBIE_TTS_VOICE", THEO_SILK)
# 1.0: speeding Turbo v2.5 up made it sound robotic; pace lines by writing them shorter.
SPEED = float(os.environ.get("HUBIE_TTS_SPEED", "1.0"))
# ElevenLabs' content filter refuses swearing, name-calling and violent jokes. Those lines go to
# an unfiltered voice instead: a different voice from Theo Silk, picked by ear (2026-11-07).
UNFILTERED_MODEL = os.environ.get("HUBIE_TTS_UNFILTERED_MODEL", "tts-gradium-v1")
UNFILTERED_VOICE = os.environ.get("HUBIE_TTS_UNFILTERED_VOICE", "Jack")
# Semitones to shift a voice by; Jack reads a little high next to Theo Silk (-2 picked by ear).
PITCH = {(UNFILTERED_MODEL, UNFILTERED_VOICE): float(os.environ.get("HUBIE_TTS_UNFILTERED_PITCH", "-2"))}
# Low-end boost (dB at ~160 Hz): Jack is thinner than Theo Silk; +3 matched them on measurement.
BASS = {(UNFILTERED_MODEL, UNFILTERED_VOICE): 3.0}
# Every clip is levelled to one loudness so the two voices (and Theo's own lines, which vary by
# ~4 dB) sit at the same volume; mismatched level and EQ made them sound like two different mics.
LOUDNESS_LUFS = -20
WAV_ONLY = {"tts-gradium-v1", "tts-chatterbox-hd", "tts-orpheus", "tts-inworld-1-5-max"}   # no mp3 output
MAX_CHARS = 4000                         # Venice caps input at 4096
CACHE = Path(__file__).resolve().parent / ".cache" / "tts"
RETRY_STATUS = {429, 500, 502, 503, 504}


class TTSError(RuntimeError):
    pass


def chunks(text: str) -> list[str]:
    """Split on sentence boundaries so each request stays under the input cap."""
    text = " ".join(text.split())
    if len(text) <= MAX_CHARS:
        return [text] if text else []
    out, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > MAX_CHARS:               # one huge "sentence": hard split
            out.append(sentence[:MAX_CHARS])
            sentence = sentence[MAX_CHARS:]
        if len(cur) + len(sentence) + 1 > MAX_CHARS:
            out.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        out.append(cur)
    return out


def synthesize(text: str, *, voice: str = VOICE, model: str = MODEL, speed: float = SPEED,
               timeout: float = 12, attempts: int = 2) -> bytes:
    # A live stream can't wait minutes for one line: a normal line takes 1-3 s, so a stalled request
    # is abandoned after 12 s, retried once, and then the line is skipped (Hubie keeps playing).
    """MP3 bytes for one chunk of text (<= MAX_CHARS). Raises TTSError on failure."""
    pitch, bass = PITCH.get((model, voice), 0.0), BASS.get((model, voice), 0.0)
    key = hashlib.sha256(f"{model}|{voice}|{speed}|{pitch}|{bass}|{LOUDNESS_LUFS}|{text}".encode()).hexdigest()[:32]
    cached = CACHE / f"{key}.mp3"
    if cached.exists():
        return cached.read_bytes()
    api_key = os.environ.get("VENICE_API_KEY")
    if not api_key:
        raise TTSError("VENICE_API_KEY is not set")
    fmt = "wav" if model in WAV_ONLY else "mp3"
    body = {"model": model, "voice": voice, "input": text, "speed": speed, "response_format": fmt}
    last = ""
    for attempt in range(attempts):
        try:
            r = requests.post(API, json=body, timeout=timeout,
                              headers={"Authorization": f"Bearer {api_key}"})
        except requests.RequestException as e:
            last = type(e).__name__
        else:
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("audio/"):
                audio = _finish(r.content, pitch, bass)
                CACHE.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(audio)
                return audio
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in RETRY_STATUS:
                break                                      # 400/401/402: retrying won't help
        if attempt < attempts - 1:
            time.sleep(1)
    raise TTSError(last)


def _finish(audio: bytes, pitch: float = 0.0, bass: float = 0.0) -> bytes:
    """One format and one sound for every clip: mp3 at 44.1 kHz, levelled loudness, plus the
    voice's pitch shift (resample, then restore the tempo) and bass boost."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TTSError("ffmpeg is needed to finish voice clips")
    chain = []
    if pitch:
        f = 2 ** (pitch / 12)
        chain += ["aresample=48000", f"asetrate={48000 * f:.0f}", "aresample=48000", f"atempo={1 / f:.5f}"]
    if bass:
        chain.append(f"bass=g={bass}:f=160")
    chain.append(f"loudnorm=I={LOUDNESS_LUFS}:TP=-2:LRA=11")
    try:
        out = subprocess.run([ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-af", ",".join(chain), "-ar", "44100",
                              "-b:a", "128k", "-f", "mp3", "pipe:1"], input=audio, capture_output=True, check=True,
                             timeout=20)
    except subprocess.SubprocessError as e:
        raise TTSError(f"finishing the clip failed: {type(e).__name__}")
    return out.stdout


def _rhubarb() -> str | None:
    env = os.environ.get("HUBIE_RHUBARB")
    if env and os.access(env, os.X_OK):
        return env
    here = Path(__file__).resolve().parent.parent / "tools"
    for path in sorted(glob.glob(str(here / "rhubarb*" / "*" / "rhubarb"))):
        if os.access(path, os.X_OK):
            return path
    return shutil.which("rhubarb")


def visemes(mp3: bytes, timeout: float = 30) -> list[list] | None:
    """[[start_seconds, shape], ...] for the clip, or None if lip-sync analysis isn't
    available or fails; the puppet then falls back to loudness-driven mouths."""
    key = hashlib.sha256(mp3).hexdigest()[:32]
    cached = CACHE / f"{key}.visemes.json"
    if cached.exists():
        return json.loads(cached.read_text())
    rhubarb, ffmpeg = _rhubarb(), shutil.which("ffmpeg")
    if not rhubarb or not ffmpeg:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, wav = Path(tmp, "in.mp3"), Path(tmp, "in.wav")
        src.write_bytes(mp3)
        try:
            subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
                            str(wav)], check=True, timeout=timeout)
            out = subprocess.run([rhubarb, "-q", "-f", "json", "-r", "phonetic", "--extendedShapes", "GHX",
                                  str(wav)], check=True, timeout=timeout, capture_output=True)
            cues = [[round(c["start"], 3), c["value"]] for c in json.loads(out.stdout)["mouthCues"]]
        except (subprocess.SubprocessError, ValueError, KeyError) as e:
            print(f"[tts] viseme analysis failed: {type(e).__name__}", file=sys.stderr)
            return None
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(cues))
    return cues


def duration(mp3: bytes) -> float:
    """Clip length in seconds. Voices encode at different bitrates (Theo Silk 128 kbps, Kokoro ~32), so
    a size-based guess would cut Kokoro lines short."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(mp3)
            f.flush()
            try:
                out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                      f.name], check=True, timeout=10, capture_output=True, text=True)
                return float(out.stdout.strip())
            except (subprocess.SubprocessError, ValueError):
                pass
    return len(mp3) / 16000


def speak(text: str, **kw) -> list[bytes]:
    """One MP3 per chunk; send each as its own `say` so long text stays under the cap."""
    return [synthesize(c, **kw) for c in chunks(text)]


if __name__ == "__main__":
    for mp3 in speak(" ".join(sys.argv[1:]) or "Hubie here. Testing, one, two."):
        sys.stdout.buffer.write(mp3)
