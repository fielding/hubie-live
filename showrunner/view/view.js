'use strict';
// Game view: renders whatever game the show runner is playing. All values come from the
// show runner's own game state (numbers and fixed strings), but it is still built with
// DOM methods and textContent only.
(function () {
  const qs = new URLSearchParams(location.search);
  const WS_URL = /^wss?:\/\/[^\s]{1,500}$/.test(qs.get('ws') || '') ? qs.get('ws') : 'ws://127.0.0.1:8766';
  const card = document.getElementById('card');

  const SVG_NS = 'http://www.w3.org/2000/svg';
  function svg(paths) {
    const s = document.createElementNS(SVG_NS, 'svg');
    s.setAttribute('viewBox', '0 0 100 100');
    for (const [tag, attrs] of paths) {
      const el = document.createElementNS(SVG_NS, tag);
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
      s.appendChild(el);
    }
    return s;
  }
  const GEM = () => svg([
    ['polygon', { points: '50,92 8,38 26,12 74,12 92,38', fill: '#39e27d', stroke: '#0c0b18', 'stroke-width': 5, 'stroke-linejoin': 'round' }],
    ['polygon', { points: '50,92 30,38 50,12 70,38', fill: '#7cf5ad' }],
    ['polyline', { points: '8,38 92,38', stroke: '#0c0b18', 'stroke-width': 4, fill: 'none' }],
  ]);
  const MINE = () => svg([
    ...[0, 45, 90, 135].map((a) => ['rect', { x: 46, y: 6, width: 8, height: 88, rx: 4, fill: '#0c0b18', transform: `rotate(${a} 50 50)` }]),
    ['circle', { cx: 50, cy: 50, r: 30, fill: '#2b2838', stroke: '#0c0b18', 'stroke-width': 5 }],
    ['circle', { cx: 50, cy: 50, r: 9, fill: '#ff4d6d' }],
  ]);

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function stat(k, v, vcls) {
    const b = el('div', 'box');
    b.append(el('div', 'k', k), el('div', 'v' + (vcls ? ' ' + vcls : ''), v));
    return b;
  }
  function fairBox(f) {
    return el('div', 'fair', f ? `Provably fair · round ${f.nonce} · seed hash ${String(f.server_seed_hash).slice(0, 24)}…` : '');
  }
  const fmt = (n) => Number(n).toLocaleString('en-US', { maximumFractionDigits: 2 });

  const RENDER = {
    mines(state, event) {
      const r = state.round;
      const side = el('div', 'side');
      side.append(stat('Demo balance', fmt(state.balance)));
      if (r) {
        side.append(stat('Bet · Mines', `${r.bet} · ${r.mines}`),
          stat('Multiplier', r.multiplier.toFixed(2) + '×', r.status === 'busted' ? 'red' : 'green'),
          stat('Next gem', r.status === 'playing' ? r.next_multiplier.toFixed(2) + '×' : '-', 'cyan'));
      }
      side.append(fairBox(state.fair));
      const main = el('div', 'main');
      const grid = el('div', 'grid');
      const revealed = new Set(r ? r.revealed : []);
      const mines = new Set(r && r.mine_tiles ? r.mine_tiles : []);
      for (let i = 0; i < 25; i++) {
        const t = el('div', 'tile');
        if (revealed.has(i)) { t.classList.add('gem'); t.appendChild(GEM()); }
        else if (mines.has(i)) { t.classList.add('mine'); if (r.hit === i) t.classList.add('hit'); else t.classList.add('dim'); t.appendChild(MINE()); }
        else if (r && r.status !== 'playing') { t.classList.add('dim'); t.appendChild(GEM()); }
        grid.appendChild(t);
      }
      main.appendChild(grid);
      if (event && (event.kind === 'cashout' || event.kind === 'bust')) {
        const p = el('div', 'popup' + (event.kind === 'bust' ? ' bust' : ''));
        p.append(el('div', 'big', event.kind === 'bust' ? 'BUST' : event.multiplier.toFixed(2) + '×'),
          el('div', 'k', event.kind === 'bust' ? `-${fmt(event.lost)} demo` : `+${fmt(event.payout)} demo`));
        main.appendChild(p);
      }
      return [side, main];
    },

    dice(state, event) {
      const last = state.last;
      const side = el('div', 'side');
      side.append(stat('Demo balance', fmt(state.balance)));
      if (last) {
        side.append(stat('Bet · Chance', `${last.bet} · ${last.chance}%`),
          stat('Roll ' + last.side, (last.side === 'over' ? '> ' : '< ') + last.line.toFixed(2)),
          stat('Payout', last.win ? last.multiplier.toFixed(2) + '×' : '0×', last.win ? 'green' : 'red'));
      }
      side.append(fairBox(state.fair));
      const main = el('div', 'main');
      const d = el('div', 'dice');
      const roll = el('div', 'roll', last ? last.roll.toFixed(2) : '--.--');
      if (last) roll.style.color = last.win ? 'var(--green)' : 'var(--red)';
      const track = el('div', 'track');
      if (last) {
        const zone = el('div', 'zone');
        if (last.side === 'over') { zone.style.left = last.line + '%'; zone.style.right = '0'; }
        else { zone.style.left = '0'; zone.style.width = last.line + '%'; }
        const marker = el('div', 'marker');
        marker.style.left = 'calc(' + last.roll + '% - 3px)';
        track.append(zone, marker);
      }
      const ticks = el('div', 'ticks');
      for (const n of [0, 25, 50, 75, 100]) ticks.appendChild(el('span', '', String(n)));
      d.append(roll, track, ticks);
      main.appendChild(d);
      return [side, main];
    },
  };

  const sw = document.getElementById('switch');
  let swTimer = 0;
  RENDER.mock_mines = RENDER.mines;     // the offline practice copies draw like the real ones
  RENDER.mock_dice = RENDER.dice;

  function showSwitch(ev) {
    sw.querySelector('.why').textContent = ev.reason === 'vote' ? 'CHAT VOTED' : 'NOW PLAYING';
    sw.querySelector('.what').textContent = String(ev.title || '').slice(0, 20);
    const n = Number(ev.votes);
    sw.querySelector('.votes').textContent = ev.reason === 'vote' && Number.isFinite(n) ? `${n} vote${n === 1 ? '' : 's'}` : '';
    sw.classList.remove('on');
    void sw.offsetWidth;                         // restart the animation
    sw.classList.add('on');
    clearTimeout(swTimer);
    swTimer = setTimeout(() => sw.classList.remove('on'), 3500);
  }

  const seg = document.getElementById('segment');
  let segTimer = 0;
  function showSegment(m) {
    // Only our own assets: the show runner validated the path, and so do we.
    if (typeof m.image !== 'string' || !/^\/assets\/segments\/[\w./-]+$/.test(m.image) || m.image.includes('..')) return;
    const secs = Math.min(120, Math.max(1, Number(m.seconds) || 6));
    seg.querySelector('img').src = m.image;
    seg.classList.add('on');
    clearTimeout(segTimer);
    segTimer = setTimeout(() => seg.classList.remove('on'), secs * 1000);
  }

  // Over a real-site window (?realsite=1, the OBS front layer) the card goes transparent while
  // a real-site game is on, so the captured site shows through; simulated games still draw here.
  const overSite = new URLSearchParams(location.search).get('realsite') === '1';

  function render(msg) {
    if (msg.event && msg.event.kind === 'switch') showSwitch(msg.event);
    if (overSite) card.style.visibility = msg.state && msg.state.site ? 'hidden' : 'visible';
    const fn = RENDER[msg.game];
    const parts = fn ? fn(msg.state || {}, msg.event) : [el('div', 'main', '')];
    if (!fn) parts[0].appendChild(el('div', 'waiting', String(msg.title || msg.game || '').slice(0, 30)));
    card.replaceChildren(...parts);
  }

  let backoff = 500;
  function connect() {
    let ws;
    try { ws = new WebSocket(WS_URL); } catch (_) { return setTimeout(connect, backoff); }
    ws.onopen = () => { backoff = 500; document.getElementById('offline').classList.remove('on'); };   // the show runner re-sends it if it's still down
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string' || ev.data.length > 65536) return;
      try {
        const m = JSON.parse(ev.data);
        if (m && m.type === 'state' && typeof m.game === 'string') render(m);
        else if (m && m.type === 'segment') showSegment(m);
        else if (m && m.type === 'offline') document.getElementById('offline').classList.toggle('on', m.on === true);
      } catch (e) { console.error('[view]', e); }
    };
    ws.onclose = () => { ws.onopen = ws.onmessage = ws.onclose = null; setTimeout(connect, backoff); backoff = Math.min(5000, backoff * 2); };
  }
  connect();
})();
