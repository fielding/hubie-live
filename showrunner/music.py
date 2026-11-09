"""Background music: a folder of tracks cleared for streaming (NCS, ncs.io) that the stage page
shuffles, ducking under Hubie's voice. NCS asks for a credit per track, so the current track is
shown on the HUD and `!song` posts the credit in chat.

Drop mp3s into assets/music/. NCS downloads are named "Artist - Title [NCS Release].mp3"; other
names fall back to the file name as the title."""
import json
import re
from pathlib import Path

EXTS = {".mp3", ".m4a", ".ogg", ".wav"}


def describe(path: Path) -> dict:
    stem = re.sub(r"\s*[\[(](NCS Release|NCS|Official.*?)[\])]\s*$", "", path.stem, flags=re.I).strip()
    artist, sep, title = stem.partition(" - ")
    if not sep:
        artist, title = "", stem
    return {"file": path.name, "artist": artist.strip(), "title": title.strip()}


def tracks(folder: Path) -> list[dict]:
    """Every track in the folder, with its NCS page and genre when tools/ncs_fetch.py recorded them."""
    if not folder.is_dir():
        return []
    try:
        meta = json.loads((folder / "tracks.json").read_text())
    except (OSError, ValueError):
        meta = {}
    return [{**describe(p), **meta.get(p.name, {})} for p in sorted(folder.iterdir())
            if p.suffix.lower() in EXTS and p.is_file()]


def credit(track: dict) -> str:
    """NCS's credit format: song, "Music provided by NoCopyrightSounds", free download/stream link."""
    name = f"{track['artist']} - {track['title']}" if track["artist"] else track["title"]
    genre = f" ({track['genre']})" if track.get("genre") else ""
    return (f"Now playing: {name}{genre}. Music provided by NoCopyrightSounds. "
            f"Free download and stream: {track.get('page') or 'https://ncs.io'}")
