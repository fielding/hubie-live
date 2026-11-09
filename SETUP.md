# Setup: zero to live

Everything runs on one Mac. Plan on about 30 minutes the first time.

**1. Install the tools.**
- `brew install ffmpeg uv`, plus Google Chrome and OBS 28+.
- Optional: unzip [Rhubarb Lip Sync](https://github.com/DanielSWolf/rhubarb-lip-sync/releases) into `tools/` for lip sync.

**2. Get the keys.**
- **Required:** a [Venice](https://venice.ai) API key as `VENICE_API_KEY`. It covers the brain, voice and safety.
- **Optional:** a Kick app (`KICK_CLIENT_ID`, `KICK_CLIENT_SECRET`) and a second Kick account as the bot, made a moderator of your channel.
- **Optional:** `OBS_WS_PASSWORD` for auto-clips.
- Never commit any of these. `run.sh` reads them from the environment, or from 1Password if `op` is set up.

**3. Set up Chrome.**
- **Start Chrome with** `--disable-backgrounding-occluded-windows`, so a covered window keeps playing.
- **Load the extension:** go to `chrome://extensions`, turn on Developer mode, click "Load unpacked" and pick `extension/`.
- **Keep the site active:** in `chrome://settings/performance`, add `housebets.com` to "Always keep these sites active".

**4. Get music (optional).**
- `uv run --with httpx python tools/ncs_fetch.py --genres 3,29 --per-genre 40` downloads NCS drum and bass.

**5. Start Hubie.**
- Run `./showrunner/run.sh --chat kick --kick-channel <your channel id>`. It also opens the Housebets game window.
- **First time only:** open `http://localhost:8000/kick/login` in a browser logged in as the bot account.

**6. Set up OBS.** One 1920×1080 scene, three sources, top to bottom:
1. **Browser source:** `http://127.0.0.1:8000/stage/live.html?part=over`. Turn on "Control audio via OBS".
2. **macOS Screen Capture, Window:** "… in House Games | Housebets". Crop the title bar, put it at 880,87 and size it 843×501.
3. **Browser source:** `http://127.0.0.1:8000/stage/live.html?part=under`.

**7. Set up auto-clips (optional).**
- **Settings → Output → Replay Buffer:** turn it on, 60 s.
- **Tools → WebSocket Server Settings:** turn it on, port 4455, with a password. That password is `OBS_WS_PASSWORD`.

**8. Go live.**
- In OBS, use your Kick stream key with a 2 s keyframe interval and press Start Streaming.
- From then on, don't touch anything. Hubie recovers from crashes, site outages and frozen windows himself.
- Settings in `showrunner/config.toml` can be changed while he runs. Changing them counts as a human action under the contest rules.

**If something looks wrong:**
- **The game is black:** Hubie reloads it on his own within about 90 s. Viewers see a "be right back" card meanwhile.
- **No voice:** check `VENICE_API_KEY`.
- **No chat:** check the channel id you passed as `--kick-channel` (the default is the author's channel).
- Architecture, agents and tools are in [README.md](README.md).
