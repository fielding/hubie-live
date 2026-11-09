"""Watch what the stream actually shows of the Housebets window.

After many hours, Chrome sometimes stops painting the game window (covered or on another display):
the page keeps working, so Hubie keeps playing, but OBS captures a black rectangle until someone
refreshes. The page can't see that from inside, so this looks at real pixels: a screenshot of the
window (macOS screencapture by window id) and its average brightness.

Needs Screen Recording permission for the terminal running the show (OBS-style capture)."""
import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger("screenwatch")
TITLE = "House Games | Housebets"       # the stream popup's window title ends with this
# Big enough that a few falling Plinko balls register as change: a quiet game must not look frozen.
THUMB = (128, 80)
CHROME_TOP_PX = 140                     # title/tab bar at 2x: not part of the page


def window_id() -> int | None:
    try:
        import Quartz
    except ImportError:
        return None
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    for w in wins:
        if w.get("kCGWindowOwnerName") == "Google Chrome" and TITLE in str(w.get("kCGWindowName", "")) \
                and w.get("kCGWindowLayer") == 0:
            return int(w["kCGWindowNumber"])
    return None


def snapshot() -> tuple[float, bytes] | None:
    """(average brightness 0-255, tiny grayscale thumbnail) of the game page as captured, or None
    if it can't be measured. Comparing thumbnails over time catches a frozen window: when Chrome
    stops painting it, OBS shows black while the window's last frame stays readable here."""
    wid, ffmpeg = window_id(), shutil.which("ffmpeg")
    if wid is None or not ffmpeg:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "w.png")
        try:
            subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), png], check=True, timeout=15,
                           capture_output=True)
            out = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", png, "-vf",
                                  f"crop=iw:ih-{CHROME_TOP_PX}:0:{CHROME_TOP_PX},scale={THUMB[0]}:{THUMB[1]},format=gray",
                                  "-f", "rawvideo", "-"], capture_output=True, timeout=15)
        except (subprocess.SubprocessError, OSError):
            return None
    thumb = out.stdout
    if len(thumb) != THUMB[0] * THUMB[1]:
        return None
    return sum(thumb) / len(thumb), thumb


def changed(a: bytes, b: bytes) -> float:
    """Mean absolute difference between two thumbnails (0 = identical)."""
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def brightness() -> float | None:
    snap = snapshot()
    return snap[0] if snap else None
