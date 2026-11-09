# Hubie Streamer

Hubie is a dapper sloth in a top hat who streams casino house games on Kick, fully on his own, 24/7.
He plays the real [Housebets](https://www.housebets.com) originals in a Chrome window (every bet is
$0, checked before each click), talks with an AI voice, trash-talks chat, takes votes, and keeps the
show going through site outages without a human touching anything.

## What he does

- **Plays real games:** Dice, Limbo, Coin Flip, Wheel, Plinko, Keno, Max Win, Mines, HiLo, Dragon
  Tower and Hubie's Road, on the live site, through a small Chrome extension.
- **Plays like a degen, not a spreadsheet:**
  - **Step games** (Tower, Road, Mines, HiLo): one AI plan per round (difficulty, cash-out target),
    then he climbs without stopping and only talks when the run gets interesting (5x, 10x, 20x...).
  - **One-shot games:** continuous auto sessions with pretend bet sizing that presses on wins.
    Dice at 6x, Limbo at 7x, Plinko in 5-ball bursts on max rows.
  - **Streak talk scales with the odds:** two 6x wins in a row is a tease, five is the clip of the night.
  - **Commentary never stops play:** he talks over the rolling and holds at most one line in the queue.
- **Talks:** DeepSeek through Venice for the brain, ElevenLabs "Theo Silk" through Venice for the
  voice, with Rhubarb lip sync. He swears (18+ stream), roasts chat back, and mocks balance questions.
- **Reads chat:**
  - **Replies with context:** answers in priority order and knows what each viewer said before.
  - **Votes:** `!<game>` votes, `!mines 10` picks, `!skip`/`!next`/`!song` for music.
  - **Thanks new followers** with an on-screen alert.
- **Moderates:** a Kick bot account deletes links, prompt-injection attempts and hate, and times out
  repeat offenders. Strict injection filtering, since it all runs on a home machine.
- **Auto-clips big hits:**
  - **Triggers:** any 500x+ win, a 1,000x Plinko ball, or a new stream record.
  - **Output:** a 16:9 clip and a vertical 9:16 cut (caption, game, Hubie) saved from OBS's replay buffer.
  - **The moment:** he hypes it on air, shows a "Clipped" card, and posts it in chat.
- **Stays up:**
  - **Game failures:** a failing game gets benched and he moves to another.
  - **Site self-reloads:** the move in flight is retried once.
  - **Frozen window:** the page is reloaded.
  - **Downtime:** viewers see a "be right back" card while the site is down.
  - **Crashes:** the runner restarts itself without asking for keys again.
- **Fills the room:** NCS drum and bass that ducks under his voice (the tracks are credited on
  screen and with `!song`), timed ads and reminders, game rotation every 25 minutes when chat is
  quiet, and a "no human input" runtime clock.

## How it works

```
Kick chat ──► showrunner (Python, asyncio) ──► Venice: brain, voice, safety classifier
                 │        │         │
                 │        │         └──► OBS websocket: replay-buffer clips
                 │        └──► Kick bot (official API): replies, moderation
                 ▼
   ws :8768 ◄──► Chrome extension ──► the real Housebets game tab (reads + clicks)
   ws :8765/6/7 ──► stage/live.html in OBS: room art, puppet (Hubie), HUD, game overlays
```

| Part | Where |
|---|---|
| Show runner: brain, safety, chat, votes, fast play | `showrunner/` |
| Live config, re-read while streaming | `showrunner/config.toml` |
| Real-site game plugins, hot-reloaded | `showrunner/games/housebets_*.py` |
| Chrome extension that plays the site | `extension/` |
| Puppet: Hubie, lip sync, faces | `puppet/` ([README](puppet/README.md)) |
| Stream scene, music player | `stage/live.html` |
| HUD: chat, stats, alerts | `overlay/` |
| Art pipeline (face parts, desk props) | `assets/desk/`, `tools/gen_faces.py` |
| Music fetcher (NCS) | `tools/ncs_fetch.py` |

## Requirements

