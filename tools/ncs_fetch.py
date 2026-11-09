"""Download NCS tracks (ncs.io, free to stream with credit) into assets/music/ for Hubie's
background music. Names files "Artist - Title [NCS Release].mp3" so the HUD and !song credit them.

    uv run --with httpx python tools/ncs_fetch.py --genres 2,60,44 --per-genre 15
    uv run --with httpx python tools/ncs_fetch.py --list-genres

Genre ids come from ncs.io/music-search (e.g. 2 Chill, 60 Lofi Hip-Hop, 44 Chill House, 35 Chill
Pop, 50 Future Funk, 54 Melodic House, 23 Ambient, 87 Nu-Jazz). Existing files are skipped."""
import argparse
import html
import json
import re
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "music"
SEARCH = "https://ncs.io/music-search"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
# One row per play button; data-artist holds HTML (with ">"), so match attribute by attribute.
ROW = re.compile(r'data-url="(?P<url>https://ncsmusic\.s3[^"]+\.mp3)"(?:(?!data-url=).)*?'
                 r'data-artistraw="(?P<artist>[^"]*)"\s+data-track="(?P<track>[^"]*)"', re.S)


def safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", html.unescape(s)).strip()


META = OUT / "tracks.json"            # file name -> {page, genre}: lets !song give NCS's full credit


def page(c: httpx.Client, genre: str, n: int) -> list[dict]:
    r = c.get(SEARCH, params={"q": "", "genre": genre, "mood": "", "page": n})
    r.raise_for_status()
    rows, starts = [], [m.start() for m in ROW.finditer(r.text)] + [len(r.text)]
    for m, end in zip(ROW.finditer(r.text), starts[1:]):
        block = r.text[m.start():end]
        g = re.search(r'data-genre="([^"]*)"', block)
        link = next((h for h in re.findall(r'href="/([A-Za-z0-9_-]+)"', block)
                     if h not in ("music-search", "artist")), None)
        rows.append({**m.groupdict(), "genre": html.unescape(g.group(1)) if g else "",
                     "page": f"https://ncs.io/{link}" if link else "https://ncs.io"})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--genres", default="2,60,44,35,50,54")
    ap.add_argument("--per-genre", type=int, default=12)
    ap.add_argument("--list-genres", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as c:
        if a.list_genres:
            text = c.get(SEARCH).text
            for gid, name in re.findall(r'<option value="(\d+)"[^>]*>([^<]+)', text):
                print(gid, html.unescape(name))
            return
        got = 0
        meta = json.loads(META.read_text()) if META.exists() else {}
        for genre in a.genres.split(","):
            n, want, pageno = 0, a.per_genre, 1
            while n < want and pageno <= 5:
                rows = page(c, genre.strip(), pageno)
                if not rows:
                    break
                for row in rows:
                    if n >= want:
                        break
                    dest = OUT / f"{safe(row['artist'])} - {safe(row['track'])} [NCS Release].mp3"
                    n += 1
                    meta[dest.name] = {"page": row["page"], "genre": row["genre"]}
                    if dest.exists():
                        continue
                    data = c.get(row["url"]).content
                    if len(data) < 500_000:              # a preview or an error page, not a track
                        print("skip (too small):", dest.name)
                        continue
                    dest.write_bytes(data)
                    got += 1
                    print(f"[{genre}] {dest.name} ({len(data) // 1_000_000} MB)")
                    time.sleep(0.5)                     # be polite
                pageno += 1
        META.write_text(json.dumps(meta, indent=1, sort_keys=True))
        print(f"downloaded {got} new tracks into {OUT}")


if __name__ == "__main__":
    main()
