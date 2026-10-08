// Jev operator DOM snapshot. Evaluate in a page to install window.__freyjaJev.
// Idempotent per version: evaluating the same version again keeps the first
// install (one observer only); a newer version replaces an older one.
(function () {
  'use strict';
  var VERSION = 6;
  var prior = window.__freyjaJev;
  if (prior && prior.version === VERSION) return 'already-installed';
  if (prior && prior.observer) { try { prior.observer.disconnect(); } catch (e) { /* ignore */ } }

  var MAX_ACTIONS = 250, MAX_LABEL = 200, MAX_VALUE = 500, MAX_CTX = 300, MAX_TEXT = 6000;
  var MAX_OPTIONS = 25;
  var ids = new WeakMap(), byId = new Map(), nextId = 1, mutations = 0;

  var observer = new MutationObserver(function (recs) { mutations += recs.length || 1; });
  var OBSERVE = { childList: true, subtree: true, attributes: true, characterData: true };
  observer.observe(document.documentElement, OBSERVE);
  var watchedRoots = new WeakSet();

  // Page dialogs. This script runs in the browser's isolated world (it shares
  // the DOM, not the page's globals), so overriding window.confirm here would
  // not reach the page. A small hook goes into the page's own world through a
  // <script> element and talks to us with DOM events. While a run is active
  // (15 s after its last call) the hook makes alert/prompt non-blocking and
  // answers confirm() Cancel unless the run may confirm, reports each dialog,
  // and reports window.open (a new tab). A strict Content-Security-Policy can
  // block the hook; then the page's dialogs behave as they normally do.
  var MAIN = "(function(){if(window.__freyjaJevMain)return;window.__freyjaJevMain=1;" +
    "var n={alert:window.alert,confirm:window.confirm,prompt:window.prompt,open:window.open},until=0,ans=false;" +
    "document.addEventListener('freyja-jev-touch',function(e){until=Date.now()+15000;ans=e.detail==='confirm';});" +
    "function live(){return Date.now()<until;}" +
    "function rep(k,m,a){document.dispatchEvent(new CustomEvent('freyja-jev-dialog',{detail:JSON.stringify({kind:k,message:String(m==null?'':m).slice(0,500),answer:a})}));}" +
    "window.alert=function(m){if(!live())return n.alert.apply(window,arguments);rep('alert',m);};" +
    "window.confirm=function(m){if(!live())return n.confirm.apply(window,arguments);rep('confirm',m,ans);return ans;};" +
    "window.prompt=function(m){if(!live())return n.prompt.apply(window,arguments);rep('prompt',m);return null;};" +
    "window.open=function(){if(live())rep('open',arguments[0]||'');return n.open.apply(window,arguments);};" +
    "document.dispatchEvent(new CustomEvent('freyja-jev-main-ready'));})();";
  var dialogs = [], pageOpened = false, mainHook = false;
  document.addEventListener('freyja-jev-main-ready', function () { mainHook = true; });
  document.addEventListener('freyja-jev-dialog', function (e) {
    var d = null;
    try { d = JSON.parse(e.detail); } catch (x) { return; }
    if (!d) return;
    if (d.kind === 'open') pageOpened = true; else dialogs.push(d);
  });
  try {
    var hook = document.createElement('script');
    hook.textContent = MAIN;
    (document.head || document.documentElement).appendChild(hook);
    hook.remove();
  } catch (e) { /* blocked: dialogs behave as usual */ }
  // Every call keeps the run active; only an action sets whether a confirm() it
  // triggers (now or after a delay) may be answered OK.
  var mayConfirmNow = false;
  function touch(mayConfirm) {
    if (typeof mayConfirm === 'boolean') mayConfirmNow = mayConfirm;
    document.dispatchEvent(new CustomEvent('freyja-jev-touch', { detail: mayConfirmNow ? 'confirm' : '' }));
  }
  function drain() { var d = dialogs; dialogs = []; return d; }

  var SELECTOR = [
    'a[href]', 'button', 'input', 'textarea', 'select', 'summary', '[contenteditable]',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=tab]',
    '[role=menuitem]', '[role=menuitemcheckbox]', '[role=menuitemradio]', '[role=switch]',
    '[role=combobox]', '[role=textbox]', '[role=searchbox]', '[role=option]', '[role=treeitem]',
    '[onclick]',
  ].join(',');
  var ARIA_ROLES = {
    button: 1, link: 1, checkbox: 1, radio: 1, tab: 1, menuitem: 1, menuitemcheckbox: 1,
    menuitemradio: 1, switch: 1, combobox: 1, textbox: 1, searchbox: 1, option: 1, treeitem: 1,
  };
  var BUTTON_INPUTS = { button: 1, submit: 1, reset: 1, image: 1 };
  var TOGGLE_INPUTS = { checkbox: 1, radio: 1 };
  var NON_TEXT_INPUTS = { range: 1, color: 1, date: 1, 'datetime-local': 1, month: 1, time: 1, week: 1 };
  var KEY_CODES = { Enter: 13, Escape: 27, Tab: 9 };
  var SEARCHY = /search|filter|find|query|lookup/i;

  function norm(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }
  function clip(s, n) { s = norm(s); return s.length > n ? s.slice(0, n) : s; }
  function attr(el, n) { return el.getAttribute(n); }

  // Open shadow roots (web components) are part of the page: walk them in
  // composed order, so a control inside one appears where its host is.
  function eachElement(root, fn) {
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      fn(all[i]);
      var sr = all[i].shadowRoot;
      if (sr) {
        if (!watchedRoots.has(sr)) { watchedRoots.add(sr); try { observer.observe(sr, OBSERVE); } catch (e) { /* ignore */ } }
        eachElement(sr, fn);
      }
    }
  }

  function parentOf(n) { return n.parentElement || (n.parentNode && n.parentNode.host) || null; }

  function composedContains(a, b) {
    for (var n = b; n; n = n.parentNode || n.host) if (n === a) return true;
    return false;
  }

  function deepActive() {
    var a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  }

  function deepElementFromPoint(x, y) {
    var top = document.elementFromPoint(x, y);
    while (top && top.shadowRoot) {
      var inner = top.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === top) break;
      top = inner;
    }
    return top;
  }

  function isEditableHost(el) {
    var ce = attr(el, 'contenteditable');
    return ce !== null && ce.toLowerCase() !== 'false';
  }

  function excludedInput(el) {
    if (el.tagName !== 'INPUT') return false;
    var t = (el.type || '').toLowerCase();
    return t === 'password' || t === 'file' || t === 'hidden';
  }

  function roleOf(el) {
    var r = attr(el, 'role');
    if (r) { r = r.trim().split(/\s+/)[0].toLowerCase(); if (ARIA_ROLES[r]) return r; }
    var tag = el.tagName;
    if (tag === 'A') return 'link';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      var t = (el.type || 'text').toLowerCase();
      if (BUTTON_INPUTS[t]) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'search') return 'searchbox';
      return 'textbox';
    }
    if (isEditableHost(el)) return 'textbox';
    return r || 'button';
  }

  function kindOf(el, role) {
    var tag = el.tagName;
    if (tag === 'SELECT') return 'select';
    if (tag === 'TEXTAREA') return 'fill';
    if (tag === 'INPUT') {
      var t = (el.type || 'text').toLowerCase();
      if (BUTTON_INPUTS[t]) return 'click';
      if (TOGGLE_INPUTS[t]) return 'toggle';
      if (NON_TEXT_INPUTS[t]) return 'fill';
      return 'fill';
    }
    if (isEditableHost(el) || role === 'textbox' || role === 'searchbox') return 'fill';
    if (role === 'checkbox' || role === 'radio' || role === 'switch' ||
        role === 'menuitemcheckbox' || role === 'menuitemradio') return 'toggle';
    if (role === 'link') return 'link';
    // A combobox that is not a text input is a button that opens a list.
    return 'click';
  }

  function hiddenByAncestry(el) {
    for (var n = el; n && n.nodeType === 1; n = parentOf(n)) {
      if (attr(n, 'aria-hidden') === 'true') return true;
      if (n.hasAttribute('inert')) return true;
    }
    return false;
  }

  function isVisible(el) {
    if (!el.isConnected) return false;
    if (hiddenByAncestry(el)) return false;
    if (el.checkVisibility) {
      if (!el.checkVisibility({ checkVisibilityCSS: true })) return false;
    } else {
      var cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    }
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function isDisabled(el) {
    if (el.disabled === true) return true;
    if (attr(el, 'aria-disabled') === 'true') return true;
    var fs = el.closest && el.closest('fieldset[disabled]');
    if (fs && !(el.closest('legend') && el.closest('legend').parentElement === fs)) return true;
    return false;
  }

  function isReadOnly(el) {
    return el.readOnly === true || attr(el, 'aria-readonly') === 'true';
  }

  function textOf(el) { return norm(el.innerText != null ? el.innerText : el.textContent); }

  function labelledBy(el) {
    var ids_ = attr(el, 'aria-labelledby');
    if (!ids_) return '';
    var parts = [], root = el.getRootNode ? el.getRootNode() : document;
    ids_.split(/\s+/).forEach(function (i) {
      var t = i && ((root.getElementById && root.getElementById(i)) || document.getElementById(i));
      if (t) parts.push(norm(t.textContent));
    });
    return norm(parts.join(' '));
  }

  function nameOf(el) {
    var v = norm(attr(el, 'aria-label'));
    if (v) return clip(v, MAX_LABEL);
    v = labelledBy(el);
    if (v) return clip(v, MAX_LABEL);
    if (el.labels && el.labels.length) {
      var ls = [];
      for (var i = 0; i < el.labels.length; i++) ls.push(norm(el.labels[i].textContent));
      v = norm(ls.join(' '));
      if (v) return clip(v, MAX_LABEL);
    }
    if (el.tagName === 'INPUT') {
      var t = (el.type || '').toLowerCase();
      if (BUTTON_INPUTS[t]) {
        v = norm(el.value) || norm(attr(el, 'alt'));
        if (v) return clip(v, MAX_LABEL);
      }
    }
    var alt = norm(attr(el, 'alt'));
    if (alt) return clip(alt, MAX_LABEL);
    var tag = el.tagName;
    if (tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'SELECT') {
      v = textOf(el);
      if (v) return clip(v, MAX_LABEL);
      var img = el.querySelector && el.querySelector('img[alt],[aria-label],svg title');
      if (img) {
        v = norm(attr(img, 'alt') || attr(img, 'aria-label') || img.textContent);
        if (v) return clip(v, MAX_LABEL);
      }
    }
    var keys = ['title', 'placeholder', 'data-tooltip', 'mattooltip', 'data-title', 'name'];
    for (var k = 0; k < keys.length; k++) {
      v = norm(attr(el, keys[k]));
      if (v) return clip(v, MAX_LABEL);
    }
    return '';
  }

  function valueOf(el, kind) {
    var v = '';
    if (el.tagName === 'SELECT') {
      var o = el.selectedOptions && el.selectedOptions[0];
      v = o ? norm(o.text) : '';
    } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      if (kind === 'fill') v = el.value || '';
    } else if (kind === 'fill') {
      v = el.innerText != null ? el.innerText : (el.textContent || '');
    } else if (attr(el, 'role') === 'combobox') {
      v = textOf(el);
    }
    return String(v).slice(0, MAX_VALUE);
  }

  function boolAttr(el, n) {
    var v = attr(el, n);
    return v === 'true' ? true : v === 'false' ? false : null;
  }

  function checkedOf(el) {
    if (el.tagName === 'INPUT' && TOGGLE_INPUTS[(el.type || '').toLowerCase()]) return !!el.checked;
    var c = boolAttr(el, 'aria-checked');
    if (c !== null) return c;
    var s = boolAttr(el, 'aria-selected');
    if (s !== null) return s;
    var p = boolAttr(el, 'aria-pressed');
    return p;
  }

  function expandedOf(el) {
    var e = boolAttr(el, 'aria-expanded');
    if (e !== null) return e;
    if (el.tagName === 'SUMMARY' && el.parentElement && el.parentElement.tagName === 'DETAILS') {
      return !!el.parentElement.open;
    }
    return null;
  }

  // What Enter does in a text field: "search" (filters or searches; safe),
  // "form:<submit label>" (submits that form), "form" (a form with no submit
  // button), or "unknown" (page scripts decide; a chat box may send).
  function enterOf(el) {
    var hay = [attr(el, 'type'), attr(el, 'role'), attr(el, 'aria-label'), attr(el, 'placeholder'),
      attr(el, 'name'), el.id, nameOf(el)].join(' ');
    if ((el.type || '').toLowerCase() === 'search' || attr(el, 'role') === 'searchbox' ||
        (el.closest && el.closest('[role=search]')) || SEARCHY.test(hay)) return 'search';
    var f = el.form;
    if (f) {
      var b = f.querySelector('button[type=submit],button:not([type]),input[type=submit],input[type=image]');
      return b ? 'form:' + nameOf(b) : 'form';
    }
    return 'unknown';
  }

  function optionsOf(el) {
    if (el.tagName !== 'SELECT') return null;
    var out = [];
    for (var i = 0; i < el.options.length && out.length < MAX_OPTIONS; i++) {
      var o = el.options[i];
      if (o.disabled || o.hidden) continue;
      out.push({ v: o.value, t: norm(o.text), sel: o.selected });
    }
    return out;
  }

  function hash(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
    return h.toString(36);
  }

  function idFor(el) {
    var id = ids.get(el);
    if (!id) { id = nextId++; ids.set(el, id); byId.set(id, new WeakRef(el)); }
    return id;
  }

  function onclickNoise(el) {
    // [onclick] catches clickable divs without a role; skip big containers.
    if (!el.hasAttribute('onclick') || el.matches('a[href],button,input,select,textarea,[role]')) return false;
    return textOf(el).length > 120;
  }

  // Collect every visible candidate control (no viewport filtering, no cap).
  function collect() {
    var els = [];
    eachElement(document, function (el) { if (el.matches(SELECTOR)) els.push(el); });
    var out = [], groups = new Map(), seen = new Set();
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (seen.has(el)) continue;
      seen.add(el);
      if (excludedInput(el)) continue;
      if (onclickNoise(el)) continue;
      if (isEditableHost(el) && el.parentElement && el.parentElement.closest('[contenteditable]:not([contenteditable=false])')) {
        continue; // nested editable child of an editable host: the host is the control
      }
      if (!isVisible(el)) continue;
      var role = roleOf(el), kind = kindOf(el, role);
      var e = {
        el: el, role: role, kind: kind, label: nameOf(el), value: valueOf(el, kind),
        disabled: isDisabled(el), checked: checkedOf(el), expanded: expandedOf(el),
        href: el.tagName === 'A' ? (el.href || '') : '', context: '',
      };
      out.push(e);
      var key = role + '\u0000' + e.label;
      var g = groups.get(key);
      if (!g) groups.set(key, g = []);
      g.push(e);
    }
    groups.forEach(function (g) {
      if (g.length < 2) return;
      g.forEach(function (e) {
        for (var a = e.el.parentElement; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
          var clash = false;
          for (var k = 0; k < g.length; k++) {
            if (g[k] !== e && a.contains(g[k].el)) { clash = true; break; }
          }
          if (clash) break;
          var t = textOf(a);
          if (t && t !== e.label) { e.context = clip(t, MAX_CTX); break; }
        }
      });
    });
    out.forEach(function (e) {
      e.guard = hash([e.label, e.value, e.disabled ? 1 : 0, e.checked, e.expanded,
        attr(e.el, 'aria-selected'), e.href, e.context].join('\u0001'));
    });
    return out;
  }

  function offscreenOf(el) {
    var r = el.getBoundingClientRect(), vh = window.innerHeight;
    if (r.bottom <= 0) return 'above';
    if (r.top >= vh) return 'below';
    return null;
  }

  // Distance from the visible area, in pixels (0 = on screen).
  function distance(el) {
    var r = el.getBoundingClientRect(), vh = window.innerHeight, vw = window.innerWidth;
    var dy = r.bottom < 0 ? -r.bottom : r.top > vh ? r.top - vh : 0;
    var dx = r.right < 0 ? -r.right : r.left > vw ? r.left - vw : 0;
    return dy + dx;
  }

  // Visible text first; then, while the budget lasts, text below the visible area.
  // Shadow roots are read where their host is.
  function pageText() {
    var vis = [], below = [], total = 0, vh = window.innerHeight, vw = window.innerWidth, range = document.createRange();
    function walk(root) {
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, null);
      var n;
      while ((n = walker.nextNode())) {
        if (total > MAX_TEXT) return;
        if (n.nodeType === 1) {
          if (n.shadowRoot) walk(n.shadowRoot);
          continue;
        }
        var s = norm(n.nodeValue);
        if (!s) continue;
        var p = n.parentElement;
        if (!p) continue;
        var tag = p.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE') continue;
        if (!isVisibleLoose(p)) continue;
        range.selectNodeContents(n);
        var r = range.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if (r.right <= 0 || r.left >= vw) continue;
        if (r.bottom <= 0) continue;
        if (r.top >= vh) { below.push(s); continue; }
        vis.push(s);
        total += s.length + 1;
      }
    }
    walk(document.body || document.documentElement);
    var text = vis.join('\n');
    if (below.length && text.length < MAX_TEXT - 200) {
      text += '\n[below the visible area]\n' + below.join('\n');
    }
    return text.slice(0, MAX_TEXT);
  }

  function isVisibleLoose(el) {
    if (hiddenByAncestry(el)) return false;
    return el.checkVisibility ? el.checkVisibility({ checkVisibilityCSS: true }) : true;
  }

  function snapshot() {
    touch();
    var all = collect(), cands = [], unoffered = [];
    for (var i = 0; i < all.length; i++) {
      var e = all[i];
      if (e.disabled) { unoffered.push({ label: e.label, context: e.context }); continue; }
      e.order = i;
      e.dist = distance(e.el);
      cands.push(e);
    }
    // The nearest MAX_ACTIONS controls, shown in document order.
    if (cands.length > MAX_ACTIONS) {
      cands.sort(function (a, b) { return a.dist - b.dist || a.order - b.order; });
      cands = cands.slice(0, MAX_ACTIONS).sort(function (a, b) { return a.order - b.order; });
    }
    var active = deepActive();
    var actions = cands.map(function (e) {
      var a = {
        id: idFor(e.el), kind: e.kind, role: e.role, label: e.label, value: e.value,
        checked: e.checked, expanded: e.expanded, offscreen: offscreenOf(e.el),
        context: e.context, focused: active === e.el, guard: e.guard,
      };
      if (e.kind === 'fill') a.enter = enterOf(e.el);
      if (e.kind === 'select') a.options = optionsOf(e.el);
      return a;
    });
    var de = document.documentElement;
    var maxY = Math.max(0, Math.max(de.scrollHeight, document.body ? document.body.scrollHeight : 0) - window.innerHeight);
    return JSON.stringify({
      url: location.href, title: document.title, readyState: document.readyState,
      scroll: { x: Math.round(window.scrollX), y: Math.round(window.scrollY), maxY: Math.round(maxY) },
      viewport: { w: window.innerWidth, h: window.innerHeight },
      text: pageText(), mutations: mutations, actions: actions, unoffered: unoffered,
      dialogs: drain(), hook: mainHook,
    });
  }

  function quiet() {
    touch();
    return JSON.stringify({ m: mutations, rs: document.readyState, u: location.href });
  }

  // Navigate this tab (one the run opened itself) to another address.
  function go(url) {
    touch();
    location.assign(url);
    return JSON.stringify({ ok: true });
  }

  function res(ok, reason, readback, extra) {
    var o = { ok: !!ok, reason: reason || null, readback: readback == null ? null : String(readback) };
    if (extra) for (var k in extra) o[k] = extra[k];
    if (dialogs.length) o.dialogs = drain();
    return JSON.stringify(o);
  }

  function fire(el, type, ctor, init) {
    init = init || {};
    init.bubbles = true; init.cancelable = true;
    el.dispatchEvent(new (ctor || Event)(type, init));
  }

  function keyEvent(type, key) {
    var ev = new KeyboardEvent(type, { key: key, code: key, bubbles: true, cancelable: true, composed: true });
    var code = KEY_CODES[key] || 0;
    try {
      Object.defineProperty(ev, 'keyCode', { get: function () { return code; } });
      Object.defineProperty(ev, 'which', { get: function () { return code; } });
    } catch (e) { /* ignore */ }
    return ev;
  }

  // The full sequence a real pointer produces; many widgets act on mousedown.
  function pointerClick(el) {
    var r = el.getBoundingClientRect();
    var init = {
      bubbles: true, cancelable: true, composed: true, view: window, button: 0, buttons: 1,
      clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, pointerId: 1, pointerType: 'mouse', isPrimary: true,
    };
    var P = typeof PointerEvent === 'function' ? PointerEvent : MouseEvent;
    el.dispatchEvent(new P('pointerover', init));
    el.dispatchEvent(new MouseEvent('mouseover', init));
    el.dispatchEvent(new P('pointerdown', init));
    var down = el.dispatchEvent(new MouseEvent('mousedown', init));
    if (down && el.focus) el.focus({ preventScroll: true });
    init.buttons = 0;
    el.dispatchEvent(new P('pointerup', init));
    el.dispatchEvent(new MouseEvent('mouseup', init));
    el.click();
  }

  function isEditableActive(a) {
    if (!a) return false;
    if (a.tagName === 'TEXTAREA') return !a.readOnly && !a.disabled;
    if (a.tagName === 'INPUT') {
      var t = (a.type || 'text').toLowerCase();
      return !a.readOnly && !a.disabled && !BUTTON_INPUTS[t] && !TOGGLE_INPUTS[t] && t !== 'password' && t !== 'file' && t !== 'hidden';
    }
    return a.isContentEditable === true;
  }

  function doKey(key) {
    var a = deepActive();
    if (key !== 'Enter' && key !== 'Escape' && key !== 'Tab') return res(false, 'unsupported_key');
    if (a && a.tagName === 'INPUT' && (a.type || '').toLowerCase() === 'password') return res(false, 'refused_password');
    if (key === 'Enter' && !isEditableActive(a)) return res(false, 'no_editable_focused');
    var target = a || document.body;
    var notPrevented = target.dispatchEvent(keyEvent('keydown', key));
    if (notPrevented) {
      if (key === 'Enter') {
        target.dispatchEvent(keyEvent('keypress', key));
        if (target.tagName === 'INPUT' && target.form && target.form.requestSubmit) {
          try { target.form.requestSubmit(); } catch (e) { /* invalid form: ignore */ }
        }
      } else if (key === 'Tab') {
        var f = Array.prototype.filter.call(
          document.querySelectorAll('a[href],button,input,select,textarea,summary,[tabindex],[contenteditable]'),
          function (x) { return !excludedInput(x) && !isDisabled(x) && x.tabIndex >= 0 && isVisible(x); });
        var idx = f.indexOf(target);
        var nx = f[(idx + 1) % (f.length || 1)];
        if (nx) nx.focus();
      }
    }
    target.dispatchEvent(keyEvent('keyup', key));
    var now = deepActive();
    return res(true, null, now ? (now.value != null ? now.value : '') : '');
  }

  // The element that scrolls: the page if it can, else the largest visible scroll container.
  function scroller() {
    var se = document.scrollingElement || document.documentElement;
    if (se.scrollHeight > window.innerHeight + 20) return null;
    var best = null, area = 0, all = document.querySelectorAll('body *');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (el.scrollHeight <= el.clientHeight + 20 || el.clientHeight < 80) continue;
      var oy = getComputedStyle(el).overflowY;
      if (oy !== 'auto' && oy !== 'scroll' && oy !== 'overlay') continue;
      var r = el.getBoundingClientRect();
      var a = Math.max(0, Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0)) * r.width;
      if (a > area) { area = a; best = el; }
    }
    return best;
  }

  function doScroll(dy) {
    var s = scroller();
    if (!s) {
      var y0 = window.scrollY;
      window.scrollBy(0, dy);
      var moved = Math.round(window.scrollY - y0);
      return res(true, null, 'page at ' + Math.round(window.scrollY) + ' of ' +
        Math.round(Math.max(0, (document.scrollingElement || document.documentElement).scrollHeight - window.innerHeight)) +
        (moved ? '' : ' (did not move: already at the ' + (dy > 0 ? 'bottom' : 'top') + ')'), { moved: moved });
    }
    var before = s.scrollTop;
    s.scrollTop = before + dy;
    var m = Math.round(s.scrollTop - before);
    return res(true, null, 'panel at ' + Math.round(s.scrollTop) + ' of ' + Math.round(s.scrollHeight - s.clientHeight) +
      (m ? '' : ' (did not move: already at the ' + (dy > 0 ? 'bottom' : 'top') + ')'), { moved: m });
  }

  // Type the way a person does (select, then insert), so frameworks and rich
  // editors see a real input; fall back to setting the value directly.
  function fillText(el, text, append) {
    el.focus();
    var isField = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA';
    var want = append ? (isField ? (el.value || '') : (el.innerText || '')) + text : text;
    try {
      if (isField) {
        if (append) { var n = (el.value || '').length; el.setSelectionRange(n, n); } else { el.select(); }
      } else {
        var sel = window.getSelection(), rng = document.createRange();
        rng.selectNodeContents(el);
        if (append) rng.collapse(false);
        sel.removeAllRanges(); sel.addRange(rng);
      }
      var ok = document.execCommand('insertText', false, text);
      var now = isField ? el.value : el.innerText;
      if (ok && norm(now) === norm(want)) return now;
    } catch (e) { /* fall through */ }
    if (isField) {
      var proto = el.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, want);
      fire(el, 'input', typeof InputEvent === 'function' ? InputEvent : Event, { inputType: 'insertText', data: text });
      fire(el, 'change');
      return el.value;
    }
    el.textContent = want;
    fire(el, 'input', typeof InputEvent === 'function' ? InputEvent : Event, { inputType: 'insertText', data: text });
    return el.innerText;
  }

  function coveredAt(el) {
    var r = el.getBoundingClientRect();
    var top = deepElementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return !top || !(composedContains(el, top) || composedContains(top, el));
  }

  function act(id, op, arg, expectedGuard, mayConfirm) {
    touch(!!mayConfirm);
    pageOpened = false;
    try {
      if (op === 'scroll') {
        var dy = Number(arg);
        if (!isFinite(dy)) return res(false, 'bad_arg');
        return doScroll(dy);
      }
      if (op === 'key') return doKey(String(arg));
      if (op !== 'click' && op !== 'fill' && op !== 'select') return res(false, 'unknown_op');

      var ref = byId.get(id), el = ref && ref.deref();
      if (!el) return res(false, 'unknown_id');
      if (!el.isConnected) return res(false, 'detached');
      if (excludedInput(el)) return res(false, 'refused_sensitive');
      if (!isVisible(el)) return res(false, 'not_visible');
      var entry = null, all = collect();
      for (var i = 0; i < all.length; i++) if (all[i].el === el) { entry = all[i]; break; }
      if (!entry) return res(false, 'not_actionable');
      if (expectedGuard && expectedGuard !== entry.guard) return res(false, 'stale');
      if (entry.disabled) return res(false, 'disabled');

      if (op === 'fill') {
        if (entry.kind !== 'fill') return res(false, 'not_fillable');
        if (isReadOnly(el)) return res(false, 'read_only');
      }
      if (op === 'select' && entry.kind !== 'select') return res(false, 'not_selectable');

      el.scrollIntoView({ block: 'center', inline: 'center' });
      if (coveredAt(el)) {
        if (el.focus) el.focus({ preventScroll: true });
        if (coveredAt(el)) return res(false, 'covered');
      }

      if (op === 'click') {
        var opened = false;
        var link = el.closest && el.closest('a[href]');
        if (link && link.target && !/^_(self|top|parent)$/i.test(link.target)) opened = true;
        pointerClick(el);
        if (pageOpened) opened = true;
        return res(true, null, entry.kind === 'toggle' ? String(!!el.checked) : valueOf(el, entry.kind), opened ? { newTab: true } : null);
      }

      if (op === 'fill') {
        var spec = arg && typeof arg === 'object' ? arg : { text: arg, mode: 'replace' };
        var text = String(spec.text == null ? '' : spec.text);
        return res(true, null, fillText(el, text, spec.mode === 'append'));
      }

      // select
      var want = String(arg), idx = -1;
      for (var k = 0; k < el.options.length; k++) if (el.options[k].value === want) { idx = k; break; }
      if (idx < 0) for (var j = 0; j < el.options.length; j++) if (norm(el.options[j].text) === norm(want)) { idx = j; break; }
      if (idx < 0) return res(false, 'no_such_option');
      if (el.options[idx].disabled) return res(false, 'option_disabled');
      el.focus();
      el.selectedIndex = idx;
      fire(el, 'input');
      fire(el, 'change');
      return res(true, null, el.value);
    } catch (err) {
      return res(false, 'error: ' + (err && err.message ? err.message : err));
    }
  }

  window.__freyjaJev = {
    version: VERSION, snapshot: snapshot, act: act, quiet: quiet, go: go, observer: observer,
  };
  return 'installed';
})();