- macOS (window capture and the screen watchdog use macOS APIs), Python 3.13 with
  [uv](https://docs.astral.sh/uv/), ffmpeg, Google Chrome, OBS 28+.
- A [Venice](https://venice.ai) API key (LLM, TTS, safety classifier, image edits).
- [Rhubarb Lip Sync](https://github.com/DanielSWolf/rhubarb-lip-sync) in `tools/` for lip sync (optional:
  without it the mouth follows loudness).
- Optional: a Kick app (client id/secret) and a bot account for chat replies and moderation; an
  OBS websocket password for auto-clips.

## Run

```sh
export VENICE_API_KEY=...            # or let run.sh read it from 1Password (`op`)
./showrunner/run.sh --chat kick      # real Kick chat; no args = simulated chat
```

- `run.sh` restarts the show if it crashes, reusing the keys it already read.
- `--paused --chat none` connects everything but stays idle, for working on game plugins.
- **Admin API:** `POST http://127.0.0.1:8000/admin/<cmd>` with `X-Admin-Token: $(cat showrunner/.admin_token)`.
  - **Commands:** `status`, `pause`, `resume`, `say`, `game`, `reload`, `chat`, `bridge` and `act`.
  - **Most commands count as human input,** which restarts the on-screen "no human input" clock.
  - **Maintenance commands don't:** bridge `query`, `status`, `stream_mode` and `reload_extension`.
- **Kick bot sign-in:** one time, open `http://localhost:8000/kick/login` in a browser logged in as the bot account.

## Chrome setup (one time)

1. **Load the extension:** go to `chrome://extensions`, turn on Developer mode, click "Load
   unpacked" and pick `extension/`.
   - **Use your main profile.** Fresh profiles may be blocked by Cloudflare.
   - **What it does:** it opens the Housebets stream window by itself and reconnects on its own.
2. **Start Chrome with `--disable-backgrounding-occluded-windows`.** A small launcher app works well.
   - **Why:** Chrome pauses windows you can't see, and Housebets games wait on their animations, so
     a covered window would freeze Hubie mid-round.
   - **Where it has to go:** it's a command-line switch, not a `chrome://flags` setting. Check it in
     `chrome://version`.
3. **Keep the site active:** in `chrome://settings/performance`, add `housebets.com` to "Always keep
   these sites active".
4. **Don't open Housebets in another tab of the same profile while streaming.** The tabs can fight
   over the game session.

## OBS

Sources, top to bottom:

1. **Browser:** `http://127.0.0.1:8000/stage/live.html?part=over`, 1920×1080, with "Control audio
   via OBS" on. This is the bezel, chat, Hubie, HUD, the cards and the music, and it carries his voice.
2. **macOS Screen Capture, Window:** the "… in House Games | Housebets" window. Crop the title bar,
   then place it at 880,87 with an 843×501 bounding box.
3. **Browser:** `http://127.0.0.1:8000/stage/live.html?part=under`, 1920×1080. This is the room and the back of the monitor.

For auto-clips:
- **Settings → Output → Replay Buffer:** on, 60 s.
- **Tools → WebSocket Server Settings:** on, port 4455, with a password. Pass it as `OBS_WS_PASSWORD`.
- **Where clips land:** `~/Movies/Hubie Clips/`.

After changing scene or HUD files, select the browser source and click **Refresh**.

## Games and chat commands

| Chat | Does |
|---|---|
| `!tower` `!road` `!plinko` `!dice` `!limbo` `!flip` `!wheel` `!hilo` `!maxwin` `!mines` | vote for the next game (60 s poll) |
| `!mines 1-24` | pick the next Mines round's mine count |
| `!song` / `!track`, `!next`, `!skip` | current track (NCS credit), up next, vote to skip |
| `!commands`, `!record`, `!uptime`, `!rules` | the basics |
| say "Hubie" | he answers |

Keno is off the vote menu for now: its page reloads itself under load.

## Configuration

`showrunner/config.toml` is re-read between rounds, and a bad edit is rejected and logged. It covers:
- **Models and voice:** model fallbacks, voice speed.
- **Pacing:** `fast_play`, `talk_from_x`, game rotation, chat reply rate.
- **Safety and persona:** safety thresholds and the persona prompt (his voice, trash-talk rules, hard lines).
- **Chat:** bot commands, catchphrases.
- **Segments:** timed ads and reminders.
- **Music:** volume, ducking and skip votes.

## Music

No music ships with the repo. `tools/ncs_fetch.py` downloads [NoCopyrightSounds](https://ncs.io)
tracks, which are free to stream with credit, into `assets/music/`, with their track pages recorded so
the HUD and `!song` can credit each one.

## Notes

- **Bets:** every bet is $0. Each game checks the bet amount right before clicking and fails closed.
  This is entertainment, not gambling advice, and the stream is 18+.
- **Secrets:** API keys come from the environment (or 1Password via `op`) and are never written to disk.
  Kick bot tokens live in `showrunner/.kick_tokens.json` (gitignored, mode 600).
- **AI assets:** the art and face parts were generated with Venice image models from a single
  hand-picked concept (`Dapper Sloth at the Desk.png`). `tools/gen_faces.py` rebuilds new face
  parts the same way.
