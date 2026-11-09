// The socket lives here, not in the page: the page's CSP and private-network rules
// don't apply to the extension's own origin. Content scripts relay through us.
const URL_WS = 'ws://127.0.0.1:8768';
let ws = null;
let backoff = 500;

function connect() {
  if (ws && ws.readyState !== WebSocket.CLOSED) return;   // already connected or connecting
  ws = new WebSocket(URL_WS);
  ws.onopen = () => { backoff = 500; ws.send(JSON.stringify({ type: 'hello', agent: 'extension' })); report(); };
  ws.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'ping') { ws.send('{"type":"pong"}'); return; }
    if (msg.type === 'status') { report(); return; }
    // Picks up new extension code from disk without anyone clicking reload in chrome://extensions.
    if (msg.type === 'reload_extension') { chrome.runtime.reload(); return; }
    if (msg.type === 'stream_window') { streamWindow(msg); return; }
    if (msg.type === 'navigate') { navigate(msg); return; }
    if (msg.type === 'mute') { mute(msg); return; }
    // Commands go to ONE tab (the game Hubie plays), never to every Housebets tab open.
    const t = await targetTab();
    if (!t) { ws.send(JSON.stringify({ type: 'done', id: msg.id, ok: false, error: 'no housebets game tab' })); return; }
    chrome.tabs.sendMessage(t.id, msg).catch(() => {});
  };
  ws.onclose = () => { ws = null; setTimeout(connect, backoff); backoff = Math.min(5000, backoff * 2); };
  ws.onerror = () => {};
}
connect();

