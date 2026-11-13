# Hubie desk puppet

A browser page that renders and animates Hubie at his desk and lip-syncs his voice. It is loaded as an OBS browser source and controlled over a WebSocket by the orchestrator (AI brain, TTS, game loop), which lives elsewhere.

Plain JavaScript and Canvas 2D. No build step, no frameworks, no network requests beyond the local asset server and the control socket.

## Run it

```sh
uv run --with websockets python puppet/dev_server.py
# then open
http://127.0.0.1:8000/puppet/index.html?debug=1
```

`dev_server.py` serves the repo root over HTTP on :8000 and runs a demo orchestrator on `ws://127.0.0.1:8765`. The demo cycles every expression and reaction, sends speech as base64 and as `audio_url`, and throws in bad input (broken JSON, unknown types, undecodable audio, a 404 URL, a mid-speech `stop`). Test audio comes from `espeak-ng` if it's installed, otherwise from amplitude-modulated noise. Use `--no-demo` to serve without the demo, and `--log heartbeats.jsonl` to record heartbeats.

The page must be served over HTTP, not opened as a `file://` URL: it fetches its manifest and images.

### Tests

```sh
uv run --with playwright --with pillow python puppet/shoot.py    # screenshots -> puppet/screenshots/ (gitignored)
uv run --with playwright python puppet/soak.py --minutes 30      # soak -> puppet/soak/results.json
```

`shoot.py` needs the static server running. `soak.py` also needs the demo orchestrator.

## URL parameters

| Param | Default | Meaning |
|---|---|---|
| `x` | `1420` | Screen x of the desk's bottom-center (Hubie sits centered on it) |
| `y` | `1080` | Screen y of the desk's bottom edge |
| `scale` | `0.34` | Hubie source px to screen px. At 0.34 the desk is about 980px wide and the whole scene about 600px tall |
| `ws` | `ws://127.0.0.1:8765` | Control socket |
| `debug` | off | `1` shows the debug panel: buttons for every expression, reaction and test speech, plus fps, RMS, mouth state and error counts |
| `bg` | transparent | Hex background color for testing, for example `%23394058` |

The canvas is 1920x1080 with a transparent background. Hubie and his desk sit bottom-right with the game area to their left.

## Control protocol

JSON text messages. Every field is validated: unknown types are ignored, bad values are dropped or clamped, and nothing a message contains can crash the page.

In (orchestrator to puppet):

```jsonc
{"type": "say", "id": "abc", "audio": "<base64 mp3/wav>", "expression": "happy"}  // e.g. ElevenLabs mp3
{"type": "say", "id": "abc", "audio_url": "http://127.0.0.1:8000/x.mp3",
 "visemes": [[0.0, "X"], [0.07, "B"], [0.11, "F"], [0.36, "A"]]}            // optional lip-sync timeline
{"type": "stop"}                           // stop speech and clear the queue
{"type": "expression", "name": "thinking"} // neutral | happy | laugh | surprised | thinking
{"type": "reaction", "name": "pop_up"}     // lean_in | pop_up | slump | shake | hat_tip | nod
{"type": "typing", "seconds": 0.9}         // hands tap on the keyboard (0-5 s)
{"type": "ping"}
```

Out (puppet to orchestrator):

```jsonc
{"type": "ready", "overlays": ["mouth_mid", "..."]}     // on every (re)connect
{"type": "speech_start", "id": "abc"}
{"type": "speech_end", "id": "abc", "ok": true}          // ok:false with "error" on any failure
{"type": "heartbeat", "fps": 30, "state": "idle", "queue": 0, "uptime_s": 1234,
 "errors": 0, "speech_failures": 0}                      // every 5s
{"type": "pong"}
```

**Lip sync.** If a `say` carries `visemes` (Rhubarb Lip Sync shapes A–H and X, as `[start_seconds, shape]` sorted by time), the mouth follows that timeline on the audio clock: A lips together (M/B/P), B teeth together (EE/S/T), C "eh", D "ah", E "oh", F "oo", G F/V, H L, X rest. Each shape falls back to the nearest available overlay. Without `visemes`, or if the timeline is malformed, the mouth is driven by loudness (closed, mid, open). Mouth and eye changes crossfade over ~45ms, and blinks go half → closed → half.

