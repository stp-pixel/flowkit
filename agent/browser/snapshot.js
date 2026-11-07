// Adapted from browser-use/jev-ultrafast (MIT). https://github.com/browser-use/jev-ultrafast
//
// In-page DOM index for Flow Kit. Read-only: it inspects the DOM and, for
// selectOption() only, sets a <select> value. It never touches the network.
// Ids are stable ("e<n>" from a WeakMap counter) for as long as the node lives.
(() => {
  const VERSION = 1;
  if (window.__fkIndex && window.__fkIndex.version === VERSION) return;

  const ROLES = [
    'button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem',
    'menuitemradio', 'option', 'gridcell', 'combobox', 'textbox', 'searchbox',
    'spinbutton',
  ];
  const SELECTOR = [
    'a[href]', 'button', 'input', 'textarea', 'select', 'summary',
    '[contenteditable="true"]', '[contenteditable=""]',
    ...ROLES.map((r) => `[role="${r}"]`),
  ].join(',');
  const SKIP_TYPES = new Set(['password', 'file', 'hidden']);
  const TEXT_TYPES = new Set(['text', 'search', 'email', 'url', 'tel', 'number']);
  const FILL_ROLES = new Set(['textbox', 'searchbox', 'spinbutton']);
  const BUTTON_TYPES = new Set(['button', 'submit', 'reset', 'image']);
  const MAX_ACTIONS = 250;
  const MAX_TEXT = 6000;
  const MAX_LABEL = 80;
  const MAX_OPTIONS = 20;

  const ids = new WeakMap();
  const nodes = new Map();
  let counter = 0;

  const clean = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const cut = (s) => clean(s).slice(0, MAX_LABEL);

  const idOf = (e) => {
    let n = ids.get(e);
    if (n === undefined) {
      n = ++counter;
      ids.set(e, n);
      nodes.set(n, new WeakRef(e));
    }
    return n;
  };

  const isTextInput = (e) => e.tagName === 'INPUT' && TEXT_TYPES.has(e.type);
  const isEditable = (e) => {
    const ce = e.getAttribute('contenteditable');
    return ce === '' || ce === 'true';
  };

  const kindOf = (e) => {
    if (e.tagName === 'SELECT') return 'select';
    if (e.tagName === 'TEXTAREA' || isTextInput(e) || isEditable(e)) return 'fill';
    if (FILL_ROLES.has((e.getAttribute('role') || '').toLowerCase())) return 'fill';
    return 'click';
  };

  const roleOf = (e) => {
    const explicit = (e.getAttribute('role') || '').trim().toLowerCase();
    if (explicit) return explicit;
    const tag = e.tagName;
    if (tag === 'BUTTON') return 'button';
    if (tag === 'A' && e.hasAttribute('href')) return 'link';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA' || isEditable(e)) return 'textbox';
    if (tag === 'INPUT') {
      if (e.type === 'checkbox') return 'checkbox';
      if (e.type === 'radio') return 'radio';
      if (BUTTON_TYPES.has(e.type)) return 'button';
      if (TEXT_TYPES.has(e.type)) return 'textbox';
    }
    return tag.toLowerCase();
  };

  const labelOf = (e) => {
    const by = e.getAttribute('aria-labelledby');
    const candidates = [
      () => by && by.split(/\s+/).map((id) => {
        const t = document.getElementById(id);
        return t ? t.innerText || t.textContent : '';
      }).join(' '),
      () => e.getAttribute('aria-label'),
      () => e.labels && e.labels[0] && (e.labels[0].innerText || e.labels[0].textContent),
      () => (e.tagName === 'INPUT' && BUTTON_TYPES.has(e.type) ? e.value : e.getAttribute('alt')),
      () => e.innerText,
      () => e.getAttribute('title'),
      () => e.getAttribute('placeholder'),
    ];
    for (const get of candidates) {
      const v = cut(get() || '');
      if (v) return v;
    }
    return '';
  };

  const optionLabel = (o) => clean(o.label || o.textContent);

  const isHidden = (e) =>
    !!e.closest('[aria-hidden="true"],[inert]') ||
    !e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
  const isDisabled = (e) =>
    e.matches(':disabled') || e.getAttribute('aria-disabled') === 'true';
  const centre = (e) => {
    const r = e.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  };
  const inViewport = (p) =>
    p.x >= 0 && p.x <= window.innerWidth && p.y >= 0 && p.y <= window.innerHeight;

  const skip = (e) =>
    (e.tagName === 'INPUT' && SKIP_TYPES.has(e.type)) ||
    isHidden(e) || !inViewport(centre(e)) || isDisabled(e);

  const describe = (e) => {
    const kind = kindOf(e);
    const a = {
      id: 'e' + idOf(e), kind, role: roleOf(e), label: labelOf(e),
      value: null, options: null,
    };
    if (kind === 'fill') {
      const v = 'value' in e && !isEditable(e) ? e.value : e.innerText;
      a.value = cut(v);
    } else if (kind === 'select') {
      const sel = e.selectedOptions && e.selectedOptions[0];
      a.value = sel ? optionLabel(sel) : '';
      a.options = Array.from(e.options).slice(0, MAX_OPTIONS).map(optionLabel);
    }
    return a;
  };

  const visibleText = () => {
    const w = window.innerWidth, h = window.innerHeight;
    const walker = document.createTreeWalker(
      document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    const parts = [];
    let total = 0;
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      const s = clean(t.nodeValue);
      if (!s) continue;
      const p = t.parentElement;
      if (!p || p.closest('script,style,noscript,template,select')) continue;
      if (!p.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) continue;
      range.selectNodeContents(t);
      const r = range.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      if (r.bottom <= 0 || r.top >= h || r.right <= 0 || r.left >= w) continue;
      parts.push(s);
      total += s.length + 1;
      if (total >= MAX_TEXT) break;
    }
    return parts.join('\n').slice(0, MAX_TEXT);
  };

  const prune = () => {
    for (const [n, ref] of nodes) if (!ref.deref()) nodes.delete(n);
  };

  const snapshot = () => {
    prune();
    const actions = [];
    let omitted = 0;
    for (const e of document.querySelectorAll(SELECTOR)) {
      if (skip(e)) continue;
      if (actions.length < MAX_ACTIONS) actions.push(describe(e));
      else omitted++;
    }
    const se = document.scrollingElement || document.documentElement;
    return {
      url: location.href,
      title: document.title,
      text: visibleText(),
      actions,
      omitted,
      can_scroll_up: se.scrollTop > 0,
      can_scroll_down: se.scrollTop + window.innerHeight < se.scrollHeight - 1,
    };
  };

  const lookup = (n) => {
    const ref = nodes.get(n);
    const e = ref && ref.deref();
    return e && e.isConnected ? e : null;
  };

  const resolve = (n) => {
    const e = lookup(n);
    if (!e) return { ok: false, reason: 'stale' };
    if (isHidden(e) || isDisabled(e)) return { ok: false, reason: 'hidden' };
    const { x, y } = centre(e);
    if (!inViewport({ x, y })) return { ok: false, reason: 'offscreen' };
    const hit = document.elementFromPoint(x, y);
    if (!hit || !e.contains(hit)) return { ok: false, reason: 'covered' };
    return { ok: true, x, y, kind: kindOf(e) };
  };

  const selectOption = (n, label) => {
    const e = lookup(n);
    if (!e) return { ok: false, reason: 'stale' };
    if (e.tagName !== 'SELECT') return { ok: false, reason: 'wrong_kind' };
    const want = clean(label);
    const opt = Array.from(e.options).find((o) => optionLabel(o) === want);
    if (!opt) return { ok: false, reason: 'no_such_option' };
    e.value = opt.value;
    e.dispatchEvent(new Event('input', { bubbles: true }));
    e.dispatchEvent(new Event('change', { bubbles: true }));
    return { ok: true };
  };

  window.__fkIndex = { version: VERSION, snapshot, resolve, selectOption, visibleText };
})();
