// Quillmere Desk: an invented back-office console for computer-use evals.
// Every page is a thin HTML shell (<body data-page="...">). This script draws
// the shell (top bar, navigation, breadcrumbs) and the page from the episode's
// JSON API. The server validates, stores and judges every decision; the page
// only shows and sends. Each trap is a quirk switch from the session (see
// ../README.md): with a quirk off the page draws the plain twin of the widget.
(function () {
  'use strict';
  var API = '/desk/api/';
  var S = null;  // session: staff, today, counts, quirks
  var Q = {};    // quirk switches
  var PAGE = document.body.getAttribute('data-page') || 'home';
  var APP = null, MAIN = null;

  // ─── helpers ─────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) {
      var v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'text') e.textContent = v;
      else if (k.slice(0, 2) === 'on' && typeof v === 'function') e.addEventListener(k.slice(2), v);
      else if (v === true) e.setAttribute(k, '');
      else e.setAttribute(k, v);
    }
    (kids || []).forEach(function (c) {
      if (c == null || c === false) return;
      e.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
    });
    return e;
  }
  function api(name, body) {
    var opt = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    };
    return fetch(API + name, opt).then(function (r) {
      return r.text().then(function (t) {
        var d = {};
        try { d = t ? JSON.parse(t) : {}; } catch (e) { d = {}; }
        return { status: r.status, data: d };
      });
    }, function () { return { status: 0, data: { error: 'The server did not answer. Try again.' } }; });
  }
  function track(what, id, detail) { api('track', { what: what, id: id || '', detail: detail || '' }); }
  function qp(k) { return new URLSearchParams(location.search).get(k) || ''; }
  function enc(s) { return encodeURIComponent(s == null ? '' : s); }
  function money(n) {
    if (n == null || n === '') return '';
    return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function ready() { document.body.setAttribute('data-ready', '1'); }
  function loaded(what) { document.body.setAttribute('data-loaded', (document.body.getAttribute('data-loaded') || '') + ' ' + what); }
  function setTitle(t) { document.title = t + ' · Quillmere Desk'; }

  // ─── dates ───────────────────────────────────────────────────────────
  // A: 13 Oct 2026; B: 10/13/2026 (under a MM/DD/YYYY header); C: 2026-10-13;
  // relative: 3 weeks ago, with the date in its title. Without mixed_dates: C.
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function parts(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(iso || '');
    return m ? { y: +m[1], mo: +m[2], d: +m[3], hh: m[4], mm: m[5] } : null;
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dA(iso, time) {
    var p = parts(iso); if (!p) return '';
    var s = p.d + ' ' + MON[p.mo - 1] + ' ' + p.y;
    return time && p.hh ? s + ', ' + p.hh + ':' + p.mm : s;
  }
  function dB(iso, time) {
    var p = parts(iso); if (!p) return '';
    var s = pad(p.mo) + '/' + pad(p.d) + '/' + p.y;
    return time && p.hh ? s + ' ' + p.hh + ':' + p.mm : s;
  }
  function dC(iso, time) {
    var p = parts(iso); if (!p) return '';
    var s = p.y + '-' + pad(p.mo) + '-' + pad(p.d);
    return time && p.hh ? s + ' ' + p.hh + ':' + p.mm : s;
  }
  function fmt(iso, kind, time) {
    if (!iso) return '—';
    if (!Q.mixed_dates) return dC(iso, time);
    return kind === 'B' ? dB(iso, time) : kind === 'C' ? dC(iso, time) : dA(iso, time);
  }
  function monthYear(iso) { var p = parts(iso); return Q.mixed_dates ? MON[p.mo - 1] + ' ' + p.y : p.y + '-' + pad(p.mo); }
  function daysAgo(iso) {
    var p = parts(iso), t = parts(S.today);
    return Math.round((Date.UTC(t.y, t.mo - 1, t.d) - Date.UTC(p.y, p.mo - 1, p.d)) / 86400000);
  }
  function relWords(iso) {
    var n = daysAgo(iso);
    if (n <= 0) return 'today';
    if (n === 1) return 'yesterday';
    if (n < 7) return n + ' days ago';
    if (n < 14) return '1 week ago';
    if (n < 28) return Math.floor(n / 7) + ' weeks ago';
    if (n < 45) return 'a month ago';
    return Math.round(n / 30) + ' months ago';
  }
  function when(iso, time) {
    if (!iso) return document.createTextNode('—');
    if (!Q.mixed_dates) return document.createTextNode(dC(iso, time));
    var p = parts(iso);
    return el('span', { class: 'rel', title: dA(iso, true), text: relWords(iso) + (time && p.hh ? ', ' + p.hh + ':' + p.mm : '') });
  }
  function colB(label) { return Q.mixed_dates ? label + ' (MM/DD/YYYY)' : label; }

  // ─── icons and logo (drawn for this app) ─────────────────────────────
  var ST = ' fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
  var ICONS = {
    search: '<circle' + ST + ' cx="11" cy="11" r="6"/><path' + ST + ' d="M20 20l-4.6-4.6"/>',
    bell: '<path' + ST + ' d="M6 16.5h12l-1.5-2V10a4.5 4.5 0 0 0-9 0v4.5z"/><path' + ST + ' d="M10.3 19.2a1.9 1.9 0 0 0 3.4 0"/>',
    chev: '<path' + ST + ' d="M6.5 9.5l5.5 5.5 5.5-5.5"/>',
    check: '<path' + ST + ' d="M5 12.5l4.5 4.5L19 7.5"/>',
    x: '<path' + ST + ' d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    alert: '<circle' + ST + ' cx="12" cy="12" r="8.5"/><path' + ST + ' d="M12 7.5v5.5M12 16.3h.01"/>',
    info: '<circle' + ST + ' cx="12" cy="12" r="8.5"/><path' + ST + ' d="M12 11v5.5M12 7.8h.01"/>',
    refund: '<path' + ST + ' d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path' + ST + ' d="M4.5 4.5v4h4"/><path' + ST + ' d="M12 8.3v7.4M14.3 9.8c-.4-.7-1.2-1-2.3-1-1.3 0-2.2.6-2.2 1.5 0 2 4.6 1 4.6 3.1 0 .9-1 1.6-2.4 1.6-1.1 0-2-.4-2.4-1.1"/>',
    receipt: '<path' + ST + ' d="M6.5 3.5h11v17l-2-1.4-1.8 1.4-1.7-1.4-1.7 1.4-1.8-1.4-2 1.4z"/><path' + ST + ' d="M9 8h6M9 11.5h6M9 15h3.5"/>',
    key: '<circle' + ST + ' cx="8" cy="15" r="3.5"/><path' + ST + ' d="M10.5 12.5l8-8M15.5 7.5l2 2M17.5 5.5l2 2"/>',
    inbox: '<path' + ST + ' d="M4 13.5l2.2-7.5h11.6l2.2 7.5v5H4z"/><path' + ST + ' d="M4 13.5h4.5l1 2h5l1-2H20"/>',
    file: '<path' + ST + ' d="M7 3.5h7l4 4v13H7z"/><path' + ST + ' d="M14 3.5v4h4M9.5 12h6M9.5 15.5h6"/>',
    box: '<path' + ST + ' d="M4 8l8-4 8 4v8.5l-8 4-8-4z"/><path' + ST + ' d="M4 8l8 4 8-4M12 12v8.5"/>',
    people: '<circle' + ST + ' cx="9" cy="9" r="3"/><path' + ST + ' d="M3.5 19a5.5 5.5 0 0 1 11 0"/><circle' + ST + ' cx="16.5" cy="8.5" r="2.4"/><path' + ST + ' d="M16 13.6a4.6 4.6 0 0 1 4.5 4.6"/>',
    book: '<path' + ST + ' d="M5 4.5h5.5a2 2 0 0 1 2 2v13a1.7 1.7 0 0 0-1.7-1.7H5zM19 4.5h-5.5a2 2 0 0 0-1 .3"/><path' + ST + ' d="M19 4.5v13.3h-5.8a1.7 1.7 0 0 0-1.7 1.7"/>',
    clip: '<path' + ST + ' d="M15.5 7.5l-6 6a2 2 0 0 0 2.8 2.8l6.4-6.4a3.8 3.8 0 0 0-5.4-5.4L6.8 11a5.5 5.5 0 0 0 7.8 7.8l4.4-4.4"/>',
    image: '<rect' + ST + ' x="4" y="5" width="16" height="14" rx="2"/><circle' + ST + ' cx="9" cy="10" r="1.6"/><path' + ST + ' d="M20 16l-4.5-4.5L7 19"/>',
  };
  function icon(name, size) {
    var s = size || 18, w = document.createElement('span');
    w.innerHTML = '<svg width="' + s + '" height="' + s + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + (ICONS[name] || '') + '</svg>';
    return w.firstChild;
  }
  function logo() {
    var w = document.createElement('span');
    w.innerHTML = '<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true" focusable="false">' +
      '<rect width="28" height="28" rx="7" fill="#2c6152"/>' +
      '<path d="M20 5.2c-5.3 1-9.5 5.4-11 11.6L8.2 21l2.4-2.1c4.8-1.2 8.5-5.4 9.4-13.7z" fill="#f3ead9"/>' +
      '<path d="M8.4 20.8l6.3-8.3" stroke="#2c6152" stroke-width="1.2" stroke-linecap="round"/>' +
      '<path d="M7 23h10" stroke="#d9a65a" stroke-width="1.6" stroke-linecap="round"/></svg>';
    return w.firstChild;
  }

  // ─── shell ───────────────────────────────────────────────────────────
  var NAV = [
    ['Work'],
    ['refunds', 'refunds.html', 'Refunds', 'refund', 'refunds'],
    ['expenses', 'expenses.html', 'Expenses', 'receipt', 'expenses'],
    ['access', 'access.html', 'Access requests', 'key', 'access'],
    ['inbox', 'inbox.html', 'Inbox', 'inbox', 'inbox'],
    ['contracts', 'contracts.html', 'Contracts', 'file', 'contracts'],
    ['Reference'],
    ['orders', 'orders.html', 'Orders', 'box'],
    ['directory', 'directory.html', 'Directory', 'people'],
    ['policies', 'policies.html', 'Policy center', 'book'],
  ];
  var SECTION = { refund: 'refunds', order: 'orders', expense: 'expenses', 'access-request': 'access', ticket: 'inbox',
    'case': 'inbox', contract: 'contracts', policy: 'policies' };

  function buildShell() {
    var b = document.body;
    var search = el('form', { class: 'gsearch', role: 'search', action: 'help.html', method: 'get' }, [
      icon('search', 16),
      el('input', { type: 'search', name: 'q', id: 'gsearch', 'aria-label': 'Search help articles',
        placeholder: 'Search help articles', autocomplete: 'off', value: PAGE === 'help' ? qp('q') : null }),
    ]);
    var top = el('header', { class: 'topbar' }, [
      el('a', { class: 'brand', href: 'index.html', 'aria-label': 'Quillmere Desk home' },
        [logo(), el('span', { class: 'brand-name' }, [el('b', { text: 'Quillmere' }), el('span', { text: 'Desk' })])]),
      search,
      el('div', { class: 'tb-right' }, [
        el('span', { class: 'today', text: 'Today: ' + S.today_label }),
        el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Notifications',
          onclick: function () { toast('No new notifications.'); } }, [icon('bell', 18)]),
        el('div', { class: 'me' }, [
          el('span', { class: 'avatar', 'aria-hidden': 'true', text: S.staff.initials }),
          el('span', { class: 'me-text' }, [el('span', { class: 'me-name', text: S.staff.name }),
            el('span', { class: 'me-role', text: S.staff.role })]),
        ]),
      ]),
    ]);
    var on = SECTION[PAGE] || PAGE;
    var nav = el('nav', { class: 'sidenav', 'aria-label': 'Main' }, NAV.map(function (n) {
      if (n.length === 1) return el('div', { class: 'nav-head', text: n[0] });
      var count = n[4] && S.counts[n[4]] ? el('span', { class: 'nav-count', text: String(S.counts[n[4]]) }) : null;
      return el('a', { class: 'nav-item' + (n[0] === on ? ' on' : ''), href: n[1], 'aria-current': n[0] === on ? 'page' : null },
        [icon(n[3], 18), el('span', { text: n[2] }), count]);
    }).concat([el('div', { class: 'nav-foot', text: 'Quillmere Desk 4.12 · Customer care' })]));
    MAIN = el('main', { class: 'main', id: 'main' });
    APP = el('div', { id: 'app' }, [top, el('div', { class: 'layout' }, [nav, MAIN])]);
    b.insertBefore(APP, b.firstChild);
    b.appendChild(el('div', { id: 'overlays' }));
    b.appendChild(el('div', { id: 'toasts', class: 'toasts', 'aria-live': 'polite' }));
    document.addEventListener('keydown', function (e) {
      var t = (document.activeElement && document.activeElement.tagName) || '';
      if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(t) && !document.querySelector('.modal-root')) {
        e.preventDefault();
        $('gsearch').focus();
      }
    });
  }

  function crumbs(list) {
    var nav = el('nav', { class: 'crumbs', 'aria-label': 'Breadcrumb' });
    list.forEach(function (c, i) {
      if (i) nav.appendChild(el('span', { class: 'sep', 'aria-hidden': 'true', text: '/' }));
      nav.appendChild(c[1] ? el('a', { href: c[1], text: c[0] }) : el('span', { 'aria-current': 'page', text: c[0] }));
    });
    return nav;
  }
  function pageHead(title, o) {
    o = o || {};
    var h1 = el('h1', { text: title });
    var meta = el('div', { class: 'head-meta' }, o.meta || []);
    var head = el('div', { class: 'page-head' }, [
      el('div', {}, [h1, o.sub ? el('div', { class: 'sub', text: o.sub }) : null, meta]),
      o.actions ? el('div', { class: 'actions' }, o.actions) : null,
    ]);
    head.meta = meta;
    return head;
  }
  var PILL = { 'Open': 'open', 'New': 'new', 'Pending': 'pending', 'Waiting for approval': 'waiting', 'In review': 'review',
    'Decided': 'decided', 'Routed': 'routed', 'Closed': 'closed', 'Signed': 'signed', 'Reviewed': 'reviewed',
    'Approved': 'approved', 'Delivered': 'delivered', 'Returned': 'returned', 'In transit': 'transit',
    'Processing': 'processing', 'With finance': 'finance', 'Rejected': 'rejected', 'Lost in transit': 'lost', 'Expired': 'expired' };
  function pill(text) { return el('span', { class: 'pill ' + (PILL[text] || 'decided'), text: text }); }
  function card(title, body, o) {
    o = o || {};
    var b = el('div', { class: 'card-body' }, [].concat(body || []));
    var c = el('section', { class: 'card' + (o.side ? ' side' : ''), id: o.id || null, 'aria-label': title }, [
      title ? el('div', { class: 'card-head' }, [el('h2', { text: title })].concat(o.right ? [el('div', { class: 'right' }, o.right)] : [])) : null,
      b,
    ]);
    c.body = b;
    return c;
  }
  function kv(rows) {
    var dl = el('dl', { class: 'kv' });
    rows.forEach(function (r) {
      if (!r) return;
      dl.appendChild(el('dt', { text: r[0] }));
      dl.appendChild(el('dd', {}, [].concat(r[1] == null || r[1] === '' ? ['—'] : r[1])));
    });
    return dl;
  }
  function banner(kind, text, extra) {
    return el('div', { class: 'banner ' + kind, role: kind === 'error' ? 'alert' : 'status' },
      [icon(kind === 'error' ? 'alert' : 'info', 18), el('div', {}, [text].concat(extra || []))]);
  }
  function setPage(nodes) {
    MAIN.textContent = '';
    nodes.forEach(function (n) { if (n) MAIN.appendChild(n); });
  }
  function skeleton(caption) {
    return el('div', { class: 'skeleton', 'aria-busy': 'true' }, [el('div', { class: 'sk-line mid' }),
      el('div', { class: 'sk-line' }), el('div', { class: 'sk-line short' }), el('p', { class: 'sk-caption', text: caption })]);
  }
  function toast(text) {
    var t = el('div', { class: 'toast', role: 'status' }, [icon('check', 16), el('span', { text: text })]);
    $('toasts').appendChild(t);
    setTimeout(function () { t.classList.add('out'); setTimeout(function () { t.remove(); }, 300); }, 5000);
  }
  function notFound(what, back, label) {
    setTitle('Not found');
    setPage([crumbs([[label, back], ['Not found']]), pageHead(what + ' was not found'),
      card(null, [el('p', {}, ['Check the address, or go back to ', el('a', { href: back, text: label }), '.'])])]);
    ready();
  }
  function policyLink(id, title, code) {
    return card('Policy', [el('p', { class: 'small muted', text: 'Decide this record under:' }),
      el('a', { href: 'policy.html?id=' + id, text: title + ' (' + code + ')' })], { side: true });
  }

  // ─── choice fields: a custom combobox (combo_selects) or a native select ─
  var OPEN_LISTS = [];
  function closeAllLists() { OPEN_LISTS.slice().forEach(function (c) { c.close(false); }); }
  document.addEventListener('mousedown', function (e) {
    if (!OPEN_LISTS.length) return;
    var t = e.target;
    if (t.closest && (t.closest('.listbox') || t.closest('.combo'))) return;
    closeAllLists();
  }, true);
  window.addEventListener('scroll', function () { OPEN_LISTS.forEach(function (c) { c.place(); }); }, true);
  window.addEventListener('resize', function () { OPEN_LISTS.forEach(function (c) { c.place(); }); });

  function choice(id, label, options, placeholder, o) {
    o = o || {};
    var value = '';
    var err = el('span', { class: 'err hidden', id: id + '-err' });
    var field = el('div', { class: 'field' + (o.wide ? ' wide' : '') });
    var self = {
      node: field, get: function () { return value; },
      error: function (m) { err.textContent = m || ''; err.classList.toggle('hidden', !m); field.classList.toggle('invalid', !!m); },
    };
    if (Q.combo_selects) {
      var shown = el('span', { class: 'combo-value placeholder', text: placeholder });
      var btn = el('button', { type: 'button', id: id, class: 'combo', role: 'combobox', 'aria-haspopup': 'listbox',
        'aria-expanded': 'false', 'aria-labelledby': id + '-label', 'aria-controls': id + '-list' }, [shown, icon('chev', 16)]);
      var list = null, active = -1, typed = '', typedAt = 0;
      var ctl = {
        close: function (refocus) {
          if (!list) return;
          list.remove(); list = null; active = -1;
          btn.setAttribute('aria-expanded', 'false');
          var i = OPEN_LISTS.indexOf(ctl);
          if (i >= 0) OPEN_LISTS.splice(i, 1);
          if (refocus) btn.focus({ preventScroll: true });
        },
        place: function () {
          if (!list) return;
          var r = btn.getBoundingClientRect(), h = list.offsetHeight;
          list.style.minWidth = r.width + 'px';
          list.style.left = r.left + 'px';
          list.style.top = (window.innerHeight - r.bottom < h + 8 && r.top > h + 8 ? r.top - h - 4 : r.bottom + 4) + 'px';
        },
      };
      var opts = function () { return list ? Array.prototype.slice.call(list.querySelectorAll('[role=option]')) : []; };
      var setActive = function (i) {
        var os = opts(); if (!os.length) return;
        active = Math.max(0, Math.min(os.length - 1, i));
        os.forEach(function (x, k) { x.classList.toggle('active', k === active); });
        list.setAttribute('aria-activedescendant', os[active].id);
        os[active].scrollIntoView({ block: 'nearest' });
      };
      var pick = function (v) {
        value = v; shown.textContent = v; shown.classList.remove('placeholder');
        self.error('');
        ctl.close(true);
        if (o.onchange) o.onchange(v);
      };
      var key = function (e) {
        var k = e.key;
        if (k === 'ArrowDown') { e.preventDefault(); setActive(active + 1); }
        else if (k === 'ArrowUp') { e.preventDefault(); setActive(active - 1); }
        else if (k === 'Home') { e.preventDefault(); setActive(0); }
        else if (k === 'End') { e.preventDefault(); setActive(options.length - 1); }
        else if (k === 'Enter' || k === ' ') { e.preventDefault(); if (active >= 0) pick(options[active]); }
        else if (k === 'Escape') { e.preventDefault(); e.stopPropagation(); ctl.close(true); }
        else if (k === 'Tab') { ctl.close(false); }
        else if (k.length === 1 && /\S/.test(k)) {  // type-ahead: jump to the first option starting so
          var t = Date.now();
          typed = (t - typedAt > 700 ? '' : typed) + k.toLowerCase();
          typedAt = t;
          var i = options.findIndex(function (x) { return x.toLowerCase().indexOf(typed) === 0; });
          if (i >= 0) setActive(i);
        }
      };
      var open = function () {
        if (list) return;
        closeAllLists();
        list = el('ul', { class: 'listbox', role: 'listbox', id: id + '-list', 'aria-labelledby': id + '-label', tabindex: '-1' },
          options.map(function (x, i) {
            return el('li', { role: 'option', id: id + '-opt-' + i, 'aria-selected': String(x === value), 'data-value': x, text: x,
              onclick: function (e) { e.stopPropagation(); pick(x); } });
          }));
        list.addEventListener('keydown', key);
        $('overlays').appendChild(list);
        OPEN_LISTS.push(ctl);
        btn.setAttribute('aria-expanded', 'true');
        ctl.place();
        setActive(Math.max(0, options.indexOf(value)));
        list.focus({ preventScroll: true });
      };
      btn.addEventListener('click', function () { if (list) ctl.close(true); else open(); });
      btn.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        else if (e.key === 'Escape') { ctl.close(true); }
        else if (e.key.length === 1 && /\S/.test(e.key)) { open(); key(e); }
      });
      field.appendChild(el('span', { class: 'label', id: id + '-label', text: label }));
      field.appendChild(btn);
    } else {
      var sel = el('select', { id: id, name: id },
        [el('option', { value: '', text: placeholder })].concat(options.map(function (x) { return el('option', { value: x, text: x }); })));
      sel.addEventListener('change', function () { value = sel.value; self.error(''); if (o.onchange) o.onchange(value); });
      field.appendChild(el('label', { class: 'label', for: id, text: label }));
      field.appendChild(sel);
    }
    if (o.hint) field.appendChild(el('span', { class: 'hint', text: o.hint }));
    field.appendChild(err);
    return self;
  }
  function inputField(id, label, o) {
    o = o || {};
    var input = o.area ? el('textarea', { id: id, name: id, rows: '3' })
      : el('input', { type: 'text', id: id, name: id, autocomplete: 'off', inputmode: o.money || o.numeric ? (o.money ? 'decimal' : 'numeric') : null,
        placeholder: o.placeholder || (o.money ? '0.00' : null) });
    var err = el('span', { class: 'err hidden', id: id + '-err' });
    var control = o.money ? el('div', { class: 'money' }, [el('span', { 'aria-hidden': 'true', text: '$' }), input]) : input;
    var field = el('div', { class: 'field' + (o.wide ? ' wide' : '') }, [el('label', { class: 'label', for: id, text: label }), control,
      o.hint ? el('span', { class: 'hint', text: o.hint }) : null, err]);
    return {
      node: field, input: input, get: function () { return input.value; },
      error: function (m) { err.textContent = m || ''; err.classList.toggle('hidden', !m); field.classList.toggle('invalid', !!m); },
    };
  }
  // A decision form: fields, an error summary, Submit decision. post(values)
  // returns the API promise; done(record) swaps in the read-only summary.
  function decisionForm(fields, post, done, label) {
    var box = el('div', { class: 'form-error-box' });
    var submit = el('button', { type: 'submit', class: 'btn primary', id: 'submitDecision', text: label || 'Submit decision' });
    var form = el('form', { id: 'decisionForm', novalidate: true }, [box,
      el('div', { class: 'form-grid' }, fields.map(function (f) { return f.node; })),
      el('div', { class: 'form-actions' }, [submit, el('span', { class: 'note', text: 'A submitted decision is final.' })])]);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (submit.disabled) return;
      submit.disabled = true;
      box.textContent = '';
      fields.forEach(function (f) { if (f.error) f.error(''); });
      post().then(function (res) {
        submit.disabled = false;
        if (res.status === 200 && res.data.ok) { done(res.data.record); return; }
        box.appendChild(banner('error', res.data.error || 'The decision could not be recorded.'));
        var errs = res.data.fields || {};
        fields.forEach(function (f) { if (f.key && errs[f.key]) f.error(errs[f.key]); });
        if (form.onrefused) form.onrefused(res);
      });
    });
    return form;
  }
  function recorded(rows, by, at) {
    return el('div', { class: 'decided' }, [banner('info', 'Decision recorded. Decisions are final.'), kv(rows),
      el('p', { class: 'muted small', text: 'Recorded by ' + by + ', ' + fmt(at, 'A', true) + '.' })]);
  }

  // ─── tables: sortable, with a row count, paged with paged_lists ──────
  function segmented(items, current, onpick) {
    var seg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Filter' });
    items.forEach(function (it) {
      seg.appendChild(el('button', { type: 'button', 'aria-pressed': String(it[0] === current), 'data-filter': it[0], text: it[1],
        onclick: function () {
          Array.prototype.forEach.call(seg.children, function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-filter') === it[0])); });
          onpick(it[0]);
        } }));
    });
    return seg;
  }
  function dataTable(o) {
    var st = { key: o.sort.key, dir: o.sort.dir, page: 0, rows: o.rows };
    var size = o.paged ? 5 : 0;
    var count = el('span', { class: 'count' });
    var tools = el('div', { class: 'table-tools' }, (o.tools || []).concat([count]));
    var thead = el('thead'), tbody = el('tbody');
    var pager = el('div', { class: 'pager' });
    var wrap = el('section', { class: 'card table-card', 'aria-label': o.label || 'Table' },
      [tools, el('table', { class: 'data', id: o.id || null }, [thead, tbody]), pager]);
    function val(c, r) { var v = c.sort ? c.sort(r) : r[c.key]; return v == null ? '' : v; }
    function render() {
      thead.textContent = ''; tbody.textContent = ''; pager.textContent = '';
      var tr = el('tr');
      o.columns.forEach(function (c) {
        var on = st.key === c.key;
        var th = el('th', { scope: 'col', class: c.num ? 'num' : null,
          'aria-sort': c.nosort ? null : on ? (st.dir === 'asc' ? 'ascending' : 'descending') : 'none' });
        if (c.nosort) th.appendChild(el('span', { class: 'th', text: c.label }));
        else th.appendChild(el('button', { type: 'button', class: 'th', onclick: function () {
          if (st.key === c.key) st.dir = st.dir === 'asc' ? 'desc' : 'asc';
          else { st.key = c.key; st.dir = c.firstDir || 'asc'; }
          st.page = 0; render();
        } }, [c.label, on ? el('span', { class: 'ind', 'aria-hidden': 'true', text: st.dir === 'asc' ? '▲' : '▼' }) : null]));
        tr.appendChild(th);
      });
      thead.appendChild(tr);
      var col = o.columns.filter(function (c) { return c.key === st.key; })[0];
      var rows = st.rows.slice();
      if (col) rows.sort(function (a, b) {
        var x = val(col, a), y = val(col, b);
        var c = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
        return st.dir === 'asc' ? c : -c;
      });
      var total = rows.length, pages = size ? Math.max(1, Math.ceil(total / size)) : 1;
      if (st.page >= pages) st.page = pages - 1;
      var shown = size ? rows.slice(st.page * size, st.page * size + size) : rows;
      if (!shown.length) tbody.appendChild(el('tr', {}, [el('td', { colspan: String(o.columns.length), class: 'empty', text: o.empty || 'Nothing to show.' })]));
      shown.forEach(function (r) {
        var row = el('tr', { 'data-id': r.id });
        o.columns.forEach(function (c) {
          var v = c.render ? c.render(r) : r[c.key];
          row.appendChild(el('td', { class: c.num ? 'num' : null }, [v == null ? '' : typeof v === 'object' ? v : String(v)]));
        });
        tbody.appendChild(row);
      });
      count.textContent = total + ' ' + (total === 1 ? o.noun[0] : o.noun[1]);
      if (size) {
        var from = total ? st.page * size + 1 : 0, to = Math.min(total, st.page * size + size);
        pager.appendChild(el('span', { text: 'Showing ' + from + '–' + to + ' of ' + total }));
        pager.appendChild(el('div', { class: 'right' }, [
          el('span', { text: 'Page ' + (st.page + 1) + ' of ' + pages }),
          el('button', { type: 'button', class: 'btn small', text: 'Previous page', disabled: st.page === 0,
            onclick: function () { st.page -= 1; render(); } }),
          el('button', { type: 'button', class: 'btn small', text: 'Next page', disabled: st.page >= pages - 1,
            onclick: function () { st.page += 1; render(); } }),
        ]));
      } else {
        pager.appendChild(el('span', { text: total ? 'Showing all ' + total : '' }));
      }
    }
    render();
    return { node: wrap, setRows: function (rows) { st.rows = rows; st.page = 0; render(); } };
  }
  function nameCell(name, email) { return el('span', {}, [name, el('span', { class: 'sub', text: email })]); }
  function link(href, text) { return el('a', { href: href, text: text }); }

  // ─── tabs: only the open tab's panel is in the page ──────────────────
  function tabs(id, list) {
    var bar = el('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Sections' });
    var panel = el('div', { class: 'tabpanel', role: 'tabpanel', id: id + '-panel' });
    var cur = 0;
    var btns = list.map(function (t, i) {
      var b = el('button', { type: 'button', role: 'tab', id: id + '-tab-' + i, 'aria-selected': 'false', 'aria-controls': id + '-panel',
        tabindex: '-1', text: t.label, onclick: function () { show(i, true); } });
      b.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
          e.preventDefault();
          var n = (cur + (e.key === 'ArrowRight' ? 1 : list.length - 1)) % list.length;
          show(n, true); btns[n].focus();
        }
      });
      bar.appendChild(b);
      return b;
    });
    function show(i, user) {
      cur = i;
      btns.forEach(function (b, k) { b.setAttribute('aria-selected', String(k === i)); b.tabIndex = k === i ? 0 : -1; });
      panel.setAttribute('aria-labelledby', btns[i].id);
      panel.textContent = '';
      panel.appendChild(list[i].render());
      if (user) track('tab', id, list[i].label);
    }
    show(0, false);
    return el('div', { class: 'tabset' }, [bar, panel]);
  }
  function timeline(items, kind) {
    if (!items.length) return el('p', { class: 'empty', text: 'Nothing yet.' });
    return el('ul', { class: 'timeline' }, items.map(function (it) {
      return el('li', {}, [el('span', { class: 'when', text: fmt(it[0], kind || 'C', true) }), el('span', { text: it[1] })]);
    }));
  }

  // ─── dialog (inert page behind it) ───────────────────────────────────
  function openDialog(title, content) {
    closeAllLists();
    var before = document.activeElement;
    var x = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Close dialog' }, [icon('x', 18)]);
    var body = el('div', { class: 'modal-body' }, [content]);
    var root = el('div', { class: 'modal-root', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'dlgTitle' }, [
      el('div', { class: 'modal-backdrop' }),
      el('div', { class: 'modal' }, [el('div', { class: 'modal-head' }, [el('h2', { id: 'dlgTitle', text: title }), x]), body,
        el('div', { class: 'modal-foot' }, [el('button', { type: 'button', class: 'btn', id: 'dlgClose', text: 'Close' })])]),
    ]);
    function key(e) { if (e.key === 'Escape') { e.preventDefault(); close(); } }
    function close() {
      root.remove();
      APP.removeAttribute('inert'); APP.removeAttribute('aria-hidden');
      document.removeEventListener('keydown', key, true);
      if (before && before.focus) before.focus({ preventScroll: true });
    }
    x.addEventListener('click', close);
    root.querySelector('.modal-backdrop').addEventListener('click', close);
    root.querySelector('#dlgClose').addEventListener('click', close);
    APP.setAttribute('inert', ''); APP.setAttribute('aria-hidden', 'true');
    document.body.appendChild(root);
    document.addEventListener('keydown', key, true);
    x.focus({ preventScroll: true });
    return { body: body, close: close };
  }

  // ─── home ────────────────────────────────────────────────────────────
  function pageHome() {
    setTitle('Home');
    var c = S.counts;
    function tile(title, n, what, href) {
      return el('section', { class: 'card tile', 'aria-label': title }, [el('div', { class: 'muted small', text: title }),
        el('div', { class: 'big', text: String(n) }), el('div', { class: 'muted', text: what }),
        el('p', {}, [link(href, 'Open ' + title)])]);
    }
    api('policies').then(function (r) {
      var pol = card('Policy updates', [el('ul', { class: 'results' }, (r.data.rows || []).slice(0, 5).map(function (p) {
        return el('li', {}, [link('policy.html?id=' + p.id, p.title), el('span', { class: 'muted small', text: '  ·  Last updated ' + fmt(p.updated, 'A') })]);
      }))]);
      setPage([
        crumbs([['Home']]),
        pageHead('Good morning, ' + S.staff.name.split(' ')[0], { sub: 'Your queues in Quillmere Desk.' }),
        el('div', { class: 'tiles' }, [
          tile('Refunds', c.refunds, 'open requests', 'refunds.html'),
          tile('Expenses', c.expenses, 'claims waiting for approval', 'expenses.html'),
          tile('Access requests', c.access, 'pending requests', 'access.html'),
          tile('Inbox', c.inbox, 'new tickets and open cases', 'inbox.html'),
          tile('Contracts', c.contracts, 'review in progress', 'contracts.html'),
        ]),
        el('div', { class: 'grid2', style: 'margin-top:18px' }, [pol, card('Notices', [el('p', { text:
          'Planned maintenance: Saturday 18 October, 22:00 to 23:00. Quillmere Desk will be read only.' })], { side: true })]),
      ]);
      ready();
    });
  }

  // ─── refunds ─────────────────────────────────────────────────────────
  function pageRefunds() {
    setTitle('Refunds');
    api('refunds').then(function (r) {
      var rows = r.data.rows || [];
      var n = function (s) { return rows.filter(function (x) { return x.status === s; }).length; };
      var filter = 'all';
      var pickRows = function () { return rows.filter(function (x) { return filter === 'all' || x.status.toLowerCase() === filter; }); };
      var t = dataTable({
        id: 'queue', label: 'Refund requests', noun: ['request', 'requests'], paged: Q.paged_lists,
        sort: { key: 'received', dir: 'desc' }, rows: rows,
        tools: [segmented([['all', 'All (' + rows.length + ')'], ['open', 'Open (' + n('Open') + ')'], ['decided', 'Decided (' + n('Decided') + ')']],
          'all', function (v) { filter = v; t.setRows(pickRows()); })],
        columns: [
          { key: 'id', label: 'Request', render: function (x) { return link('refund.html?id=' + enc(x.id), x.id); } },
          { key: 'customer', label: 'Customer', sort: function (x) { return x.customer.name; },
            render: function (x) { return nameCell(x.customer.name, x.customer.email); } },
          { key: 'type', label: 'Type' },
          { key: 'order', label: 'Order', render: function (x) { return link('order.html?id=' + enc(x.order), x.order); } },
          { key: 'received', label: colB('Received'), firstDir: 'desc', render: function (x) { return fmt(x.received, 'B', true); } },
          { key: 'status', label: 'Status', render: function (x) { return pill(x.status); } },
        ],
      });
      setPage([crumbs([['Home', 'index.html'], ['Refunds']]),
        pageHead('Refunds', { sub: 'Refund requests from customers. Decide every open request under the Refund policy.',
          actions: [el('a', { class: 'btn', href: 'policy.html?id=refunds', text: 'Refund policy' })] }), t.node]);
      ready();
    });
  }

  function pageRefund() {
    var id = qp('id');
    api('refund?id=' + enc(id)).then(function (res) {
      if (res.status !== 200) return notFound('Request ' + id, 'refunds.html', 'Refunds');
      var r = res.data;
      setTitle(r.id);
      var head = pageHead(r.id, { meta: [pill(r.status), el('span', { text: r.type }), el('span', { text: '·' }),
        el('span', {}, ['Received ', when(r.received, true)])] });
      var decision = card('Decision', [], { id: 'decisionCard' });
      function showRecord(d) {
        decision.body.textContent = '';
        decision.body.appendChild(recorded([['Decision', d.decision], ['Amount', d.amount == null ? '—' : money(d.amount)],
          ['Reason', d.reason], ['Goodwill credit', d.goodwill ? money(d.goodwill) : '—'], ['Note', d.note || '—']], d.by, d.at));
      }
      if (r.decision) showRecord(r.decision);
      else {
        var dec = choice('decision', 'Decision', r.form.decisions, 'Choose a decision'); dec.key = 'decision';
        var amt = inputField('amount', 'Amount', { money: true, hint: 'In US dollars, for example 24.50.' }); amt.key = 'amount';
        var rea = choice('reason', 'Reason', r.form.reasons, 'Choose a reason'); rea.key = 'reason';
        var gw = inputField('goodwill', 'Goodwill credit (optional)', { money: true }); gw.key = 'goodwill';
        var note = inputField('note', 'Note (optional)', { area: true, wide: true, hint: 'Visible to Customer care only.' });
        decision.body.appendChild(decisionForm([dec, amt, rea, gw, note], function () {
          return api('refund/decide', { id: r.id, decision: dec.get(), amount: amt.get(), reason: rea.get(), goodwill: gw.get(), note: note.get() });
        }, function (rec) {
          showRecord(rec.decision);
          head.meta.firstChild.replaceWith(pill('Decided'));
          toast('Decision recorded for ' + r.id + '.');
        }));
      }
      var files = r.photos.length ? el('ul', { class: 'files' }, r.photos.map(function (p) {
        return el('li', { class: 'file' }, [el('span', { class: 'thumb' }, [icon('image', 20)]), el('span', {}, [el('b', { text: p.name }),
          el('span', { class: 'muted small', text: 'Photo · ' + p.size })])]);
      })) : el('p', { class: 'empty', text: 'No attachments.' });
      var main = el('div', { class: 'stack' }, [
        r.banner ? banner('warn', r.banner) : null,
        card('Request', [kv([
          ['Request type', r.type + (r.why ? ' · ' + r.why : '')],
          ['Items in this request', el('span', {}, r.items.map(function (x, i) { return (i ? ', ' : '') + x; }))],
          ['Order', link('order.html?id=' + enc(r.order), r.order)],
          ['Received', when(r.received, true)],
          ['Channel', r.channel],
        ])], { right: [el('a', { class: 'btn small', href: 'order.html?id=' + enc(r.order), text: 'View order' })] }),
        card('Customer message', [el('p', { class: 'message', text: r.message })]),
        card('Attachments', [files], { right: [el('span', { class: 'muted small' }, [icon('clip', 14), ' ' + r.photos.length])] }),
        decision,
      ]);
      var side = el('div', { class: 'stack' }, [
        card('Customer', [kv([['Name', r.customer.name], ['Email', r.customer.email], ['Customer since', monthYear(r.customer.since)],
          ['Orders', String(r.customer.orders)]])], { side: true }),
        card('Request details', [kv([['Request', r.id], ['Received', fmt(r.received, 'A', true)], ['Respond by', fmt(r.respond_by, 'A')],
          ['Assigned to', 'Customer care queue']])], { side: true }),
        policyLink('refunds', 'Refund policy', 'CC-04'),
      ]);
      setPage([crumbs([['Refunds', 'refunds.html'], [r.id]]), head, el('div', { class: 'grid2' }, [main, side])]);
      ready();
    });
  }

  // ─── orders ──────────────────────────────────────────────────────────
  function pageOrders() {
    setTitle('Orders');
    var input = el('input', { type: 'search', id: 'orderSearch', name: 'q', 'aria-label': 'Search orders by number, customer or email',
      placeholder: 'Order number, customer or email', value: qp('q') || null });
    var t = null;
    function load() {
      api('orders?q=' + enc(input.value)).then(function (r) {
        var rows = r.data.rows || [];
        if (!t) {
          t = dataTable({ id: 'orders', label: 'Orders', noun: ['order', 'orders'], paged: false, sort: { key: 'placed', dir: 'desc' }, rows: rows,
            tools: [el('form', { role: 'search', class: 'inline-search', onsubmit: function (e) { e.preventDefault(); load(); } },
              [input, ' ', el('button', { type: 'submit', class: 'btn small', text: 'Search' })])],
            columns: [
              { key: 'id', label: 'Order', render: function (x) { return link('order.html?id=' + enc(x.id), x.id); } },
              { key: 'customer', label: 'Customer', render: function (x) { return nameCell(x.customer, x.email); } },
              { key: 'placed', label: 'Placed', firstDir: 'desc', render: function (x) { return fmt(x.placed, 'A'); } },
              { key: 'delivered', label: colB('Delivered'), firstDir: 'desc', render: function (x) { return x.delivered ? fmt(x.delivered, 'B') : '—'; } },
              { key: 'total', label: 'Total', num: true, render: function (x) { return money(x.total); } },
              { key: 'status', label: 'Status', render: function (x) { return pill(x.status); } },
            ] });
          setPage([crumbs([['Home', 'index.html'], ['Orders']]), pageHead('Orders', { sub: 'Customer orders from the Quillmere shop.' }), t.node]);
          ready();
        } else t.setRows(rows);
      });
    }
    load();
  }

  function pageOrder() {
    var id = qp('id');
    setTitle('Order ' + id);
    var head = pageHead('Order ' + id);
    var main = el('div', { class: 'stack' }, [card('Order details', [skeleton('Loading order…')])]);
    var side = el('div', { class: 'stack' }, [card('Key dates', [skeleton('Loading…')], { side: true })]);
    setPage([crumbs([['Orders', 'orders.html'], [id]]), head, el('div', { class: 'grid2' }, [main, side])]);
    ready();
    api('order?id=' + enc(id)).then(function (res) {
      if (res.status !== 200) return notFound('Order ' + id, 'orders.html', 'Orders');
      var o = res.data;
      head.meta.appendChild(pill(o.status));
      head.meta.appendChild(el('span', { text: 'Placed ' + fmt(o.placed, 'A') }));
      var items = el('table', { class: 'data' }, [
        el('thead', {}, [el('tr', {}, ['Item', 'SKU', 'Qty', 'Price'].map(function (h, i) {
          return el('th', { class: i > 1 ? 'num' : null }, [el('span', { class: 'th', text: h })]);
        }))]),
        el('tbody', {}, o.items.map(function (x) {
          return el('tr', {}, [el('td', {}, [x.name, x.final_sale ? el('span', { class: 'badge final', text: 'Final sale' }) : null]),
            el('td', { class: 'mono small', text: x.sku }), el('td', { class: 'num', text: String(x.qty) }),
            el('td', { class: 'num', text: money(x.price * x.qty) })]);
        })),
      ]);
      function summary() {
        return el('div', {}, [items, el('div', { class: 'totals' }, [
          el('div', {}, [el('span', { text: 'Item subtotal' }), el('span', { text: money(o.subtotal) })]),
          el('div', {}, [el('span', { text: 'Shipping' }), el('span', { text: o.shipping ? money(o.shipping) : '$0.00 (free shipping)' })]),
          el('div', {}, [el('span', { text: 'Tax' }), el('span', { text: money(o.tax) })]),
          el('div', { class: 'total' }, [el('span', { text: 'Order total' }), el('span', { text: money(o.total) })]),
        ]), el('div', { style: 'margin-top:14px' }, [kv([
          ['Payment', o.payment],
          ['Promised delivery', fmt(o.promised, 'A')],
          ['Delivered', o.delivered ? when(o.delivered, true) : 'Not delivered'],
        ])])]);
      }
      function shipping() {
        return el('div', {}, [kv([['Carrier', o.carrier || 'Not shipped yet'], ['Service', o.service], ['Tracking number', o.tracking_no],
          ['Promised delivery', fmt(o.promised, 'C')]]), el('h3', { class: 'small muted', style: 'margin:16px 0 4px', text: 'Tracking events' }),
          timeline(o.events, 'C')]);
      }
      function activity() { return timeline(o.activity, 'C'); }
      main.textContent = '';
      if (o.banner) main.appendChild(banner('warn', o.banner));
      main.appendChild(el('section', { class: 'card', 'aria-label': 'Order' }, [tabs('order', [
        { label: 'Summary', render: summary }, { label: 'Shipping', render: shipping }, { label: 'Activity', render: activity }])]));
      side.textContent = '';
      side.appendChild(card('Customer', [kv([['Name', o.customer.name], ['Email', o.customer.email]])], { side: true }));
      side.appendChild(card('Key dates', [kv([['Placed', fmt(o.placed, 'A', true)], ['Promised', fmt(o.promised, 'A')],
        ['Delivered', o.delivered ? fmt(o.delivered, 'A', true) : '—']])], { side: true }));
      side.appendChild(card('Payment', [kv([['Method', o.payment], ['Order total', money(o.total)]])], { side: true }));
      side.appendChild(card('Requests', [o.requests.length ? el('ul', { class: 'results' }, o.requests.map(function (q) {
        return el('li', {}, [link('refund.html?id=' + enc(q.id), q.id), ' · ' + q.type + ' ', pill(q.status)]);
      })) : el('p', { class: 'empty', text: 'No requests for this order.' })], { side: true }));
      loaded('order');
    });
  }

  // ─── expenses ────────────────────────────────────────────────────────
  function pageExpenses() {
    setTitle('Expenses');
    api('expenses').then(function (r) {
      var rows = r.data.rows || [];
      var filter = 'waiting';
      var by = { waiting: function (x) { return x.status === 'Waiting for approval'; }, approved: function (x) { return x.status === 'Approved'; },
        returned: function (x) { return x.status === 'Returned'; }, all: function () { return true; } };
      var nW = rows.filter(by.waiting).length;
      var t = dataTable({
        id: 'claims', label: 'Expense claims', noun: ['claim', 'claims'], paged: Q.paged_lists, sort: { key: 'submitted', dir: 'desc' },
        rows: rows.filter(by.waiting),
        tools: [segmented([['waiting', 'Waiting for approval (' + nW + ')'], ['approved', 'Approved'], ['returned', 'Returned'], ['all', 'All']],
          'waiting', function (v) { filter = v; t.setRows(rows.filter(by[filter])); })],
        columns: [
          { key: 'id', label: 'Claim', render: function (x) { return link('expense.html?id=' + enc(x.id), x.id); } },
          { key: 'employee', label: 'Employee' },
          { key: 'merchant', label: 'Merchant' },
          { key: 'category', label: 'Category' },
          { key: 'amount', label: 'Amount', num: true, render: function (x) { return money(x.amount); } },
          { key: 'submitted', label: colB('Submitted'), firstDir: 'desc', render: function (x) { return fmt(x.submitted, 'B'); } },
          { key: 'status', label: 'Status', render: function (x) { return pill(x.status); } },
        ],
      });
      setPage([crumbs([['Home', 'index.html'], ['Expenses']]),
        pageHead('Expenses', { sub: 'Expense claims for the October 2026 close. Decide every claim waiting for approval under the Expense policy.',
          actions: [el('a', { class: 'btn', href: 'policy.html?id=expenses', text: 'Expense policy' })] }), t.node]);
      ready();
    });
  }

  function receiptNode(lines) {
    return el('div', { class: 'receipt', role: 'document', 'aria-label': 'Receipt' }, lines.map(function (l) {
      if (l[0] === 'h') return el('div', { class: 'r-h', text: l[1] });
      if (l[0] === 't') return el('div', { class: 'r-t', text: l[1] });
      if (l[0] === 'kv') return el('div', { class: 'r-kv' }, [el('span', { text: l[1] }), el('span', { text: l[2] })]);
      if (l[0] === 'rule') return el('div', { class: 'r-rule' + (l[1] === '=' ? ' double' : ''), 'aria-hidden': 'true' });
      if (l[0] === 'note') return el('div', { class: 'r-note', text: l[1] });
      return null;
    }));
  }

  function pageExpense() {
    var id = qp('id');
    api('expense?id=' + enc(id)).then(function (res) {
      if (res.status !== 200) return notFound('Claim ' + id, 'expenses.html', 'Expenses');
      var x = res.data;
      setTitle(x.id);
      var head = pageHead(x.id, { meta: [pill(x.status), el('span', { text: x.category + ' · ' + x.merchant })] });
      var viewBtn = el('button', { type: 'button', class: 'btn', id: 'viewReceipt' }, [icon('receipt', 16), 'View receipt']);
      viewBtn.addEventListener('click', function () {
        var d = openDialog('Receipt for ' + x.id, skeleton('Loading receipt…'));
        api('receipt?id=' + enc(x.id)).then(function (r) {
          d.body.textContent = '';
          d.body.appendChild(receiptNode(r.data.lines || []));
          loaded('receipt');
        });
      });
      function details() {
        return el('div', {}, [kv([['Employee', x.employee + (x.team ? ' (' + x.team + ')' : '')], ['Merchant', x.merchant], ['Category', x.category],
          ['Date of expense', fmt(x.spent, 'A')], ['Description', x.description],
          ['Claimed amount', el('b', { style: 'font-size:16px', text: money(x.amount) })], ['Receipt', viewBtn]])]);
      }
      function history() {
        var h = x.history;
        var earlier = h.earlier.length ? el('table', { class: 'data' }, [
          el('thead', {}, [el('tr', {}, ['Claim', colB('Submitted'), 'Merchant', 'Receipt no.', 'Amount', 'Status'].map(function (c, i) {
            return el('th', { class: i === 4 ? 'num' : null }, [el('span', { class: 'th', text: c })]);
          }))]),
          el('tbody', {}, h.earlier.map(function (e) {
            return el('tr', {}, [el('td', {}, [link('expense.html?id=' + enc(e.id), e.id)]), el('td', { text: fmt(e.submitted, 'B') }),
              el('td', { text: e.merchant }), el('td', { class: 'mono', text: e.receipt_no }), el('td', { class: 'num', text: money(e.amount) }),
              el('td', {}, [pill(e.status)])]);
          })),
        ]) : el('p', { class: 'empty', text: 'No earlier claims.' });
        return el('div', {}, [el('h3', { class: 'small muted', style: 'margin:0 0 4px', text: 'Claim activity' }), timeline(h.activity, 'C'),
          el('h3', { class: 'small muted', style: 'margin:18px 0 6px', text: 'Earlier claims by ' + x.employee }), earlier]);
      }
      var decision = card('Decision', [], { id: 'decisionCard' });
      function showRecord(d) {
        decision.body.textContent = '';
        decision.body.appendChild(recorded([['Decision', d.decision], ['Reason', d.reason], ['Comment', d.comment || '—']], d.by, d.at));
      }
      if (x.decision) showRecord(x.decision);
      else {
        var dec = choice('decision', 'Decision', x.form.decisions, 'Choose a decision'); dec.key = 'decision';
        var rea = choice('reason', 'Reason', x.form.reasons, 'Choose a reason'); rea.key = 'reason';
        var com = inputField('comment', 'Comment (optional)', { area: true, wide: true, hint: 'The employee sees this comment.' });
        decision.body.appendChild(decisionForm([dec, rea, com], function () {
          return api('expense/decide', { id: x.id, decision: dec.get(), reason: rea.get(), comment: com.get() });
        }, function (rec) {
          showRecord(rec.decision);
          head.meta.firstChild.replaceWith(pill(rec.status));
          toast('Decision recorded for ' + x.id + '.');
        }));
      }
      var main = el('div', { class: 'stack' }, [
        x.banner ? banner('warn', x.banner) : null,
        el('section', { class: 'card', 'aria-label': 'Claim' }, [tabs('claim', [{ label: 'Details', render: details }, { label: 'History', render: history }])]),
        decision,
      ]);
      var side = el('div', { class: 'stack' }, [
        card('Employee', [kv([['Name', x.employee], ['Email', x.email], ['Team', x.team], ['Manager', x.manager]])], { side: true }),
        card('Claim', [kv([['Claim', x.id], ['Submitted', fmt(x.submitted, 'A', true)], ['Close', 'October 2026']])], { side: true }),
        policyLink('expenses', 'Expense policy', 'FIN-07'),
      ]);
      setPage([crumbs([['Expenses', 'expenses.html'], [x.id]]), head, el('div', { class: 'grid2' }, [main, side])]);
      ready();
    });
  }

  // ─── access requests ─────────────────────────────────────────────────
  function pageAccess() {
    setTitle('Access requests');
    api('access').then(function (r) {
      var rows = r.data.rows || [];
      var filter = 'all';
      var nP = rows.filter(function (x) { return x.status === 'Pending'; }).length;
      var t = dataTable({
        id: 'access', label: 'Access requests', noun: ['request', 'requests'], paged: Q.paged_lists, sort: { key: 'submitted', dir: 'desc' }, rows: rows,
        tools: [segmented([['all', 'All (' + rows.length + ')'], ['pending', 'Pending (' + nP + ')'], ['decided', 'Decided (' + (rows.length - nP) + ')']],
          'all', function (v) { filter = v; t.setRows(rows.filter(function (x) { return filter === 'all' || x.status.toLowerCase() === filter; })); })],
        columns: [
          { key: 'id', label: 'Request', render: function (x) { return link('access-request.html?id=' + enc(x.id), x.id); } },
          { key: 'requester', label: 'Requester', sort: function (x) { return x.requester.name; },
            render: function (x) { return nameCell(x.requester.name, x.requester.email); } },
          { key: 'workspace', label: 'Workspace' },
          { key: 'level', label: 'Access' },
          { key: 'approval', label: 'Manager approval' },
          { key: 'submitted', label: colB('Submitted'), firstDir: 'desc', render: function (x) { return fmt(x.submitted, 'B', true); } },
          { key: 'status', label: 'Status', render: function (x) { return pill(x.status); } },
        ],
      });
      setPage([crumbs([['Home', 'index.html'], ['Access requests']]),
        pageHead('Access requests', { sub: 'Requests for access to Quillmere workspaces. Decide every pending request under the Access policy.',
          actions: [el('a', { class: 'btn', href: 'policy.html?id=access', text: 'Access policy' })] }), t.node]);
      ready();
    });
  }

  function pageAccessRequest() {
    var id = qp('id');
    api('access-request?id=' + enc(id)).then(function (res) {
      if (res.status !== 200) return notFound('Request ' + id, 'access.html', 'Access requests');
      var a = res.data;
      setTitle(a.id);
      var head = pageHead(a.id, { meta: [pill(a.status), el('span', { text: a.level + ' access · ' + a.workspace })] });
      var decision = card('Decision', [], { id: 'decisionCard' });
      function showRecord(d) {
        decision.body.textContent = '';
        decision.body.appendChild(recorded([['Decision', d.decision], ['Reason', d.reason]], d.by, d.at));
      }
      if (a.decision) showRecord(a.decision);
      else {
        var dec = choice('decision', 'Decision', a.form.decisions, 'Choose a decision'); dec.key = 'decision';
        var rea = choice('reason', 'Reason', a.form.reasons, 'Choose a reason'); rea.key = 'reason';
        decision.body.appendChild(decisionForm([dec, rea], function () {
          return api('access/decide', { id: a.id, decision: dec.get(), reason: rea.get() });
        }, function (rec) {
          showRecord(rec.decision);
          head.meta.firstChild.replaceWith(pill('Decided'));
          toast('Decision recorded for ' + a.id + '.');
        }));
      }
      var main = el('div', { class: 'stack' }, [
        a.banner ? banner('warn', a.banner) : null,
        card('Request', [kv([['Requester', nameCell(a.requester.name, a.requester.email)], ['Workspace', a.workspace], ['Access level', a.level],
          ['Manager approval', a.approval], ['Submitted', when(a.submitted, true)]])]),
        card('Justification', [el('p', { class: 'message', text: a.justification })]),
        decision,
      ]);
      var side = el('div', { class: 'stack' }, [
        card('Requester', [kv([['Name', a.requester.name], ['Email', a.requester.email]])], { side: true }),
        card('Request details', [kv([['Request', a.id], ['Submitted', fmt(a.submitted, 'A', true)]])], { side: true }),
        policyLink('access', 'Access policy', 'SEC-02'),
      ]);
      setPage([crumbs([['Access requests', 'access.html'], [a.id]]), head, el('div', { class: 'grid2' }, [main, side])]);
      ready();
    });
  }

  // ─── directory ───────────────────────────────────────────────────────
  function pageDirectory() {
    setTitle('Directory');
    var st = { q: qp('q'), page: +(qp('page') || 1), sort: 'name', dir: 'asc' };
    var input = el('input', { type: 'search', id: 'dirSearch', name: 'q', 'aria-label': 'Search people by name or email',
      placeholder: 'Name or email', value: st.q || null });
    var form = el('form', { role: 'search', class: 'inline-search' }, [input, ' ', el('button', { type: 'submit', class: 'btn small', id: 'dirSearchBtn', text: 'Search' })]);
    var count = el('span', { class: 'count' });
    var thead = el('thead'), tbody = el('tbody'), pager = el('div', { class: 'pager' });
    var cols = [['name', 'Name'], ['email', 'Email'], ['team', 'Team'], ['type', 'Type'], ['contract_end', colB('Contract end')], ['manager', 'Manager']];
    form.addEventListener('submit', function (e) { e.preventDefault(); st.q = input.value.trim(); st.page = 1; load(); });
    function load() {
      var qs = 'directory?q=' + enc(st.q) + '&page=' + st.page + '&sort=' + st.sort + '&dir=' + st.dir;
      history.replaceState(null, '', 'directory.html' + (st.q || st.page > 1 ? '?q=' + enc(st.q) + (st.page > 1 ? '&page=' + st.page : '') : ''));
      api(qs).then(function (r) {
        var d = r.data;
        st.page = d.page;
        thead.textContent = ''; tbody.textContent = ''; pager.textContent = '';
        thead.appendChild(el('tr', {}, cols.map(function (c) {
          var on = st.sort === c[0];
          return el('th', { scope: 'col', 'aria-sort': on ? (st.dir === 'asc' ? 'ascending' : 'descending') : 'none' }, [el('button', { type: 'button', class: 'th',
            onclick: function () { if (st.sort === c[0]) st.dir = st.dir === 'asc' ? 'desc' : 'asc'; else { st.sort = c[0]; st.dir = 'asc'; } st.page = 1; load(); } },
            [c[1], on ? el('span', { class: 'ind', 'aria-hidden': 'true', text: st.dir === 'asc' ? '▲' : '▼' }) : null])]);
        })));
        if (!d.rows.length) tbody.appendChild(el('tr', {}, [el('td', { colspan: '6', class: 'empty', text: 'No one matches "' + st.q + '".' })]));
        d.rows.forEach(function (p) {
          tbody.appendChild(el('tr', {}, [el('td', { text: p.name }), el('td', { class: 'small', text: p.email }), el('td', { text: p.team }),
            el('td', { text: p.type }), el('td', { text: p.contract_end ? fmt(p.contract_end, 'B') : '—' }), el('td', { text: p.manager })]));
        });
        count.textContent = d.total + (d.total === 1 ? ' person' : ' people') + (st.q ? ' match "' + st.q + '"' : '');
        var from = d.total ? (d.page - 1) * d.per_page + 1 : 0, to = Math.min(d.total, d.page * d.per_page);
        pager.appendChild(el('span', { text: 'Showing ' + from + '–' + to + ' of ' + d.total }));
        pager.appendChild(el('div', { class: 'right' }, [el('span', { text: 'Page ' + d.page + ' of ' + d.pages }),
          el('button', { type: 'button', class: 'btn small', text: 'Previous page', disabled: d.page <= 1, onclick: function () { st.page -= 1; load(); } }),
          el('button', { type: 'button', class: 'btn small', text: 'Next page', disabled: d.page >= d.pages, onclick: function () { st.page += 1; load(); } })]));
        loaded('directory');
      });
    }
    setPage([crumbs([['Home', 'index.html'], ['Directory']]),
      pageHead('Directory', { sub: 'Everyone at Quillmere: employees and contractors, their teams and managers.' }),
      el('section', { class: 'card table-card', 'aria-label': 'People' }, [el('div', { class: 'table-tools' }, [form, count]),
        el('table', { class: 'data', id: 'people' }, [thead, tbody]), pager])]);
    ready();
    load();
  }

  // ─── inbox: tickets and cases ────────────────────────────────────────
  function pageInbox() {
    setTitle('Inbox');
    api('inbox').then(function (r) {
      var rows = r.data.rows || [];
      var by = { all: function () { return true; }, 'new': function (x) { return x.kind === 'Ticket' && x.status === 'New'; },
        routed: function (x) { return x.kind === 'Ticket' && x.status === 'Routed'; }, cases: function (x) { return x.kind === 'Case'; } };
      var t = dataTable({
        id: 'inbox', label: 'Inbox', noun: ['item', 'items'], paged: Q.paged_lists, sort: { key: 'received', dir: 'desc' }, rows: rows,
        tools: [segmented([['all', 'All (' + rows.length + ')'], ['new', 'New tickets (' + rows.filter(by['new']).length + ')'],
          ['routed', 'Routed'], ['cases', 'Cases']], 'all', function (v) { t.setRows(rows.filter(by[v])); })],
        columns: [
          { key: 'id', label: 'ID', render: function (x) { return link((x.kind === 'Case' ? 'case.html?id=' : 'ticket.html?id=') + enc(x.id), x.id); } },
          { key: 'subject', label: 'Subject', render: function (x) { return el('span', {}, [x.subject, el('span', { class: 'sub', text: x.kind })]); } },
          { key: 'from', label: 'From', render: function (x) { return nameCell(x.from, x.email); } },
          { key: 'received', label: colB('Received'), firstDir: 'desc', render: function (x) { return fmt(x.received, 'B', true); } },
          { key: 'status', label: 'Status', render: function (x) { return pill(x.status); } },
          { key: 'team', label: 'Team', render: function (x) { return x.team || '—'; } },
        ],
      });
      setPage([crumbs([['Home', 'index.html'], ['Inbox']]),
        pageHead('Inbox', { sub: 'Tickets from Quillmere Money customers and cases for Customer care.',
          actions: [el('a', { class: 'btn', href: 'policy.html?id=routing', text: 'Routing guide' })] }), t.node]);
      ready();
    });
  }

  function pageTicket() {
    var id = qp('id');
    api('ticket?id=' + enc(id)).then(function (res) {
      if (res.status !== 200) return notFound('Ticket ' + id, 'inbox.html', 'Inbox');
      var t = res.data;
      setTitle(t.id);
      var head = pageHead(t.id + ' · ' + t.subject, { meta: [pill(t.status), el('span', {}, [t.channel + ' · Received ', when(t.received, true)])] });
      var decision = card('Route', [], { id: 'decisionCard' });
      function showRecord(d) {
        decision.body.textContent = '';
        decision.body.appendChild(recorded([['Routed to', d.route]], d.by, d.at));
      }
      if (t.decision) showRecord(t.decision);
      else {
        var route = choice('route', 'Route to', t.form.routes, 'Choose a team'); route.key = 'route';
        decision.body.appendChild(decisionForm([route], function () { return api('ticket/route', { id: t.id, route: route.get() }); },
          function (rec) {
            showRecord(rec.decision);
            head.meta.firstChild.replaceWith(pill('Routed'));
            toast(t.id + ' routed to ' + rec.decision.route + '.');
          }));
      }
      var main = el('div', { class: 'stack' }, [
        card('Message', [kv([['From', nameCell(t.from, t.email)], ['Subject', t.subject], ['Received', when(t.received, true)]]),
          el('p', { class: 'message', style: 'margin-top:12px', text: t.body })]),
        decision,
      ]);
      var side = el('div', { class: 'stack' }, [
        card('Customer', [kv([['Name', t.from], ['Email', t.email], ['Products held', t.products.length ? t.products.join(', ') : 'None']])], { side: true }),
        card('Ticket details', [kv([['Ticket', t.id], ['Received', fmt(t.received, 'A', true)], ['Channel', t.channel]])], { side: true }),
        policyLink('routing', 'Ticket routing guide', 'QM-03'),
      ]);
      setPage([crumbs([['Inbox', 'inbox.html'], [t.id]]), head, el('div', { class: 'grid2' }, [main, side])]);
      ready();
    });
  }

  function pageCase() {
    var id = qp('id');
    api('case?id=' + enc(id)).then(function (res) {
      if (res.status !== 200) return notFound('Case ' + id, 'inbox.html', 'Inbox');
      var c = res.data;
      setTitle(c.id);
      var head = pageHead(c.id + ' · ' + c.subject, { meta: [pill(c.status), el('span', { text: 'Order ' + c.order.id + ' · ' + c.customer.name })] });
      var alerts = el('div', { id: 'caseAlerts' });
      var tracking = card('Tracking', [el('p', { class: 'sk-caption', 'aria-busy': 'true', text: 'Loading tracking...' }), skeleton('')], { id: 'tracking' });
      var convo = card('Conversation', [], { id: 'conversation' });
      var showAll = false;
      function drawConvo() {
        convo.body.textContent = '';
        var msgs = c.messages, hidden = showAll ? 0 : Math.max(0, msgs.length - 3);
        if (hidden) convo.body.appendChild(el('button', { type: 'button', class: 'linkbtn', id: 'earlier', text: 'Show earlier messages (' + hidden + ')',
          onclick: function () { showAll = true; drawConvo(); track('earlier_messages', c.id, String(hidden)); } }));
        convo.body.appendChild(el('ul', { class: 'convo' }, msgs.slice(hidden).map(function (m) {
          return el('li', {}, [el('div', {}, [el('span', { class: 'who' }, [m.who, el('span', { class: 'role', text: m.role === 'staff' ? 'Quillmere' : 'Customer' })]),
            el('span', { class: 'when', text: fmt(m.at, 'A', true) })]), el('div', { class: 'text', text: m.text })]);
        })));
      }
      drawConvo();
      var decision = card('Decision', [], { id: 'decisionCard' });
      function showRecord(d) {
        decision.body.textContent = '';
        decision.body.appendChild(recorded([['Decision', d.decision], ['Reason', d.reason], ['Note', d.note || '—']], d.by, d.at));
      }
      if (c.decision) showRecord(c.decision);
      else {
        var dec = choice('decision', 'Decision', c.form.decisions, 'Choose a decision'); dec.key = 'decision';
        var rea = choice('reason', 'Reason', c.form.reasons, 'Choose a reason'); rea.key = 'reason';
        var note = inputField('note', 'Note to the customer (optional)', { area: true, wide: true });
        var form = decisionForm([dec, rea, note], function () {
          return api('case/decide', { id: c.id, decision: dec.get(), reason: rea.get(), note: note.get(), version: c.version });
        }, function (rec) {
          alerts.textContent = '';
          showRecord(rec.decision);
          head.meta.firstChild.replaceWith(pill('Decided'));
          toast('Decision recorded for ' + c.id + '.');
        });
        form.onrefused = function (r) {
          if (r.status !== 409) return;
          alerts.textContent = '';
          alerts.appendChild(banner('error', r.data.error || 'This case changed.', [' ', el('a', { href: location.pathname + location.search, id: 'reloadCase', text: 'Reload' })]));
          window.scrollTo(0, 0);
        };
        decision.body.appendChild(form);
      }
      var main = el('div', { class: 'stack' }, [alerts, tracking, convo, decision]);
      var side = el('div', { class: 'stack' }, [
        card('Customer', [kv([['Name', c.customer.name], ['Email', c.customer.email]])], { side: true }),
        card('Order', [kv([['Order', link('order.html?id=' + enc(c.order.id), c.order.id)], ['Items', c.order.items.map(function (x) { return x.name; }).join(', ')],
          ['Total', money(c.order.total)], ['Carrier', c.order.carrier]])], { side: true }),
        card('Case details', [kv([['Case', c.id], ['Opened', fmt(c.opened, 'A', true)], ['Team', 'Customer care']])], { side: true }),
        policyLink('lost-package', 'Lost-package policy', 'CC-09'),
      ]);
      setPage([crumbs([['Inbox', 'inbox.html'], [c.id]]), head, el('div', { class: 'grid2' }, [main, side])]);
      ready();
      api('case/tracking?id=' + enc(c.id)).then(function (r) {
        var t = r.data;
        tracking.body.textContent = '';
        tracking.body.appendChild(kv([['Status', el('span', {}, [pill(t.status), ' ' + t.detail])], ['Carrier', t.carrier], ['Tracking number', t.tracking_no]]));
        tracking.body.appendChild(el('div', { style: 'margin-top:10px' }, [timeline(t.events, 'C')]));
        loaded('tracking');
      });
    });
  }

  // ─── contracts ───────────────────────────────────────────────────────
  function pageContracts() {
    setTitle('Contracts');
    api('contracts').then(function (r) {
      var t = dataTable({
        id: 'contracts', label: 'Contracts', noun: ['contract', 'contracts'], paged: false, sort: { key: 'updated', dir: 'desc' }, rows: r.data.rows || [],
        columns: [
          { key: 'id', label: 'Contract', render: function (x) { return link('contract.html?id=' + enc(x.id), x.title + ' · ' + x.counterparty); } },
          { key: 'kind', label: 'Type' },
          { key: 'status', label: 'Status', render: function (x) { return pill(x.status); } },
          { key: 'review', label: 'Review' },
          { key: 'updated', label: 'Updated', firstDir: 'desc', render: function (x) { return fmt(x.updated, 'A'); } },
        ],
      });
      setPage([crumbs([['Home', 'index.html'], ['Contracts']]), pageHead('Contracts', { sub: 'Supplier contracts and their reviews.' }), t.node]);
      ready();
    });
  }

  function contractBlocks(blocks, jump) {
    return blocks.map(function (b) {
      var t = b[0];
      if (t === 'title') return el('div', { class: 'c-title', text: b[1] });
      if (t === 'sub') return el('div', { class: 'c-sub', text: b[1] });
      if (t === 'h') return el('div', { class: 'c-h', role: 'heading', 'aria-level': '3', text: b[1] });
      if (t === 'h2') return el('div', { class: 'c-h2', role: 'heading', 'aria-level': '4', text: b[1] });
      if (t === 'p') return el('p', { text: b[1] });
      if (t === 'c') return el('div', { class: 'clause' }, [el('b', { text: b[1] }), el('span', { text: b[2] })]);
      if (t === 'def') return el('p', { class: 'def' }, [el('b', { text: b[1] }), ': ' + b[2]]);
      if (t === 'quote') return el('div', { class: 'quote', text: b[1] });
      if (t === 'sig') return el('div', { class: 'sig' }, [el('div', { text: b[1] }), el('div', { text: b[2] })]);
      if (t === 'table') {
        return el('table', {}, [el('thead', {}, [el('tr', {}, b[1][0].map(function (h) { return el('th', { text: h }); }))]),
          el('tbody', {}, b[1].slice(1).map(function (row) { return el('tr', {}, row.map(function (c) { return el('td', { text: c }); })); }))]);
      }
      if (t === 'toc') {
        return el('div', { class: 'toc' }, [el('div', { class: 'c-h', text: 'CONTENTS' })].concat(b[1].map(function (row) {
          var label = (/^\d+$/.test(row[0]) ? row[0] + '. ' : row[0] + ': ') + row[1];
          return el('div', { class: 'toc-row' }, [el('button', { type: 'button', text: label, onclick: function () { jump(row[2]); } }),
            el('span', { class: 'dots', 'aria-hidden': 'true' }), el('span', { text: String(row[2]) })]);
        })));
      }
      return null;
    });
  }

  function viewer(c) {
    var n = c.pages.length, cur = 1;
    var wrap = el('section', { class: 'viewer', 'aria-label': 'Document viewer' });
    function paper(i, jump) {
      return el('article', { class: 'paper', id: 'page-' + i, 'aria-label': 'Page ' + i + ' of ' + n },
        contractBlocks(c.pages[i - 1], jump).concat([el('div', { class: 'foot', text: 'Page ' + i + ' of ' + n })]));
    }
    var name = el('span', { class: 'doc-name', text: c.title + ', ' + c.counterparty });
    if (Q.paged_viewer) {
      var input = el('input', { type: 'text', id: 'pageNo', inputmode: 'numeric', 'aria-label': 'Page number', value: '1', autocomplete: 'off' });
      var prev = el('button', { type: 'button', class: 'btn small', id: 'prevPage', text: 'Previous page' });
      var next = el('button', { type: 'button', class: 'btn small', id: 'nextPage', text: 'Next page' });
      var host = el('div', { class: 'pages' });
      var go = function (p) {
        cur = Math.max(1, Math.min(n, p));
        input.value = String(cur);
        prev.disabled = cur === 1; next.disabled = cur === n;
        host.textContent = '';
        host.appendChild(paper(cur, go));
        document.body.setAttribute('data-doc-page', String(cur));
        track('page', c.id, String(cur));
      };
      prev.addEventListener('click', function () { go(cur - 1); });
      next.addEventListener('click', function () { go(cur + 1); });
      input.addEventListener('change', function () { var v = parseInt(input.value, 10); if (v) go(v); else input.value = String(cur); });
      input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); var v = parseInt(input.value, 10); if (v) go(v); } });
      wrap.appendChild(el('div', { class: 'viewer-bar' }, [name, el('div', { class: 'right' }, [prev,
        el('span', { class: 'pageno' }, ['Page ', input, ' of ' + n]), next])]));
      wrap.appendChild(host);
      go(1);
    } else {
      var all = el('div', { class: 'pages' });
      var jump = function (p) { var e = $('page-' + p); if (e) e.scrollIntoView({ block: 'start' }); };
      for (var i = 1; i <= n; i++) all.appendChild(paper(i, jump));
      wrap.appendChild(el('div', { class: 'viewer-bar' }, [name, el('div', { class: 'right' }, [el('span', { class: 'pageno', text: n + ' pages' })])]));
      wrap.appendChild(all);
    }
    return wrap;
  }

  function checklist(c, done) {
    var box = el('div');
    var form = el('form', { id: 'checklistForm', novalidate: true }, [box, el('p', { class: 'small muted', style: 'margin:0 0 10px', text:
      'Answer from the agreement\'s own wording. Section: the numbered clause that decides the answer, for example 4.2. ' +
      'Leave Section blank when no clause of the agreement deals with the question.' })]);
    var errs = {};
    c.checklist.questions.forEach(function (q) {
      var fs = el('fieldset', { class: 'q', id: 'fs-' + q.key }, [el('legend', { text: q.text })]);
      fs.appendChild(el('div', { class: 'radios', role: 'radiogroup', 'aria-label': q.text }, q.choices.map(function (ch, i) {
        return el('label', { for: q.key + '-' + i }, [el('input', { type: 'radio', name: q.key, id: q.key + '-' + i, value: ch }), ' ' + ch]);
      })));
      var fields = el('div', { class: 'q-fields' });
      if (q.fields.indexOf('section') >= 0) {
        fields.appendChild(el('div', { class: 'field' }, [el('label', { class: 'label', for: q.key + '-section', text: 'Section' }),
          el('input', { type: 'text', id: q.key + '-section', name: q.key + '-section', autocomplete: 'off', placeholder: 'e.g. 4.2' })]));
      }
      if (q.fields.indexOf('days') >= 0) {
        fields.appendChild(el('div', { class: 'field' }, [el('label', { class: 'label', for: q.key + '-days', text: 'Notice (days)' }),
          el('input', { type: 'text', id: q.key + '-days', name: q.key + '-days', autocomplete: 'off', inputmode: 'numeric' })]));
      }
      fs.appendChild(fields);
      if (q.fields.indexOf('source') >= 0) {
        fs.appendChild(el('div', { class: 'label', style: 'margin-top:6px', id: q.key + '-source-label', text: 'Source' }));
        fs.appendChild(el('div', { class: 'radios', role: 'radiogroup', 'aria-labelledby': q.key + '-source-label' }, c.checklist.sources.map(function (s, i) {
          return el('label', { for: q.key + '-src-' + i }, [el('input', { type: 'radio', name: q.key + '-source', id: q.key + '-src-' + i, value: s }), ' ' + s]);
        })));
      }
      errs[q.key] = el('span', { class: 'err hidden' });
      fs.appendChild(errs[q.key]);
      form.appendChild(fs);
    });
    var submit = el('button', { type: 'submit', class: 'btn primary', id: 'submitChecklist', text: 'Submit checklist' });
    form.appendChild(el('div', { class: 'form-actions' }, [submit, el('span', { class: 'note', text: 'A submitted checklist cannot be changed.' })]));
    function val(name) { var e = form.querySelector('input[name="' + name + '"]:checked'); return e ? e.value : ''; }
    function text(id) { var e = $(id); return e ? e.value : ''; }
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (submit.disabled) return;
      var answers = {};
      c.checklist.questions.forEach(function (q) {
        answers[q.key] = { answer: val(q.key), section: text(q.key + '-section'), days: text(q.key + '-days'), source: val(q.key + '-source') };
        errs[q.key].classList.add('hidden');
      });
      submit.disabled = true;
      box.textContent = '';
      api('contract/checklist', { id: c.id, answers: answers }).then(function (res) {
        submit.disabled = false;
        if (res.status === 200 && res.data.ok) { done(res.data.record); return; }
        box.appendChild(banner('error', res.data.error || 'The checklist could not be submitted.'));
        var f = res.data.fields || {};
        Object.keys(f).forEach(function (k) {
          var key = k.split('_')[0];
          if (errs[key]) { errs[key].textContent = f[k]; errs[key].classList.remove('hidden'); }
        });
      });
    });
    return form;
  }

  function pageContract() {
    var id = qp('id');
    api('contract?id=' + enc(id)).then(function (res) {
      if (res.status !== 200) return notFound('Contract ' + id, 'contracts.html', 'Contracts');
      var c = res.data;
      setTitle(c.title + ' · ' + c.counterparty);
      var head = pageHead(c.title + ': ' + c.counterparty, { meta: [pill(c.status), el('span', { text: c.id + ' · ' + c.kind + ' · ' + c.pages.length + (c.pages.length === 1 ? ' page' : ' pages') })] });
      var right;
      if (!c.checklist) {
        right = card('Review', [el('p', { class: 'empty', text: 'This contract has no open review.' })], { side: true });
      } else {
        right = card('Review checklist', [], { id: 'checklist', side: true });
        right.classList.add('checklist');
        var showRecord = function (rec) {
          right.body.textContent = '';
          var rows = c.checklist.questions.map(function (q) {
            var a = rec.answers[q.key] || {};
            var parts = [a.answer];
            if (a.section) parts.push('section ' + a.section);
            if (a.days != null) parts.push(a.days + ' days');
            if (a.source) parts.push('source: ' + a.source);
            return [q.key.toUpperCase(), parts.join(', ')];
          });
          right.body.appendChild(el('div', {}, [banner('info', 'Checklist submitted. It cannot be changed.'), kv(rows),
            el('p', { class: 'muted small', text: 'Submitted by ' + rec.by + ', ' + fmt(rec.at, 'A', true) + '.' })]));
        };
        if (c.submitted) showRecord(c.submitted);
        else right.body.appendChild(checklist(c, function (rec) {
          showRecord(rec.submitted);
          head.meta.firstChild.replaceWith(pill('Reviewed'));
          toast('Checklist submitted for ' + c.counterparty + '.');
        }));
      }
      setPage([crumbs([['Contracts', 'contracts.html'], [c.counterparty]]), head, el('div', { class: 'contract-layout' }, [viewer(c), right])]);
      ready();
    });
  }

  // ─── policies and help ───────────────────────────────────────────────
  function pagePolicies() {
    setTitle('Policy center');
    api('policies').then(function (r) {
      var t = dataTable({
        id: 'policies', label: 'Policies', noun: ['policy', 'policies'], paged: false, sort: { key: 'title', dir: 'asc' }, rows: r.data.rows || [],
        columns: [
          { key: 'title', label: 'Policy', render: function (p) { return el('span', {}, [link('policy.html?id=' + enc(p.id), p.title), el('span', { class: 'sub', text: p.summary })]); } },
          { key: 'code', label: 'Code' },
          { key: 'owner', label: 'Owner' },
          { key: 'updated', label: 'Last updated', firstDir: 'desc', render: function (p) { return fmt(p.updated, 'A'); } },
        ],
      });
      setPage([crumbs([['Home', 'index.html'], ['Policy center']]),
        pageHead('Policy center', { sub: 'The policies staff decide by. A policy changes only when it is republished here.' }), t.node]);
      ready();
    });
  }

  function pagePolicy() {
    var id = qp('id');
    api('policy?id=' + enc(id)).then(function (res) {
      if (res.status !== 200) return notFound('Policy ' + id, 'policies.html', 'Policy center');
      var p = res.data;
      setTitle(p.title);
      var body = el('div', { class: 'card-body policy' }, [el('p', { class: 'policy-meta', text: p.summary })]);
      p.sections.forEach(function (s, i) {
        body.appendChild(el('h2', { id: 's' + (i + 1), text: (i + 1) + '. ' + s[0] }));
        s[1].forEach(function (b) {
          if (typeof b === 'string') body.appendChild(el('p', { text: b }));
          else if (b.sub) body.appendChild(el('p', { class: 'rule-sub' }, [el('b', { text: b.sub[0] }), b.sub[1]]));
          else if (b.list) body.appendChild(el('ul', {}, b.list.map(function (x) { return el('li', { text: x }); })));
          else if (b.steps) body.appendChild(el('ol', {}, b.steps.map(function (x) { return el('li', { text: x }); })));
          else if (b.example) body.appendChild(el('div', { class: 'example', text: b.example }));
          else if (b.table) body.appendChild(el('table', {}, [el('thead', {}, [el('tr', {}, b.table[0].map(function (h) { return el('th', { text: h }); }))]),
            el('tbody', {}, b.table.slice(1).map(function (row) { return el('tr', {}, row.map(function (c) { return el('td', { text: c }); })); }))]));
        });
      });
      body.appendChild(el('p', { class: 'policy-meta', style: 'margin-top:22px', text: 'Questions about this policy: ask your team lead. ' + p.code + ', last updated ' + fmt(p.updated, 'A') + '.' }));
      var toc = el('nav', { class: 'card side toc-mini', 'aria-label': 'On this page' }, [el('div', { class: 'card-body' },
        [el('div', { class: 'small muted', style: 'margin-bottom:6px', text: 'On this page' })].concat(p.sections.map(function (s, i) {
          return el('a', { href: '#s' + (i + 1), text: (i + 1) + '. ' + s[0] });
        })))]);
      setPage([crumbs([['Policy center', 'policies.html'], [p.title]]),
        pageHead(p.title, { meta: [el('span', { class: 'badge code', style: 'margin-left:0', text: p.code }), el('span', { text: 'Owner: ' + p.owner }),
          el('span', { text: '·' }), el('span', { text: 'Last updated ' + fmt(p.updated, 'A') })] }),
        el('div', { class: 'grid2' }, [el('section', { class: 'card', 'aria-label': p.title }, [body]), toc])]);
      ready();
    });
  }

  function pageHelp() {
    var a = qp('a');
    if (a) {
      api('help?a=' + enc(a)).then(function (r) {
        if (r.status !== 200) return notFound('Article', 'help.html', 'Help');
        setTitle(r.data.title);
        setPage([crumbs([['Help', 'help.html'], [r.data.title]]), pageHead(r.data.title), card(null, [el('p', { text: r.data.body })])]);
        ready();
      });
      return;
    }
    var q = qp('q');
    setTitle('Help');
    api('help?q=' + enc(q)).then(function (r) {
      var res = r.data.results || [];
      var list = res.length ? el('ul', { class: 'results' }, res.map(function (x) {
        return el('li', {}, [link('help.html?a=' + enc(x.id), x.title), el('div', { class: 'muted small', text: x.snippet + '…' })]);
      })) : el('p', { class: 'empty', style: 'padding:14px 16px', text: q ? 'No help articles match "' + q + '". The search covers help articles only.' : 'Type in the search box to find help articles.' });
      setPage([crumbs([['Home', 'index.html'], ['Help']]), pageHead(q ? 'Help articles for "' + q + '"' : 'Help', { sub: 'Search covers help articles only.' }),
        el('section', { class: 'card', 'aria-label': 'Results' }, [list])]);
      ready();
    });
  }

  var PAGES = {
    home: pageHome, refunds: pageRefunds, refund: pageRefund, orders: pageOrders, order: pageOrder,
    expenses: pageExpenses, expense: pageExpense, access: pageAccess, 'access-request': pageAccessRequest,
    directory: pageDirectory, inbox: pageInbox, ticket: pageTicket, 'case': pageCase, contracts: pageContracts,
    contract: pageContract, policies: pagePolicies, policy: pagePolicy, help: pageHelp,
  };

  api('session').then(function (r) {
    if (r.status !== 200) {
      document.body.appendChild(el('p', { style: 'padding:24px', text: 'Quillmere Desk could not load your session. Open the app from its start link.' }));
      return;
    }
    S = r.data;
    Q = S.quirks || {};
    buildShell();
    (PAGES[PAGE] || pageHome)();
  });
})();
