// Stream HUD: autonomy clock, chat, session stats, game vote. Runs for days, so all input
// is validated, the chat list is capped, and a bad message is dropped, never thrown.
'use strict';

(function () {
  const qs = new URLSearchParams(location.search);
  const WS_URL = (() => {
    const v = qs.get('ws');
    return v && /^wss?:\/\/[^\s]{1,500}$/.test(v) ? v : 'ws://127.0.0.1:8767';
  })();
  const MAX_CHAT = 14;
  const MAX_TEXT = 180;
  const MAX_USER = 24;
  const MAX_OPTS = 5;
  const NAME_COLORS = ['#3ee6ff', '#de3cff', '#f2b33d', '#39e27d', '#ff8a3d', '#9d8cff', '#ff5fa2'];

  const $ = (id) => document.getElementById(id);
  const msgs = $('msgs');
  const clean = (s, n) => String(s).replace(/[\u0000-\u001f\u007f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]/g, '').slice(0, n).trim();
  const finite = (v, lo, hi) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : null);

  function nameColor(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return NAME_COLORS[h % NAME_COLORS.length];
  }

  // ---- widgets ------------------------------------------------------------------
  function chat(m) {
    const user = clean(m.user || '', MAX_USER);
    const text = clean(m.text || '', MAX_TEXT);
    if (!user || !text) return;
    if (m.answering) for (const el of msgs.querySelectorAll('.answering')) el.classList.remove('answering');
    const row = document.createElement('div');
    row.className = 'msg' + (m.answering ? ' answering' : '');
    if (typeof m.id === 'string') row.dataset.id = m.id.slice(0, 64);
    const u = document.createElement('span');
    u.className = 'user';
    u.style.color = nameColor(user);
    u.textContent = user;
    const t = document.createElement('span');
    t.textContent = text;
    row.append(u, t);
    if (m.vote === true) {                       // "your vote counted"
      const tag = document.createElement('span');
      tag.className = 'votetag';
      tag.textContent = 'VOTE ✓';
      row.appendChild(tag);
    }
    msgs.appendChild(row);
    // Drop the oldest lines until everything fits, then enforce the hard cap.
    while (msgs.children.length > 1 && msgs.scrollHeight > msgs.clientHeight + 1) msgs.firstChild.remove();
    while (msgs.children.length > MAX_CHAT) msgs.firstChild.remove();
  }

  // Mark the line Hubie is answering, in place (never adds a second copy).
  function answering(m) {
    if (typeof m.id !== 'string') return;
    for (const el of msgs.querySelectorAll('.answering')) el.classList.remove('answering');
    for (const el of msgs.children) if (el.dataset.id === m.id) el.classList.add('answering');
  }

  function stats(m) {
    // "HOUSEBETS WHEEL" wrapped onto two lines in the console strip: the brand is on screen anyway.
    if (typeof m.game === 'string') $('s-game').textContent = clean(m.game.replace(/^\s*housebets\s+/i, ''), 16).toUpperCase();
    const rounds = finite(m.rounds, 0, 1e9);
    if (rounds !== null) $('s-rounds').textContent = Math.round(rounds).toLocaleString('en-US');
    const best = finite(m.best, 0, 1e9);
    // Big records read better without the decimals (and fit): 13,188× rather than 13188.31×.
    if (best !== null) $('s-best').textContent = (best >= 1000 ? Math.round(best).toLocaleString('en-US') : best.toFixed(2)) + '×';
    const streak = finite(m.streak, -1e6, 1e6);
    if (streak !== null) {
      const el = $('s-streak');
      el.textContent = streak === 0 ? '-' : (streak > 0 ? 'W' : 'L') + Math.abs(Math.round(streak));
      el.className = 'val' + (streak > 0 ? ' win' : streak < 0 ? ' loss' : '');
    }
    // Auto sessions: the pretend bet ladder (units) and bets left; hidden otherwise.
    if ('ladder' in m) {
      const l = m.ladder && typeof m.ladder === 'object' ? m.ladder : null;
      const u = l ? finite(l.units, 0, 1e9) : null, left = l ? finite(l.left, 0, 1000) : null;
      $('s-ladder-box').style.display = l ? '' : 'none';
      const short = u >= 1e6 ? (u / 1e6).toFixed(1) + 'M' : u >= 1000 ? (u / 1000).toFixed(u >= 1e4 ? 0 : 1) + 'k' : u >= 100 ? Math.round(u) : Math.round(u * 10) / 10;
      if (u !== null) $('s-ladder').textContent = short + 'u' + (left !== null ? ' · ' + left + ' left' : '');
    }
    // Real-site games play $0 bets and send no balance: the stat is hidden (it also frees room).
    const bal = finite(m.balance, -1e12, 1e12);
    $('s-balance').parentElement.style.display = 'balance' in m && m.balance === null ? 'none' : '';
    if (bal !== null) $('s-balance').textContent = Math.round(bal).toLocaleString('en-US');
  }

  function viewers(m) {
    const n = finite(m.count, 0, 1e9);
    $('viewers').textContent = n === null ? '' : Math.round(n).toLocaleString('en-US') + ' watching';
  }

  // Follower alerts play one at a time; a burst queues up (capped so a follow raid can't stall it).
  const followQ = [];
  let followBusy = false;
  function follow(m) {
    const user = m.user === null ? null : clean(m.user || '', MAX_USER);
    const n = finite(m.count, 1, 1e6) || 1;
    if (user === '' ) return;
    // (also used for "Clipped" moments: same card, different kicker)
    const kicker = typeof m.kicker === 'string' ? clean(m.kicker, 24) : 'New follower';
    if (followQ.length < 10) followQ.push({ who: user || (n > 1 ? n + ' new followers' : 'Thanks for the follow!'), kicker });
    if (!followBusy) nextFollow();
  }
  function nextFollow() {
    const item = followQ.shift();
    if (item === undefined) { followBusy = false; return; }
    followBusy = true;
    $('follow-who').textContent = item.who;
    document.querySelector('#follow .kicker').textContent = item.kicker;
    $('follow').classList.add('show');
    setTimeout(() => { $('follow').classList.remove('show'); setTimeout(nextFollow, 600); }, 5000);
  }

  // Now playing (NCS asks for a credit per track): shown on the hint line when a track starts,
  // and in the hint rotation while it plays.
  let nowPlaying = '';
  function music(m) {
    const title = clean(m.title || '', 60), artist = clean(m.artist || '', 40);
    nowPlaying = title ? (artist ? artist + ' - ' + title : title) : '';
    if (nowPlaying) { musicUntil = performance.now() + 8000; showHint(); }
  }
  let musicUntil = 0;

  let voteEndsAt = 0;
  let hints = [], hintIdx = 0, liveVote = null, voteResult = '', voteResultUntil = 0;
  function showHint() {
    const hint = $('cmdline');
    if (voteResult && performance.now() < voteResultUntil) {
      hint.replaceChildren(document.createTextNode('Chat picked: '));
      const b = document.createElement('b');
      b.textContent = voteResult;
      hint.appendChild(b);
      return;
    }
    const musicHint = nowPlaying && (performance.now() < musicUntil || hintIdx % (hints.length + 1) === hints.length);
    if (musicHint && !(liveVote && liveVote.length)) {
      hint.replaceChildren(document.createTextNode('♪ '));
      const b = document.createElement('b');
      b.textContent = nowPlaying;
      hint.append(b, document.createTextNode(' · NCS'));
      return;
    }
    if (liveVote && liveVote.length) {
      const left = Math.max(0, Math.round((voteEndsAt - performance.now()) / 1000));
      hint.replaceChildren(document.createTextNode('VOTING '));
      liveVote.slice(0, 3).forEach((o) => {
        hint.appendChild(document.createTextNode(' · '));
        const b = document.createElement('b');
        b.textContent = o.cmd.slice(1).toUpperCase() + ' ' + o.votes;
        hint.appendChild(b);
      });
      hint.appendChild(document.createTextNode(` · ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`));
      return;
    }
    if (!hints.length) return;
    const [label, cmds] = hints[hintIdx % hints.length];
    hint.replaceChildren(document.createTextNode(label));
    cmds.forEach((c, i) => {
      if (i) hint.appendChild(document.createTextNode(' · '));
      const b = document.createElement('b');
      b.textContent = c;
      hint.appendChild(b);
    });
  }
  setInterval(() => { hintIdx++; showHint(); }, 6000);
  setInterval(() => { if (liveVote && liveVote.length) showHint(); }, 1000);   // countdown
  function vote(m) {
    if (!Array.isArray(m.options)) return;
    const opts = m.options.slice(0, MAX_OPTS)
      .map((o) => ({ cmd: clean(o && o.cmd || '', 14), votes: finite(o && o.votes, 0, 1e9) || 0 }))
      .filter((o) => /^![a-z0-9_]{1,13}$/i.test(o.cmd));
    const total = opts.reduce((a, o) => a + o.votes, 0);
    const box = $('opts');
    // Rebuild only when the option set changes, so bars animate between updates.
    if (box.dataset.key !== opts.map((o) => o.cmd).join()) {
      box.replaceChildren(...opts.map((o) => {
        const el = document.createElement('div');
        el.className = 'opt';
        el.innerHTML = '<div class="bar"></div><span class="cmd"></span><span class="pct"></span>';
        el.querySelector('.cmd').textContent = o.cmd;     // never innerHTML with data
        return el;
      }));
      box.dataset.key = opts.map((o) => o.cmd).join();
    }
    opts.forEach((o, i) => {
      const pct = total ? Math.round((100 * o.votes) / total) : 0;
      box.children[i].querySelector('.bar').style.width = pct + '%';
      box.children[i].querySelector('.pct').textContent = pct + '%';
    });
    // The commands as a one-line hint at the bottom of the chat panel (room layout). The line is
    // narrow, so it rotates between a few short hints instead of listing everything.
    const all = m.options
      .map((o) => ({ cmd: clean(o && o.cmd || '', 14), votes: finite(o && o.votes, 0, 1e9) || 0 }))
      .filter((o) => /^![a-z0-9_]{1,13}$/i.test(o.cmd));
    hints = [['Pick a game: ', ['!<game>']]];
    if (all.length >= 2) hints.push(['Vote next game: ', all.slice(0, 4).map((o) => o.cmd)]);
    if (all.some((o) => o.cmd === '!mines')) hints.push(['Pick my mines: ', ['!mines 1-24']]);
    // While a vote is open the hint line becomes the live tally + countdown, so a voter can see
    // their vote counted and how long until it's decided.
    const ends = finite(m.ends_in, 0, 3600);
    if (ends !== null) voteEndsAt = performance.now() + ends * 1000;
    liveVote = m.open ? all.filter((o) => o.votes > 0).sort((a, b) => b.votes - a.votes) : null;
    if (typeof m.result === 'string' && m.result) { voteResult = clean(m.result, 20); voteResultUntil = performance.now() + 5000; }
    showHint();
  }

  let autonomySince = Date.now();
  function autonomy(m) {
    const since = finite(m.since, 0, Date.now());
    if (since !== null) autonomySince = since;
  }

  function tickClocks() {
    let s = Math.max(0, Math.floor((Date.now() - autonomySince) / 1000));
    const d = Math.floor(s / 86400); s %= 86400;
    const h = Math.floor(s / 3600); s %= 3600;
    const mnt = Math.floor(s / 60); s %= 60;
    const pad = (n) => String(n).padStart(2, '0');
    const clock = $('clock');
    // Built from numbers only, so innerHTML is safe here; it lets the units be styled.
    clock.innerHTML = (d ? d + '<span class="unit">d</span>' : '') + pad(h) + '<span class="unit">h</span>'
      + pad(mnt) + '<span class="unit">m</span>' + pad(s) + '<span class="unit">s</span>';
    const left = Math.max(0, Math.ceil((voteEndsAt - performance.now()) / 1000));
    $('v-timer').textContent = voteEndsAt ? (left ? left + 's left' : 'locked') : '';
  }
  setInterval(() => { try { tickClocks(); } catch (e) { console.error('[hud]', e); } }, 250);

  function handle(m) {
    if (!m || typeof m !== 'object') return;
    if (m.type === 'chat') chat(m);
    else if (m.type === 'answering') answering(m);
    else if (m.type === 'stats') stats(m);
    else if (m.type === 'vote') vote(m);
    else if (m.type === 'autonomy') autonomy(m);
    else if (m.type === 'viewers') viewers(m);
    else if (m.type === 'follow') follow(m);
    else if (m.type === 'music') music(m);
  }
  window.hud = { handle };

  // ---- data sources -------------------------------------------------------------
  function connect() {
    let backoff = 500;
    const open = () => {
      let ws;
      try { ws = new WebSocket(WS_URL); } catch (_) { return retry(); }
      ws.onopen = () => { backoff = 500; };
      ws.onmessage = (ev) => {
        if (typeof ev.data !== 'string' || ev.data.length > 8192) return;
        try { handle(JSON.parse(ev.data)); } catch (_) { /* malformed: ignore */ }
      };
      ws.onclose = () => { ws.onopen = ws.onmessage = ws.onclose = null; retry(); };
    };
    const retry = () => { setTimeout(open, backoff); backoff = Math.min(5000, backoff * 2); };
    open();
  }

  function demo() {
    const people = ['gemhunter', 'slothfan42', 'crashkid', 'minesweeper_irl', 'tophat_tony', 'luckylemon', 'paws_up', 'nora_bets'];
    const lines = ['push it hubie', 'cash out!!', 'that hat is lucky fr', '!mines', 'how many mines this time?',
      'sloth speed run lol', '!limbo', 'never rush a sloth', 'gg 41x', '!dice', 'what are the odds on 7 mines?', 'hubie for president'];
    const pick = (a) => a[Math.floor(Math.random() * a.length)];
    autonomy({ since: Date.now() - ((3 * 24 + 4) * 3600 + 12 * 60 + 33) * 1000 });
    stats({ game: 'Mines', rounds: 128, best: 41.6, streak: 3, balance: 12480 });
    viewers({ count: 37 });
    setTimeout(() => follow({ user: 'tophat_tony' }), 1500);
    const votes = { '!mines': 14, '!limbo': 9, '!dice': 6, '!plinko': 4 };
    const pushVote = () => vote({ options: Object.entries(votes).map(([cmd, v]) => ({ cmd, votes: v })), ends_in: 45 });
    pushVote();
    for (let i = 0; i < 5; i++) chat({ user: pick(people), text: pick(lines) });
    setInterval(() => {
      const text = pick(lines);
      chat({ user: pick(people), text, answering: Math.random() < 0.18 });
      if (votes[text] !== undefined) { votes[text]++; vote({ options: Object.entries(votes).map(([cmd, v]) => ({ cmd, votes: v })) }); }
    }, 1800);
    setInterval(() => { votes[pick(Object.keys(votes))] += 1; vote({ options: Object.entries(votes).map(([cmd, v]) => ({ cmd, votes: v })) }); }, 2500);
    setInterval(() => pushVote(), 45000);
  }

  if (qs.get('layout') === 'room') document.body.classList.add('room');
  if (qs.get('demo') === '1') demo();
  else connect();
})();