`puppet/tts.py` makes both halves: `speak(text)` returns MP3s from Venice (Theo Silk via ElevenLabs Turbo v2.5), and `visemes(mp3)` runs Rhubarb with its phonetic recognizer (~0.1× the clip's length on one core). Rhubarb is a separate binary: download the release for your platform from https://github.com/DanielSWolf/rhubarb-lip-sync/releases into `tools/` (or set `HUBIE_RHUBARB`). It also needs `ffmpeg`. Without them, `visemes()` returns `None` and lines play with loudness lip sync.

**Guarantees**

- Every accepted `say` gets exactly one `speech_end`: on success, on decode or fetch failure, on timeout, on `stop` (`error: "stopped"`), and when the queue is full (`error: "queue_full"`, max 16 queued). A watchdog ends playback at duration + 3s even if the audio clock stalls.
- `say` items play in order. `expression` in a `say` takes effect when that line starts. Expressions persist until changed. Reactions are one-shot.
- `errors` counts page faults: exceptions in a frame, the socket or a handler. Bad input such as undecodable audio counts toward `speech_failures` instead.
- `speech_end` is sent on the open socket. If the socket is down at that moment the message is lost; after a reconnect the orchestrator gets `ready` and should treat any in-flight line as finished.

When disconnected, the page keeps idling and blinking and reconnects with backoff (0.5s doubling to a 5s cap). Nothing is ever shown on screen.

For local testing, `window.hubie` exposes `say({...})`, `stop()`, `expression(name)`, `reaction(name)`, `testSpeech(seconds)` and `stats()`.

## OBS and Chromium settings

- **OBS browser source**: URL `http://127.0.0.1:8000/puppet/index.html` (plus params), width 1920, height 1080. Turn on **"Control audio via OBS"**, or the TTS audio won't be captured in the stream.
- **Plain Chromium / Playwright**: launch with `--autoplay-policy=no-user-gesture-required`. Without it the AudioContext stays suspended. The page calls `resume()` on every `say` with a 1s timeout, and the watchdog still delivers `speech_end` (`ok: false`), but there will be no sound.
- **Safety banner**: add `overlay/banner.html` as its own browser source. It is static HTML with no scripts and no network access, so puppet failures can't affect it.

## How the rig works

Assets live in `assets/desk/layers/`, all on the source image's 1312x1199 canvas. They were made by
the Nettie project (the scripts named below live there, not in this repo):

| File | What it is |
|---|---|
| `desk_body.png`, `desk_head.png`, `desk_hat.png` | Cut by `cut_layers.py`. The hidden areas are filled: under the brim with the top of the skull from a generated hatless edit, under the chin with extended torso colors |
| `desk_arms.png` | Hands, cuffs and outer sleeves, drawn a second time over the desk surface |
| `desk_torso_ext.png` | Torso continued below its cut line, so `pop_up` doesn't expose the edge |
| `face_<variant>.png` | Mouth and eye overlays cut from the Venice edits by `cut_faces.py` (ECC-aligned, color-matched, feathered) |
| `desk_manifest.json` | Layer order, pivots, overlay bboxes and desk geometry |

| `desk_pose_<name>.png` | Alternate body layers cut by `cut_poses.py` from generated pose edits, aligned to the rig's head: `hands_behind_head` (used by `slump`) and `arms_up` (used by `pop_up`). The rig's own head, face overlays and hat are drawn on top |
| `desk_scene_back.png`, `desk_scene_front.png` | The desk scene, composed by `build_desk.py` (Nettie) from separate prop art: desk, mat, keyboard and mouse behind his hands, and mug, figurine, books and mic in front |

Draw order per frame: torso (clipped at the desk's back edge), the desk with mat, keyboard and mouse, arms (between the desk's back edge and its front face), head with eyes overlay, mouth overlay and hat, then the desk's front face again and the props in front. The desk never moves.

The keyboard and mouse were generated in a normal user's-side view and rotated 180° by `build_desk.py`, because Hubie sits across the desk from the viewer. To move or swap a prop, edit `LAYOUT` in Nettie's `build_desk.py`, rerun it, and copy the new scene layers here.

Motion stays inside what the fills cover: head ±4px and ±3°, hat ±6px and ±4° relative to the head (soft-limited). Whole-character reactions move body, head and hat together. Reactions that move him far swap the body layer first, so his hands are off the desk before he moves: `slump` puts his hands behind his head, then sinks, rises and snaps back onto the keyboard, and `pop_up` throws both paws in the air for the jump. Each swap has a small squash so it reads as a cartoon pose snap. If a pose image is missing, the hands slide back over the desk's back edge instead.

Reliability: all images load once as `ImageBitmap`s with timeouts, and a failed image is logged and skipped. If the manifest fails, built-in defaults are used. There is one `AudioContext`. Buffers and sources are released after each line, and no object URLs are created. Every frame runs inside try/catch with counted errors. Rendering is capped at 30fps on a single `performance.now()` clock.
