// Runs in every Housebets frame. Reports what's on the page (so the show runner can
// learn and track the game UI) and performs clicks/inputs it is told to.
(() => {
  // Injected again into a tab that already has us (extension reloaded): keep one copy.
  // A copy left over from before an extension reload is dead (its runtime id is gone) and
  // must not block the new one.
  if (window.__hubieBridgeAlive && window.__hubieBridgeAlive()) return;
  window.__hubieBridgeAlive = () => { try { return !!chrome.runtime.id; } catch { return false; } };
  const send = (m) => { try { chrome.runtime.sendMessage(m); } catch { /* extension reloaded */ } };

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  };

  const describe = (el) => {
    const r = el.getBoundingClientRect();
    const attrs = {};
    for (const a of el.attributes) {
      if (a.name === 'style' || a.name === 'd') continue;
      attrs[a.name] = a.value.slice(0, 120);
    }
    return {
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 80),
      attrs,
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
    };
  };

  // Housebets renders its games inside open shadow roots, which plain querySelectorAll
  // doesn't enter; every lookup here walks into them.
  function roots(from = document) {
    const out = [from];
    for (let i = 0; i < out.length; i++) {
      for (const el of out[i].querySelectorAll('*')) if (el.shadowRoot) out.push(el.shadowRoot);
    }
    return out;
  }
  const deepAll = (selector, from = document) => roots(from).flatMap((r) => [...r.querySelectorAll(selector)]);

  // Housebets pages can hold several copies of a game (preloaded guest placeholders that say
  // "Log in", disabled). Only the live, logged-in copy may be read or clicked: scope "game"
  // searches inside that one.
  function liveGameRoot() {
    const hosts = deepAll('[data-housegames-direct][data-game-slug]').filter((h) => h.shadowRoot);
    const live = hosts.filter((h) => !h.shadowRoot.querySelector('[data-hg-guest-bet], [data-hg-connecting]'));
    // While a page loads, placeholder copies may not be marked yet: the live one is the copy whose
    // bet button is enabled and labelled.
    const working = live.filter((h) => [...h.shadowRoot.querySelectorAll('[class*="p-betbtn"]')]
      .some((b) => !b.disabled && (b.innerText || '').trim()));
    const pick = working[working.length - 1] || live[live.length - 1] || null;
    return pick ? pick.shadowRoot : null;
  }
  const scoped = (msg) => {
    if (msg.scope !== 'game') return document;
    return liveGameRoot();
  };

  function snapshot(reason) {
    const sel = 'button, a, input, select, textarea, [role=button], [role=tab], [role=switch], [tabindex], [onclick], [data-testid]';
    const controls = deepAll(sel).filter(visible).slice(0, 600).map(describe);
    const shadows = roots().slice(1).map((r) => ({
      host: describe(r.host),
      html: r.innerHTML.replace(/<(script|style|svg)[\s\S]*?<\/\1>/g, '<$1/>').slice(0, 300000),
    }));
    const clone = document.body ? document.body.cloneNode(true) : null;
    if (clone) clone.querySelectorAll('script, style, noscript, path, link').forEach((n) => n.remove());
    send({
      type: 'snapshot', reason,
      url: location.href, top: window === window.top, title: document.title,
      viewport: [innerWidth, innerHeight],
      controls,
      shadows,
      iframes: [...document.querySelectorAll('iframe')].map((f) => f.src).slice(0, 20),
      html: clone ? clone.outerHTML.slice(0, 400000) : '',
    });
  }

  // Snapshots are heavy (whole page + shadow roots), so they're only taken when asked for
  // ("probe"); a live game page changes constantly and must not be re-scanned all day.

  const find = (selector, index = 0, text = null, from = document) => {
    if (!from) return null;
    let els = deepAll(selector, from);
    if (text != null) els = els.filter((e) => (e.innerText || '').trim().toLowerCase() === String(text).toLowerCase());
    return els[index] || null;
  };

  const safeSnapshot = (reason) => {
    try { snapshot(reason); } catch (e) { send({ type: 'error', where: 'snapshot', error: String(e && e.stack || e).slice(0, 500), url: location.href }); }
  };
  // How this page came to be: a fresh navigation (ours when we switch games or reload), the page
  // reloading itself, or just this script being re-injected into a page that was already open
  // (extension reload), which isn't a page load at all.
  const nav = (performance.getEntriesByType('navigation')[0] || {}).type || 'unknown';
  send({ type: 'loaded', url: location.href, top: window === window.top, title: document.title,
         nav, reinjected: performance.now() > 10000 });
  // Nudge the extension worker now and then so it's awake to reconnect to the show runner.
  if (window === window.top) setInterval(() => send({ type: 'keepalive' }), 25000);

  // Stream mode: everything but the game card is hidden and the card is pinned full-width,
  // centred. The card's containers move with page scroll, so the page is held at the top.
  let streamTimer = 0;
  function streamCss(on) {
    let st = document.getElementById('hubie-stream-css');
    if (on) {
      if (!st) {
        st = document.createElement('style');
        st.id = 'hubie-stream-css';
        document.documentElement.appendChild(st);
      }
      // Always rewritten: an older version's rules may still be in the page.
      st.textContent = `
        html, body { overflow: hidden !important; background: #020c14 !important; }
        body * { visibility: hidden !important; }
        article[data-game-frame], article[data-game-frame] * { visibility: visible !important; }
        article[data-game-frame] { position: fixed !important; left: 0 !important; top: 50% !important;
          transform: translateY(-50%) !important; width: 100vw !important; max-width: none !important;
          margin: 0 !important; z-index: 2147483647 !important; }
        /* The game sizes itself from its container and can get stuck in a tall mobile layout
           after reflows; hold it to its desktop shape, fitted to the window. */
        article[data-game-frame] [data-testid$="-surface"] { aspect-ratio: 1214 / 630 !important;
          height: auto !important; min-height: 0 !important;
          max-height: calc(100vh - 90px) !important; }`;
    } else if (st) {
      st.remove();
    }
    clearInterval(streamTimer);
    const a = deepAll('article[data-game-frame]')[0];
    if (a) { a.style.transform = ''; a.style.transformOrigin = ''; }   // leftovers from an older version
    if (on) {
      window.scrollTo(0, 0);
      streamTimer = setInterval(checkStream, 2000);
    }
  }
  // Stream mode self-check: the page re-renders on its own schedule, so keep verifying that the
  // card is fully in the window and repair (and report) when it isn't.
  let lastBad = 0;
  function checkStream() {
    if (scrollX || scrollY) window.scrollTo(0, 0);
    const a = deepAll('article[data-game-frame]')[0];
    const r = a && a.getBoundingClientRect();
    const ok = r && r.top >= -4 && r.bottom <= innerHeight + 4 && r.width > innerWidth * 0.8;
    if (ok || Date.now() - lastBad < 10000) return;
    lastBad = Date.now();
    send({ type: 'error', where: 'stream_layout', card: r && [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)], viewport: [innerWidth, innerHeight] });
    const st = document.getElementById('hubie-stream-css');
    if (st) { st.remove(); void document.body.offsetHeight; document.documentElement.appendChild(st); }   // force a relayout
    window.dispatchEvent(new Event('resize'));
  }

  // Stream mode survives reloads of this tab (sessionStorage is per tab).
  try { if (window === window.top && sessionStorage.getItem('hubieStream') === '1') streamCss(true); } catch { /* storage blocked */ }

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg.type === 'ping') { reply(true); return; }
    if (msg.type === 'probe') { safeSnapshot('probe'); return; }
    // Only the top frame reports "not found"; blank sub-frames would otherwise answer first.
    const top = window === window.top;
    if (msg.type === 'query') {
      const from = scoped(msg);
      const els = from ? deepAll(msg.selector, from).slice(0, 200) : [];
      if (!els.length && !top) return;
      const data = els.map((el) => ({
        text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 200),
        cls: typeof el.className === 'string' ? el.className : '',
        value: 'value' in el ? String(el.value) : null,
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
        attrs: Object.fromEntries([...el.attributes].filter((a) => a.name.startsWith('data-') || a.name.startsWith('aria-')).map((a) => [a.name, a.value.slice(0, 80)])),
      }));
      send({ type: 'done', id: msg.id, ok: true, data, url: location.href });
      return;
    }
    // Stamp elements as already seen, so the next new one (e.g. a result chip) is unmistakable even
    // when its text is identical to the last ("Result NOPE, lost." fifty times in a row).
    if (msg.type === 'mark') {
      const from = scoped(msg);
      const els = from ? deepAll(msg.selector, from) : [];
      if (!els.length && !top) return;
      els.forEach((el) => el.setAttribute('data-hubie-seen', '1'));
      send({ type: 'done', id: msg.id, ok: true, data: { marked: els.length }, url: location.href });
      return;
    }
    if (msg.type === 'click') {
      const el = find(msg.selector, msg.index || 0, msg.text, scoped(msg));
      if (!el && !top) return;
      // Games are played with buttons. Never click a link: that is how a click could take the
      // stream window somewhere else.
      if (el && el.closest('a[href], area[href]')) {
        send({ type: 'done', id: msg.id, ok: false, error: 'refused: element is a link', url: location.href });
        return;
      }
      if (el) el.click();
      send({ type: 'done', id: msg.id, ok: !!el, url: location.href });
      return;
    }
    // Stream mode: only the game card, filling the window, for OBS to capture.
    if (msg.type === 'stream_mode') {
      if (!top) return;
      try { if (msg.on) sessionStorage.setItem('hubieStream', '1'); else sessionStorage.removeItem('hubieStream'); } catch { /* storage blocked */ }
      streamCss(msg.on);
      const a = deepAll('article[data-game-frame]')[0];
      const r = a ? a.getBoundingClientRect() : null;
      send({ type: 'done', id: msg.id, ok: !!a, data: { on: !!msg.on, viewport: [innerWidth, innerHeight],
        dpr: devicePixelRatio, card: r && [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] } });
      return;
    }
    if (msg.type === 'set') {
      const el = find(msg.selector, msg.index || 0, null, scoped(msg));
      if (!el && !top) return;
      if (el) {
        // React-controlled inputs ignore plain .value writes; go through the native setter. The games
        // also only commit a typed value like a person's: on Enter or when the box loses focus
        // (without that the box shows the new value while the game keeps playing the old one).
        const opts = { bubbles: true, composed: true };
        el.focus();
        const proto = Object.getPrototypeOf(el);
        const setter = Object.getOwnPropertyDescriptor(proto, 'value');
        const put = (v) => { if (setter && setter.set) setter.set.call(el, v); else el.value = v; };
        if (msg.typed) {
          // Like a person typing: clear, then one character at a time with the key and InputEvents a
          // real keystroke makes (some inputs only commit what arrives as insertText).
          el.select && el.select();
          put('');
          el.dispatchEvent(new InputEvent('input', { ...opts, inputType: 'deleteContentBackward' }));
          let cur = '';
          for (const ch of String(msg.value)) {
            el.dispatchEvent(new KeyboardEvent('keydown', { ...opts, key: ch }));
            el.dispatchEvent(new InputEvent('beforeinput', { ...opts, inputType: 'insertText', data: ch, cancelable: true }));
            cur += ch; put(cur);
            el.dispatchEvent(new InputEvent('input', { ...opts, inputType: 'insertText', data: ch }));
            el.dispatchEvent(new KeyboardEvent('keyup', { ...opts, key: ch }));
          }
        } else {
          put(String(msg.value));
          el.dispatchEvent(new Event('input', opts));
        }
        el.dispatchEvent(new Event('change', opts));
        for (const type of ['keydown', 'keypress', 'keyup']) {
          el.dispatchEvent(new KeyboardEvent(type, { ...opts, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
        }
        el.blur();
        el.dispatchEvent(new FocusEvent('focusout', opts));
      }
      send({ type: 'done', id: msg.id, ok: !!el, url: location.href });
    }
  });
})();