// Chrome stops an idle extension worker, and a reconnect timer dies with it, so after the show
// runner restarts nothing would ever reconnect. An alarm (and any message from a Housebets page)
// wakes the worker; it then reconnects if needed.
const ensureConnected = () => { if (!ws || ws.readyState === WebSocket.CLOSED) connect(); };
chrome.alarms.create('hubie-keepalive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(ensureConnected);

// The tab Hubie plays: the one moved into the stream window if any, else a Housebets game page.
async function targetTab() {
  const { streamTab } = await chrome.storage.session.get('streamTab');
  if (streamTab) {
    try { return await chrome.tabs.get(streamTab); } catch { await chrome.storage.session.remove('streamTab'); }
  }
  const tabs = (await chrome.tabs.query({})).filter((t) => /^https:\/\/([\w-]+\.)?housebets\.com\/games\//.test(t.url || ''));
  // After a Chrome restart the remembered tab is gone: prefer a game tab already in a popup
  // (the stream window) over one in a normal window.
  for (const t of tabs) {
    try {
      if ((await chrome.windows.get(t.windowId)).type === 'popup') {
        // Re-arm the stay-on-the-game guard (session storage is wiped when the extension reloads).
        await chrome.storage.session.set({ streamTab: t.id, streamUrl: t.url.split('?')[0] });
        return t;
      }
    } catch { /* closed */ }
  }
  return tabs[0] || null;
}

// Mute or unmute the game tab (rapid auto play makes the game's click sounds a constant rattle).
async function mute(msg) {
  const reply = (ok, data, error) => ws.send(JSON.stringify({ type: 'done', id: msg.id, ok, data, error }));
  try {
    const t = await targetTab();
    if (!t) return reply(false, null, 'no housebets game tab');
    await chrome.tabs.update(t.id, { muted: !!msg.on });
    reply(true, { muted: !!msg.on });
  } catch (e) { reply(false, null, String(e.message || e)); }
}

// Load another Housebets game in the game tab (same window, so OBS's capture is unchanged) and
// reply once the page has loaded. Only housebets.com game pages are allowed.
async function navigate(msg) {
  const reply = (ok, data, error) => ws.send(JSON.stringify({ type: 'done', id: msg.id, ok, data, error }));
  try {
    const url = new URL(msg.url);
    if (!/(^|\.)housebets\.com$/.test(url.hostname) || !url.pathname.startsWith('/games/')) return reply(false, null, 'not a housebets game url');
    let t = await targetTab();
    if (!t) {
      // No game tab at all (closed, crashed, Chrome restarted): open the stream window ourselves.
      const win = await chrome.windows.create({ url: url.href, type: 'popup', focused: false, left: 40, top: 40, width: 1350, height: 834 });
      t = win.tabs[0];
      await chrome.storage.session.set({ streamTab: t.id, streamUrl: url.origin + url.pathname });
      await new Promise((resolve) => {
        const done = (id, info) => { if (id === t.id && info.status === 'complete') { chrome.tabs.onUpdated.removeListener(done); resolve(); } };
        chrome.tabs.onUpdated.addListener(done);
        setTimeout(() => { chrome.tabs.onUpdated.removeListener(done); resolve(); }, 20000);
      });
      return reply(true, { url: url.href, opened: true });
    }
    const loaded = new Promise((resolve) => {
      const done = (id, info) => { if (id === t.id && info.status === 'complete') { chrome.tabs.onUpdated.removeListener(done); resolve(); } };
      chrome.tabs.onUpdated.addListener(done);
      setTimeout(() => { chrome.tabs.onUpdated.removeListener(done); resolve(); }, 20000);
    });
    // Point the stay-on-the-game guard at the new game first, or it would pull the tab back.
    const { streamTab } = await chrome.storage.session.get('streamTab');
    if (streamTab === t.id) await chrome.storage.session.set({ streamUrl: url.origin + url.pathname });
    await chrome.tabs.update(t.id, { url: url.href });
    await loaded;
    reply(true, { url: url.href });
  } catch (e) { reply(false, null, String(e.message || e)); }
}

// Move the game tab into its own popup window whose page area is exactly width x height CSS px,
// so OBS can capture it into the monitor screen. Replies with how much window chrome to crop.
async function streamWindow(msg) {
  const reply = (ok, data, error) => ws.send(JSON.stringify({ type: 'done', id: msg.id, ok, data, error }));
  try {
    const t = await targetTab();
    if (!t) return reply(false, null, 'no housebets game tab');
    let win = await chrome.windows.get(t.windowId);
    if (win.type !== 'popup') {
      win = await chrome.windows.create({ tabId: t.id, type: 'popup', focused: false, left: msg.left ?? 40, top: msg.top ?? 40 });
    }
    await chrome.storage.session.set({ streamTab: t.id, streamUrl: t.url.split('?')[0] });
    const measure = async () => (await chrome.scripting.executeScript({
      target: { tabId: t.id }, func: () => [innerWidth, innerHeight, outerWidth, outerHeight, devicePixelRatio],
    }))[0].result;
    let [iw, ih, ow, oh] = await measure();
    await chrome.windows.update(win.id, { width: msg.width + (ow - iw), height: msg.height + (oh - ih) });
    await new Promise((r) => setTimeout(r, 400));
    const [iw2, ih2, ow2, oh2, dpr] = await measure();
    reply(true, { window: win.id, inner: [iw2, ih2], outer: [ow2, oh2], chrome_top: oh2 - ih2, chrome_side: (ow2 - iw2) / 2, dpr });
  } catch (e) { reply(false, null, String(e.message || e)); }
}

// Diagnostics: which Housebets tabs exist in this profile and whether our script answers in them.
async function report() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const all = await chrome.tabs.query({});
  const tabs = [];
  for (const t of all) {
    if (!/housebets/i.test(t.url || t.pendingUrl || '')) continue;
    let alive = false;
    try { alive = !!(await chrome.tabs.sendMessage(t.id, { type: 'ping' })); } catch (e) { alive = 'no: ' + String(e.message || e).slice(0, 120); }
    if (alive !== true) {
      // Tabs opened before this extension (re)loaded don't get the content script; attach it now.
      try {
        await chrome.scripting.executeScript({ target: { tabId: t.id, allFrames: true }, files: ['content.js'] });
        alive = 'injected';
      } catch (e) { alive += ' | inject failed: ' + String(e.message || e).slice(0, 120); }
    }
    tabs.push({ id: t.id, url: t.url, title: t.title, status: t.status, alive });
  }
  ws.send(JSON.stringify({ type: 'status', tabs, total_tabs: all.length }));
}
chrome.tabs.onUpdated.addListener(async (id, info, tab) => {
  if (info.status !== 'complete') return;
  report();
  // The stream tab must stay on the game: if the site navigates it elsewhere, go back.
  const { streamTab, streamUrl } = await chrome.storage.session.get(['streamTab', 'streamUrl']);
  // Anywhere but a Housebets game page sends it back, whatever navigated it (a site link, a redirect).
  if (id === streamTab && streamUrl && !/^https:\/\/([\w-]+\.)?housebets\.com\/games\/housegames-/.test(tab.url || '')) {
    setTimeout(() => chrome.tabs.update(id, { url: streamUrl }).catch(() => {}), 5000);
  }
});

chrome.runtime.onMessage.addListener((msg, sender) => {
  ensureConnected();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ ...msg, tab: sender.tab && sender.tab.id, frame: sender.frameId }));
  }
});

// MV3 workers sleep after ~30s idle; socket traffic every 20s keeps this one alive.
setInterval(() => { if (ws && ws.readyState === WebSocket.OPEN) ws.send('{"type":"keepalive"}'); }, 20000);
