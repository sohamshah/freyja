// Jev operator DOM snapshot. Evaluate in a page to install window.__freyjaJev.
// Idempotent: a second evaluation keeps the first install (one observer only).
(function () {
  'use strict';
  if (window.__freyjaJev && window.__freyjaJev.version) return 'already-installed';

  var MAX_ACTIONS = 250, MAX_LABEL = 200, MAX_VALUE = 500, MAX_CTX = 300, MAX_TEXT = 6000;
  var ids = new WeakMap(), byId = new Map(), nextId = 1, mutations = 0;

  var observer = new MutationObserver(function (recs) { mutations += recs.length || 1; });
  observer.observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, characterData: true,
  });

  var SELECTOR = [
    'a[href]', 'button', 'input', 'textarea', 'select', 'summary', '[contenteditable]',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=tab]',
    '[role=menuitem]', '[role=switch]', '[role=combobox]', '[role=textbox]', '[role=option]',
  ].join(',');
  var ARIA_ROLES = { button: 1, link: 1, checkbox: 1, radio: 1, tab: 1, menuitem: 1, switch: 1, combobox: 1, textbox: 1, option: 1 };
  var BUTTON_INPUTS = { button: 1, submit: 1, reset: 1, image: 1 };
  var TOGGLE_INPUTS = { checkbox: 1, radio: 1 };
  var NON_TEXT_INPUTS = { range: 1, color: 1, date: 1, 'datetime-local': 1, month: 1, time: 1, week: 1 };

  function norm(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }
  function clip(s, n) { s = norm(s); return s.length > n ? s.slice(0, n) : s; }
  function attr(el, n) { return el.getAttribute(n); }

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
    if (isEditableHost(el) || role === 'textbox') return 'fill';
    if (role === 'checkbox' || role === 'radio' || role === 'switch') return 'toggle';
    if (role === 'link') return 'link';
    if (role === 'combobox') return 'select';
    return 'click';
  }

  function hiddenByAncestry(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
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
    var parts = [];
    ids_.split(/\s+/).forEach(function (i) {
      var t = i && document.getElementById(i);
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
      var img = el.querySelector && el.querySelector('img[alt],[aria-label]');
      if (img) {
        v = norm(attr(img, 'alt') || attr(img, 'aria-label'));
        if (v) return clip(v, MAX_LABEL);
      }
    }
    v = norm(attr(el, 'title'));
    if (v) return clip(v, MAX_LABEL);
    v = norm(attr(el, 'placeholder'));
    if (v) return clip(v, MAX_LABEL);
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
      v = el.textContent || '';
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

  // Collect every visible candidate control (no viewport filtering, no cap).
  function collect() {
    var els = document.querySelectorAll(SELECTOR);
    var out = [], groups = new Map();
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (excludedInput(el)) continue;
      if (isEditableHost(el) && el.parentElement && el.parentElement.isContentEditable &&
          !(el.tagName === 'INPUT')) {
        // nested editable child of an editable host: the host is the control
        if (isEditableHost(el.parentElement) || el.parentElement.closest('[contenteditable]:not([contenteditable=false])')) {
          if (attr(el, 'contenteditable') === '' || attr(el, 'contenteditable') === 'true') {
            if (el.parentElement.closest('[contenteditable]:not([contenteditable=false])')) continue;
          }
        }
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

  function inRange(el) {
    var r = el.getBoundingClientRect(), vh = window.innerHeight, vw = window.innerWidth;
    return r.bottom > -vh && r.top < 2 * vh && r.right > -vw && r.left < 2 * vw;
  }

  function visibleText() {
    var walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT, null);
    var parts = [], total = 0, vh = window.innerHeight, vw = window.innerWidth, range = document.createRange();
    var n;
    while ((n = walker.nextNode())) {
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
      if (r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
      parts.push(s);
      total += s.length + 1;
      if (total > MAX_TEXT) break;
    }
    return parts.join('\n').slice(0, MAX_TEXT);
  }

  function isVisibleLoose(el) {
    if (hiddenByAncestry(el)) return false;
    return el.checkVisibility ? el.checkVisibility({ checkVisibilityCSS: true }) : true;
  }

  function snapshot() {
    var all = collect(), actions = [], unoffered = [];
    for (var i = 0; i < all.length; i++) {
      var e = all[i];
      if (!inRange(e.el)) continue;
      if (e.disabled) { unoffered.push({ label: e.label, context: e.context }); continue; }
      if (actions.length >= MAX_ACTIONS) continue;
      actions.push({
        id: idFor(e.el), kind: e.kind, role: e.role, label: e.label, value: e.value,
        checked: e.checked, expanded: e.expanded, offscreen: offscreenOf(e.el),
        context: e.context, focused: document.activeElement === e.el, guard: e.guard,
      });
    }
    var de = document.documentElement;
    var maxY = Math.max(0, Math.max(de.scrollHeight, document.body ? document.body.scrollHeight : 0) - window.innerHeight);
    return JSON.stringify({
      url: location.href, title: document.title, readyState: document.readyState,
      scroll: { x: Math.round(window.scrollX), y: Math.round(window.scrollY), maxY: Math.round(maxY) },
      viewport: { w: window.innerWidth, h: window.innerHeight },
      text: visibleText(), mutations: mutations, actions: actions, unoffered: unoffered,
    });
  }

  function res(ok, reason, readback) {
    return JSON.stringify({ ok: !!ok, reason: reason || null, readback: readback == null ? null : String(readback) });
  }

  function fire(el, type, ctor, init) {
    init = init || {};
    init.bubbles = true; init.cancelable = true;
    el.dispatchEvent(new (ctor || Event)(type, init));
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
    var a = document.activeElement;
    if (key !== 'Enter' && key !== 'Escape' && key !== 'Tab') return res(false, 'unsupported_key');
    if (a && a.tagName === 'INPUT' && (a.type || '').toLowerCase() === 'password') return res(false, 'refused_password');
    if (key === 'Enter' && !isEditableActive(a)) return res(false, 'no_editable_focused');
    var target = a || document.body;
    var init = { key: key, code: key === 'Escape' ? 'Escape' : key, bubbles: true, cancelable: true };
    var down = new KeyboardEvent('keydown', init);
    var notPrevented = target.dispatchEvent(down);
    if (notPrevented) {
      if (key === 'Enter') {
        target.dispatchEvent(new KeyboardEvent('keypress', init));
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
    target.dispatchEvent(new KeyboardEvent('keyup', init));
    return res(true, null, document.activeElement ? (document.activeElement.value != null ? document.activeElement.value : '') : '');
  }

  function act(id, op, arg, expectedGuard) {
    try {
      if (op === 'scroll') {
        var dy = Number(arg);
        if (!isFinite(dy)) return res(false, 'bad_arg');
        window.scrollBy(0, dy);
        return res(true, null, Math.round(window.scrollY));
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
      if (op === 'click' && entry.kind === 'fill') {
        // clicking an editable just focuses it
      }

      el.scrollIntoView({ block: 'center', inline: 'center' });
      var r = el.getBoundingClientRect();
      var top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (!top || !(top === el || el.contains(top) || top.contains(el))) return res(false, 'covered');

      if (op === 'click') {
        el.focus && el.focus({ preventScroll: true });
        el.click();
        return res(true, null, entry.kind === 'toggle' ? String(!!el.checked) : valueOf(el, entry.kind));
      }

      if (op === 'fill') {
        var spec = arg && typeof arg === 'object' ? arg : { text: arg, mode: 'replace' };
        var text = String(spec.text == null ? '' : spec.text);
        var append = spec.mode === 'append';
        el.focus();
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
          var proto = el.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
          var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
          setter.call(el, append ? (el.value || '') + text : text);
          fire(el, 'input', typeof InputEvent === 'function' ? InputEvent : Event, { inputType: 'insertText', data: text });
          fire(el, 'change');
          return res(true, null, el.value);
        }
        el.textContent = append ? (el.textContent || '') + text : text;
        fire(el, 'input', typeof InputEvent === 'function' ? InputEvent : Event, { inputType: 'insertText', data: text });
        return res(true, null, el.textContent);
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

  window.__freyjaJev = { version: 1, snapshot: snapshot, act: act, observer: observer };
  return 'installed';
})();
