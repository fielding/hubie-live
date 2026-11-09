#!/usr/bin/env python3
"""Screenshot every expression and reaction mid-animation into puppet/screenshots/.

    uv run --with websockets python puppet/dev_server.py --no-demo &   # static files
    uv run --with playwright --with pillow python puppet/shoot.py

Drives the page through window.hubie, with the WebSocket pointed at a closed port so
no orchestrator interferes. Timings are each reaction's most telling moment.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "screenshots"
URL = "http://127.0.0.1:8000/puppet/index.html?bg=%23394058&ws=ws://127.0.0.1:8799"
CROP = (920, 480, 1920, 1080)        # Hubie and the desk
EXPRESSIONS = ["neutral", "happy", "laugh", "surprised", "thinking"]
REACTIONS = [("lean_in", 800), ("pop_up", 230), ("slump", 2300), ("shake", 170), ("hat_tip", 600), ("nod", 110)]


def main():
    OUT.mkdir(exist_ok=True)
    shots = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(URL, wait_until="load")
        page.wait_for_function("window.hubie && hubie.stats().overlays.length > 0", timeout=20000)
        page.wait_for_timeout(1000)

        def snap(name, full=False):
            path = OUT / f"{name}.png"
            page.screenshot(path=str(path))
            if not full:
                Image.open(path).crop(CROP).save(path)
            shots.append((name, path))

        for e in EXPRESSIONS:
            page.evaluate(f"hubie.expression('{e}')")
            page.wait_for_timeout(1200)
            snap(f"expression_{e}")
        page.evaluate("hubie.expression('neutral')")
        page.wait_for_timeout(800)

        for r, at in REACTIONS:
            page.evaluate(f"hubie.reaction('{r}')")
            page.wait_for_timeout(at)
            snap(f"reaction_{r}")
            page.wait_for_timeout(6000 if r == "slump" else 2000)

        page.evaluate("hubie.testSpeech(4)")
        page.wait_for_timeout(2200)
        snap("speaking")
        stats = page.evaluate("hubie.stats()")
        browser.close()

    contact_sheet(shots)
    print("stats:", stats)
    print("page errors:", errors or "none")
    return 1 if errors or stats["errors"] else 0


def contact_sheet(shots):
    tiles = [(n, Image.open(p)) for n, p in shots]
    tw, th = 500, 300
    cols = 4
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 10) + 10, rows * (th + 40) + 10), (24, 24, 30))
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
    except OSError:
        font = ImageFont.load_default()
    for i, (name, img) in enumerate(tiles):
        x, y = 10 + (i % cols) * (tw + 10), 10 + (i // cols) * (th + 40)
        d.text((x, y + 6), name, fill=(230, 230, 230), font=font)
        sheet.paste(img.convert("RGB").resize((tw, th), Image.LANCZOS), (x, y + 32))
    sheet.save(OUT / "contact_sheet.png")


if __name__ == "__main__":
    sys.exit(main())
