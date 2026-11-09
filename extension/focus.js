// Runs in the page itself (MAIN world) before the site's own scripts. Housebets games pause
// when their window loses focus or is hidden (normal for web games); the stream window is
// never the focused one, so the page is told it always has focus and is always visible.
(() => {
  const def = (obj, prop, value) => { try { Object.defineProperty(obj, prop, { configurable: true, get: () => value }); } catch { /* locked */ } };
  def(Document.prototype, 'hidden', false);
  def(Document.prototype, 'webkitHidden', false);
  def(Document.prototype, 'visibilityState', 'visible');
  def(Document.prototype, 'webkitVisibilityState', 'visible');
  Document.prototype.hasFocus = () => true;
  const swallow = (e) => { if (e.target === window || e.target === document) e.stopImmediatePropagation(); };
  window.addEventListener('blur', swallow, true);
  window.addEventListener('pagehide', swallow, true);
  document.addEventListener('visibilitychange', swallow, true);
  document.addEventListener('webkitvisibilitychange', swallow, true);
})();
