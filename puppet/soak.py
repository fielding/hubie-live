#!/usr/bin/env python3
"""Soak test: run the page against the dev server's demo loop in headless Chromium and
sample fps, JS heap (after a forced GC), DOM size and error counts once a minute.

    uv run --with websockets python puppet/dev_server.py &
    uv run --with playwright python puppet/soak.py --minutes 30

Writes puppet/soak/results.json. Exit status 1 if the heap grew, the page threw, or the
frame rate sagged.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "soak"
URL = "http://127.0.0.1:8000/puppet/index.html"
ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--enable-precise-memory-info",     # otherwise performance.memory is bucketed
    "--js-flags=--expose-gc",           # GC before sampling so the heap reading is retained memory
]
SAMPLE_JS = """() => {
  gc();
  const m = performance.memory, s = hubie.stats();
  return { heap_used: m.usedJSHeapSize, heap_total: m.totalJSHeapSize,
           dom_nodes: document.getElementsByTagName('*').length, ...s };
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--every", type=float, default=60, help="seconds between samples")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    samples, page_errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=ARGS)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.goto(URL, wait_until="load")
        page.wait_for_function("window.hubie && hubie.stats().ws === 1", timeout=30000)
        t0 = time.monotonic()
        end = t0 + a.minutes * 60
        while True:
            s = page.evaluate(SAMPLE_JS)
            s["t_min"] = round((time.monotonic() - t0) / 60, 2)
            samples.append(s)
            print(f"{s['t_min']:6.1f} min  fps {s['fps']:5.1f}  heap {s['heap_used'] / 1e6:6.2f} MB  "
                  f"dom {s['dom_nodes']}  errors {s['errors']}  speech_failures {s['speech_failures']}  "
                  f"state {s['state']}", flush=True)
            if time.monotonic() >= end:
                break
            time.sleep(min(a.every, max(0, end - time.monotonic())))
        browser.close()

    # Compare the median of the first and last few samples: single readings are noisy.
    k = max(1, len(samples) // 6)
    first = sorted(x["heap_used"] for x in samples[1:1 + k] or samples[:1])[k // 2]
    last = sorted(x["heap_used"] for x in samples[-k:])[k // 2]
    fps = [x["fps"] for x in samples[1:]] or [samples[0]["fps"]]
    summary = {
        "minutes": a.minutes,
        "samples": len(samples),
        "heap_start_mb": round(first / 1e6, 3),
        "heap_end_mb": round(last / 1e6, 3),
        "heap_growth_pct": round(100 * (last - first) / first, 2),
        "fps_min": min(fps), "fps_mean": round(sum(fps) / len(fps), 2),
        "dom_nodes_start": samples[0]["dom_nodes"], "dom_nodes_end": samples[-1]["dom_nodes"],
        "errors_end": samples[-1]["errors"],
        "speech_failures_end": samples[-1]["speech_failures"],
        "page_errors": page_errors[:20],
    }
    (OUT / "results.json").write_text(json.dumps({"summary": summary, "samples": samples}, indent=2))
    print(json.dumps(summary, indent=2))
    ok = summary["heap_growth_pct"] < 10 and not page_errors and summary["errors_end"] == 0 and summary["fps_min"] >= 27
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
