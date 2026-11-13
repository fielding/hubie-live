// Hubie desk puppet: renders the rig, idles, lip-syncs, and takes orders over a WebSocket.
//
// It runs unattended for days, so every input is validated, every async step has a
// timeout, nothing grows without bound, and a failure anywhere degrades to "keep idling".
'use strict';

(function () {
  // ---------------------------------------------------------------- config
  const W = 1920;
  const H = 1080;
  const FRAME_MS = 1000 / 30;
  const SRC_W = 1312;
  const ASSET_TIMEOUT_MS = 15000;
  const FETCH_TIMEOUT_MS = 15000;
  const DECODE_TIMEOUT_MS = 15000;
  const MAX_QUEUE = 16;
  const MAX_MSG_CHARS = 24 * 1024 * 1024;     // base64 audio rides inside messages

  // The fills behind the head and under the brim only cover this much motion (source px).
  const LIMIT = { headPx: 4, headDeg: 3, hatPx: 6, hatDeg: 4 };

  const qs = new URLSearchParams(location.search);
  function numParam(name, fallback, lo, hi) {
    const v = parseFloat(qs.get(name));
    return Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : fallback;
  }
  const CFG = {
    // Screen position of the desk's bottom-center (Hubie sits centered on it), and the
    // Hubie-source-px -> screen-px scale. Defaults put the desk in the bottom-right corner.
    x: numParam('x', 1420, -4000, 6000),
    y: numParam('y', 1080, -4000, 6000),
    scale: numParam('scale', 0.34, 0.05, 4),
    ws: (() => {
      const v = qs.get('ws');
      return v && /^wss?:\/\/[^\s]{1,500}$/.test(v) ? v : 'ws://127.0.0.1:8765';
    })(),
    debug: qs.get('debug') === '1',
    // Which side of Hubie the game is on: glances and "thinking" look that way.
    look: qs.get('look') === 'right' ? 'right' : 'left',
    bg: (() => {
      const v = qs.get('bg');
      return v && /^#?[0-9a-fA-F]{3,8}$/.test(v) ? (v[0] === '#' ? v : '#' + v) : null;
    })(),
    assets: '../assets/desk/layers/',
  };

  // Used if desk_manifest.json can't be fetched; overlays then load at full canvas size.
  const DEFAULT_MANIFEST = {
    layers: [
      { name: 'body', file: 'desk_body.png', pivot: [656, 1199] },
      {
        name: 'head', file: 'desk_head.png', pivot: [662, 749],
        overlays: {
          mouth: { mouth_mid: {}, mouth_open: {}, mouth_laugh: {}, mouth_oo: {} },
          eyes: { eyes_closed: {}, eyes_wide: {}, eyes_happy: {}, eyes_look_left: {} },
        },
      },
      { name: 'hat', file: 'desk_hat.png', pivot: [608, 371] },
    ],
    desk: {
      torso_cut_y: 1126,
      arm_regions: [
        [[40, 960], [390, 960], [522, 1050], [522, 1199], [40, 1199]],
        [[796, 1050], [930, 960], [1272, 960], [1272, 1199], [796, 1199]],
      ],
      torso_extension: { file: 'desk_torso_ext.png', y: 1112 },
      arms: { file: 'desk_arms.png' },
      scene: {
        rear: 'desk_scene_rear.png', back: 'desk_scene_back.png', front: 'desk_scene_front.png',
        x: -482.98, y: -640.15, scale: 2.778,
        back_y: 1026.7, front_y: 1229.4, bottom_y: 2093.4, left_x: -483.0, right_x: 5550.8,
      },
    },
  };

  // Each expression names overlays in preference order; missing ones fall through.
  const EXPRESSIONS = {
    neutral:   { eyes: [], mouth: [], tilt: 0, dy: 0 },
    happy:     { eyes: ['eyes_happy', 'eyes_closed'], mouth: [], tilt: -1.0, dy: -0.8 },
    laugh:     { eyes: ['eyes_happy', 'eyes_closed'], mouth: ['mouth_laugh', 'mouth_open', 'mouth_mid'], tilt: -0.6, dy: -1, chuckle: true },
    surprised: { eyes: ['eyes_wide'], mouth: ['mouth_oo', 'mouth_mid'], tilt: 0, dy: -1.5, hatPop: true },
    thinking:  { eyes: [CFG.look === 'right' ? 'eyes_look_right' : 'eyes_look_left'], mouth: [], tilt: CFG.look === 'right' ? 2.0 : -2.0, dy: 0.5 },
    // 2026-11-08 (Nettie gen_faces.py): faces for long stretches of fast play.
    focused:   { eyes: ['eyes_squint'], mouth: ['mouth_smirk'], tilt: 0.6, dy: 0.4 },
    smug:      { eyes: ['eyes_half'], mouth: ['mouth_smirk'], tilt: -0.8, dy: -0.3 },
    grimace:   { eyes: ['eyes_squint'], mouth: ['mouth_grimace', 'mouth_B'], tilt: 1.2, dy: 0.6 },
    sad:       { eyes: ['eyes_sad', 'eyes_half'], mouth: ['mouth_frown'], tilt: 1.5, dy: 1.2 },
    eyeroll:   { eyes: ['eyes_roll'], mouth: ['mouth_frown'], tilt: -1.2, dy: -0.4 },
  };
  const REACTIONS = new Set(['lean_in', 'pop_up', 'slump', 'shake', 'hat_tip', 'nod', 'bowtie']);

  // Rhubarb Lip Sync mouth shapes -> overlays, best first; missing art falls through to the
  // nearest shape, and an empty list means the base face. A: M/B/P, B: K/S/T/EE, C: EH,
  // D: AA, E: AO/ER, F: UW/OW/W, G: F/V, H: L, X: rest.
  const VISEMES = {
    A: ['mouth_A'],
    B: ['mouth_B', 'mouth_mid'],
    C: ['mouth_C', 'mouth_mid'],
    D: ['mouth_open', 'mouth_C', 'mouth_mid'],
    E: ['mouth_E', 'mouth_oo', 'mouth_mid'],
    F: ['mouth_oo', 'mouth_E', 'mouth_mid'],
    G: ['mouth_G', 'mouth_B', 'mouth_mid'],
    H: ['mouth_H', 'mouth_C', 'mouth_mid'],
    X: [],
    MID: ['mouth_mid', 'mouth_B'],            // not a Rhubarb shape: loud speech it marked as rest
  };
  const MAX_CUES = 6000;
  const FADE_MS = { mouth: 45, eyes: 45 };   // overlay crossfades: ~1 blended frame at 30fps
  const BLINK_HALF_MS = 40;                  // half-lid frames either side of a blink

  // ---------------------------------------------------------------- dom
  const canvas = document.getElementById('stage');
  const ctx = canvas.getContext('2d', { alpha: true });
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';

  const startedAt = performance.now();
  let frameErrors = 0;
  let asyncErrors = 0;
  let speechFailures = 0;     // bad input (undecodable audio, 404s), not page faults
  function logError(where, err) {
    // Rate-limited: a persistent fault must not flood the console for days.
    const n = frameErrors + asyncErrors;
    if (n < 20 || n % 1000 === 0) console.error('[hubie]', where, err);
  }

  // ---------------------------------------------------------------- assets
  const rig = {
    body: null, head: null, hat: null, ext: null, arms: null, sceneRear: null, sceneBack: null, sceneFront: null,
    poses: new Map(),             // alternate body layers (arms off the desk), by name
    handL: null, handR: null, armsStatic: null,   // typing: hands animate over the static arms
    headPivot: [662, 749], hatPivot: [608, 371],
    overlays: { mouth: new Map(), eyes: new Map() },
    torsoCutY: 1126, extY: 1112, armPaths: [],
  };

  function withTimeout(promise, ms, what) {
    let timer;
    const t = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(what + ' timed out')), ms); });
    return Promise.race([promise, t]).finally(() => clearTimeout(timer));
  }

  async function fetchWithTimeout(url, ms, as) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), ms);
    try {
      const res = await fetch(url, { signal: ctl.signal });
      if (!res.ok) throw new Error('HTTP ' + res.status + ' for ' + url);
      return await (as === 'json' ? res.json() : as === 'blob' ? res.blob() : res.arrayBuffer());
    } finally {
      clearTimeout(timer);
    }
  }

  async function loadBitmap(file, bbox) {
    const blob = await fetchWithTimeout(CFG.assets + file, ASSET_TIMEOUT_MS, 'blob');
    // Overlays are mostly transparent full-canvas images; keep only their bbox.
    if (bbox && bbox.length === 4) return createImageBitmap(blob, bbox[0], bbox[1], bbox[2], bbox[3]);
    return createImageBitmap(blob);
  }

  async function loadAssets() {
    let manifest = DEFAULT_MANIFEST;
    try {
      manifest = await fetchWithTimeout(CFG.assets + 'desk_manifest.json', ASSET_TIMEOUT_MS, 'json');
    } catch (err) {
      console.warn('[hubie] manifest unavailable, using built-in defaults:', err && err.message);
    }
    const layers = Array.isArray(manifest.layers) ? manifest.layers : DEFAULT_MANIFEST.layers;
    const desk = manifest.desk || DEFAULT_MANIFEST.desk;
    const jobs = [];

    for (const layer of layers) {
      if (!layer || !['body', 'head', 'hat'].includes(layer.name) || typeof layer.file !== 'string') continue;
      if (Array.isArray(layer.pivot) && layer.pivot.length === 2) {
        if (layer.name === 'head') rig.headPivot = layer.pivot.map(Number);
        if (layer.name === 'hat') rig.hatPivot = layer.pivot.map(Number);
      }
      jobs.push(loadBitmap(layer.file).then((b) => { rig[layer.name] = b; },
        (err) => console.error('[hubie] layer ' + layer.name + ' failed, skipping:', err && err.message)));
      if (layer.name === 'head' && layer.overlays) {
        for (const group of ['mouth', 'eyes']) {
          const entries = layer.overlays[group] || {};
          for (const name of Object.keys(entries)) {
            const meta = entries[name] || {};
            const file = typeof meta.file === 'string' ? meta.file : 'face_' + name + '.png';
            const bbox = Array.isArray(meta.bbox) ? meta.bbox.map(Number) : null;
            jobs.push(loadBitmap(file, bbox).then((b) => {
              rig.overlays[group].set(name, { bmp: b, x: bbox ? bbox[0] : 0, y: bbox ? bbox[1] : 0 });
            }, (err) => console.error('[hubie] overlay ' + name + ' failed, skipping:', err && err.message)));
          }
        }
      }
    }

    const poses = manifest.poses && typeof manifest.poses === 'object' ? manifest.poses : {};
    for (const name of Object.keys(poses)) {
      const file = poses[name] && poses[name].file;
      if (typeof file !== 'string') continue;
      jobs.push(loadBitmap(file).then((b) => { rig.poses.set(name, b); },
        (err) => console.error('[hubie] pose ' + name + ' failed, skipping:', err && err.message)));
    }

    if (Number.isFinite(desk.torso_cut_y)) rig.torsoCutY = desk.torso_cut_y;
    const ext = desk.torso_extension;
    if (ext && typeof ext.file === 'string') {
      if (Number.isFinite(ext.y)) rig.extY = ext.y;
      jobs.push(loadBitmap(ext.file).then((b) => { rig.ext = b; },
        (err) => console.error('[hubie] torso extension failed, skipping:', err && err.message)));
    }
    const hands = desk.hands && typeof desk.hands === 'object' ? desk.hands : null;
    if (hands) {
      for (const [slot, file] of [['handL', hands.left], ['handR', hands.right], ['armsStatic', hands.static_arms]]) {
        if (typeof file !== 'string') continue;
        jobs.push(loadBitmap(file).then((b) => { rig[slot] = b; },
          (err) => console.error('[hubie] hand layer ' + slot + ' failed, typing disabled:', err && err.message)));
      }
    }
    if (desk.arms && typeof desk.arms.file === 'string') {
      jobs.push(loadBitmap(desk.arms.file).then((b) => { rig.arms = b; },
        (err) => console.error('[hubie] arms layer failed, falling back to arm regions:', err && err.message)));
    }
    const sc = desk.scene && typeof desk.scene === 'object' ? desk.scene : DEFAULT_MANIFEST.desk.scene;
    for (const [k, key] of [['x', 'x'], ['y', 'y'], ['scale', 'scale'], ['back', 'back_y'], ['front', 'front_y'],
      ['bottom', 'bottom_y'], ['left', 'left_x'], ['right', 'right_x']]) {
      if (Number.isFinite(sc[key])) DESK[k] = sc[key];
    }
    for (const [slot, file] of [['sceneRear', sc.rear], ['sceneBack', sc.back], ['sceneFront', sc.front]]) {
      if (typeof file !== 'string') continue;
      jobs.push(loadBitmap(file).then((b) => { rig[slot] = b; },
        (err) => console.error('[hubie] desk ' + slot + ' failed, skipping:', err && err.message)));
    }
    for (const poly of Array.isArray(desk.arm_regions) ? desk.arm_regions : []) {
      const p = new Path2D();
      poly.forEach((pt, i) => (i ? p.lineTo(pt[0], pt[1]) : p.moveTo(pt[0], pt[1])));
      p.closePath();
      rig.armPaths.push(p);
    }
    await Promise.all(jobs);
  }

  function availableOverlays() {
    return [...rig.overlays.mouth.keys(), ...rig.overlays.eyes.keys()];
  }

  function firstAvailable(group, names) {
    for (const n of names) if (rig.overlays[group].has(n)) return n;
    return null;
  }

  // ---------------------------------------------------------------- desk geometry
  // The desk scene is two prebuilt images (Nettie build_desk.py) placed in
  // Hubie's source coordinates; values are refreshed from the manifest. It never moves.
  const DESK = {
    x: -482.98, y: -640.15, scale: 2.778,      // scene image origin and px scale, in source coords
    back: 1026.7,                           // desk top's back edge: torso hidden below this
    front: 1229.4,                          // top of the front face: hands slide behind this
    bottom: 2093.4, left: -483.0, right: 5550.8,
  };

  function drawScene(img) {
    ctx.save();
    ctx.translate(DESK.x, DESK.y);
    ctx.scale(DESK.scale, DESK.scale);
    ctx.drawImage(img, 0, 0);
    ctx.restore();
  }

  // ---------------------------------------------------------------- animation state
  const now0 = () => performance.now();
  const rand = (a, b) => a + Math.random() * (b - a);
  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const smooth = (x) => { const t = clamp(x, 0, 1); return t * t * (3 - 2 * t); };
  const DEG = Math.PI / 180;

  function spring(x) { return { x, v: 0 }; }
  function stepSpring(s, target, k, c, dt) {
    // Semi-implicit Euler, sub-stepped so a long frame can't blow the spring up.
    const n = Math.max(1, Math.ceil(dt / 0.008));
    const h = dt / n;
    for (let i = 0; i < n; i++) {
      s.v += (k * (target - s.x) - c * s.v) * h;
      s.x += s.v * h;
    }
  }

  const st = {
    expression: 'neutral',
    exprSince: 0,
    exprTilt: 0,
    exprDy: 0,
    reaction: null,               // { name, t0 }
    bodyPose: null,               // name of the swapped-in body layer, or null
    typingStart: 0, typingUntil: 0,
    charY: spring(0),             // source px, + is down
    charScale: spring(1),
    hatX: spring(0), hatY: spring(0), hatA: spring(0),   // hat in head-attached world frame
    hatPrimed: false,
    blinkNext: 0, blinkUntil: 0, blinkStart: 0, blinkDouble: false,
    viseme: '-',
    glanceNext: 0, glanceUntil: 0,
    // lip sync
    rms: 0, peak: 0.05, level: 0,
    mouthState: 'closed', mouthSince: 0,
    // render outputs
    pose: null,
  };

  function setExpression(name) {
    if (typeof name !== 'string' || !Object.prototype.hasOwnProperty.call(EXPRESSIONS, name)) return false;
    if (name !== st.expression) {
      st.expression = name;
      st.exprSince = now0();
      if (EXPRESSIONS[name].hatPop) st.hatY.v -= 260;   // surprise knocks the hat up
    }
    return true;
  }

  function typing(seconds) {
    const s = Number(seconds);
    if (!Number.isFinite(s) || s <= 0) return false;
    const t = now0();
    if (t > st.typingUntil) st.typingStart = t;
    st.typingUntil = t + Math.min(5, s) * 1000;
    return true;
  }

  // Hand offsets (source px) while typing: alternating quick taps with a little drift,
  // easing in and out so the hands never jump.
  const TAP_PX = 10, TAP_HZ = 7, DRIFT_PX = 3;
  function handOffsets(t, out) {
    out.lx = out.ly = out.rx = out.ry = 0;
    if (t >= st.typingUntil) return out;
    const k = clamp((t - st.typingStart) / 120, 0, 1) * clamp((st.typingUntil - t) / 150, 0, 1);
    const tau = (t - st.typingStart) / 1000;
    const ph = 2 * Math.PI * TAP_HZ * tau;
    out.ly = TAP_PX * Math.max(0, Math.sin(ph)) * k;
    out.ry = TAP_PX * Math.max(0, Math.sin(ph + Math.PI)) * k;
    out.lx = DRIFT_PX * Math.sin(2 * Math.PI * 1.1 * tau) * k;
    out.rx = -DRIFT_PX * Math.sin(2 * Math.PI * 0.9 * tau) * k;
    return out;
  }
  const hand = {};

  function react(name) {
    if (typeof name !== 'string' || !REACTIONS.has(name)) return false;
    st.reaction = { name, t0: now0(), popped: false };
    return true;
  }

  // Reaction timelines: targets for springs and additive pose offsets at time tau (s).
  function reactionPose(r, tau, out) {
    const toSrc = 1 / CFG.scale;            // reaction distances are in screen px
    out.charY = 0; out.charScale = 1; out.k = 160; out.c = 2 * Math.sqrt(160);
    out.headDy = 0; out.headDeg = 0; out.hatDy = 0; out.hatDeg = 0; out.done = false; out.pose = null;
    if (!r) return out;
    switch (r.name) {
      case 'lean_in': {
        const e = smooth(tau / 0.35) * (1 - smooth((tau - 1.15) / 0.35));
        out.charScale = 1 + 0.04 * e;
        out.charY = 10 * toSrc * e;
        out.headDy = 1.5 * e;
        out.done = tau > 1.6;
        break;
      }
      case 'pop_up':
        // Underdamped spring: aiming at 32px overshoots to a ~42px peak, then a springy settle.
        out.charY = tau < 0.16 ? -32 * toSrc : 0;
        out.k = 260; out.c = 11;
        if (tau < 1.3) out.pose = 'arms_up';            // celebrate with both paws in the air
        if (!r.popped && tau > 0.16) { r.popped = true; st.hatY.v -= 320; st.hatA.v -= 30; }
        out.done = tau > 1.8;
        break;
      case 'slump': {
        const down = smooth(tau / 1.5), up = smooth((tau - 2.5) / 2.5);
        out.charY = MAX_SINK * toSrc * (down - up);
        // Hands go behind his head before he sinks and come back once he's up, so they
        // never have to pass through the desk.
        if (tau > 0.08 && tau < 4.9) out.pose = 'hands_behind_head';
        out.k = 60; out.c = 2 * Math.sqrt(60);
        out.headDy = 3 * (down - up);
        out.headDeg = -1.5 * (down - up);
        out.done = tau > 5.3;
        break;
      }
      case 'shake':
        out.headDeg = 2.8 * Math.sin(2 * Math.PI * 4.5 * tau) * Math.max(0, 1 - tau / 0.9);
        out.done = tau > 0.9;
        break;
      case 'bowtie': {
        // Paws up to the bow tie and straighten it: two frames flipped like little tugs, a smug head
        // tilt, then back to the desk.
        if (tau > 0.06 && tau < 1.55) out.pose = Math.floor((tau - 0.06) / 0.2) % 2 ? 'bowtie_tug' : 'bowtie';
        const e = smooth(tau / 0.2) * (1 - smooth((tau - 1.35) / 0.3));
        out.headDeg = 1.8 * e;
        out.headDy = -1 * e;
        out.charScale = 1 + 0.01 * e;
        out.done = tau > 1.8;
        break;
      }
      case 'hat_tip': {
        const e = smooth(tau / 0.25) * (1 - smooth((tau - 0.75) / 0.6));
        out.hatDy = -6 * e;
        out.hatDeg = -4 * e;
        out.done = tau > 1.5;
        break;
      }
      case 'nod': {
        const n = tau < 0.9 ? Math.max(0, Math.sin(2 * Math.PI * tau / 0.45)) : 0;
        out.headDy = 3.5 * n;
        out.headDeg = 0.6 * n;
        out.done = tau > 0.9;
        break;
      }
    }
    return out;
  }
  const rp = {};

  // ---------------------------------------------------------------- audio / lip sync
  const audio = {
    ctx: null, analyser: null, buf: null,
    queue: [], current: null, source: null, watchdog: 0,
    cues: null, cueIdx: 0, t0: 0, t0Wall: 0,   // viseme timeline of the playing line
  };

  function ensureAudio() {
    if (audio.ctx) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    audio.ctx = new AC();
    audio.analyser = audio.ctx.createAnalyser();
    audio.analyser.fftSize = 1024;
    audio.analyser.smoothingTimeConstant = 0;
    audio.analyser.connect(audio.ctx.destination);
    audio.buf = new Float32Array(audio.analyser.fftSize);
  }

  function b64ToBuffer(b64) {
    const clean = b64.replace(/^data:[^,]*,/, '').replace(/\s+/g, '');
    const bin = atob(clean);                 // throws on bad input; caller reports ok:false
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  let idCounter = 0;
  function say(msg) {
    const id = typeof msg.id === 'string' && msg.id.length <= 128 ? msg.id : 'local-' + (++idCounter);
    const item = {
      id,
      audio: typeof msg.audio === 'string' && msg.audio.length > 0 && msg.audio.length <= MAX_MSG_CHARS ? msg.audio : null,
      url: typeof msg.audio_url === 'string' && /^https?:\/\/[^\s]{1,2000}$/.test(msg.audio_url) ? msg.audio_url : null,
      expression: typeof msg.expression === 'string' && EXPRESSIONS[msg.expression] ? msg.expression : null,
      cues: parseVisemes(msg.visemes),
      done: false, cancelled: false,
    };
    if (!item.audio && !item.url) { send({ type: 'speech_end', id, ok: false, error: 'no_audio' }); return id; }
    if (audio.queue.length >= MAX_QUEUE) { send({ type: 'speech_end', id, ok: false, error: 'queue_full' }); return id; }
    audio.queue.push(item);
    pump();
    return id;
  }

  // [[startSeconds, shape], ...] sorted by time; anything malformed drops the whole
  // timeline and the line falls back to loudness-driven mouths.
  function parseVisemes(v) {
    if (!Array.isArray(v) || v.length === 0 || v.length > MAX_CUES) return null;
    const out = new Array(v.length);
    let last = -1;
    for (let i = 0; i < v.length; i++) {
      const c = v[i];
      if (!Array.isArray(c) || c.length !== 2) return null;
      const at = Number(c[0]);
      if (!Number.isFinite(at) || at < last || typeof c[1] !== 'string' || !VISEMES[c[1]]) return null;
      out[i] = [at, c[1]];
      last = at;
    }
    return out;
  }

  function pump() {
    if (audio.current || audio.queue.length === 0) return;
    const item = audio.queue.shift();
    audio.current = item;
    play(item).catch((err) => {
      speechFailures++;
      console.warn('[hubie] speech ' + item.id + ' failed:', String((err && err.message) || err).slice(0, 200));
      finish(item, false, String((err && err.message) || err).slice(0, 200));
    });
  }

  async function play(item) {
    ensureAudio();
    if (audio.ctx.state === 'suspended') {
      // resume() can hang forever under an autoplay policy; don't let it stall the queue.
      await withTimeout(audio.ctx.resume(), 1000, 'AudioContext.resume').catch((e) => logError('resume', e));
    }
    const bytes = item.audio ? b64ToBuffer(item.audio) : await fetchWithTimeout(item.url, FETCH_TIMEOUT_MS, 'buffer');
    item.audio = null;
    const buffer = await withTimeout(audio.ctx.decodeAudioData(bytes), DECODE_TIMEOUT_MS, 'decodeAudioData');
    if (item.cancelled || item.done) return;

    const src = audio.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(audio.analyser);
    src.onended = () => finish(item, true);
    audio.source = src;
    if (item.expression) setExpression(item.expression);
    send({ type: 'speech_start', id: item.id });
    src.start();
    audio.cues = item.cues;
    audio.cueIdx = 0;
    audio.t0 = audio.ctx.currentTime;
    audio.t0Wall = performance.now();
    // Lets a host page (the demo stage) note exactly when the line became audible.
    try { window.dispatchEvent(new CustomEvent('hubie:speech', { detail: { id: item.id, phase: 'start' } })); } catch (_) { /* no host */ }
    // onended never fires if the context is stuck suspended; the watchdog guarantees speech_end.
    // With the context suspended (tab never clicked) nothing will play, so end on time
    // instead of 3s late: the show's pacing must not depend on whether audio is unlocked.
    const slack = audio.ctx.state === 'running' ? 3 : 0.1;
    audio.watchdog = setTimeout(() => finish(item, audio.ctx.state === 'running', 'watchdog'), (buffer.duration + slack) * 1000);
  }

  function finish(item, ok, error) {
    if (item.done) return;
    item.done = true;
    clearTimeout(audio.watchdog);
    audio.watchdog = 0;
    if (audio.current === item) {
      const src = audio.source;
      audio.source = null;
      if (src) {
        src.onended = null;
        try { src.stop(); } catch (_) { /* already stopped */ }
        try { src.disconnect(); } catch (_) { /* already disconnected */ }
      }
      audio.current = null;
      audio.cues = null;
    }
    try { window.dispatchEvent(new CustomEvent('hubie:speech', { detail: { id: item.id, phase: 'end' } })); } catch (_) { /* no host */ }
    const msg = { type: 'speech_end', id: item.id, ok: !!ok };
    if (!ok && error) msg.error = error;
    send(msg);
    setTimeout(pump, 0);
  }

  function stopSpeech() {
    const pending = audio.queue.splice(0, audio.queue.length);
    for (const item of pending) { item.cancelled = true; finish(item, false, 'stopped'); }
    if (audio.current) { audio.current.cancelled = true; finish(audio.current, false, 'stopped'); }
  }

  function updateLipSync(t, dt) {
    const speaking = !!audio.source;
    if (speaking && audio.analyser) {
      audio.analyser.getFloatTimeDomainData(audio.buf);
      let sum = 0;
      for (let i = 0; i < audio.buf.length; i++) sum += audio.buf[i] * audio.buf[i];
      st.rms = Math.sqrt(sum / audio.buf.length);
      // The peak decays slowly so quiet and loud voices both use the full mouth range;
      // the floor keeps near-silence from normalizing up to "open".
      st.peak = Math.max(st.rms, st.peak * Math.exp(-dt / 4), 0.02);
      const lv = Math.min(1, st.rms / st.peak);
      st.level = Math.max(lv, st.level * Math.exp(-dt / 0.06));
    } else {
      st.rms = 0;
      st.level *= Math.exp(-dt / 0.035);    // closes in ~120ms after audio ends
    }
    const L = st.level;
    let want = st.mouthState;
    if (st.mouthState === 'closed') want = L > 0.53 ? 'open' : L > 0.18 ? 'mid' : 'closed';
    else if (st.mouthState === 'mid') want = L < 0.12 ? 'closed' : L > 0.53 ? 'open' : 'mid';
    else want = L < 0.12 ? 'closed' : L < 0.47 ? 'mid' : 'open';
    if (want !== st.mouthState && t - st.mouthSince >= 70) {
      st.mouthState = want;
      st.mouthSince = t;
    }
  }

  // Rhubarb's fast recognizer leans on small shapes (teeth together, rest), which reads as
  // a barely moving mouth. Loud moments open the shape one step; M/B/P (A) stay closed.
  function louder(v, level) {
    if (v === 'X') return level > 0.7 ? 'C' : level > 0.35 ? 'MID' : 'X';
    if (v === 'B') return level > 0.85 ? 'D' : level > 0.6 ? 'C' : 'B';
    if (v === 'C') return level > 0.75 ? 'D' : 'C';
    return v;
  }

  function currentViseme() {
    const cues = audio.cues;
    if (!cues || !audio.source) return null;
    // Follow the audio clock while it runs. If autoplay rules keep the context suspended
    // that clock is frozen, so follow wall time instead: he still visibly talks, silently.
    const at = audio.ctx.state === 'running'
      ? audio.ctx.currentTime - audio.t0
      : (performance.now() - audio.t0Wall) / 1000;
    while (audio.cueIdx + 1 < cues.length && cues[audio.cueIdx + 1][0] <= at) audio.cueIdx++;
    return cues[audio.cueIdx][1];
  }

  // Each overlay group remembers what it showed before, so changes can crossfade.
  const fade = { mouth: { cur: null, prev: null, at: 0 }, eyes: { cur: null, prev: null, at: 0 } };
  function showOverlay(group, name, t) {
    const f = fade[group];
    if (name === f.cur) return;
    f.prev = f.cur;
    f.cur = name;
    f.at = t;
  }

  // ---------------------------------------------------------------- update
  function update(t, dt) {
    const sec = (t - startedAt) / 1000;
    const expr = EXPRESSIONS[st.expression];
    updateLipSync(t, dt);

    // Blinks and glances.
    if (!st.blinkNext) { st.blinkNext = t + rand(3000, 7000); st.glanceNext = t + rand(8000, 18000); }
    if (t >= st.blinkNext && t >= st.blinkUntil) {
      st.blinkStart = t;
      st.blinkUntil = t + rand(110, 150) + 2 * BLINK_HALF_MS;
      if (st.blinkDouble) { st.blinkDouble = false; st.blinkNext = t + rand(3000, 7000); }
      else if (Math.random() < 0.15) { st.blinkDouble = true; st.blinkNext = st.blinkUntil + rand(90, 140); }
      else st.blinkNext = t + rand(3000, 7000);
    }
    if (st.expression === 'neutral' && t >= st.glanceNext) {
      st.glanceUntil = t + rand(1000, 3000);
      st.glanceNext = st.glanceUntil + rand(8000, 20000);
    }

    // Reaction.
    if (st.reaction) {
      reactionPose(st.reaction, (t - st.reaction.t0) / 1000, rp);
      if (rp.done) { st.reaction = null; reactionPose(null, 0, rp); }
    } else reactionPose(null, 0, rp);

    const bodyPose = rp.pose && rig.poses.has(rp.pose) ? rp.pose : null;
    if (bodyPose !== st.bodyPose) {
      st.bodyPose = bodyPose;
      st.charScale.v += 0.3;                           // cartoon squash on the pose snap
    }

    stepSpring(st.charY, rp.charY, rp.k, rp.c, dt);
    stepSpring(st.charScale, rp.charScale, 120, 2 * Math.sqrt(120), dt);
    st.charY.x = clamp(st.charY.x, -46 / CFG.scale, 70 / CFG.scale);

    // Expression pose eases in rather than snapping.
    const ease = 1 - Math.exp(-dt * 5);
    st.exprTilt += (expr.tilt - st.exprTilt) * ease;
    st.exprDy += (expr.dy - st.exprDy) * ease;

    // Head relative to body (source px / degrees).
    let headDeg = 1.5 * Math.sin(2 * Math.PI * sec / 7) + st.exprTilt + rp.headDeg;
    let headDy = 1.0 * Math.sin(2 * Math.PI * sec / 5.3 + 1) + st.exprDy + rp.headDy;
    let headDx = 0.6 * Math.sin(2 * Math.PI * sec / 9.1);
    headDy += 2.2 * st.level;                                        // talking bob
    headDeg += 0.8 * st.level * Math.sin(2 * Math.PI * sec * 1.7);
    if (expr.chuckle) {
      const tau = ((t - st.exprSince) / 1000) % 3.2;
      headDeg += tau < 1.2 ? 1.3 * (1 - tau / 1.2) * Math.sin(2 * Math.PI * 6 * tau) : 0;
    }
    headDeg = clamp(headDeg, -LIMIT.headDeg, LIMIT.headDeg);
    headDy = clamp(headDy, -LIMIT.headPx, LIMIT.headPx);
    headDx = clamp(headDx, -LIMIT.headPx, LIMIT.headPx);

    const breath = 1 + 0.004 * (1 - Math.cos(2 * Math.PI * sec / 4));   // 1.000..1.008

    // Where the hat would sit if it were glued to the head (character frame).
    const [hx, hy] = rig.headPivot;
    const [px, py] = rig.hatPivot;
    const a = headDeg * DEG;
    const rx = hx + (px - hx) * Math.cos(a) - (py - hy) * Math.sin(a) + headDx;
    const ry = hy + (px - hx) * Math.sin(a) + (py - hy) * Math.cos(a) + headDy;
    const gx = rx;
    const gy = 1199 + (ry - 1199) * breath + st.charY.x;
    if (!st.hatPrimed) { st.hatX.x = gx; st.hatY.x = gy; st.hatA.x = headDeg; st.hatPrimed = true; }

    // The hat chases that spot on a damped spring, so it lags and overshoots.
    stepSpring(st.hatX, gx, 90, 9, dt);
    stepSpring(st.hatY, gy + rp.hatDy, 90, 9, dt);
    stepSpring(st.hatA, headDeg + rp.hatDeg, 70, 7, dt);
    // Soft limits: saturate smoothly instead of hitting a wall.
    const soft = (v, lim) => lim * Math.tanh(v / lim);
    const wx = st.hatX.x - gx, wy = st.hatY.x - gy;
    const hatDx = soft(wx * Math.cos(-a) - wy * Math.sin(-a), LIMIT.hatPx);   // into the head's rotated frame
    const hatDy = soft(wx * Math.sin(-a) + wy * Math.cos(-a), LIMIT.hatPx);
    const hatDeg = soft(st.hatA.x - headDeg, LIMIT.hatDeg);

    // Overlays: blink > glance > lip sync / expression.
    let eyes = firstAvailable('eyes', expr.eyes);
    if (st.expression === 'neutral' && t < st.glanceUntil) eyes = firstAvailable('eyes', ['eyes_look_' + CFG.look]);
    const eyesShut = eyes === 'eyes_happy' || eyes === 'eyes_closed';
    if (t < st.blinkUntil && !eyesShut) {
      // half -> closed -> half, so the lid travels instead of popping.
      const edge = t - st.blinkStart < BLINK_HALF_MS || st.blinkUntil - t < BLINK_HALF_MS;
      eyes = firstAvailable('eyes', edge ? ['eyes_half', 'eyes_closed'] : ['eyes_closed']) || eyes;
    }
    let mouth;
    const vis = currentViseme();
    st.viseme = vis || '-';
    if (vis) {
      mouth = firstAvailable('mouth', VISEMES[louder(vis, st.level)]);
    } else if (audio.source || st.mouthState !== 'closed') {
      mouth = st.mouthState === 'open' ? firstAvailable('mouth', ['mouth_open', 'mouth_mid'])
        : st.mouthState === 'mid' ? firstAvailable('mouth', ['mouth_mid']) : null;
    } else mouth = firstAvailable('mouth', expr.mouth);

    showOverlay('eyes', eyes, t);
    showOverlay('mouth', mouth, t);
    st.pose = { t, breath, headDeg, headDx, headDy, hatDx, hatDy, hatDeg, charY: st.charY.x, charScale: st.charScale.x, body: st.bodyPose };
  }

  // ---------------------------------------------------------------- render
  function setBase() {
    ctx.setTransform(CFG.scale, 0, 0, CFG.scale, CFG.x - 656 * CFG.scale, CFG.y - DESK.bottom * CFG.scale);
  }

  // Hands follow small sinks (lean_in) and every jump. In a deep sink they slide back
  // toward him instead, so they leave over the desk's back edge rather than sinking
  // through the desk top, and are fully behind it at the bottom of a slump.
  const ARMS_FOLLOW = 30;            // source px of sink the hands simply follow
  const ARMS_BOTTOM = 1195;          // lowest arm pixel in source coords (fingertips)
  const MAX_SINK = 60;               // screen px, the slump depth
  function armsOffset(charY) {
    if (charY <= ARMS_FOLLOW) return charY;
    const deepest = MAX_SINK / CFG.scale;
    const travel = ARMS_BOTTOM - DESK.back + ARMS_FOLLOW + 8;   // until the fingertips clear the back edge
    const k = travel / Math.max(1, deepest - ARMS_FOLLOW);
    return ARMS_FOLLOW - k * (charY - ARMS_FOLLOW);
  }

  function applyCharacter(p, armsPass) {
    // Whole-character moves: scale about the desk's front edge, then vertical offset, then breathing.
    ctx.translate(656, DESK.front);
    ctx.scale(p.charScale, p.charScale);
    ctx.translate(-656, -DESK.front);
    ctx.translate(0, armsPass ? armsOffset(p.charY) : p.charY);
    ctx.translate(SRC_W / 2, 1199);
    ctx.scale(1, p.breath);
    ctx.translate(-SRC_W / 2, -1199);
  }

  function applyHead(p) {
    const [hx, hy] = rig.headPivot;
    ctx.translate(p.headDx, p.headDy);
    ctx.translate(hx, hy);
    ctx.rotate(p.headDeg * DEG);
    ctx.translate(-hx, -hy);
  }

  function drawOverlay(group, name, alpha) {
    const o = name && rig.overlays[group].get(name);
    if (!o || alpha <= 0) return;
    ctx.globalAlpha = alpha;
    ctx.drawImage(o.bmp, o.x, o.y);
    ctx.globalAlpha = 1;
  }

  // The patches cover nearly the same area, so drawing the old one solid and the new one
  // over it at rising alpha is a clean crossfade; only a change to or from the base face
  // has to fade the old patch itself.
  function drawGroup(group, t) {
    const f = fade[group];
    const k = clamp((t - f.at) / FADE_MS[group], 0, 1);
    if (k < 1 && f.prev) drawOverlay(group, f.prev, f.cur ? 1 : 1 - k);
    drawOverlay(group, f.cur, k);
  }

  function render() {
    const p = st.pose;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (CFG.bg) { ctx.fillStyle = CFG.bg; ctx.fillRect(0, 0, W, H); }
    if (!p) return;

    // 0. Behind Hubie: the chair.
    if (rig.sceneRear) {
      ctx.save();
      setBase();
      drawScene(rig.sceneRear);
      ctx.restore();
    }

    // 1. Torso: everything of the body above the desk's back edge (static clip).
    ctx.save();
    setBase();
    ctx.beginPath();
    ctx.rect(DESK.left - 2000, -5000, DESK.right - DESK.left + 4000, 5000 + DESK.back);
    ctx.clip();
    applyCharacter(p);
    const poseBody = p.body ? rig.poses.get(p.body) : null;
    if (poseBody) ctx.drawImage(poseBody, 0, 0);       // pose torsos run to the canvas bottom
    else {
      if (rig.ext) ctx.drawImage(rig.ext, 0, rig.extY);
      if (rig.body) ctx.drawImage(rig.body, 0, 0);
    }
    ctx.restore();

    // 2. Desk, mat, keyboard, mouse.
    if (rig.sceneBack) {
      ctx.save();
      setBase();
      drawScene(rig.sceneBack);
      ctx.restore();
    }

    // 3. Forearms and hands on the desk top, between its back edge and its front face.
    if (!poseBody && (rig.arms || (rig.body && rig.armPaths.length))) {
      ctx.save();
      setBase();
      ctx.beginPath();
      // Starts a few px above the torso clip: two antialiased clip edges at the same line
      // leave a hairline seam with the desk's back edge showing through.
      ctx.rect(DESK.left - 2000, DESK.back - 4, DESK.right - DESK.left + 4000, DESK.front - DESK.back + 4);
      ctx.clip();
      applyCharacter(p, true);
      if (rig.armsStatic && rig.handL && rig.handR) {
        handOffsets(p.t, hand);
        ctx.drawImage(rig.armsStatic, 0, 0);
        ctx.drawImage(rig.handL, hand.lx, hand.ly);
        ctx.drawImage(rig.handR, hand.rx, hand.ry);
      } else if (rig.arms) ctx.drawImage(rig.arms, 0, 0);
      else {
        for (const path of rig.armPaths) {         // coarse fallback: clips some jacket too
          ctx.save();
          ctx.clip(path);
          ctx.drawImage(rig.body, 0, 0);
          ctx.restore();
        }
      }
      ctx.restore();
    }

    // 4. Head with face overlays (eyes first, mouth on top), then the hat as its child.
    ctx.save();
    setBase();
    applyCharacter(p);
    applyHead(p);
    if (rig.head) ctx.drawImage(rig.head, 0, 0);
    drawGroup('eyes', p.t);
    drawGroup('mouth', p.t);
    if (rig.hat) {
      const [px, py] = rig.hatPivot;
      ctx.translate(p.hatDx, p.hatDy);
      ctx.translate(px, py);
      ctx.rotate(p.hatDeg * DEG);
      ctx.translate(-px, -py);
      ctx.drawImage(rig.hat, 0, 0);
    }
    ctx.restore();

    // 5. The desk's front face again, over anything sinking behind it, then the props
    //    standing in front of him (mug, figurine, books, mic).
    ctx.save();
    setBase();
    if (rig.sceneBack) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(DESK.left - 2000, DESK.front, DESK.right - DESK.left + 4000, 5000);
      ctx.clip();
      drawScene(rig.sceneBack);
      ctx.restore();
    }
    if (rig.sceneFront) drawScene(rig.sceneFront);
    ctx.restore();
  }

  // ---------------------------------------------------------------- loop
  let lastFrame = 0;
  let framesThisWindow = 0;
  let fpsWindowStart = performance.now();
  let fps = 0;

  function frame(t) {
    requestAnimationFrame(frame);
    if (lastFrame && t - lastFrame < FRAME_MS - 2) return;
    const dt = lastFrame ? Math.min(0.1, (t - lastFrame) / 1000) : FRAME_MS / 1000;
    lastFrame = t;
    try {
      update(t, dt);
      render();
    } catch (err) {
      frameErrors++;
      logError('frame', err);
    }
    framesThisWindow++;
    if (t - fpsWindowStart >= 1000) {
      fps = (framesThisWindow * 1000) / (t - fpsWindowStart);
      framesThisWindow = 0;
      fpsWindowStart = t;
    }
    if (CFG.debug) updateDebug(t);
  }

  // ---------------------------------------------------------------- websocket
  let ws = null;
  let backoff = 500;
  let reconnectTimer = 0;

  function send(obj) {
    if (!ws || ws.readyState !== 1) return;
    try { ws.send(JSON.stringify(obj)); } catch (err) { asyncErrors++; logError('send', err); }
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = 0; connect(); }, backoff);
    backoff = Math.min(5000, backoff * 2);
  }

  function connect() {
    let sock;
    try {
      sock = new WebSocket(CFG.ws);
    } catch (err) {
      logError('ws', err);
      scheduleReconnect();
      return;
    }
    ws = sock;
    sock.onopen = () => {
      backoff = 500;
      send({ type: 'ready', overlays: availableOverlays() });
    };
    sock.onmessage = (ev) => {
      if (typeof ev.data !== 'string' || ev.data.length > MAX_MSG_CHARS + 4096) return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      try { handle(msg); } catch (err) { asyncErrors++; logError('message', err); }
    };
    sock.onerror = () => { /* onclose follows and reconnects */ };
    sock.onclose = () => {
      sock.onopen = sock.onmessage = sock.onerror = sock.onclose = null;
      if (ws === sock) ws = null;
      scheduleReconnect();
    };
  }

  function handle(msg) {
    if (!msg || typeof msg !== 'object' || Array.isArray(msg)) return;
    switch (msg.type) {
      case 'say': say(msg); break;
      case 'stop': stopSpeech(); break;
      case 'expression': setExpression(msg.name); break;
      case 'reaction': react(msg.name); break;
      case 'typing': typing(msg.seconds); break;
      case 'ping': send({ type: 'pong' }); break;
      default: break;                                   // unknown types are ignored
    }
  }

  setInterval(() => {
    send({
      type: 'heartbeat',
      fps: Math.round(fps * 10) / 10,
      state: audio.current ? 'speaking' : 'idle',
      queue: audio.queue.length,
      uptime_s: Math.round((performance.now() - startedAt) / 1000),
      errors: frameErrors + asyncErrors,
      speech_failures: speechFailures,
    });
  }, 5000);

  // ---------------------------------------------------------------- local API + debug
  function makeTestWav(seconds) {
    // Amplitude-modulated noise in syllable-sized bursts: enough to drive the mouth.
    const rate = 16000, n = Math.floor(seconds * rate);
    const buf = new ArrayBuffer(44 + n * 2);
    const v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true);
    v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, n * 2, true);
    let lp = 0;
    for (let i = 0; i < n; i++) {
      const tt = i / rate;
      const syl = Math.max(0, Math.sin(2 * Math.PI * 4.2 * tt)) ** 0.7;
      const word = Math.sin(2 * Math.PI * 0.7 * tt) > -0.6 ? 1 : 0.05;
      lp += 0.25 * (Math.random() * 2 - 1 - lp);
      v.setInt16(44 + i * 2, clamp(lp * syl * word * 0.9, -1, 1) * 32767, true);
    }
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  window.hubie = {
    say: (opts) => say(opts && typeof opts === 'object' ? opts : {}),
    stop: () => stopSpeech(),
    wake: () => wakeAudio(),      // for a host page (stage) to call from its own click
    expression: (name) => setExpression(name),
    reaction: (name) => react(name),
    typing: (seconds) => typing(seconds),
    testSpeech: (seconds) => say({ audio: makeTestWav(clamp(Number(seconds) || 3, 0.5, 20)) }),
    stats: () => ({
      fps: Math.round(fps * 10) / 10,
      state: audio.current ? 'speaking' : 'idle',
      queue: audio.queue.length,
      errors: frameErrors + asyncErrors,
      speech_failures: speechFailures,
      uptime_s: Math.round((performance.now() - startedAt) / 1000),
      expression: st.expression,
      mouth: fade.mouth.cur || 'base',
      viseme: st.viseme,
      overlays: availableOverlays(),
      audio: audio.ctx ? audio.ctx.state : 'none',
      ws: ws ? ws.readyState : -1,
    }),
  };

  let debugPre = null;
  let debugNext = 0;
  function buildDebug() {
    const root = document.getElementById('debug');
    root.style.display = 'block';
    const section = (title, names, fn) => {
      const h = document.createElement('h4');
      h.textContent = title;
      root.appendChild(h);
      for (const n of names) {
        const b = document.createElement('button');
        b.textContent = n;
        b.addEventListener('click', () => fn(n));
        root.appendChild(b);
      }
    };
    section('expression', Object.keys(EXPRESSIONS), setExpression);
    section('reaction', [...REACTIONS], react);
    section('speech', ['test speech', 'stop'], (n) => (n === 'stop' ? stopSpeech() : window.hubie.testSpeech()));
    debugPre = document.createElement('pre');
    root.appendChild(debugPre);
  }
  function updateDebug(t) {
    if (!debugPre || t < debugNext) return;
    debugNext = t + 250;
    const s = window.hubie.stats();
    debugPre.textContent =
      'fps    ' + s.fps + '\nrms    ' + st.rms.toFixed(3) + '  level ' + st.level.toFixed(2) +
      '\nmouth  ' + (fade.mouth.cur || 'base') + '  viseme ' + st.viseme + '\nstate  ' + s.state + '  queue ' + s.queue +
      '\nexpr   ' + s.expression + '\naudio  ' + (s.audio === 'suspended' ? 'BLOCKED: click the page' : s.audio) + '  ws ' + s.ws + '\nerrors ' + s.errors;
  }

  // ---------------------------------------------------------------- start
  if (CFG.debug) buildDebug();
  // A browser tab (unlike OBS or Chromium with --autoplay-policy=no-user-gesture-required)
  // keeps audio suspended until the page gets a click or key press; use the first one.
  const wakeAudio = () => {
    try {
      ensureAudio();
      if (audio.ctx.state !== 'running') audio.ctx.resume().catch(() => {});
    } catch (err) { logError('wake audio', err); }
  };
  window.addEventListener('pointerdown', wakeAudio, true);
  window.addEventListener('keydown', wakeAudio, true);
  window.addEventListener('error', (e) => { asyncErrors++; logError('window', e.error || e.message); });
  window.addEventListener('unhandledrejection', (e) => { asyncErrors++; logError('promise', e.reason); });
  requestAnimationFrame(frame);
  loadAssets()
    .catch((err) => logError('assets', err))
    .finally(connect);
})();
