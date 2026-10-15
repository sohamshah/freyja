// Jev operator DOM snapshot. Evaluate in a page to install window.__freyjaJev.
// Idempotent per version: evaluating the same version again keeps the first
// install (one observer only); a newer version replaces an older one.
(function () {
  'use strict';
  var VERSION = 8;
  var prior = window.__freyjaJev;
  if (prior && prior.version === VERSION) return 'already-installed';
  if (prior && prior.observer) { try { prior.observer.disconnect(); } catch (e) { /* ignore */ } }

  var MAX_ACTIONS = 250, MAX_LABEL = 200, MAX_VALUE = 500, MAX_CTX = 300, MAX_TEXT = 6000;
  // A row's text as a context: enough for a name, an email and a status.
  var ROW_CTX = 140;
  // Look-alike contexts are compared pairwise only in groups up to this size.
  var LOOKALIKE_MAX = 80;
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
    // Requests an action started and that have not answered yet: a Continue that
    // posts and navigates only when the server answers looks finished to a
    // mutation count but is not.
    "var mark=0,busy=0;function net(){document.dispatchEvent(new CustomEvent('freyja-jev-net',{detail:String(busy)}));}" +
    "document.addEventListener('freyja-jev-mark',function(){mark=Date.now();busy=0;net();});" +
    "function track(p){if(Date.now()<mark||!live())return p;busy++;net();var done=function(){busy=Math.max(0,busy-1);net();};" +
    "if(p&&p.then)p.then(done,done);return p;}" +
    "if(window.fetch){var f0=window.fetch;window.fetch=function(){return track(f0.apply(this,arguments));};}" +
    "var s0=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(){var x=this;" +
    "if(Date.now()>=mark&&live()){busy++;net();x.addEventListener('loadend',function(){busy=Math.max(0,busy-1);net();});}" +
    "return s0.apply(this,arguments);};" +
    "window.addEventListener('beforeunload',function(){document.dispatchEvent(new CustomEvent('freyja-jev-net',{detail:'unload'}));});" +
    "document.dispatchEvent(new CustomEvent('freyja-jev-main-ready'));})();";
  var dialogs = [], pageOpened = false, mainHook = false, netBusy = 0, unloading = false;
  document.addEventListener('freyja-jev-main-ready', function () { mainHook = true; });
  document.addEventListener('freyja-jev-net', function (e) {
    if (e.detail === 'unload') unloading = true; else netBusy = +e.detail || 0;
  });
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
    '[onclick]', '[tabindex]',
  ].join(',');
  // Elements that are controls by their markup alone. The rest of SELECTOR
  // ([onclick], [tabindex]) and pointer-cursor elements are inferred controls:
  // kept only when small and holding no control of their own.
  var SEMANTIC = 'a[href],button,input,textarea,select,summary,[contenteditable],[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],' +
    '[role=switch],[role=combobox],[role=textbox],[role=searchbox],[role=option],[role=treeitem],[role=slider],' +
    '[role=spinbutton]';
  var INFERRED_MAX_TEXT = 120;
  var MONTH_YEAR = /\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(19|20)\d\d\b/i;
  var ARIA_ROLES = {
    button: 1, link: 1, checkbox: 1, radio: 1, tab: 1, menuitem: 1, menuitemcheckbox: 1,
    menuitemradio: 1, switch: 1, combobox: 1, textbox: 1, searchbox: 1, option: 1, treeitem: 1,
  };
  var BUTTON_INPUTS = { button: 1, submit: 1, reset: 1, image: 1 };
  var TOGGLE_INPUTS = { checkbox: 1, radio: 1 };
  var NON_TEXT_INPUTS = { range: 1, color: 1, date: 1, 'datetime-local': 1, month: 1, time: 1, week: 1 };
  var KEY_CODES = { Enter: 13, Escape: 27, Tab: 9, ArrowDown: 40, ArrowUp: 38 };
  var PAGE_KEYS = { Enter: 1, Escape: 1, Tab: 1, ArrowDown: 1, ArrowUp: 1 };
  // Widgets whose page script handles keys: Enter opens a combobox, picks an
  // option or a menu item, or selects a tab.
  var KEY_WIDGETS = {
    combobox: 1, listbox: 1, option: 1, menuitem: 1, menuitemcheckbox: 1, menuitemradio: 1,
    tab: 1, treeitem: 1,
  };
  var SEARCHY = /search|filter|find|query|lookup/i;

  function norm(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }
  // Two texts a reader could mistake for each other: the same, or at most two
  // letters apart (Hooper and Hopper), ignoring case. Different numbers are not
  // look-alikes: "Passenger 1" and "Passenger 2" are read exactly.
  function lookAlike(a, b) {
    if (!a || !b) return false;
    a = a.toLowerCase(); b = b.toLowerCase();
    if (a === b) return true;
    if (a.replace(/\D/g, '') !== b.replace(/\D/g, '')) return false;
    if (Math.min(a.length, b.length) < 6 || Math.abs(a.length - b.length) > 2) return false;
    var prev = [], cur, i, j;
    for (j = 0; j <= b.length; j++) prev[j] = j;
    for (i = 1; i <= a.length; i++) {
      cur = [i];
      var low = i;
      for (j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
        if (cur[j] < low) low = cur[j];
      }
      if (low > 2) return false;
      prev = cur;
    }
    return prev[b.length] <= 2;
  }
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
      // A read-only field (travelers, a date) opens a panel or picker when clicked;
      // it cannot be typed into, so it is offered as a click.
      if (isReadOnly(el)) return 'click';
      if (NON_TEXT_INPUTS[t]) return 'fill';
      return 'fill';
    }
    if ((role === 'textbox' || role === 'searchbox') && attr(el, 'aria-readonly') === 'true' && !isEditableHost(el)) return 'click';
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
      if (!el.checkVisibility({ checkVisibilityCSS: true, opacityProperty: true })) return false;
    } else {
      var cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    }
    var r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    return !offCanvas(el, r);
  }

  // Parked beside the window, out of reach: a slide-in panel waiting off the
  // right edge. Its controls are not on the page until it slides in. A real
  // airline's six closed panels each listed a "Close" button on every page.
  // Something to the side of a carousel or a wide table can be scrolled to and
  // still counts.
  function offCanvas(el, r) {
    var vw = window.innerWidth;
    if (r.right > 0 && r.left < vw) return false;
    var se = document.scrollingElement || document.documentElement;
    if (se.scrollWidth > vw + 1) return false;
    for (var n = parentOf(el); n && n.nodeType === 1 && n !== document.body; n = parentOf(n)) {
      if (n.scrollWidth > n.clientWidth + 1) {
        var ox = getComputedStyle(n).overflowX;
        if (ox === 'auto' || ox === 'scroll') return false;
      }
    }
    return true;
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

  // A control with no accessible name, named the way a person reads an icon:
  // from its image file (search_btn.svg), its icon (svg <use href="#close">),
  // its id (btnSearch) or an icon class (fa-times). Returns {name, from} or null.
  // A real airline's Search control was a link around an image with no alt
  // text; the run labelled it with the surrounding text and never clicked it.
  var NOISE = { btn: 1, button: 1, img: 1, image: 1, icon: 1, icons: 1, ico: 1, svg: 1, png: 1, link: 1, ui: 1,
    white: 1, blue: 1, black: 1, gray: 1, grey: 1, green: 1, sm: 1, md: 1, lg: 1, small: 1, large: 1, w: 1, h: 1,
    inline: 1, block: 1, right: 1, left: 1, active: 1, default: 1, primary: 1, secondary: 1, fa: 1, mdi: 1,
    material: 1, glyphicon: 1, bi: 1, js: 1, el: 1, wrap: 1, wrapper: 1, container: 1, item: 1, the: 1,
    id: 1, instance: 1, root: 1, main: 1, view: 1 };
  var ICON_WORDS = { close: 'close', times: 'close', x: 'close', cross: 'close', dismiss: 'close', clear: 'clear',
    search: 'search', magnifier: 'search', magnify: 'search', loupe: 'search', next: 'next', forward: 'next',
    prev: 'previous', previous: 'previous', back: 'back', menu: 'menu', hamburger: 'menu', burger: 'menu',
    calendar: 'calendar', trash: 'delete', delete: 'delete', remove: 'remove', minus: 'remove', plus: 'add',
    add: 'add', edit: 'edit', pencil: 'edit', settings: 'settings', gear: 'settings', cog: 'settings',
    share: 'share', download: 'download', upload: 'upload', info: 'info', help: 'help', cart: 'cart',
    filter: 'filter', sort: 'sort', expand: 'expand', collapse: 'collapse', more: 'more', kebab: 'more',
    ellipsis: 'more', play: 'play', pause: 'pause', submit: 'submit', send: 'send', refresh: 'refresh',
    home: 'home', user: 'account', account: 'account', profile: 'account', logout: 'log out', login: 'log in',
    chevron: '', arrow: '', caret: '' };
  function words(s) {
    return String(s || '').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/\.(svg|png|gif|jpe?g|webp)(\?.*)?$/i, '')
      .split(/[^A-Za-z]+/).map(function (w) { return w.toLowerCase(); }).filter(Boolean);
  }
  function derivedName(el) {
    function plain(s) { return words(s).filter(function (w) { return !NOISE[w] && w.length > 1; }); }
    function icons(s) { return words(s).map(function (w) { return ICON_WORDS[w]; }).filter(Boolean); }
    var img = el.tagName === 'IMG' ? el : el.querySelector && el.querySelector('img[src]');
    if (img) {
      var base = (attr(img, 'src') || '').split(/[?#]/)[0].split('/').pop();
      var w = plain(base);
      if (w.length && w.length <= 4) return { name: w.join(' '), from: 'image ' + base };
    }
    var use = el.querySelector && el.querySelector('svg use');
    var href = use && (attr(use, 'href') || attr(use, 'xlink:href'));
    if (href) {
      var u = plain(href.split('#').pop());
      if (u.length && u.length <= 4) return { name: u.join(' '), from: 'icon ' + href.split('#').pop() };
    }
    if (el.id) {
      var d = plain(el.id);
      if (d.length && d.length <= 4) return { name: d.join(' '), from: 'id ' + el.id };
    }
    var cls = [String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || '')];
    var inner = el.querySelector && el.querySelector('i[class], span[class*=icon], svg[class]');
    if (inner) cls.push(String(inner.className && inner.className.baseVal !== undefined ? inner.className.baseVal : inner.className));
    for (var i = 0; i < cls.length; i++) {
      var c = icons(cls[i]);
      if (c.length) return { name: c[0], from: 'class ' + clip(cls[i], 60) };
    }
    return null;
  }

  function valueOf(el, kind) {
    var v = '';
    if (el.tagName === 'SELECT') {
      var o = el.selectedOptions && el.selectedOptions[0];
      v = o ? norm(o.text) : '';
    } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      if (kind === 'fill' || isReadOnly(el)) v = el.value || '';
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
    var search = (el.type || '').toLowerCase() === 'search' || attr(el, 'role') === 'searchbox' ||
        (el.closest && el.closest('[role=search]')) || SEARCHY.test(hay);
    // A field whose suggestion list is open: Enter picks the highlighted
    // suggestion (after Down), which commits a value, not a form. In a search
    // box Enter on the typed text runs the search: told to pick, a catalog run
    // pressed Down and Return and opened the first model instead.
    if (el === deepActive() && openList(el)) return search ? 'search-pick' : 'pick';
    if (search) return 'search';
    var f = el.form;
    if (f) {
      var b = f.querySelector('button[type=submit],button:not([type]),input[type=submit],input[type=image]');
      return b ? 'form:' + nameOf(b) : 'form';
    }
    return 'unknown';
  }

  // A field that shows suggestions as you type (it fetches them after a short
  // pause, so the page looks settled before the list arrives): by ARIA, by an
  // autocomplete widget's class, or by turning off the browser's own suggestions.
  function suggests(el) {
    if (el.tagName !== 'INPUT') return false;
    var cls = String(el.className || '') + ' ' + (attr(el, 'role') || '');
    return attr(el, 'aria-autocomplete') !== null || attr(el, 'role') === 'combobox' ||
      /autocomplete|typeahead|suggest|combobox/i.test(cls) || (attr(el, 'autocomplete') || '').toLowerCase() === 'off';
  }

  // The suggestion list a text field drives, when one is showing: by ARIA
  // (aria-expanded, aria-controls/owns, aria-activedescendant), or a visible
  // list of items just below or above the field (autocomplete widgets with no
  // roles, like the airport field of a real booking site).
  var LISTY = /autocomplete|typeahead|suggest|listbox|dropdown|combobox|menu|results|options/i;
  function openList(el) {
    if (!el || (el.tagName !== 'INPUT' && attr(el, 'role') !== 'combobox')) return null;
    var ctl = attr(el, 'aria-controls') || attr(el, 'aria-owns');
    if (ctl) {
      var t = document.getElementById(ctl.split(/\s+/)[0]);
      if (t && isVisible(t) && t.children.length) return t;
    }
    if (attr(el, 'aria-expanded') === 'true' || attr(el, 'aria-activedescendant')) {
      var lb = document.querySelector('[role=listbox]');
      if (lb && isVisible(lb)) return lb;
    }
    var r = el.getBoundingClientRect();
    var lists = document.querySelectorAll('ul,ol,[role=listbox],[role=menu]');
    for (var i = 0; i < lists.length; i++) {
      var u = lists[i];
      if (u.children.length < 1 || !isVisible(u)) continue;
      var cls = String(u.className && u.className.baseVal !== undefined ? u.className.baseVal : u.className || '');
      if (!LISTY.test(cls + ' ' + (attr(u, 'role') || '') + ' ' + (u.id || ''))) continue;
      var b = u.getBoundingClientRect();
      var near = (b.top >= r.bottom - 4 && b.top <= r.bottom + 40) || (b.bottom <= r.top + 4 && b.bottom >= r.top - 40);
      var over = b.left < r.right && b.right > r.left;
      if (near && over) return u;
    }
    return null;
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

  // An inferred control ([onclick], [tabindex], a pointer cursor) that is really a
  // container: long text, a real control inside, or a large part of the page.
  function inferredNoise(el) {
    if (el === document.body || el === document.documentElement) return true;
    if (el.querySelector && el.querySelector(SEMANTIC)) return true;
    if (textOf(el).length > INFERRED_MAX_TEXT) return true;
    var r = el.getBoundingClientRect();
    return r.width * r.height > 0.4 * window.innerWidth * window.innerHeight;
  }

  // An inferred control inside a popup list (suggestions, a custom dropdown) is
  // an option of that list.
  function inferredRole(el) {
    for (var n = parentOf(el), i = 0; n && n.nodeType === 1 && i < 4; n = parentOf(n), i++) {
      var r = attr(n, 'role');
      if (r === 'listbox' || r === 'menu') return 'option';
      var cls = String(n.className && n.className.baseVal !== undefined ? n.className.baseVal : n.className || '');
      if ((n.tagName === 'UL' || n.tagName === 'OL') && LISTY.test(cls)) return 'option';
    }
    return 'button';
  }

  // Where a pointer cursor starts: the outermost element of a clickable area
  // (the cursor is inherited, so its children report it too).
  function pointerRoot(el) {
    if (getComputedStyle(el).cursor !== 'pointer') return false;
    var p = parentOf(el);
    return !p || p.nodeType !== 1 || getComputedStyle(p).cursor !== 'pointer';
  }

  // A radio or checkbox drawn by the page: the native input has no size (or is
  // hidden), and the visible box around it is what a person clicks. Real fare
  // grids and add-on cards are built this way; with the input dropped as
  // invisible, a whole fare grid had no controls.
  function proxyFor(el) {
    if (el.tagName !== 'INPUT' || !TOGGLE_INPUTS[(el.type || '').toLowerCase()]) return null;
    if (!el.isConnected || hiddenByAncestry(el)) return null;
    var cands = [];
    if (el.labels) for (var i = 0; i < el.labels.length; i++) cands.push(el.labels[i]);
    for (var n = parentOf(el), k = 0; n && n.nodeType === 1 && k < 5 && n !== document.body; n = parentOf(n), k++) cands.push(n);
    var vw = window.innerWidth, vh = window.innerHeight;
    for (var j = 0; j < cands.length; j++) {
      var c = cands[j];
      if (!isVisible(c)) continue;
      var r = c.getBoundingClientRect();
      if (r.width * r.height > 0.25 * vw * vh) return null;
      if (c.tagName === 'LABEL' || getComputedStyle(c).cursor === 'pointer') return c;
    }
    return null;
  }

  // The first text of a part of the page that is not a control's own text, as
  // its line (the nearest block around it) when that is short.
  function leadText(own, controls) {
    var w = document.createTreeWalker(own, NodeFilter.SHOW_TEXT, null), n;
    // A control's text, or a link's even without an address (a date picker's
    // "Prev" is an <a> with no href and no pointer cursor).
    function inControl(p) {
      for (var a = p; a && a !== own; a = a.parentElement) if (controls.has(a)) return true;
      return !!p.closest(SEMANTIC + ',a,button,[data-handler]');
    }
    // The text of a block without its controls' text ("October 2026", not
    // "Prev October 2026").
    function plainText(b) {
      var parts = [], tw = document.createTreeWalker(b, NodeFilter.SHOW_TEXT, null), x;
      while ((x = tw.nextNode())) {
        var v = norm(x.nodeValue);
        if (v && x.parentElement && !inControl(x.parentElement)) parts.push(v);
      }
      return parts.join(' ');
    }
    while ((n = w.nextNode())) {
      var t = norm(n.nodeValue);
      var p = n.parentElement;
      if (!t || !p || inControl(p) || !isVisibleLoose(p)) continue;
      for (var b = p; b && b !== own; b = b.parentElement) {
        if (getComputedStyle(b).display !== 'inline') {
          var bt = plainText(b);
          return bt.length <= 60 ? bt : t;
        }
      }
      return t;
    }
    return '';
  }

  // Controls drawn as cells of a grid (fare boxes under "Basic / Economy /
  // Business" headers, plan columns): name each by its column header and its
  // row, as a person reads the grid. Only drawn controls (proxied radios,
  // inferred clickables) are considered; a cell's column is the short text
  // above the topmost cell of its column, and its row is the start of the
  // nearest ancestor that holds another cell.
  function gridCells(all) {
    var cells = all.filter(function (e) { return e.proxy || (e.inferred && e.role !== 'option'); });
    if (cells.length < 4) return;
    var cols = new Map();
    cells.forEach(function (e) {
      var r = (e.proxy || e.el).getBoundingClientRect();
      if (r.width < 1) return;
      e._r = r;
      var key = Math.round((r.left + r.width / 2) / 12);
      var c = cols.get(key);
      if (!c) cols.set(key, c = []);
      c.push(e);
    });
    var multi = []; cols.forEach(function (c) { if (c.length >= 2) multi.push(c); });
    if (multi.length < 2) return;
    var heads = null;
    multi.forEach(function (c) {
      var top = c.reduce(function (a, b) { return a._r.top <= b._r.top ? a : b; });
      if (!heads) {
        heads = [];
        document.querySelectorAll('body *').forEach(function (h) {
          if (h.children.length > 2 || h.closest(SEMANTIC)) return;
          var t = textOf(h);
          if (!t || t.length > 30 || /^[$\d.,\s]+$/.test(t)) return;
          var hr = h.getBoundingClientRect();
          if (hr.width < 1 || hr.height < 1) return;
          heads.push({ t: t, r: hr });
        });
      }
      var r = top._r, cx = r.left + r.width / 2, best = null;
      heads.forEach(function (h) {
        if (h.r.bottom > r.top + 2 || h.r.left > cx || h.r.right < cx) return;
        if (r.top - h.r.bottom > 300) return;
        if (!best || h.r.bottom > best.r.bottom) best = h;
      });
      if (best) c.forEach(function (e) { e.column = best.t; });
    });
    // A cell's row: the nearest ancestor holding another cell of the same row
    // and none of another row; in a flat grid with no row element, the text to
    // the left of the row's first cell.
    function cy(e) { return e._r.top + e._r.height / 2; }
    function sameRow(a, b) { return Math.abs(cy(a) - cy(b)) < Math.max(8, a._r.height / 2); }
    var placed = cells.filter(function (e) { return e._r; });
    placed.forEach(function (e) {
      for (var a = parentOf(e.proxy || e.el), d = 0; a && a.nodeType === 1 && d < 8; a = parentOf(a), d++) {
        var holds = placed.filter(function (o) { return o !== e && composedContains(a, o.proxy || o.el); });
        if (!holds.length) continue;
        if (holds.every(function (o) { return sameRow(o, e); })) e.row = clip(textOf(a), 90);
        break;
      }
      if (e.row) return;
      var left = placed.filter(function (o) { return sameRow(o, e); })
        .reduce(function (m, o) { return Math.min(m, o._r.left); }, e._r.left);
      var best = null;
      (heads || []).forEach(function (h) {
        var hy = h.r.top + h.r.height / 2;
        if (h.r.right > left + 2 || hy < e._r.top || hy > e._r.bottom) return;
        if (!best || h.r.right > best.r.right) best = h;
      });
      if (best) e.row = best.t;
    });
  }

  // Collect every visible candidate control (no viewport filtering, no cap).
  function collect() {
    var els = [], others = [];
    eachElement(document, function (el) { if (el.matches(SELECTOR)) els.push(el); else others.push(el); });
    var out = [], groups = new Map(), seen = new Set();
    function add(el, proxy, inferred) {
      var role = inferred ? inferredRole(el) : roleOf(el), kind = inferred ? 'click' : kindOf(el, role);
      var label = nameOf(el), hint = '';
      if (!label) {
        var d = derivedName(el);
        if (d) { label = d.name; hint = 'no visible name; from ' + d.from; }
      }
      var also = '';
      if (proxy) {
        // What the box shows (prices, "Standard") comes first: a drawn radio's own
        // name is often generic or wrong (a fare grid gave three columns the same
        // label with the cheapest price).
        var shown = textOf(proxy);
        if (shown && shown !== label) { also = label.indexOf(shown) < 0 ? label : ''; label = shown; }
      }
      var e = {
        el: el, proxy: proxy, role: role, kind: kind, label: label, also: also, value: valueOf(el, kind),
        disabled: isDisabled(el), checked: checkedOf(el), expanded: expandedOf(el),
        href: el.tagName === 'A' ? (el.href || '') : '', context: '', hint: hint, inferred: !!inferred,
      };
      out.push(e);
    }
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (seen.has(el)) continue;
      seen.add(el);
      if (excludedInput(el)) continue;
      var inferred = !el.matches(SEMANTIC);
      if (inferred && inferredNoise(el)) continue;
      if (isEditableHost(el) && el.parentElement && el.parentElement.closest('[contenteditable]:not([contenteditable=false])')) {
        continue; // nested editable child of an editable host: the host is the control
      }
      if (!isVisible(el)) {
        var px = proxyFor(el);
        if (px) add(el, px, false);
        continue;
      }
      add(el, null, inferred);
    }
    // Pointer-cursor areas with no control in them: fare boxes, offer cards,
    // suggestion rows a page draws with divs.
    for (var j = 0; j < others.length; j++) {
      var o = others[j];
      if (!pointerRoot(o) || inferredNoise(o) || (o.parentElement && o.parentElement.closest(SEMANTIC)) || !isVisible(o)) continue;
      add(o, null, true);
    }
    // An inferred control holding another collected control is its container
    // (a list holding its suggestion rows): keep the inner ones.
    var holds = new Set();
    out.forEach(function (e) {
      for (var n = parentOf(e.el), d = 0; n && n.nodeType === 1 && d < 12; n = parentOf(n), d++) holds.add(n);
    });
    out = out.filter(function (e) { return !(e.inferred && holds.has(e.el)); });
    gridCells(out);
    out.forEach(function (e) {
      if (e.column) e.label = e.column + ': ' + e.label;
      if (e.also) e.label = e.label + ' — ' + e.also;
      e.label = clip(e.label, MAX_LABEL);
    });
    out.forEach(function (e) {
      var key = e.role + '\u0000' + e.label;
      var g = groups.get(key);
      if (!g) groups.set(key, g = []);
      g.push(e);
    });
    var controls = new Set(), repeated = new Set();
    out.forEach(function (e) { controls.add(e.el); if (e.proxy) controls.add(e.proxy); });
    groups.forEach(function (g) {
      if (g.length > 1) g.forEach(function (e) { repeated.add(e.el); if (e.proxy) repeated.add(e.proxy); });
    });
    // The text of a control's own part of the page, its blocks (cells, lines)
    // joined with " · ". The text of controls repeated on every row ("Suspend",
    // "Edit") is left out; a row's own link (the person's name) is kept.
    function regionText(own, e) {
      var parts = [], last = null, w = document.createTreeWalker(own, NodeFilter.SHOW_TEXT, null), n;
      outer: while ((n = w.nextNode())) {
        var t = norm(n.nodeValue), p = n.parentElement;
        if (!t || !p || !isVisibleLoose(p)) continue;
        for (var a = p; a && a !== own; a = a.parentElement) {
          if (a === e.el || a === e.proxy || repeated.has(a)) continue outer;
        }
        var b = p;
        while (b !== own && getComputedStyle(b).display === 'inline') b = b.parentElement;
        if (b === last && parts.length) parts[parts.length - 1] += ' ' + t;
        else parts.push(t);
        last = b;
      }
      return parts.join(' · ');
    }
    // Repeated labels ("First Name *" in each passenger's card) are told apart by
    // their own part of the page: the largest ancestor holding no other control
    // of the same label, named by its heading when it has one ("Passenger 2
    // (Adult)"), else by its text. The nearest ancestor's text was the same for
    // every card, so two fields read alike and typing in the second one did not
    // count as progress.
    groups.forEach(function (g) {
      if (g.length < 2) return;
      g.forEach(function (e) {
        var own = null;
        for (var a = e.el.parentElement; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
          var clash = false;
          for (var k = 0; k < g.length; k++) {
            if (g[k] !== e && a.contains(g[k].el)) { clash = true; break; }
          }
          if (clash) break;
          own = a;
        }
        if (!own) return;
        e._own = own;
        // A table row is read across, cell by cell: its first cell alone named
        // five "Suspend" buttons "Ada Carter", and the email that tells them
        // apart was in the next cell.
        if (own.matches('tr,[role=row]')) {
          var cells = regionText(own, e);
          if (cells && cells !== e.label) e.context = clip(cells, ROW_CTX);
          return;
        }
        var h = own.querySelector('h1,h2,h3,h4,h5,h6,legend,caption,[role=heading]');
        // No heading: its leading text ("October 2026" over a month of day
        // links), not all of it. Text of controls does not count: a real date
        // picker's first line was its "Prev" link, and day links read "29 (Prev)".
        var lead = leadText(own, controls);
        var t = h ? textOf(h) : (lead.length >= 3 && lead !== e.label ? lead : textOf(own));
        if (t && t !== e.label) e.context = clip(t, MAX_CTX);
      });
      // Controls that still read alike, the same or a letter or two apart, get
      // all of their own part's text: "Suspend (Grace Hooper)" beside "Suspend
      // (Grace Hopper)" was clicked for grace.hopper.1437, which neither showed.
      if (g.length > LOOKALIKE_MAX) return;
      var alike = new Set();
      for (var i = 0; i < g.length; i++) {
        for (var j = i + 1; j < g.length; j++) {
          if (lookAlike(g[i].context, g[j].context)) { alike.add(g[i]); alike.add(g[j]); }
        }
      }
      alike.forEach(function (e) {
        var all = e._own ? regionText(e._own, e) : '';
        if (all && all !== e.label && all.length > e.context.length) e.context = clip(all, ROW_CTX);
      });
    });
    // A grid cell is told apart by its row (the flight's times), not by the
    // section around the grid: two flights with the same Business price both
    // read "(DEPARTING FLIGHT)" and the run picked the wrong one.
    out.forEach(function (e) { if (e.row) e.context = e.row; });
    // Day numbers name their month even when only one month has that day: with
    // the past days disabled, November's "12" read just "12" beside "13
    // (November 2026)", and the run would not click it.
    var days = out.filter(function (e) { return /^\d{1,2}$/.test(e.label); });
    if (days.length >= 7) {
      days.forEach(function (e) {
        if (e.context) return;
        for (var a = e.el.parentElement, d = 0; a && a !== document.body && d < 8; a = a.parentElement, d++) {
          var lead = leadText(a, controls);
          if (MONTH_YEAR.test(lead)) { e.context = clip(lead, MAX_CTX); break; }
        }
      });
    }
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

  // The layer a hit at (x, y) belongs to when it is modal: a dialog by role, or a
  // fixed element covering most of the viewport (a backdrop and its card).
  function layerOf(t) {
    var vw = window.innerWidth, vh = window.innerHeight;
    for (var n = t; n && n.nodeType === 1 && n !== document.body && n !== document.documentElement; n = parentOf(n)) {
      if (attr(n, 'aria-modal') === 'true' || attr(n, 'role') === 'dialog' || attr(n, 'role') === 'alertdialog' ||
          (n.tagName === 'DIALOG' && n.open)) return n;
      if (getComputedStyle(n).position === 'fixed') {
        var r = n.getBoundingClientRect();
        // An empty, transparent sheet that only catches outside clicks (a
        // travelers panel puts one over the page) is not a dialog.
        if (r.width * r.height >= 0.5 * vw * vh && (textOf(n) || (n.querySelector && n.querySelector(SEMANTIC)))) return n;
      }
    }
    return null;
  }

  // A modal layer covering the page, when there is one: most sample points of the
  // viewport hit the same layer, and it is not the page's own shell (it does not
  // hold most of the controls). A membership upsell appended after a long result
  // list once hid behind 6000 characters of flight text, and the run kept
  // waiting on a page it could not see was blocked.
  function topLayer(all) {
    var vw = window.innerWidth, vh = window.innerHeight, votes = new Map();
    [[0.5, 0.5], [0.3, 0.3], [0.7, 0.3], [0.3, 0.7], [0.7, 0.7]].forEach(function (p) {
      var l = layerOf(deepElementFromPoint(vw * p[0], vh * p[1]));
      if (l) votes.set(l, (votes.get(l) || 0) + 1);
    });
    var best = null, n = 0;
    votes.forEach(function (c, l) { if (c > n) { best = l; n = c; } });
    if (!best || n < 3) return null;
    var inside = all.filter(function (e) { return composedContains(best, e.el); }).length;
    if (all.length >= 8 && inside > 0.6 * all.length) return null;
    return best;
  }

  function layerInfo(l) {
    var h = l.querySelector && l.querySelector('h1,h2,h3,h4,[role=heading]');
    var text = textOf(l);
    return { title: clip(h ? textOf(h) : text.split(/[.!?\n]/)[0], 120), text: clip(text, 600) };
  }

  // Embedded frames this surface cannot read (another origin, or any frame: the
  // snapshot runs in the top document only). Their content is invisible here.
  function framesOf() {
    var out = [];
    document.querySelectorAll('iframe,frame').forEach(function (f) {
      if (!isVisible(f)) return;
      var r = f.getBoundingClientRect();
      if (r.width < 80 || r.height < 50) return;
      var host = '';
      try { host = new URL(f.src, location.href).host; } catch (e) { host = ''; }
      out.push({ host: host || '(inline)', title: attr(f, 'title') || '', w: Math.round(r.width), h: Math.round(r.height),
        x: Math.round(r.left), y: Math.round(r.top), offscreen: offscreenOf(f) });
    });
    return out.slice(0, 6);
  }

  function snapshot() {
    touch();
    var all = collect(), cands = [], unoffered = [], behind = 0;
    var layer = topLayer(all);
    for (var i = 0; i < all.length; i++) {
      var e = all[i];
      if (e.disabled) { unoffered.push({ label: e.label, context: e.context }); continue; }
      // Under a modal layer only its own controls can be reached.
      if (layer && !composedContains(layer, e.el)) { behind++; continue; }
      e.order = i;
      e.dist = distance(e.proxy || e.el);
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
        checked: e.checked, expanded: e.expanded, offscreen: offscreenOf(e.proxy || e.el),
        context: e.context, focused: active === e.el, guard: e.guard,
      };
      if (e.hint) a.hint = e.hint;
      if (e.inferred) a.inferred = true;
      if (e.kind === 'fill') a.enter = enterOf(e.el);
      if (e.kind === 'select') a.options = optionsOf(e.el);
      return a;
    });
    var de = document.documentElement;
    var maxY = Math.max(0, Math.max(de.scrollHeight, document.body ? document.body.scrollHeight : 0) - window.innerHeight);
    var text = pageText();
    var dialog = layer ? layerInfo(layer) : null;
    if (dialog) text = '[dialog] ' + dialog.text + '\n' + text;
    return JSON.stringify({
      url: location.href, title: document.title, readyState: document.readyState,
      scroll: { x: Math.round(window.scrollX), y: Math.round(window.scrollY), maxY: Math.round(maxY) },
      viewport: { w: window.innerWidth, h: window.innerHeight },
      text: text.slice(0, MAX_TEXT), mutations: mutations, actions: actions, unoffered: unoffered,
      dialogs: drain(), hook: mainHook, dialog: dialog, behind: behind, frames: framesOf(),
    });
  }

  // p: requests the last action started that have not answered; nav: the page
  // is unloading (a navigation is under way).
  function quiet() {
    touch();
    return JSON.stringify({ m: mutations, rs: document.readyState, u: location.href, p: netBusy, nav: unloading });
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
  function pointerClick(el, at) {
    var r = el.getBoundingClientRect();
    var init = {
      bubbles: true, cancelable: true, composed: true, view: window, button: 0, buttons: 1,
      clientX: at ? at.x : r.left + r.width / 2, clientY: at ? at.y : r.top + r.height / 2,
      pointerId: 1, pointerType: 'mouse', isPrimary: true,
    };
    var P = typeof PointerEvent === 'function' ? PointerEvent : MouseEvent;
    el.dispatchEvent(new P('pointerover', init));
    el.dispatchEvent(new MouseEvent('mouseover', init));
    el.dispatchEvent(new P('pointerdown', init));
    var down = el.dispatchEvent(new MouseEvent('mousedown', init));
    if (down) {
      // A real press moves focus: to the control (or its focusable ancestor), or
      // off the field that had it. Without the blur, a field typed last never
      // fires change and frameworks that commit on change never see its value.
      var f = el;
      while (f && f.nodeType === 1 && !(f.tabIndex >= 0 || f.hasAttribute('tabindex'))) f = parentOf(f);
      if (f && f.nodeType === 1 && f.focus) f.focus({ preventScroll: true });
      else {
        var act0 = deepActive();
        if (act0 && act0 !== document.body && !composedContains(act0, el) && act0.blur) act0.blur();
      }
    }
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

  function keyWidget(a) {
    var r = a && attr(a, 'role');
    return !!(r && KEY_WIDGETS[r.trim().split(/\s+/)[0].toLowerCase()]);
  }

  function doKey(key) {
    var a = deepActive();
    if (!PAGE_KEYS[key]) return res(false, 'unsupported_key');
    if (a && a.tagName === 'INPUT' && (a.type || '').toLowerCase() === 'password') return res(false, 'refused_password');
    if (key === 'Enter' && !isEditableActive(a) && !keyWidget(a)) return res(false, 'no_editable_focused');
    var picking = key === 'Enter' && !!openList(a);
    var target = a || document.body;
    var notPrevented = target.dispatchEvent(keyEvent('keydown', key));
    if (notPrevented) {
      if (key === 'Enter') {
        target.dispatchEvent(keyEvent('keypress', key));
        // Enter in a field with an open suggestion list picks the suggestion; the
        // page's own handler did that, and the form is not submitted.
        if (!picking && target.tagName === 'INPUT' && target.form && target.form.requestSubmit) {
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
      if (isField && !append && el.value === text && text) {
        // Typing the same value again changes nothing a widget can see: an
        // autocomplete searches only when the value changes, and its list
        // stayed shut. A person retyping passes through other values; clear it.
        el.select();
        document.execCommand('delete', false);
      }
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

  // Where a person's click on `el` would land: a point whose topmost element is
  // `el`, inside it, or around it. The middle of a control can sit under parts
  // of the control that are not inside it, such as the label drawn across an
  // outlined field, so other points are tried too. Failing those, a topmost
  // element of the same small widget still counts (the nearest shared ancestor
  // is not much bigger than the control: the field's own wrapper). Anything
  // else (a dialog, a backdrop, an open menu) covers the control: null.
  function hitPoint(el) {
    var r = el.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
    var fx = [0.5, 0.25, 0.75, 0.1, 0.9], fy = [0.5, 0.25, 0.75];
    for (var j = 0; j < fy.length; j++) {
      for (var i = 0; i < fx.length; i++) {
        var x = r.left + r.width * fx[i], y = r.top + r.height * fy[j];
        if (x < 0 || y < 0 || x >= vw || y >= vh) continue;
        var top = deepElementFromPoint(x, y);
        if (top && (composedContains(el, top) || composedContains(top, el))) return { x: x, y: y };
      }
    }
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    var t = deepElementFromPoint(cx, cy);
    return t && sameWidget(el, t, r) ? { x: cx, y: cy } : null;
  }

  // The control whose panel is open (aria-expanded=true), other than the target
  // and its ancestors, and not inside a modal layer.
  function expandedOpener(el) {
    return Array.prototype.find.call(document.querySelectorAll('[aria-expanded=true]'), function (o) {
      return o !== el && !composedContains(o, el) && isVisible(o) && !layerOf(o);
    }) || null;
  }

  // A press on the page itself, outside any control: what closes an open panel.
  function outsidePress(panel) {
    // Where a person would press: a corner of the window that is page, not a
    // control and not the panel (a sheet that catches outside clicks counts).
    var vw = window.innerWidth, vh = window.innerHeight, spot = null, at = null;
    [[4, vh - 4], [vw - 4, vh - 4], [4, 4], [vw - 4, 4], [vw / 2, vh - 4]].some(function (p) {
      var t = deepElementFromPoint(p[0], p[1]);
      if (!t || (t.closest && t.closest(SEMANTIC)) || (panel && composedContains(panel, t))) return false;
      spot = t; at = p;
      return true;
    });
    var init = { bubbles: true, cancelable: true, composed: true, view: window, button: 0, buttons: 1,
      clientX: at ? at[0] : 2, clientY: at ? at[1] : 2, pointerId: 1, pointerType: 'mouse', isPrimary: true };
    var P = typeof PointerEvent === 'function' ? PointerEvent : MouseEvent;
    var b = spot || document.body || document.documentElement;
    b.dispatchEvent(new P('pointerdown', init));
    b.dispatchEvent(new MouseEvent('mousedown', init));
    init.buttons = 0;
    b.dispatchEvent(new P('pointerup', init));
    b.dispatchEvent(new MouseEvent('mouseup', init));
    b.dispatchEvent(new MouseEvent('click', init));
    var a = deepActive();
    if (a && a !== document.body && a.blur) a.blur();
  }

  // What lies over a control a click cannot reach, for the refusal message:
  // the modal layer when there is one, else the topmost element.
  function coverOf(el) {
    var r = el.getBoundingClientRect();
    var t = deepElementFromPoint(Math.min(Math.max(r.left + r.width / 2, 0), window.innerWidth - 1),
      Math.min(Math.max(r.top + r.height / 2, 0), window.innerHeight - 1));
    if (!t) return '';
    var l = layerOf(t);
    var n = l || t;
    var what = n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') + (attr(n, 'role') ? ' role=' + attr(n, 'role') : '');
    var txt = clip(textOf(l || t) || nameOf(t), 80);
    var out = what + (txt ? " '" + txt + "'" : '');
    if (!l) {
      // An open panel: name what opened it. On a real airline's form only
      // clicking Travelers again closed its panel (not Escape, not a press
      // outside), and the run did not know that was the way.
      var opener = expandedOpener(el);
      if (opener) out += "; '" + clip(nameOf(opener), 40) + "' is expanded: clicking it again usually closes it";
    }
    return out;
  }

  function sameWidget(el, other, r) {
    var mine = new Set();
    for (var n = el; n; n = n.parentNode || n.host) mine.add(n);
    for (var m = other; m; m = m.parentNode || m.host) {
      if (!mine.has(m)) continue;
      if (m.nodeType !== 1 || m === document.body || m === document.documentElement) return false;
      var a = m.getBoundingClientRect();
      return a.width * a.height <= 6 * Math.max(1, r.width * r.height);
    }
    return false;
  }

  function act(id, op, arg, expectedGuard, mayConfirm) {
    touch(!!mayConfirm);
    pageOpened = false;
    // Requests from here on belong to this action (the settle waits for them).
    unloading = false;
    document.dispatchEvent(new CustomEvent('freyja-jev-mark'));
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
      var entry = null, all = collect();
      for (var i = 0; i < all.length; i++) if (all[i].el === el) { entry = all[i]; break; }
      // A drawn radio or checkbox is reached through the box a person clicks.
      var box = entry && entry.proxy ? entry.proxy : el;
      if (!isVisible(box)) return res(false, 'not_visible');
      if (!entry) return res(false, 'not_actionable');
      if (expectedGuard && expectedGuard !== entry.guard) return res(false, 'stale');
      if (entry.disabled) return res(false, 'disabled');

      if (op === 'fill') {
        if (entry.kind !== 'fill') return res(false, 'not_fillable');
        if (isReadOnly(el)) return res(false, 'read_only');
      }
      if (op === 'select' && entry.kind !== 'select') return res(false, 'not_selectable');

      box.scrollIntoView({ block: 'center', inline: 'center' });
      var at = hitPoint(box);
      var closed = '';
      if (!at) {
        if (box.focus) box.focus({ preventScroll: true });
        at = hitPoint(box);
      }
      if (!at && op === 'click') {
        // An open panel (travelers, a date picker) over the control: a person
        // presses outside it first, and such panels close on an outside press.
        // Only for a panel, never a modal layer, and never by pressing a control.
        var r0 = box.getBoundingClientRect();
        var top0 = deepElementFromPoint(Math.min(Math.max(r0.left + r0.width / 2, 0), window.innerWidth - 1),
          Math.min(Math.max(r0.top + r0.height / 2, 0), window.innerHeight - 1));
        if (top0 && !layerOf(top0) && !composedContains(top0, box)) {
          closed = coverOf(box);
          outsidePress(top0);
          at = hitPoint(box);
          // Some panels close only from their own opener (a travelers field that
          // ignores presses outside it and Escape): click the expanded control
          // that opened it, as a person would, and go on only if that cleared it.
          var opener = at ? null : expandedOpener(box);
          var oat = opener && hitPoint(opener);
          if (oat) {
            pointerClick(opener, oat);
            at = hitPoint(box);
            if (at) closed = "by clicking '" + clip(nameOf(opener), 40) + "'";
          }
        }
      }
      if (!at) {
        var cover = coverOf(box);
        return res(false, 'covered' + (cover ? ' by ' + cover : ''));
      }

      if (op === 'click') {
        var opened = false;
        var link = el.closest && el.closest('a[href]');
        if (link && link.target && !/^_(self|top|parent)$/i.test(link.target)) opened = true;
        var was = el.checked;
        pointerClick(box, at);
        // The box's own handler usually selects the input; if it did not, select
        // it the way a click on the input itself would.
        if (box !== el && el.checked === was && !(el.type === 'radio' && was)) el.click();
        if (pageOpened) opened = true;
        var extra = opened ? { newTab: true } : {};
        if (closed) extra.closedFirst = closed;
        return res(true, null, entry.kind === 'toggle' ? String(!!el.checked) : valueOf(el, entry.kind), extra);
      }

      if (op === 'fill') {
        var spec = arg && typeof arg === 'object' ? arg : { text: arg, mode: 'replace' };
        var text = String(spec.text == null ? '' : spec.text);
        var typed = fillText(el, text, spec.mode === 'append');
        return res(true, null, typed, suggests(el) ? { suggest: true } : null);
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
