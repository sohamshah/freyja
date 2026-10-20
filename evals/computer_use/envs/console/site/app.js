// Cirrolux Cloud console: an invented cloud console for computer-use evals.
// One single-page app serves every route (catalog.html, model.html, ...): the
// file name is the route, links inside the app change the address with
// history.pushState and the content follows. The server decides everything
// that is scored; the page only shows and sends. Each trap is a quirk switch
// from the session (see ../README.md); with a quirk off the page uses the
// plain version of the same widget.
(function () {
  'use strict';
  var API = '/console/api/';
  var S = null;      // session: user, org, selected project, quirks, timings
  var Q = {};        // quirk switches
  var T = { first: 0, swap: 0, quick: 0 };
  var R = {};        // shell nodes
  var renderTimer = null, renderSeq = 0, renders = 0, pushed = 0;
  var overlays = []; // open overlays, topmost last: {escape(), close(), reposition()}
  var PLATFORM = 'Cirrolux AI Platform', CONSOLE = 'Cirrolux Cloud';

  // ─── small helpers ─────────────────────────────────────────────────────
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) {
      var v = attrs[k];
      if (k === 'text') e.textContent = v;
      else if (k.slice(0, 2) === 'on' && typeof v === 'function') e.addEventListener(k.slice(2), v);
      else if (v === true) e.setAttribute(k, '');
      else if (v !== false && v != null) e.setAttribute(k, v);
    }
    (kids || []).forEach(function (c) {
      if (c != null && c !== false) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return e;
  }
  function api(name, body) {
    var opt = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    };
    return fetch(API + name, opt).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, data: d }; },
        function () { return { status: r.status, data: {} }; });
    });
  }
  function enc(s) { return encodeURIComponent(s == null ? '' : s); }
  function params() { return new URLSearchParams(location.search); }
  function pid() { return S.selected ? S.selected.id : ''; }
  function pname() { return S.selected ? S.selected.name : ''; }
  function docs(topic) { return 'docs.html?topic=' + enc(topic); }
  function ext(text, href) {
    return el('a', { href: href, target: '_blank', rel: 'noopener' }, [text, svg('ext', 13, 'ext'),
      el('span', { class: 'sr-only', text: ' external, opens new window' })]);
  }
  function snack(text) {
    var s = el('div', { class: 'snack', role: 'status', text: text });
    document.body.insertBefore(s, R.ov || null);
    setTimeout(function () { s.remove(); }, 4000);
  }

  // Icons drawn for this console (24x24, currentColor).
  var ST = ' fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
  var FI = ' fill="currentColor"';
  var ICONS = {
    menu: '<path' + ST + ' d="M4 7h16M4 12h16M4 17h16"/>',
    search: '<circle' + ST + ' cx="10.5" cy="10.5" r="5.5"/><path' + ST + ' d="M15 15l5 5"/>',
    spark: '<path' + FI + ' d="M12 2.5c.7 5.2 4.3 8.8 9.5 9.5-5.2.7-8.8 4.3-9.5 9.5-.7-5.2-4.3-8.8-9.5-9.5 5.2-.7 8.8-4.3 9.5-9.5z"/>',
    terminal: '<rect' + ST + ' x="3" y="5" width="18" height="14" rx="2"/><path' + ST + ' d="M7 10l3 2.5L7 15M12.5 15.5h4.5"/>',
    bell: '<path' + ST + ' d="M6 17h12l-1.6-2.2V11a4.4 4.4 0 0 0-8.8 0v3.8z"/><path' + ST + ' d="M10.3 19.6a1.8 1.8 0 0 0 3.4 0"/>',
    dots: '<circle' + FI + ' cx="12" cy="5.5" r="1.8"/><circle' + FI + ' cx="12" cy="12" r="1.8"/><circle' + FI + ' cx="12" cy="18.5" r="1.8"/>',
    back: '<path' + ST + ' d="M20 12H5M11 5.5L4.5 12l6.5 6.5"/>',
    drop: '<path' + FI + ' d="M7 10l5 5 5-5z"/>',
    star: '<path' + ST + ' d="M12 4l2.4 5 5.4.6-4 3.7 1.1 5.4L12 16l-4.9 2.7 1.1-5.4-4-3.7 5.4-.6z"/>',
    starOn: '<path fill="currentColor" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" d="M12 4l2.4 5 5.4.6-4 3.7 1.1 5.4L12 16l-4.9 2.7 1.1-5.4-4-3.7 5.4-.6z"/>',
    infoFilled: '<circle' + FI + ' cx="12" cy="12" r="9"/><path stroke="#fff" stroke-width="2.2" stroke-linecap="round" d="M12 11v5"/><circle fill="#fff" cx="12" cy="7.7" r="1.3"/>',
    info: '<circle' + ST + ' cx="12" cy="12" r="8.5"/><path' + ST + ' d="M12 11v5M12 8h.01"/>',
    check: '<path' + ST + ' d="M5 12.5l4.5 4.5L19 7.5"/>',
    checkCircle: '<circle' + FI + ' cx="12" cy="12" r="10"/><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M7.5 12.3l3 3 6-6.3"/>',
    project: '<circle' + FI + ' cx="8" cy="7.5" r="2.6"/><circle' + FI + ' cx="16" cy="12" r="2.6"/><circle' + FI + ' cx="8" cy="16.5" r="2.6"/>',
    org: '<path' + ST + ' d="M4 20V8l8-4 8 4v12M9 20v-5h6v5"/>',
    panel: '<rect' + ST + ' x="4" y="5" width="16" height="14" rx="2"/><path' + ST + ' d="M9 5v14M15.5 10l-2 2 2 2"/>',
    ext: '<path' + ST + ' d="M14 4h6v6M20 4l-8.5 8.5M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    close: '<path' + ST + ' d="M6 6l12 12M18 6L6 18"/>',
    chevUp: '<path' + ST + ' d="M6 15l6-6 6 6"/>',
    chevDown: '<path' + ST + ' d="M6 9l6 6 6-6"/>',
    chevLeft: '<path' + ST + ' d="M15 6l-6 6 6 6"/>',
    chevRight: '<path' + ST + ' d="M9 6l6 6-6 6"/>',
    grid: '<rect' + ST + ' x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect' + ST + ' x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect' + ST + ' x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect' + ST + ' x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>',
    sliders: '<path' + ST + ' d="M5 7h9M18 7h1M5 12h3M12 12h7M5 17h11"/><circle' + ST + ' cx="16" cy="7" r="2"/><circle' + ST + ' cx="10" cy="12" r="2"/><circle' + ST + ' cx="18" cy="17" r="2"/>',
    bars: '<path' + ST + ' d="M6 19v-8M12 19V5M18 19v-6"/>',
    box: '<path' + ST + ' d="M4 8l8-4 8 4-8 4zM4 8v8l8 4 8-4V8M12 12v8"/>',
    plug: '<circle' + ST + ' cx="12" cy="12" r="2.5"/><path' + ST + ' d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14"/>',
    layers: '<path' + ST + ' d="M12 4l8 4-8 4-8-4zM4 12l8 4 8-4M4 16l8 4 8-4"/>',
    gauge: '<path' + ST + ' d="M4.5 17a8 8 0 1 1 15 0M12 13l4-4"/>',
    table: '<rect' + ST + ' x="4" y="5" width="16" height="14" rx="1.5"/><path' + ST + ' d="M4 10h16M4 14.5h16M10 10v9"/>',
    diamond: '<path' + ST + ' d="M12 3.5l8.5 8.5-8.5 8.5L3.5 12z"/>',
    flask: '<path' + ST + ' d="M9.5 4h5M10.5 4v5L5.5 18a1.5 1.5 0 0 0 1.3 2h10.4a1.5 1.5 0 0 0 1.3-2l-5-9V4"/>',
    target: '<circle' + ST + ' cx="12" cy="12" r="8"/><circle' + ST + ' cx="12" cy="12" r="4"/><circle' + FI + ' cx="12" cy="12" r="1.3"/>',
    nodes: '<circle' + ST + ' cx="6" cy="6" r="2"/><circle' + ST + ' cx="18" cy="6" r="2"/><circle' + ST + ' cx="12" cy="18" r="2"/><path' + ST + ' d="M7.6 7.4l3.3 8.8M16.4 7.4l-3.3 8.8M8 6h8"/>',
    flow: '<rect' + ST + ' x="3.5" y="4" width="6" height="5" rx="1"/><rect' + ST + ' x="14.5" y="15" width="6" height="5" rx="1"/><path' + ST + ' d="M6.5 9v4.5a2 2 0 0 0 2 2h6"/>',
    tag: '<path' + ST + ' d="M4 4h7l9 9-7 7-9-9z"/><circle' + FI + ' cx="8.2" cy="8.2" r="1.3"/>',
    chart: '<path' + ST + ' d="M4 19h16M5 15l4-4 3 3 6-7"/>',
    key: '<circle' + ST + ' cx="8" cy="12" r="3.5"/><path' + ST + ' d="M11.5 12H20M17 12v3M20 12v2"/>',
    card: '<rect' + ST + ' x="3.5" y="6" width="17" height="12" rx="1.5"/><path' + ST + ' d="M3.5 10h17"/>',
    cap: '<path' + ST + ' d="M3 9l9-4 9 4-9 4zM7 11v4.5c1.5 1.5 3 2 5 2s3.5-.5 5-2V11"/>',
    notebook: '<rect' + ST + ' x="5" y="4" width="14" height="16" rx="1.5"/><path' + ST + ' d="M9 4v16M12 9h4M12 12.5h4"/>',
    copy: '<rect' + ST + ' x="8" y="8" width="11" height="11" rx="1.5"/><path' + ST + ' d="M5 15V6a1 1 0 0 1 1-1h9"/>',
    plus: '<path' + ST + ' d="M12 5v14M5 12h14"/>',
    tick: '<path fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" d="M6 12.5l4 4L18 8"/>',
  };
  function svg(name, size, cls) {
    var s = size || 24, w = document.createElement('span');
    w.innerHTML = '<svg width="' + s + '" height="' + s + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false"' +
      (cls ? ' class="' + cls + '"' : '') + '>' + (ICONS[name] || '') + '</svg>';
    return w.firstChild;
  }
  function ready() { document.body.setAttribute('data-ready', '1'); }
  function shown(name) {
    renders += 1;
    document.body.setAttribute('data-shown', name);
    document.body.setAttribute('data-renders', String(renders));
    ready();
  }

  // ─── overlays: panels, menus and dialogs at the end of <body> ──────────
  function pushOverlay(o) { overlays.push(o); return o; }
  function dropOverlay(o) { var i = overlays.indexOf(o); if (i >= 0) overlays.splice(i, 1); }
  function closeAllOverlays() { overlays.slice().reverse().forEach(function (o) { o.close(); }); }
  function closeMenus() { overlays.slice().reverse().forEach(function (o) { if (o.kind === 'menu') o.close(); }); }
  function place(pane, anchor, opts) {
    opts = opts || {};
    var r = anchor.getBoundingClientRect();
    if (opts.width) pane.style.width = r.width + 'px';
    var h = pane.offsetHeight || 0;
    var below = window.innerHeight - r.bottom - 8;
    if (opts.right) { pane.style.left = ''; pane.style.right = Math.max(8, window.innerWidth - r.right) + 'px'; }
    else pane.style.left = Math.max(4, r.left) + 'px';
    if (below >= h || below >= r.top - 8) { pane.style.top = (r.bottom + (opts.gap || 0)) + 'px'; pane.style.bottom = ''; }
    else { pane.style.top = ''; pane.style.bottom = (window.innerHeight - r.top + (opts.gap || 0)) + 'px'; }
  }
  function reposition() { overlays.forEach(function (o) { if (o.reposition) o.reposition(); }); }
  // An autocomplete panel has no backdrop (Material's autocomplete): a press
  // anywhere outside the field and the panel closes it.
  function closeOnOutside(inside, close) {
    function down(e) { if (!inside.some(function (n) { return n.contains(e.target); })) close(); }
    document.addEventListener('mousedown', down, true);
    return function () { document.removeEventListener('mousedown', down, true); };
  }

  // A menu (Material style): a transparent backdrop closes it.
  function menu(anchor, items, opts) {
    opts = opts || {};
    closeMenus();
    var bd = el('div', { class: 'cdk-overlay-backdrop' });
    var panel = el('div', { class: 'menu-panel', role: 'menu', tabindex: '-1' }, items.map(function (it) {
      if (it.note) return el('div', { class: 'menu-note', text: it.note });
      return el('button', {
        type: 'button', role: it.radio ? 'menuitemradio' : 'menuitem', 'aria-checked': it.radio ? String(!!it.on) : null,
        onclick: function () { close(); if (it.onclick) it.onclick(); },
      }, [it.on ? svg('check', 18) : (it.radio ? el('span', { style: 'width:18px' }) : null), it.text]);
    }));
    var pane = el('div', { class: 'cdk-overlay-pane' }, [panel]);
    R.ov.appendChild(bd);
    R.ov.appendChild(pane);
    anchor.setAttribute('aria-expanded', 'true');
    var o = pushOverlay({ kind: 'menu', escape: close, close: close, reposition: function () { place(pane, anchor, opts); } });
    o.reposition();
    bd.addEventListener('click', close);
    function close() {
      bd.remove(); pane.remove(); dropOverlay(o);
      anchor.setAttribute('aria-expanded', 'false');
    }
    return o;
  }

  // ─── router ────────────────────────────────────────────────────────────
  var VIEWS = {};
  function viewName() {
    var f = (location.pathname.split('/').pop() || '').replace(/\.html$/, '');
    return VIEWS[f] ? f : 'catalog';
  }
  function withProject(url) {
    var u = new URL(url, location.href);
    var noProject = /\/(docs|search)\.html$/.test(u.pathname);
    if (S.selected && !u.searchParams.has('project') && !noProject) u.searchParams.set('project', S.selected.id);
    return u;
  }
  // Change the address now; the content follows after `how`: 'swap' (late, a
  // route change of the single-page app), 'quick' or 'now'.
  function go(url, how) {
    closeAllOverlays();
    var u = withProject(url);
    history.pushState(null, '', u.pathname + u.search);
    pushed += 1;
    schedule(how === 'swap' ? T.swap : how === 'now' ? 0 : T.quick);
  }
  function schedule(ms) {
    clearTimeout(renderTimer);
    var seq = ++renderSeq;
    R.progress.classList.toggle('on', ms > 0);
    renderTimer = setTimeout(function () { if (seq === renderSeq) render(); }, ms);
  }
  function render() {
    R.progress.classList.remove('on');
    var name = viewName();
    if (name !== 'keys') leaveKeys();
    markNav(name);
    VIEWS[name](renderSeq);
  }
  function current(seq) { return seq === renderSeq; }

  // ─── shell ─────────────────────────────────────────────────────────────
  var NAV = [['catalog', 'Model catalog', 'grid'], ['tuning', 'Tuning', 'sliders'], ['evaluation', 'Evaluation', 'bars'],
    ['registry', 'Model Registry', 'box'], ['endpoints', 'Endpoints', 'plug'], ['batch', 'Batch Inference', 'layers'],
    ['throughput', 'Provisioned Throughput', 'gauge']];
  var MORE = [['datasets', 'Datasets', 'table'], ['features', 'Feature Store', 'diamond'], ['experiments', 'Experiments', 'flask'],
    ['training', 'Training', 'target'], ['ray', 'Ray', 'nodes'], ['pipelines', 'Pipelines', 'flow'],
    ['metadata', 'Metadata', 'tag'], ['monitoring', 'Monitoring', 'chart']];
  var GS_LABEL = 'Search for resources, docs, products, and more';

  function buildShell() {
    var b = document.body;
    b.appendChild(el('a', { class: 'skip', href: '#main-content', text: 'Skip to main content' }));
    b.appendChild(el('button', { class: 'sr-only', type: 'button', text: 'Keyboard shortcuts',
      onclick: function () { snack('Keyboard shortcuts: / searches, . opens the navigation menu.'); } }));
    b.appendChild(el('a', { class: 'sr-only', href: docs('accessibility'), target: '_blank', text: 'Accessibility help' }));
    b.appendChild(el('a', { class: 'sr-only', href: docs('accessibility-feedback'), target: '_blank', text: 'Accessibility feedback' }));

    R.menuBtn = el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Navigation menu (.)',
      'aria-expanded': 'false', onclick: productMenu }, [svg('menu')]);
    R.chipText = el('span');
    R.chip = el('button', { class: 'chip', type: 'button', onclick: openPicker }, [svg('project', 18), R.chipText]);
    R.gsInput = el('input', { type: 'text', 'aria-label': GS_LABEL, autocomplete: 'off',
      placeholder: 'Search (/) for resources, docs, products, and more', oninput: gsInput, onkeydown: gsKey });
    R.gsBtn = el('button', { class: 'gsearch-btn', type: 'button', 'aria-label': 'See all search results', disabled: true,
      onclick: function () { gsRun(); } }, [svg('search', 20), 'Search']);
    R.gs = el('div', { class: 'gs-box', role: 'combobox', 'aria-expanded': 'false', 'aria-haspopup': 'listbox',
      'aria-label': GS_LABEL }, [R.gsInput]);
    R.gsWrap = el('div', { class: 'gsearch' }, [R.gs, R.gsBtn]);
    var top = el('header', { class: 'topbar' }, [
      R.menuBtn,
      el('a', { class: 'logo', href: 'page.html?p=home', 'data-spa': 'quick', 'aria-label': CONSOLE + ' home' },
        [el('span', { 'aria-hidden': 'true', text: CONSOLE })]),
      R.chip, R.gsWrap,
      el('div', { class: 'tb-icons' }, [
        el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Open or close Cirrolux Assist chat (Option A)',
          onclick: toggleAssist }, [svg('spark')]),
        el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Activate Cirrolux Shell (G then S)',
          onclick: toggleShell }, [svg('terminal')]),
        el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Open notifications', 'aria-haspopup': 'menu',
          'aria-expanded': 'false', onclick: function (e) { notifications(e.currentTarget); } }, [svg('bell')]),
        el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Settings and utilities', 'aria-haspopup': 'menu',
          'aria-expanded': 'false', onclick: function (e) { settings(e.currentTarget); } }, [svg('dots')]),
        el('button', { class: 'avatar', type: 'button', 'aria-label': 'Account: ' + S.user + ' (' + S.email + ')',
          'aria-haspopup': 'menu', 'aria-expanded': 'false', 'data-initial': S.user.charAt(0),
          onclick: function (e) { account(e.currentTarget); } }),
      ]),
    ]);
    R.crumbs = el('nav', { class: 'crumbs', 'aria-label': 'Breadcrumb' });

    R.navLinks = {};
    function item(n, i, total) {
      var a = el('a', { class: 'sn-item', href: n[0] === 'catalog' ? 'catalog.html' : 'page.html?p=' + n[0],
        'data-spa': n[0] === 'catalog' ? 'swap' : 'quick', 'aria-label': n[1] + ', ' + (i + 1) + ' of ' + total },
        [svg(n[2], 20), el('span', { class: 'sn-text', text: n[1] })]);
      R.navLinks[n[0]] = a;
      return a;
    }
    R.moreList = el('div', {}, MORE.map(function (n, i) { return item(n, i, MORE.length); }));
    R.moreBtn = el('button', { class: 'sn-group', type: 'button', 'aria-expanded': 'true', onclick: function () {
      var open = R.moreBtn.getAttribute('aria-expanded') !== 'true';
      R.moreBtn.setAttribute('aria-expanded', String(open));
      R.moreList.classList.toggle('hidden', !open);
      R.moreBtn.replaceChild(svg(open ? 'chevUp' : 'chevDown', 20), R.moreBtn.lastChild);
    } }, [el('span', { class: 'sn-text', text: 'More services' }), svg('chevUp', 20)]);
    R.navToggle = el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Toggle ' + PLATFORM + ' navigation menu',
      'aria-expanded': 'true', onclick: function () {
        var open = R.navToggle.getAttribute('aria-expanded') !== 'true';
        R.navToggle.setAttribute('aria-expanded', String(open));
        R.nav.classList.toggle('collapsed', !open);
        R.layout.classList.toggle('nav-collapsed', !open);
      } }, [svg('panel', 20)]);
    R.nav = el('nav', { class: 'sidenav', 'aria-label': PLATFORM }, [
      el('div', { class: 'sn-head' }, [
        el('a', { class: 'sn-title', href: 'page.html?p=dashboard', 'data-spa': 'quick' }, [svg('spark', 22), el('span', { text: PLATFORM })]),
        R.navToggle,
      ]),
      el('a', { class: 'sn-back', href: 'page.html?p=dashboard', 'data-spa': 'quick', 'aria-label': 'Back to ' + PLATFORM },
        [svg('back', 22), el('span', { class: 'sn-text', text: 'Models' })]),
      el('div', { class: 'sn-list' }, NAV.map(function (n, i) { return item(n, i, NAV.length); })
        .concat([R.moreBtn, R.moreList])),
      el('div', { class: 'sn-foot' }, [
        el('a', { class: 'sn-item sn-key', href: 'keys.html', 'data-spa': 'quick' }, [svg('key', 20), el('span', { class: 'sn-text', text: 'Get API key' })]),
        el('hr'),
        el('a', { class: 'sn-item', href: 'page.html?p=billing', 'data-spa': 'quick' }, [svg('card', 20), el('span', { class: 'sn-text', text: 'Billing' })]),
        el('a', { class: 'sn-item', href: 'page.html?p=tutorials', 'data-spa': 'quick' }, [svg('cap', 20), el('span', { class: 'sn-text', text: 'Tutorials' })]),
      ]),
    ]);
    R.head = el('div', { class: 'card-head hidden' });
    R.progress = el('div', { class: 'progress' });
    R.body = el('div', { class: 'card-body' });
    R.foot = el('div', { class: 'card-foot hidden' });
    R.card = el('main', { class: 'card', id: 'main-content' }, [R.head, R.progress, R.body, R.foot]);
    b.appendChild(top);
    R.layout = el('div', { class: 'layout' }, [R.crumbs, R.nav, R.card]);
    b.appendChild(R.layout);
    R.ov = el('div', { class: 'cdk-overlay-container' });
    b.appendChild(R.ov);
    R.body.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);
    updateChip();
  }

  function updateChip() {
    if (S.selected) {
      R.chipText.textContent = S.selected.name;
      R.chip.setAttribute('aria-label', "You're currently working in " + S.selected.name +
        '. Hit enter to switch projects, folders, or organizations.');
    } else {
      R.chipText.textContent = 'Select a project';
      R.chip.setAttribute('aria-label', 'No project selected. Hit enter to select a project, folder, or organization.');
    }
  }
  function markNav(name) {
    var sel = name === 'page' ? params().get('p') : (['catalog', 'model', 'questionnaire', 'terms', 'program'].indexOf(name) >= 0 ? 'catalog' : '');
    Object.keys(R.navLinks).forEach(function (k) {
      var on = k === sel;
      R.navLinks[k].classList.toggle('sel', on);
      if (on) R.navLinks[k].setAttribute('aria-current', 'page'); else R.navLinks[k].removeAttribute('aria-current');
    });
  }
  function setCrumbs(list) {
    R.crumbs.textContent = '';
    list.forEach(function (c, i) {
      if (i) R.crumbs.appendChild(el('span', { class: 'sep', 'aria-hidden': 'true', text: '/' }));
      R.crumbs.appendChild(el('a', { href: c[1], 'data-spa': c[2] || 'quick', text: c[0] }));
    });
  }
  var BASE_CRUMBS = [[PLATFORM, 'page.html?p=dashboard'], ['Models', 'page.html?p=models'], ['Model catalog', 'catalog.html', 'swap']];
  var TITLE = '';
  function setTitle(t) {
    TITLE = t;
    document.title = [t, PLATFORM, pname(), CONSOLE].filter(Boolean).join(' – ');
  }
  // The card: a header row (back arrow and title), the scrolling body, and an
  // optional bottom bar.
  function setCard(o) {
    R.head.textContent = '';
    R.head.className = 'card-head' + (o.title == null ? ' hidden' : '') + (o.back ? '' : ' no-back');
    if (o.back) R.head.appendChild(o.back);
    if (o.title != null) R.head.appendChild(el('h1', { class: 'card-title', text: o.title }));
    if (o.actions) R.head.appendChild(el('div', { class: 'head-actions' }, o.actions));
    R.body.textContent = '';
    R.body.scrollTop = 0;
    if (S.notice) { R.body.appendChild(el('div', { class: 'notice', role: 'alert', text: S.notice })); S.notice = null; }
    (o.body || []).forEach(function (n) { if (n) R.body.appendChild(n); });
    R.foot.textContent = '';
    R.foot.classList.toggle('hidden', !o.foot);
    (o.foot || []).forEach(function (n) { R.foot.appendChild(n); });
  }
  function backLink(href, label, how) {
    return el('a', { class: 'back', href: href, 'data-spa': how || 'quick', 'aria-label': label }, [svg('back', 22)]);
  }
  function errorCard(title, text, link) {
    setCard({ title: title, body: [el('div', { class: 'empty' }, [el('h2', { text: text || 'Something went wrong.' }),
      link ? el('a', { href: link[1], 'data-spa': 'quick', text: link[0] }) : null])] });
  }

  // ─── top bar widgets ───────────────────────────────────────────────────
  function productMenu() {
    menu(R.menuBtn, [
      { text: 'Cloud overview', onclick: function () { go('page.html?p=home', 'quick'); } },
      { text: PLATFORM, onclick: function () { go('page.html?p=dashboard', 'quick'); } },
      { text: 'Compute', onclick: function () { go('page.html?p=compute', 'quick'); } },
      { text: 'Storage', onclick: function () { go('page.html?p=storage', 'quick'); } },
      { text: 'IAM & Admin', onclick: function () { go('page.html?p=iam', 'quick'); } },
      { text: 'Billing', onclick: function () { go('page.html?p=billing', 'quick'); } },
    ]);
  }
  function notifications(btn) {
    menu(btn, [{ note: 'Quota increase approved for lumen-prod: 2,000 requests per minute for partner models.' },
      { note: 'Scheduled maintenance for Batch Inference on Oct 18, 02:00–03:00 UTC.' }], { right: true });
  }
  function settings(btn) {
    menu(btn, [{ text: 'Preferences', onclick: function () { go('page.html?p=preferences', 'quick'); } },
      { text: 'Download Cirrolux CLI', onclick: function () { window.open(docs('cli'), '_blank'); } },
      { text: 'Terms of service', onclick: function () { window.open(docs('terms'), '_blank'); } }], { right: true });
  }
  function account(btn) {
    menu(btn, [{ note: S.user + ' · ' + S.email }, { text: 'Manage your account', onclick: function () { go('page.html?p=account', 'quick'); } },
      { text: 'Sign out', onclick: function () { snack('Sign out is turned off in this session.'); } }], { right: true });
  }
  function toggleAssist() {
    if (R.assist) { R.assist.remove(); R.assist = null; return; }
    R.assist = el('aside', { class: 'sidepanel', 'aria-label': 'Cirrolux Assist' }, [
      el('header', {}, [el('span', { text: 'Cirrolux Assist' }), el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close Cirrolux Assist',
        onclick: toggleAssist }, [svg('close', 20)])]),
      el('div', { class: 'sp-body' }, [el('p', { text: 'Cirrolux Assist answers questions about your projects. It cannot change resources for you.' }),
        el('p', { text: 'Assist is not turned on for organization ' + S.org + '. Ask an administrator to turn it on.' })]),
    ]);
    document.body.insertBefore(R.assist, R.ov);
  }
  function toggleShell() {
    if (R.shell) { R.shell.remove(); R.shell = null; return; }
    R.shell = el('section', { class: 'shellpanel', 'aria-label': 'Cirrolux Shell' }, [
      el('header', {}, [el('span', { text: 'Cirrolux Shell' }), el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close Cirrolux Shell',
        onclick: toggleShell }, [svg('close', 20)])]),
      el('pre', { text: 'Welcome to Cirrolux Shell.\nProvisioning your shell machine…\nCirrolux Shell is turned off by an organization policy for ' + S.org + '.' }),
    ]);
    document.body.insertBefore(R.shell, R.ov);
  }
  // The console-wide search: it searches docs, products and resources, not the model catalog.
  function gsInput() {
    var v = R.gsInput.value.trim();
    R.gsBtn.disabled = !v;
    if (R.gsPanel) R.gsPanel.close();
    if (!v) return;
    var rows = [
      el('div', { class: 'mat-option', role: 'option', id: 'gs-all', 'aria-selected': 'false',
        onclick: function () { gsRun(); } }, [svg('search', 18), el('span', { style: 'margin-left:10px', text: 'Search all of ' + CONSOLE + ' for "' + v + '"' })]),
      el('div', { class: 'mat-option', role: 'option', id: 'gs-docs', 'aria-selected': 'false',
        onclick: function () { gsRun(); } }, [svg('notebook', 18), el('span', { style: 'margin-left:10px', text: 'Documentation results for "' + v + '"' })]),
    ];
    var panel = el('div', { class: 'select-panel', role: 'listbox', id: 'gs-panel', 'aria-label': 'Search suggestions' }, rows);
    var pane = el('div', { class: 'cdk-overlay-pane' }, [panel]);
    R.ov.appendChild(pane);
    R.gs.setAttribute('aria-expanded', 'true');
    R.gsInput.setAttribute('aria-controls', 'gs-panel');
    var o = pushOverlay({ escape: close, close: close, reposition: function () { place(pane, R.gsWrap, { width: true, gap: 2 }); } });
    o.reposition();
    var off = closeOnOutside([R.gsWrap, pane], close);
    R.gsPanel = o;
    function close() { off(); pane.remove(); dropOverlay(o); R.gs.setAttribute('aria-expanded', 'false'); R.gsInput.removeAttribute('aria-controls'); R.gsPanel = null; }
  }
  function gsKey(e) { if (e.key === 'Enter') { e.preventDefault(); gsRun(); } }
  function gsRun() {
    var v = R.gsInput.value.trim();
    if (!v) return;
    go('search.html?q=' + enc(v), 'quick');
  }

  // ─── outlined fields ───────────────────────────────────────────────────
  // Material-style outlined field: the label rests across the middle of an
  // empty control and floats into the outline once it has a value or focus.
  function skeleton(id, label, required) {
    var lab = el('label', { class: 'ff-label' + (required ? ' req' : ''), id: id + '-label', for: id, text: label });
    var wrapper = el('div', { class: 'ff-wrapper' }, [
      el('div', { class: 'ff-outline' }, [el('div', { class: 'ff-leading' }), el('div', { class: 'ff-notch' }, [lab]), el('div', { class: 'ff-trailing' })]),
    ]);
    var sub = el('div', { class: 'ff-sub', id: id + '-err', 'aria-live': 'polite' });
    var ff = el('div', { class: 'ff' + (Q.floating_labels ? '' : ' static') }, [wrapper, sub]);
    return {
      ff: ff, wrapper: wrapper, label: lab, sub: sub,
      float: function (on) { if (Q.floating_labels) ff.classList.toggle('floated', !!on); },
      error: function (msg, ctl) {
        sub.textContent = msg || '';
        ff.classList.toggle('invalid', !!msg);
        if (ctl) { if (msg) ctl.setAttribute('aria-invalid', 'true'); else ctl.removeAttribute('aria-invalid'); }
      },
    };
  }
  function textField(id, label, opts) {
    opts = opts || {};
    var k = skeleton(id, label, opts.required);
    // opts.rows: a multi-line field (a textarea in the same outline).
    var input = opts.rows ? el('textarea', { class: 'ff-input', id: id, rows: String(opts.rows), 'aria-describedby': id + '-err' })
      : el('input', { class: 'ff-input', id: id, type: opts.type || 'text', autocomplete: 'off',
        'aria-required': opts.required ? 'true' : null, 'aria-describedby': id + '-err' });
    if (opts.rows) k.wrapper.classList.add('ff-area');
    k.wrapper.appendChild(el('div', { class: 'ff-infix' }, [input]));
    function sync() { k.float(input.value || document.activeElement === input); }
    k.wrapper.addEventListener('click', function (e) { if (e.target !== input && e.target !== k.label) input.focus(); });
    input.addEventListener('focus', function () { k.ff.classList.add('focused'); sync(); });
    input.addEventListener('blur', function () { k.ff.classList.remove('focused'); sync(); });
    input.addEventListener('input', sync);
    input.addEventListener('change', sync);
    sync();
    return { node: k.ff, ctl: input, get: function () { return input.value; }, error: function (m) { k.error(m, input); } };
  }
  // The select. With overlay_selects it is Material's: a div with role
  // combobox; a click on the field (or the trigger) opens an option panel that
  // exists only while open, at the end of <body> behind a transparent
  // backdrop. Off: a native <select> in the same outline. opts.optional: not
  // a required field.
  function selectField(id, label, options, value, onChange, opts) {
    var req = !(opts && opts.optional);
    var k = skeleton(id, label, req);
    var cur = value || '';
    if (!Q.overlay_selects) {
      var sel = el('select', { class: 'ff-native' + (cur ? '' : ' empty'), id: id, 'aria-required': String(req), 'aria-describedby': id + '-err' },
        [el('option', { value: '', text: '' })].concat(options.map(function (o) { return el('option', { value: o, text: o }); })));
      sel.value = cur;
      k.wrapper.appendChild(sel);
      k.wrapper.appendChild(el('div', { class: 'ff-arrow' }, [svg('drop', 24)]));
      var syncN = function () { sel.classList.toggle('empty', !sel.value); k.float(sel.value || document.activeElement === sel); };
      sel.addEventListener('focus', function () { k.ff.classList.add('focused'); syncN(); });
      sel.addEventListener('blur', function () { k.ff.classList.remove('focused'); syncN(); });
      sel.addEventListener('change', function () { syncN(); if (onChange) onChange(sel.value); });
      syncN();
      return { node: k.ff, ctl: sel, get: function () { return sel.value; }, error: function (m) { k.error(m, sel); } };
    }
    var valueText = el('span', { class: 'ff-value-text' });
    var box = el('div', { class: 'ff-select', id: id, role: 'combobox', tabindex: '0', 'aria-haspopup': 'listbox',
      'aria-expanded': 'false', 'aria-labelledby': id + '-label', 'aria-required': String(req), 'aria-disabled': 'false',
      'aria-describedby': id + '-err' }, [el('div', { class: 'ff-trigger' }, [el('div', { class: 'ff-value' }, [valueText])])]);
    k.wrapper.appendChild(box);
    k.wrapper.appendChild(el('div', { class: 'ff-arrow' }, [svg('drop', 24)]));
    var isOpen = false, active = -1, bd = null, pane = null, panel = null, ovl = null, typed = '', typedAt = 0;
    function show() { valueText.textContent = cur; k.float(cur || isOpen || document.activeElement === box); }
    function setValue(v) {
      if (v === cur) return;
      cur = v; show();
      if (onChange) onChange(v);
    }
    function setActive(i, scroll) {
      if (!panel) return;
      active = Math.max(0, Math.min(options.length - 1, i));
      Array.prototype.forEach.call(panel.children, function (o, j) { o.classList.toggle('active', j === active); });
      var opt = panel.children[active];
      if (opt) {
        box.setAttribute('aria-activedescendant', opt.id);
        if (scroll) {
          var top = opt.offsetTop, bottom = top + opt.offsetHeight;
          if (top < panel.scrollTop) panel.scrollTop = top - 8;
          else if (bottom > panel.scrollTop + panel.clientHeight) panel.scrollTop = bottom - panel.clientHeight + 8;
        }
      }
    }
    function openPanel() {
      if (isOpen) return;
      isOpen = true;
      box.setAttribute('aria-expanded', 'true');
      k.ff.classList.add('focused');
      bd = el('div', { class: 'cdk-overlay-backdrop cdk-overlay-transparent-backdrop' });
      panel = el('div', { class: 'select-panel', role: 'listbox', id: id + '-panel', tabindex: '-1', 'aria-labelledby': id + '-label' },
        options.map(function (o, i) {
          var opt = el('div', { class: 'mat-option', role: 'option', id: id + '-opt-' + i, tabindex: '-1',
            'aria-selected': String(o === cur), 'aria-disabled': 'false' }, [el('span', { class: 'mat-option-text', text: o })]);
          opt.addEventListener('click', function () { choose(o); });
          opt.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(o); }
            else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); setActive(active + (e.key === 'ArrowDown' ? 1 : -1), true); panel.children[active].focus(); }
          });
          return opt;
        }));
      pane = el('div', { class: 'cdk-overlay-pane' }, [panel]);
      bd.addEventListener('click', function () { closePanel(true); });
      R.ov.appendChild(bd);
      R.ov.appendChild(pane);
      box.setAttribute('aria-controls', panel.id);
      ovl = pushOverlay({ escape: function () { closePanel(true); }, close: function () { closePanel(false); },
        reposition: function () { place(pane, k.wrapper, { width: true, gap: 1 }); } });
      ovl.reposition();
      var i = options.indexOf(cur);
      setActive(i < 0 ? 0 : i, false);
      if (i > 0) panel.scrollTop = Math.max(0, panel.children[i].offsetTop - 8);
      show();
    }
    function closePanel(refocus) {
      if (!isOpen) return;
      isOpen = false;
      bd.remove(); pane.remove(); dropOverlay(ovl);
      bd = pane = panel = ovl = null;
      box.setAttribute('aria-expanded', 'false');
      box.removeAttribute('aria-controls');
      box.removeAttribute('aria-activedescendant');
      if (refocus) box.focus({ preventScroll: true });
      if (document.activeElement !== box) k.ff.classList.remove('focused');
      show();
    }
    function choose(o) { setValue(o); closePanel(true); }
    function typeahead(ch, open) {
      var now = Date.now();
      typed = (now - typedAt < 700 ? typed : '') + ch.toLowerCase();
      typedAt = now;
      var start = open ? active : options.indexOf(cur);
      for (var n = 1; n <= options.length; n++) {
        var j = (start + (typed.length > 1 ? 0 : n) + options.length) % options.length;
        if (options[j].toLowerCase().indexOf(typed) === 0) {
          if (open) setActive(j, true); else setValue(options[j]);
          return;
        }
      }
    }
    // Material's form field opens its control on a click anywhere in the field;
    // the trigger opens it too.
    k.wrapper.addEventListener('click', function () { if (!isOpen) { box.focus({ preventScroll: true }); openPanel(); } });
    box.addEventListener('focus', function () { k.ff.classList.add('focused'); show(); });
    box.addEventListener('blur', function () { if (!isOpen) k.ff.classList.remove('focused'); show(); });
    box.addEventListener('keydown', function (e) {
      var key = e.key;
      if (!isOpen) {
        if (key === 'Enter' || key === ' ') { e.preventDefault(); openPanel(); }
        else if (key === 'ArrowDown' || key === 'ArrowUp') {
          e.preventDefault();
          if (e.altKey) { openPanel(); return; }
          var i = options.indexOf(cur);
          setValue(options[i < 0 ? 0 : Math.max(0, Math.min(options.length - 1, i + (key === 'ArrowDown' ? 1 : -1)))]);
        } else if (key.length === 1 && key !== ' ') typeahead(key, false);
        return;
      }
      if (key === 'ArrowDown' || key === 'ArrowUp') { e.preventDefault(); setActive(active + (key === 'ArrowDown' ? 1 : -1), true); }
      else if (key === 'Enter' || key === ' ') { e.preventDefault(); choose(options[active]); }
      else if (key === 'Tab') closePanel(false);
      else if (key.length === 1) typeahead(key, true);
    });
    show();
    return { node: k.ff, ctl: box, get: function () { return cur; }, error: function (m) { k.error(m, box); } };
  }
  // Radio buttons drawn the Material way: the native input is invisible over
  // the drawn circle, and the label beside it names it.
  function radio(name, value, id, kids, checked, onChange) {
    var input = el('input', { type: 'radio', name: name, value: value, id: id, checked: !!checked });
    input.addEventListener('change', function () { if (input.checked && onChange) onChange(value); });
    return el('div', { class: 'radio-row' }, [
      el('div', { class: 'mdc-radio' }, [input, el('div', { class: 'ring' }), el('div', { class: 'dot' })]),
      el('label', { for: id }, kids),
    ]);
  }

  // A modal dialog hides the rest of the page from assistive technology (and
  // from page readers): every other child of <body> gets aria-hidden while it
  // is open, as Angular Material's dialog does.
  function hideBehind() {
    var hidden = [];
    Array.prototype.forEach.call(document.body.children, function (c) {
      if (c === R.ov || c.tagName === 'SCRIPT' || c.getAttribute('aria-hidden') === 'true' || c.getAttribute('aria-live')) return;
      c.setAttribute('aria-hidden', 'true');
      hidden.push(c);
    });
    return function () { hidden.forEach(function (c) { c.removeAttribute('aria-hidden'); }); };
  }

  // ─── project picker ────────────────────────────────────────────────────
  function openPicker() {
    if (R.picker) return;
    closeAllOverlays();
    var st = { tab: 'recent', q: '', page: 0, offset: 0, seq: 0 };
    var input = el('input', { type: 'text', 'aria-label': 'Search projects and folders', placeholder: 'Search projects and folders', autocomplete: 'off' });
    var lens = el('span', { style: 'display:flex;color:#5e6266' }, [svg('search', 20)]);
    var backBtn = el('button', { class: 'icon-btn hidden', type: 'button', 'aria-label': 'Back out of search', onclick: exitSearch }, [svg('back', 18)]);
    var clearBtn = el('button', { class: 'icon-btn hidden', type: 'button', 'aria-label': 'Clear search', onclick: exitSearch }, [svg('close', 18)]);
    var tabs = {};
    var tabBar = el('div', { class: 'pk-tabs' }, [['recent', 'Recent'], ['starred', 'Starred'], ['all', 'All']].map(function (t) {
      tabs[t[0]] = el('button', { type: 'button', text: t[1], onclick: function () { st.tab = t[0]; st.offset = 0; loadTab(); } });
      return tabs[t[0]];
    }));
    var list = el('div', { class: 'pk-list' });
    var range = el('span', { class: 'pk-range' });
    var prev = el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Previous 10 rows', onclick: function () { st.page -= 1; loadSearch(); } }, [svg('chevLeft', 20)]);
    var next = el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Next 10 rows', onclick: function () { st.page += 1; loadSearch(); } }, [svg('chevRight', 20)]);
    var paging = el('div', { class: 'pk-paging hidden' }, [range, prev, next]);
    var orgBtn = el('button', { class: 'pk-org', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-labelledby': 'pk-org-l pk-org-v',
      onclick: function () { menu(orgBtn, [{ text: S.org, radio: true, on: true }, { text: 'No organization', radio: true, on: false,
        onclick: function () { snack('Every project of yours is in ' + S.org + '.'); } }]); } },
      [el('span', { id: 'pk-org-l', class: 'sr-only', text: 'Select from organization selector' }), el('span', { id: 'pk-org-v', text: S.org }), svg('drop', 20)]);
    var moreBtn = el('button', { class: 'icon-btn', type: 'button', 'aria-labelledby': 'pk-more-l pk-title', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      style: 'color:var(--primary)', onclick: function () { menu(moreBtn, [{ text: 'Manage resources', onclick: function () { closePicker(); go('page.html?p=resources', 'quick'); } },
        { text: 'Refresh', onclick: function () { if (st.q) loadSearch(); else loadTab(); } }], { right: true }); } },
      [el('span', { id: 'pk-more-l', class: 'sr-only', text: 'More actions' }), svg('dots', 20)]);
    var headRow = el('div', { class: 'pk-head' }, [el('h2', { id: 'pk-title', text: 'Select a resource' }),
      el('button', { class: 'btn text', type: 'button', text: 'New project', onclick: function () { closePicker(); go('page.html?p=new-project', 'quick'); } }),
      moreBtn]);
    var dlg = el('div', { class: 'dialog picker', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'pk-title', tabindex: '-1' }, [
      headRow,
      orgBtn,
      el('div', { class: 'pk-search' }, [lens, backBtn, input, clearBtn]),
      tabBar, list, paging,
      el('div', { class: 'pk-foot' }, [el('button', { class: 'btn text', type: 'button', text: 'Cancel', onclick: closePicker })]),
    ]);
    var bd = el('div', { class: 'cdk-overlay-backdrop dark' });
    var wrap = el('div', { class: 'cdk-global-wrapper' }, [el('div', { class: 'cdk-overlay-pane', style: 'position:static' }, [dlg])]);
    // A click outside the dialog closes it only without sticky_picker.
    bd.addEventListener('click', function () { if (!Q.sticky_picker) closePicker(); });
    R.ov.appendChild(bd);
    R.ov.appendChild(wrap);
    var unhide = hideBehind();
    R.chip.setAttribute('aria-expanded', 'true');
    var ovl = pushOverlay({
      // Escape leaves search mode; it closes the picker only without sticky_picker.
      escape: function () { if (st.q) exitSearch(); else if (!Q.sticky_picker) closePicker(); },
      close: closePicker,
    });
    R.picker = { close: closePicker };
    var timer = null;
    input.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var v = input.value.trim();
        if (!v) { exitSearch(); return; }
        st.q = v; st.page = 0;
        loadSearch();
      }, 300);
    });
    loadTab();
    setTimeout(function () { input.focus(); }, 0);

    function closePicker() {
      bd.remove(); wrap.remove(); dropOverlay(ovl); unhide();
      R.picker = null;
      R.chip.setAttribute('aria-expanded', 'false');
      R.chip.focus({ preventScroll: true });
    }
    function exitSearch() {
      input.value = '';
      st.q = '';
      loadTab();
    }
    function mode(searching) {
      headRow.classList.toggle('hidden', searching);
      orgBtn.classList.toggle('hidden', searching);
      lens.classList.toggle('hidden', searching);
      backBtn.classList.toggle('hidden', !searching);
      clearBtn.classList.toggle('hidden', !searching);
      tabBar.classList.toggle('hidden', searching);
      paging.classList.toggle('hidden', !searching || !Q.paged_picker);
    }
    function loadTab() {
      mode(false);
      Object.keys(tabs).forEach(function (t) { tabs[t].classList.toggle('on', t === st.tab); });
      var seq = ++st.seq;
      api('projects?tab=' + st.tab).then(function (r) {
        if (seq !== st.seq || !R.picker) return;
        list.textContent = '';
        if (st.tab === 'all') {
          var toggle = el('button', { type: 'button', 'aria-label': 'Toggle node', 'aria-expanded': 'true' }, [svg('chevDown', 18)]);
          var holder = el('div');
          toggle.addEventListener('click', function () {
            var open = toggle.getAttribute('aria-expanded') !== 'true';
            toggle.setAttribute('aria-expanded', String(open));
            holder.classList.toggle('hidden', !open);
          });
          list.appendChild(el('div', { class: 'pk-tree' }, [toggle, svg('org', 18), el('button', { type: 'button', style: 'margin-left:6px', text: S.org,
            onclick: function () { toggle.click(); } })]));
          list.appendChild(holder);
          holder.appendChild(table(r.data.rows, false));
          addMore(holder, r.data);
        } else if (!r.data.rows.length) {
          list.appendChild(el('div', { class: 'pk-msg', text: st.tab === 'starred' ? 'No starred projects. Star a project to see it here.' : 'No recent projects.' }));
        } else list.appendChild(table(r.data.rows, st.tab === 'recent'));
      });
    }
    function addMore(holder, d) {
      if (!d.more) return;
      var b = el('button', { type: 'button', text: 'More results' });
      var row = el('div', { class: 'pk-more' }, [b]);
      b.addEventListener('click', function () {
        b.textContent = 'Loading';
        b.disabled = true;
        api('projects?tab=all&offset=' + d.offset).then(function (r) {
          if (!R.picker) return;
          row.remove();
          holder.appendChild(table(r.data.rows, false, true));
          addMore(holder, r.data);
        });
      });
      holder.appendChild(row);
    }
    function loadSearch() {
      mode(true);
      var seq = ++st.seq;
      api('projects?q=' + enc(st.q) + '&page=' + st.page).then(function (r) {
        if (seq !== st.seq || !R.picker) return;
        var d = r.data;
        list.textContent = '';
        if (!d.rows.length) { list.appendChild(el('div', { class: 'pk-msg', text: 'No results for "' + st.q + '".' })); }
        else list.appendChild(table(d.rows, true));
        var a = d.page * d.size + 1, b = Math.min(d.total, (d.page + 1) * d.size);
        range.textContent = d.total ? a + ' – ' + b + ' of ' + d.total : '';
        prev.disabled = d.page <= 0;
        next.disabled = (d.page + 1) * d.size >= d.total;
        list.scrollTop = 0;
      });
    }
    function table(rows, info, bare) {
      var tb = el('tbody', {}, rows.map(function (p) {
        var star = el('button', { class: 'pk-star', type: 'button', 'aria-pressed': String(!!p.starred),
          'aria-label': (p.starred ? 'Remove star for ' : 'Add star for ') + p.name }, [svg(p.starred ? 'starOn' : 'star', 20)]);
        star.addEventListener('click', function () {
          p.starred = !p.starred;
          api('star', { project: p.id, on: p.starred });
          star.setAttribute('aria-pressed', String(p.starred));
          star.setAttribute('aria-label', (p.starred ? 'Remove star for ' : 'Add star for ') + p.name);
          star.replaceChild(svg(p.starred ? 'starOn' : 'star', 20), star.firstChild);
        });
        var name = el('button', { class: 'pk-name', type: 'button', text: p.name, onclick: function () { choose(p); } });
        var nameCell = [name];
        if (info) {
          var ib = el('button', { class: 'pk-info', type: 'button', 'aria-label': 'Ancestry information for ' + p.name }, [svg('info', 18)]);
          ib.addEventListener('click', function () {
            var pop = el('div', { class: 'pop', role: 'tooltip', text: S.org + '  ›  ' + p.name });
            ib.parentNode.style.position = 'relative';
            ib.parentNode.appendChild(pop);
            setTimeout(function () { pop.remove(); }, 2500);
          });
          nameCell.push(ib);
        }
        return el('tr', {}, [
          el('td', { class: 'cur' }, p.current ? [svg('check', 18), el('span', { class: 'sr-only', text: 'Current' })] : [el('span', { class: 'sr-only', text: 'Not current' })]),
          el('td', { class: 'ic' }, [svg('project', 18)]),
          el('td', { class: 'nm' }, nameCell),
          el('td', { class: 'ty', text: 'Project' }),
          el('td', { class: 'id', text: p.id }),
          el('td', { class: 'st' }, [star]),
        ]);
      }));
      var head = bare ? null : el('thead', {}, [el('tr', {}, [el('th', { class: 'cur' }), el('th'), el('th', { text: 'Name' }), el('th', { text: 'Type' }),
        el('th', { text: 'ID' }), el('th')])]);
      return el('table', { class: 'pk-table' }, [head, tb]);
    }
    function choose(p) {
      api('select', { project: p.id, via: 'picker' }).then(function (r) {
        if (r.status !== 200) { snack(r.data.error || 'That project could not be selected.'); return; }
        closePicker();
        setSelected(r.data.selected);
      });
    }
  }
  // A new project: the chip changes, the address carries it, and the page's
  // content reloads for it (late on the catalog, with lazy_content).
  function setSelected(sel) {
    S.selected = sel;
    updateChip();
    setTitle(TITLE);
    var u = new URL(location.href);
    if (sel) u.searchParams.set('project', sel.id); else u.searchParams.delete('project');
    history.replaceState(null, '', u.pathname + u.search);
    schedule(viewName() === 'catalog' ? T.first : T.quick);
  }

  // ─── model catalog ─────────────────────────────────────────────────────
  function pageState() {
    try { return JSON.parse(params().get('pageState') || '{}') || {}; } catch (e) { return {}; }
  }
  function catalogUrl(ps) {
    var clean = {};
    if (ps.q) clean.q = ps.q;
    if (ps.f && ps.f.length) clean.f = ps.f;
    if (ps.s) clean.s = ps.s;
    if (ps.d) clean.d = ps.d;
    return 'catalog.html' + (Object.keys(clean).length ? '?pageState=' + enc(JSON.stringify(clean)) : '');
  }
  VIEWS.catalog = function (seq) {
    setCrumbs(BASE_CRUMBS);
    setTitle('Model catalog');
    if (!S.selected) {
      if (Q.lazy_content) {
        // The real console draws nothing here until a project is chosen.
        setCard({ title: null, body: [] });
      } else {
        setCard({ title: 'Model catalog', body: [el('div', { class: 'empty' }, [el('h2', { text: 'To view the model catalog, select a project.' }),
          el('button', { class: 'btn primary', type: 'button', text: 'Select project', onclick: openPicker })])] });
      }
      shown('catalog');
      return;
    }
    var ps = pageState();
    api('catalog?project=' + enc(pid()) + '&q=' + enc(ps.q || '') + '&f=' + enc((ps.f || []).join(','))).then(function (r) {
      if (!current(seq)) return;
      drawCatalog(r.data, ps);
      shown('catalog');
    });
  };
  function drawCatalog(d, ps) {
    var models = d.models.slice();
    if (ps.d === 'serverless') models = models.filter(function (m) { return m.kind !== 'open'; });
    if (ps.d === 'self') models = models.filter(function (m) { return m.kind === 'open'; });
    if (ps.s === 'name') models.sort(function (a, b) { return a.name.localeCompare(b.name); });
    var input = el('input', { type: 'text', role: 'combobox', 'aria-label': 'Search models', placeholder: 'Search models', autocomplete: 'off',
      'aria-autocomplete': 'list', 'aria-expanded': 'false' });
    input.value = ps.q || '';
    var clearBtn = el('button', { class: 'icon-btn' + (input.value ? '' : ' hidden'), type: 'button', 'aria-label': 'Clear search', style: 'width:30px;height:30px',
      onclick: function () { go(catalogUrl({ f: ps.f, s: ps.s, d: ps.d }), 'now'); } }, [svg('close', 18)]);
    var box = el('div', { class: 'msearch' }, [svg('search', 20), input, clearBtn]);
    var boxWrap = el('div', { class: 'msearch-wrap' }, [box]);
    suggestions(input, boxWrap, clearBtn, ps);
    var sortBtn = el('button', { class: 'toggle-chip', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      text: 'Sorting: ' + (ps.s === 'name' ? 'Name' : 'Trending') });
    sortBtn.addEventListener('click', function () {
      menu(sortBtn, [{ text: 'Trending', radio: true, on: ps.s !== 'name', onclick: function () { go(catalogUrl({ q: ps.q, f: ps.f, d: ps.d }), 'now'); } },
        { text: 'Name', radio: true, on: ps.s === 'name', onclick: function () { go(catalogUrl({ q: ps.q, f: ps.f, d: ps.d, s: 'name' }), 'now'); } }]);
    });
    function deploy(kind, label) {
      return el('button', { class: 'toggle-chip', type: 'button', 'aria-pressed': String(ps.d === kind), text: label,
        onclick: function () { go(catalogUrl({ q: ps.q, f: ps.f, s: ps.s, d: ps.d === kind ? '' : kind }), 'now'); } });
    }
    var filters = ps.f || [];
    var facets = el('div', { class: 'facets', role: 'region', 'aria-label': 'Filters' }, d.facets.map(function (f) {
      return el('div', { class: 'facet' }, [el('h3', { text: f.title })].concat(f.values.map(function (v) {
        var key = f.id + ':' + v.value, on = filters.indexOf(key) >= 0;
        return el('button', { type: 'button', 'aria-pressed': String(on), onclick: function () {
          var nf = on ? filters.filter(function (x) { return x !== key; }) : filters.concat([key]);
          go(catalogUrl({ q: ps.q, f: nf, s: ps.s, d: ps.d }), 'now');
        } }, [el('span', { text: v.value }), el('span', { class: 'n', text: String(v.count) })]);
      })));
    }));
    var filtered = !!(ps.q || filters.length || ps.d);
    var heading = ps.q ? 'Search results for "' + ps.q + '"' : (filtered ? 'Filtered models' : 'All models');
    var grid = el('div', { class: 'grid' }, models.map(card));
    var results = el('div', { class: 'results' }, [
      el('div', { class: 'results-head' }, [el('h2', { text: heading + ' (' + models.length + ')' }),
        filtered ? el('button', { class: 'btn text', type: 'button', text: 'Clear all filters', onclick: function () { go('catalog.html', 'now'); } }) : null]),
      models.length ? grid : el('div', { class: 'empty' }, [el('h2', { text: 'No models match your search.' }), el('p', { text: 'Try fewer words, or clear the filters.' })]),
    ]);
    var banner = null;
    if (Q.api_banner && !d.apis) {
      var enableApis = el('button', { class: 'btn text', type: 'button', text: 'Enable APIs' });
      banner = el('div', { class: 'banner', role: 'region', 'aria-label': 'Enable APIs' }, [svg('info', 20),
        el('span', { class: 'grow', text: 'Enable APIs to access full platform capabilities, such as tuning, batch inference and evaluation.' }),
        enableApis,
        el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Dismiss banner', style: 'width:32px;height:32px',
          onclick: function () { banner.remove(); } }, [svg('close', 18)])]);
      enableApis.addEventListener('click', function () {
        enableApis.disabled = true;
        enableApis.textContent = 'Enabling…';
        api('apis', { project: pid() }).then(function () {
          banner.textContent = '';
          banner.appendChild(el('span', { class: 'grow', text: 'APIs enabled for ' + pname() + '.' }));
          setTimeout(function () { banner.remove(); }, 2500);
        });
      });
    }
    setCard({
      title: 'Model catalog',
      actions: [el('button', { class: 'btn outlined', type: 'button', onclick: function () { go('page.html?p=deploy', 'quick'); } }, [svg('plus', 18), 'Deploy model']),
        el('a', { class: 'btn text', href: 'page.html?p=endpoints', 'data-spa': 'quick', text: 'View my endpoints & models' })],
      body: [el('div', { class: 'cat' }, [
        banner,
        el('p', { class: 'cat-intro' }, ['Discover, test and deploy models from Cirrolux and its partners. ',
          ext('Learn more about the model catalog', docs('model-catalog'))]),
        el('div', { class: 'cat-search' }, [boxWrap, sortBtn, deploy('serverless', 'Serverless'), deploy('self', 'Self-deployed')]),
        el('div', { class: 'cat-cols' }, [facets, results]),
      ])],
    });
  }
  // A model card. With title_only_links only the name inside it is a link; the
  // card itself does nothing. Off: the whole card is the link.
  function card(m) {
    var href = 'model.html?model=' + m.key;
    var name = Q.title_only_links ? el('a', { class: 'mcard-name', href: href, 'data-spa': 'swap', text: m.name })
      : el('span', { class: 'mcard-name', text: m.name });
    var kids = [
      el('div', { class: 'mcard-top' }, [el('div', { class: 'plogo', style: 'background:' + m.color, 'aria-hidden': 'true', text: m.mark }),
        el('div', {}, [name, el('div', { class: 'mcard-pub', text: m.publisher })])]),
      el('p', { class: 'mcard-desc', text: m.desc }),
      el('div', { class: 'tags' }, m.tasks.slice(0, 2).concat([m.collection]).map(function (t) { return el('span', { class: 'tag', text: t }); })),
      m.kind === 'partner' ? el('div', { class: 'status' + (m.enabled ? ' on' : '') }, m.enabled ? [svg('check', 16), 'Enabled'] : ['Not enabled']) : null,
    ];
    return Q.title_only_links ? el('div', { class: 'mcard' }, kids) : el('a', { class: 'mcard', href: href, 'data-spa': 'swap' }, kids);
  }
  // Suggestions under "Search models": matching models and a "Full text
  // search" row, in a panel right after the field (inline, as on the real
  // console). Return runs the search.
  function suggestions(input, box, clearBtn, ps) {
    var o = null, panel = null, active = -1, timer = null;
    function close() {
      if (!o) return;
      o.off(); panel.remove(); dropOverlay(o.ovl); o = null; panel = null; active = -1;
      input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); input.removeAttribute('aria-controls');
    }
    function run() { var v = input.value.trim(); close(); go(catalogUrl({ q: v, f: ps.f, s: ps.s, d: ps.d }), 'now'); }
    function open(rows) {
      close();
      panel = el('div', { class: 'select-panel', role: 'listbox', id: 'model-suggest', 'aria-label': 'Suggested models' },
        rows.map(function (s, i) {
          return el('div', { class: 'mat-option', role: 'option', id: 'sugg-' + i, 'aria-selected': 'false', style: 'height:auto;padding:6px 16px;display:block',
            onclick: function () { close(); go('model.html?model=' + s.key, 'swap'); } },
            [el('div', { style: 'font-weight:500', text: s.name }), el('div', { style: 'font-size:12px;color:#5e6266;overflow:hidden;text-overflow:ellipsis', text: s.desc })]);
        }).concat([el('div', { class: 'mat-option', role: 'option', id: 'sugg-full', 'aria-selected': 'false', onclick: run },
          [svg('search', 18), el('span', { style: 'margin-left:10px', text: 'Full text search' })])]));
      box.appendChild(panel);
      var ovl = pushOverlay({ escape: close, close: close });
      o = { ovl: ovl, off: closeOnOutside([box], close) };
      input.setAttribute('aria-expanded', 'true');
      input.setAttribute('aria-controls', 'model-suggest');
    }
    input.addEventListener('input', function () {
      clearBtn.classList.toggle('hidden', !input.value);
      clearTimeout(timer);
      var v = input.value.trim();
      if (!v) { close(); return; }
      timer = setTimeout(function () {
        api('suggest?q=' + enc(v)).then(function (r) { if (input.value.trim() === v && document.body.contains(input)) open(r.data); });
      }, 250);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (panel && active >= 0) { panel.children[active].click(); return; }
        run();
      } else if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && panel) {
        e.preventDefault();
        var n = panel.children.length;
        active = (active + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
        Array.prototype.forEach.call(panel.children, function (c, j) { c.classList.toggle('active', j === active); });
        input.setAttribute('aria-activedescendant', panel.children[active].id);
      }
    });
  }

  // ─── model page ────────────────────────────────────────────────────────
  VIEWS.model = function (seq) {
    var key = params().get('model') || '';
    var slug = key.split('/').pop();
    setCrumbs(BASE_CRUMBS.concat([['Model: ' + slug, 'model.html?model=' + key]]));
    api('model?model=' + enc(key) + '&project=' + enc(pid())).then(function (r) {
      if (!current(seq)) return;
      if (r.status !== 200) { errorCard('Model not found', r.data.error, ['Back to the model catalog', 'catalog.html']); shown('model'); return; }
      drawModel(r.data, key);
      shown('model');
    });
  };
  function drawModel(m, key) {
    setTitle(m.name);
    var use = 'standard';
    var program = null;
    if (Q.program_radio && m.pub === 'anthropic') {
      var onUse = function (v) { use = v; api('use_case', { model: key, project: pid(), choice: v }); };
      program = el('div', { class: 'program', role: 'radiogroup', 'aria-label': 'Use case' }, [
        el('p', {}, ['The Verified Security Research Program lets vetted security teams run high-risk, dual-use work on Claude with fewer automatic refusals and an extra review. ',
          ext('Learn more', docs('verified-research'))]),
        radio('usecase', 'standard', 'uc-standard', [el('span', { class: 'rt', text: 'Standard use cases' }), ' ',
          el('span', { class: 'rh', text: 'This model is ready to use for your project.' })], true, onUse),
        el('div', {}, [radio('usecase', 'program', 'uc-program', [el('span', { class: 'rt', text: 'Verified Security Research Program' }), ' ',
          el('span', { class: 'rh', text: 'High-risk or dual-use security work needs extra review steps.' })], false, onUse),
        el('div', { style: 'margin:-10px 0 0 34px' }, [ext('How do I know if my use case is high risk or dual use?', docs('dual-use'))])]),
      ]);
    }
    var btns = [];
    var nb = function (enabled) {
      return el('button', { class: 'btn outlined', type: 'button', disabled: !enabled, onclick: function () { window.open(docs('notebook'), '_blank'); } },
        [el('span', { class: 'co', 'aria-hidden': 'true', text: 'NB' }), 'Open Notebook']);
    };
    var viewCode = el('button', { class: 'btn outlined', type: 'button', text: 'View Code', onclick: function () { jump('try'); } });
    if (m.kind === 'partner') {
      if (m.enabled) btns = [nb(true), viewCode];
      else {
        var enableBtn = el('button', { class: 'btn primary', type: 'button', text: 'Enable' });
        enableBtn.addEventListener('click', function () {
          enableBtn.disabled = true;
          api('enable', { model: key, project: pid(), use_case: use }).then(function (r) {
            enableBtn.disabled = false;
            if (r.data.go) go(r.data.go, 'quick');
            else if (r.data.enabled) schedule(0);
            else snack(r.data.error || 'This model could not be enabled.');
          });
        });
        btns = [enableBtn, nb(false), viewCode];
      }
    } else if (m.kind === 'first') {
      btns = [el('button', { class: 'btn primary', type: 'button', text: 'Open in Studio', onclick: function () { go('page.html?p=studio', 'quick'); } }), viewCode];
    } else {
      btns = [el('button', { class: 'btn primary', type: 'button', text: 'Deploy', onclick: function () { go('page.html?p=deploy', 'quick'); } }), nb(true), viewCode];
    }
    var secs = [['overview', 'Overview'], ['details', 'Model Details'], ['features', 'Feature Support']];
    if (m.price) secs.push(['pricing', 'Pricing']);
    secs.push(['usecases', 'Use Cases'], ['documentation', 'Documentation']);
    var tabBtns = {};
    var tabs = el('div', { class: 'tabs' }, secs.map(function (s, i) {
      tabBtns[s[0]] = el('button', { type: 'button', class: i ? '' : 'on', text: s[1], onclick: function () { jump(s[0]); } });
      return tabBtns[s[0]];
    }));
    function jump(id) {
      Object.keys(tabBtns).forEach(function (k) { tabBtns[k].classList.toggle('on', k === id); });
      var sec = document.getElementById('sec-' + id);
      if (sec) R.body.scrollTop = sec.offsetTop - 12;
    }
    function sec(id, title, kids) { return el('section', { class: 'mp-sec', id: 'sec-' + id }, [el('h2', { text: title })].concat(kids)); }
    var details = el('table', { class: 'ktable' }, [el('tbody', {}, [
      el('tr', {}, [el('th', { text: 'Property' }), el('th', { text: m.name })]),
      el('tr', {}, [el('td', { text: 'Model Name' }), el('td', { text: m.slug })]),
      el('tr', {}, [el('td', { text: 'Release Date' }), el('td', { text: m.released })]),
      el('tr', {}, [el('td', { text: 'Token Limits' }), el('td', {}, ['Inputs: ' + m.inputs, el('br'), 'Output: ' + m.output])]),
      el('tr', {}, [el('td', { text: 'Supported Data Types' }), el('td', {}, ['Inputs: ' + m.data_in, el('br'), 'Output: ' + m.data_out])]),
    ])]);
    var features = m.features.map(function (f) { return el('li', {}, [ext(f, docs(f.toLowerCase().replace(/\W+/g, '-')))]); });
    var parts = [
      el('div', { class: 'mp-head' }, [el('div', { class: 'plogo', style: 'background:' + m.color, 'aria-hidden': 'true', text: m.mark }),
        el('div', {}, [el('h1', { class: 'mp-name', text: m.name }), el('p', { class: 'mp-desc', text: m.desc }),
          m.enabled ? el('div', { class: 'mp-state' }, [svg('check', 18), 'Enabled for ' + pname()]) : null])]),
      el('div', { class: 'mp-btns' }, btns),
      tabs,
      sec('overview', 'Overview', [el('p', { text: m.overview })]),
      sec('details', 'Model Details', [el('p', { text: 'Details are as follows' }), details]),
      sec('features', 'Feature Support', [el('ul', {}, features)]),
    ];
    if (m.price) {
      parts.push(sec('pricing', 'Pricing', [el('p', { text: 'Prices per 1 million tokens, billed by ' + CONSOLE + ' Billing.' }),
        el('table', { class: 'ktable' }, [el('tbody', {}, [
          el('tr', {}, [el('th', { text: 'Usage' }), el('th', { text: 'Price' })]),
          el('tr', {}, [el('td', { text: 'Input' }), el('td', { text: '$' + m.price[0].toFixed(2) })]),
          el('tr', {}, [el('td', { text: 'Output' }), el('td', { text: '$' + m.price[1].toFixed(2) })]),
          el('tr', {}, [el('td', { text: 'Batch input' }), el('td', { text: '$' + (m.price[0] / 2).toFixed(2) })]),
          el('tr', {}, [el('td', { text: 'Batch output' }), el('td', { text: '$' + (m.price[1] / 2).toFixed(2) })]),
        ])])]));
    }
    parts.push(
      sec('usecases', 'Use Cases', [el('ul', {}, ['Agents that plan and act across tools', 'Coding assistants and code review',
        'Research and analysis over long documents', 'Customer support with grounded answers'].map(function (t) { return el('li', { text: t }); }))]),
      sec('documentation', 'Documentation', [el('ul', {}, [ext(m.name + ' model card', docs('model-card')), ext('Messages API reference', docs('messages-api')),
        ext('Prompting guide', docs('prompting')), ext('Terms of service', docs('terms'))].map(function (a) { return el('li', {}, [a]); }))]),
      sec('try', 'Try it now', [el('p', {}, ['Before you start: a ', ext('Cirrolux Cloud project', docs('projects')), ', the ', ext(PLATFORM + ' API enabled', docs('enable-api')),
        ', and the ', ext('Cirrolux CLI installed', docs('cli')), '.']),
        code('Try it now', 'pip install cirrolux-ai'),
        code('Try ' + m.name, 'from cirrolux.ai import ModelClient\nclient = ModelClient(project="YOUR_PROJECT_ID", region="global")\nreply = client.messages.create(model="' + m.slug + '", max_tokens=1024,\n    messages=[{"role": "user", "content": "Write a haiku about clouds."}])\nprint(reply.text)')]));
    setCard({ back: backLink('catalog.html', 'Back to model catalog page', 'swap'), title: m.name, body: [program, el('div', { class: 'mp' }, parts)] });
  }
  function code(label, text) {
    var ta = el('textarea', { class: 'code', readonly: true, rows: String(text.split('\n').length), 'aria-label': label + ' (' + text.split('\n')[0] + ')' });
    ta.value = text;
    return el('div', { class: 'code-row' }, [ta, el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Copy code snippet for ' + label,
      onclick: function () { snack('Copied to clipboard.'); } }, [svg('copy', 20)])]);
  }

  // ─── enablement questionnaire ──────────────────────────────────────────
  VIEWS.questionnaire = function (seq) {
    var key = params().get('model') || '';
    setCrumbs(BASE_CRUMBS.concat([['Questionnaire', 'questionnaire.html?model=' + key]]));
    api('questionnaire?model=' + enc(key) + '&project=' + enc(pid())).then(function (r) {
      if (!current(seq)) return;
      if (r.status !== 200) { errorCard('Enablement', r.data.error, ['Back to the model catalog', 'catalog.html']); shown('questionnaire'); return; }
      drawQuestionnaire(r.data, key);
      shown('questionnaire');
    });
  };
  function drawQuestionnaire(d, key) {
    var m = d.model, pub = m.publisher, fam = m.family;
    setTitle(m.name + ' enablement');
    var f = {
      name: textField('q-name', 'Business name', { required: true }),
      website: textField('q-website', 'Business website', { required: true }),
      email: textField('q-email', 'Contact email address', { required: true, type: 'email' }),
      hq: selectField('q-hq', 'Where is your Business headquartered', d.countries, ''),
      industry: selectField('q-industry', 'Industry', d.industries, d.preset.industry || ''),
      users: selectField('q-users', 'Who are your intended users of ' + fam + ' models', d.users, ''),
      use_cases: textField('q-usecases', 'What are your intended use cases for ' + fam + ' models', { required: true }),
      aup_detail: textField('q-aupdetail', 'If yes, please describe how you plan to meet the additional requirements.', {}),
    };
    var aupErr = el('div', { class: 'q-err', role: 'alert' });
    var radios = el('div', { class: 'q-radios', role: 'radiogroup', 'aria-label': 'Additional requirements' }, [
      radio('aup', 'yes', 'aup-yes', ['Yes'], false), radio('aup', 'no', 'aup-no', ['No'], false)]);
    var col = el('div', { class: 'q-col' }, [
      el('div', { class: 'callout' }, [PLATFORM + ' ', ext('recommends', docs('logging')),
        ' that you turn on request and response logging for partner models, so you can review what your project sends to them and what they return.',
        el('div', {}, [el('a', { class: 'btn outlined', href: docs('logging'), target: '_blank', rel: 'noopener' }, ['Set up logging', svg('ext', 13, 'ext'),
          el('span', { class: 'sr-only', text: ' external, opens new window' })])])]),
      el('div', { class: 'infobox' }, [svg('infoFilled', 22), el('div', {}, ['This model supports features such as web search, which need the model to reach the public internet. See the ',
        ext('documentation', docs('org-policy')), ' to learn how to block that access for this model with organization policy constraints.'])]),
      el('h2', { text: 'This third-party model is licensed by ' + pub + ' and offered under ' + pub + "'s terms of service." }),
      el('p', {}, ['Fill out this form to request access. ' + CONSOLE + ' handles what you submit here, with your account and your project ID, under the ',
        ext('Cirrolux Cloud Privacy Notice', docs('privacy')), '. ' + pub + ' approves access to its ' + fam + ' models on ' + PLATFORM +
        ', so you agree that Cirrolux may share what you submit, your project number and your billing account ID with ' + pub +
        ' to process your request. ', ext(pub + "'s Privacy Policy", docs('publisher-privacy')), ' explains how ' + pub + ' handles that data.']),
      el('div', { class: 'q-fields' }, [f.name.node, f.website.node, f.email.node, f.hq.node, f.industry.node, f.users.node, f.use_cases.node]),
      el('p', { class: 'q-question' }, ['Do any of your use cases have additional requirements under ' + pub + "'s Acceptable ", ext('Use Policy', docs('acceptable-use')),
        '? These include use cases that give legal, medical or financial advice to consumers, and chatbots that talk directly with consumers.']),
      radios, aupErr, f.aup_detail.node,
    ]);
    var next = el('button', { class: 'btn primary', type: 'button', text: 'Next' });
    var cancel = el('button', { class: 'btn text dark', type: 'button', text: 'Cancel', onclick: function () { go('model.html?model=' + key, 'quick'); } });
    next.addEventListener('click', function () {
      var chosen = document.querySelector('input[name=aup]:checked');
      var values = { name: f.name.get(), website: f.website.get(), email: f.email.get(), hq: f.hq.get(), industry: f.industry.get(),
        users: f.users.get(), use_cases: f.use_cases.get(), aup: chosen ? chosen.value : '', aup_detail: f.aup_detail.get() };
      next.disabled = true;
      api('questionnaire', { model: key, project: pid(), values: values }).then(function (r) {
        next.disabled = false;
        Object.keys(f).forEach(function (k) { f[k].error(''); });
        aupErr.textContent = '';
        if (r.status === 200) { go(r.data.go, 'quick'); return; }
        var errs = (r.data && r.data.errors) || {};
        if (!Object.keys(errs).length) { snack((r.data && r.data.error) || 'Something went wrong.'); return; }
        Object.keys(errs).forEach(function (k) { if (f[k]) f[k].error(errs[k]); });
        if (errs.aup) aupErr.textContent = errs.aup;
        var first = document.querySelector('.ff.invalid') || (errs.aup ? radios : null);
        if (first) first.scrollIntoView({ block: 'center' });
      });
    });
    // Back goes to the previous page of the app, or to the model's page when the
    // questionnaire was opened directly (never out of the console).
    var back = function () { if (pushed > 0) history.back(); else go('model.html?model=' + key, 'quick'); };
    setCard({ back: el('button', { class: 'back', type: 'button', 'aria-label': 'Back to previous page', onclick: back }, [svg('back', 22)]),
      title: m.name + ' enablement', body: [col], foot: [next, cancel] });
  }

  // ─── terms and agreements ──────────────────────────────────────────────
  VIEWS.terms = function (seq) {
    var key = params().get('model') || '';
    setCrumbs(BASE_CRUMBS.concat([['Terms and agreements', 'terms.html?model=' + key]]));
    api('terms?model=' + enc(key) + '&project=' + enc(pid())).then(function (r) {
      if (!current(seq)) return;
      if (r.status === 409) { errorCard('Terms and agreements', r.data.error, ['Go to the enablement questionnaire', r.data.go]); shown('terms'); return; }
      if (r.status !== 200) { errorCard('Terms and agreements', r.data.error, ['Back to the model catalog', 'catalog.html']); shown('terms'); return; }
      drawTerms(r.data, key);
      shown('terms');
    });
  };
  function drawTerms(d, key) {
    var m = d.model;
    setTitle(m.name + ' – Terms');
    var manage = function () {
      return el('button', { class: 'btn primary', type: 'button', text: 'Manage on ' + PLATFORM, onclick: function () { go('agreements.html', 'quick'); } });
    };
    var section = el('div');
    if (d.agreed) {
      section.appendChild(el('div', { class: 'success', role: 'status' }, [el('h3', {}, [svg('checkCircle', 26), 'You already agreed to these terms']),
        el('p', { text: 'You agreed on ' + d.agreed_on + '. ' + m.name + ' can be used in project ' + pname() + '.' }), manage()]));
    } else {
      var box = el('input', { type: 'checkbox', id: 'terms-ack' });
      var agree = el('button', { class: 'btn primary', type: 'button', disabled: true, 'aria-label': 'Agree to the terms and agreements before continuing' }, ['Agree']);
      var cancel = el('button', { class: 'btn text dark', type: 'button', text: 'Cancel', onclick: function () { go('model.html?model=' + key, 'quick'); } });
      box.addEventListener('change', function () { agree.disabled = !box.checked; });
      agree.addEventListener('click', function () {
        agree.disabled = true; cancel.disabled = true; box.disabled = true;
        agree.textContent = '';
        agree.appendChild(el('span', { class: 'spin', 'aria-hidden': 'true' }));
        agree.appendChild(document.createTextNode('Loading'));
        api('agree', { model: key, project: pid(), accepted: box.checked }).then(function (r) {
          if (r.status !== 200) {
            agree.textContent = 'Agree'; agree.disabled = !box.checked; cancel.disabled = false; box.disabled = false;
            snack((r.data && r.data.error) || 'The agreement failed. Try again.');
            return;
          }
          section.textContent = '';
          section.appendChild(el('div', { class: 'success', role: 'status' }, [el('h3', {}, [svg('checkCircle', 26), 'Successfully purchased ' + m.name]),
            el('p', { text: 'Now that you have agreed to the terms, you can enable and use ' + m.name + ' in project ' + pname() + '.' }), manage()]));
          section.scrollIntoView({ block: 'center' });
        });
      });
      section.appendChild(el('div', { class: 'check-row' }, [
        el('div', { class: 'mdc-checkbox' }, [box, el('div', { class: 'box' }), el('div', { class: 'tick' }, [svg('tick', 18)])]),
        el('label', { for: 'terms-ack', text: 'By purchasing, deploying, accessing, or using this product, you acknowledge that you have read and accept the publisher\'s terms linked above, that ' +
          CONSOLE + ' bills your use under the Cirrolux Cloud Partner Terms, and that Cirrolux may share your contact and usage details with ' + m.publisher + '.' }),
      ]));
      section.appendChild(el('div', { class: 'tp-btns' }, [agree, cancel]));
    }
    var rows = d.skus.map(function (s) { return el('tr', {}, [el('td', { text: s.sku }), el('td', { text: s.region }), el('td', { text: s.price })]); });
    setCard({
      back: backLink('model.html?model=' + key, 'Back to ' + m.name), title: m.name,
      body: [el('div', { class: 'tp' }, [
        el('div', { class: 'tp-head' }, [el('div', { class: 'plogo', style: 'background:' + m.color, 'aria-hidden': 'true', text: m.mark }),
          el('div', {}, [el('h2', { text: m.name }), el('div', { class: 'sub', text: 'By ' + m.publisher + ' · Partner model · Project ' + pname() })])]),
        el('p', { text: 'Usage of ' + m.name + ' is billed to the Cloud Billing account of project ' + pname() + ' at these prices.' }),
        el('h3', { text: 'Pricing' }),
        el('table', { class: 'sku' }, [el('thead', {}, [el('tr', {}, [el('th', { text: 'SKU' }), el('th', { text: 'Location' }), el('th', { text: 'Price' })])]), el('tbody', {}, rows)]),
        el('h3', { text: 'Terms and agreements' }),
        el('p', {}, [m.name + ' is offered by ' + m.publisher + '. Your use is governed by the ', ext(m.publisher + ' Commercial Terms of Service', docs('publisher-terms')),
          ' and the ', ext('Cirrolux Cloud Partner Terms', docs('partner-terms')), '.']),
        section,
      ])],
    });
  }

  // ─── agreements ────────────────────────────────────────────────────────
  VIEWS.agreements = function (seq) {
    setCrumbs([[PLATFORM, 'page.html?p=dashboard'], ['Models', 'page.html?p=models'], ['Agreements', 'agreements.html']]);
    setTitle('Agreements');
    api('agreements?project=' + enc(pid())).then(function (r) {
      if (!current(seq)) return;
      var rows = (r.data.rows || []).map(function (a) {
        return el('tr', {}, [el('td', {}, [el('a', { href: 'model.html?model=' + a.key, 'data-spa': 'swap', text: a.name })]), el('td', { text: a.publisher }),
          el('td', {}, [el('span', { class: 'status on', style: 'margin:0' }, [svg('check', 16), a.status])]), el('td', { text: a.date }), el('td', { text: a.by })]);
      });
      setCard({ title: 'Agreements', body: [el('div', { class: 'pad' }, [
        el('p', { text: S.selected ? 'Agreements for partner models in project ' + pname() + '.' : 'Select a project to see its agreements.' }),
        el('table', { class: 'dtable' }, [el('thead', {}, [el('tr', {}, ['Product', 'Publisher', 'Status', 'Agreed on', 'Agreed by'].map(function (h) { return el('th', { text: h }); }))]),
          el('tbody', {}, rows.length ? rows : [el('tr', {}, [el('td', { colspan: '5', text: 'No agreements yet.' })])])]),
      ])] });
      shown('agreements');
    });
  };

  // ─── special program ───────────────────────────────────────────────────
  VIEWS.program = function (seq) {
    var key = params().get('model') || '';
    setCrumbs(BASE_CRUMBS.concat([['Verified Security Research Program', 'program.html?model=' + key]]));
    setTitle('Verified Security Research Program');
    api('program?model=' + enc(key) + '&project=' + enc(pid())).then(function (r) {
      if (!current(seq)) return;
      var ta = el('textarea', { class: 'code', rows: '5', id: 'program-desc', 'aria-label': 'Describe the security research you plan to do' });
      setCard({ back: backLink('model.html?model=' + key, 'Back to model page'), title: 'Verified Security Research Program', body: [el('div', { class: 'pad', style: 'max-width:720px' }, [
        el('p', { text: 'Apply for the Verified Security Research Program for ' + ((r.data.model && r.data.model.name) || 'this model') + '. Approved teams can run high-risk, dual-use security work with an extra review of each project. We review applications within 5 business days.' }),
        el('label', { for: 'program-desc', text: 'Describe the security research you plan to do' }), ta,
        el('div', { style: 'margin-top:14px' }, [el('button', { class: 'btn primary', type: 'button', text: 'Submit application', onclick: function () {
          api('program', { model: key, project: pid(), text: ta.value }).then(function () { snack('Application submitted. We review applications within 5 business days.'); });
        } })]),
      ])] });
      shown('program');
    });
  };

  // ─── new access key ────────────────────────────────────────────────────
  // A key form laid out like a code host's fine-grained token form. The page
  // reports the whole form on every change (keys/draft) and only the server's
  // copy is scored. Two quirks shape the "Add permissions" panel: with
  // portal_panel it renders in a container before the app, so its controls
  // come first in page order; with toggle_opener a second click on the button
  // closes it.
  var K = null;              // the open key form
  var draftBusy = false, draftAgain = false;
  function keyForm() {
    if (!K) return { cleared: true };
    function perms(list) { return list.map(function (p) { return { name: p.name, level: p.level }; }); }
    return { name: K.f.name.get(), description: K.f.description.get(), owner: K.owner, expiration: K.days,
      message: K.f.message.get(), access: K.access, projects: K.projects.slice(),
      permissions: { project: perms(K.perms.project), org: perms(K.perms.org) } };
  }
  // One request at a time, so the server never keeps an older form than the
  // last one sent; changes made meanwhile go out together when it answers.
  function saveDraft() {
    document.body.setAttribute('data-draft', 'pending');
    if (draftBusy) { draftAgain = true; return; }
    draftBusy = true; draftAgain = false;
    api('keys/draft', keyForm()).then(draftDone, draftDone);
  }
  function draftDone() {
    draftBusy = false;
    if (draftAgain) saveDraft(); else document.body.setAttribute('data-draft', 'saved');
  }
  function leaveKeys() { if (K) { K = null; saveDraft(); } }

  VIEWS.keys = function (seq) {
    setCrumbs([[PLATFORM, 'page.html?p=dashboard'], ['API keys', 'page.html?p=api-key'], ['New access key', 'keys.html']]);
    setTitle('New access key');
    api('keys').then(function (r) {
      if (!current(seq)) return;
      drawKeys(r.data);
      shown('keys');
      saveDraft();
    });
  };
  function slug(s) { return String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-'); }
  function drawKeys(d) {
    var k = K = { owner: d.owner, days: d.expiration, access: 'none', projects: [], perms: { project: [], org: [] } };
    var exp = {};
    d.expirations.forEach(function (x) { exp[x.label] = x; exp[x.days] = x; });
    function ownerOf(name) { return d.owners.filter(function (o) { return o.name === name; })[0] || {}; }
    function projectOf(id) { return d.projects.filter(function (p) { return p.id === id; })[0]; }
    function help(text, id) { return el('p', { class: 'k-help', id: id || null, text: text }); }

    k.f = {
      name: textField('k-name', 'Key name', { required: true }),
      description: textField('k-description', 'Description', {}),
      message: textField('k-message', 'Request message', { rows: 3 }),
    };
    ['name', 'description', 'message'].forEach(function (n) { k.f[n].ctl.addEventListener('input', saveDraft); });
    var owner = selectField('k-owner', 'Resource owner', d.owners.map(function (o) { return o.name; }), d.owner,
      function (v) { k.owner = v; syncOwner(); saveDraft(); });
    var expHelp = help('', 'k-expiration-help');
    var expiration = selectField('k-expiration', 'Expiration', d.expirations.map(function (x) { return x.label; }), exp[d.expiration].label,
      function (v) { k.days = exp[v].days; syncExpiry(); saveDraft(); });
    function syncExpiry() { expHelp.textContent = 'The key stops working on ' + exp[k.days].date + '.'; }
    var msgBox = el('div', { class: 'k-msg' }, [k.f.message.node,
      help('', 'k-message-help')]);
    function syncOwner() {
      var o = ownerOf(k.owner);
      msgBox.classList.toggle('hidden', !o.approval);
      msgBox.lastChild.textContent = k.owner + ' approves every key that reaches its projects. Tell its administrators what this key is for.';
    }

    // ─── project access ──────────────────────────────────────────────────
    var badge = el('span', { class: 'k-badge hidden' });
    var selectBtn = el('button', { class: 'btn outlined', id: 'k-select-projects', type: 'button', onclick: openProjects },
      [svg('project', 18), 'Select projects', badge]);
    var chosen = el('div', { class: 'k-chosen' });
    var pickBox = el('div', { class: 'k-pick hidden' }, [selectBtn, chosen]);
    var ACCESS = [['none', 'No project access', 'The key reaches no project.'],
      ['all', 'All projects', 'Every project the owner has now, and every project it creates later.'],
      ['selected', 'Only select projects', 'Pick the projects the key can reach, at most 50.']];
    var radios = el('div', { class: 'k-radios', role: 'radiogroup', 'aria-label': 'Project access' }, ACCESS.map(function (a) {
      return radio('k-access', a[0], 'k-access-' + a[0], [el('span', { class: 'rt', text: a[1] }), ' ', el('span', { class: 'rh', text: a[2] })],
        a[0] === k.access, function (v) { k.access = v; pickBox.classList.toggle('hidden', v !== 'selected'); saveDraft(); });
    }));
    function drawChosen() {
      badge.textContent = k.projects.length ? String(k.projects.length) : '';
      badge.classList.toggle('hidden', !k.projects.length);
      chosen.textContent = '';
      k.projects.forEach(function (id) {
        var p = projectOf(id);
        chosen.appendChild(el('div', { class: 'k-chosen-row' }, [svg('project', 18), el('span', { class: 'nm', text: p.name }),
          el('span', { class: 'id', text: p.id }),
          el('button', { class: 'btn text', type: 'button', 'aria-label': 'Remove ' + p.name, text: 'Remove', onclick: function () {
            k.projects = k.projects.filter(function (x) { return x !== id; });
            drawChosen(); saveDraft();
          } })]));
      });
    }
    // "Select projects": a modal dialog with a search field, a box per project
    // and a Close button.
    function openProjects() {
      closeAllOverlays();
      var input = el('input', { type: 'text', 'aria-label': 'Search projects', placeholder: 'Search projects', autocomplete: 'off' });
      var none = el('div', { class: 'pk-msg hidden', text: 'No projects match.' });
      var count = el('span', { class: 'kp-count' });
      var rows = d.projects.map(function (p) {
        var box = el('input', { type: 'checkbox', id: 'kp-' + p.id, 'aria-label': p.name + ' (' + p.id + ')', checked: k.projects.indexOf(p.id) >= 0 });
        box.addEventListener('change', function () {
          k.projects = k.projects.filter(function (x) { return x !== p.id; });
          if (box.checked) k.projects.push(p.id);
          drawChosen(); counted(); saveDraft();
        });
        var row = el('div', { class: 'kp-row' }, [el('div', { class: 'mdc-checkbox' }, [box, el('div', { class: 'box' }), el('div', { class: 'tick' }, [svg('tick', 18)])]),
          el('label', { for: 'kp-' + p.id, class: 'kp-name', text: p.name }), el('span', { class: 'kp-id', text: p.id })]);
        row.hay = (p.name + ' ' + p.id).toLowerCase();
        return row;
      });
      var list = el('div', { class: 'kp-list' }, rows.concat([none]));
      function counted() { count.textContent = k.projects.length + ' of ' + d.projects.length + ' selected'; }
      input.addEventListener('input', function () {
        var v = input.value.trim().toLowerCase(), n = 0;
        rows.forEach(function (r) { var on = !v || r.hay.indexOf(v) >= 0; r.classList.toggle('hidden', !on); if (on) n++; });
        none.classList.toggle('hidden', n > 0);
      });
      var dlg = el('div', { class: 'dialog kp', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'kp-title', tabindex: '-1' }, [
        el('div', { class: 'kp-head' }, [el('h2', { id: 'kp-title', text: 'Select projects' }),
          el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close', onclick: close }, [svg('close', 20)])]),
        el('p', { class: 'kp-sub', text: 'Projects of ' + k.owner + ' this key can reach.' }),
        el('div', { class: 'pk-search kp-search' }, [el('span', { style: 'display:flex;color:#5e6266' }, [svg('search', 20)]), input]),
        list,
        el('div', { class: 'kp-foot' }, [count]),
      ]);
      var bd = el('div', { class: 'cdk-overlay-backdrop dark' });
      var wrap = el('div', { class: 'cdk-global-wrapper' }, [el('div', { class: 'cdk-overlay-pane', style: 'position:static' }, [dlg])]);
      bd.addEventListener('click', close);
      R.ov.appendChild(bd);
      R.ov.appendChild(wrap);
      var unhide = hideBehind();
      var ovl = pushOverlay({ escape: close, close: close });
      counted();
      setTimeout(function () { input.focus(); }, 0);
      function close() {
        bd.remove(); wrap.remove(); dropOverlay(ovl); unhide();
        selectBtn.focus({ preventScroll: true });
      }
    }

    // ─── permissions ─────────────────────────────────────────────────────
    var KINDS = { project: ['Projects', 'No project permissions added yet', 'Project permissions apply to the projects the key can reach.'],
      org: ['Organization', 'No organization permissions added yet', 'Organization permissions apply to the owner itself, not to its projects.'] };
    var tabs = {}, counts = {}, panes = {}, lists = {}, openers = {};
    var panel = null;        // the open permission panel
    Object.keys(KINDS).forEach(function (kind) {
      counts[kind] = el('span', { class: 'k-count', text: '0' });
      tabs[kind] = el('button', { type: 'button', role: 'tab', id: 'k-tab-' + kind, 'aria-controls': 'k-pane-' + kind,
        'aria-selected': String(kind === 'project'), class: kind === 'project' ? 'on' : '', onclick: function () { showTab(kind); } },
        [KINDS[kind][0], counts[kind]]);
      openers[kind] = el('button', { class: 'btn outlined perm-opener', type: 'button', id: 'k-add-' + kind, 'aria-expanded': 'false' },
        [svg('plus', 18), 'Add permissions']);
      openers[kind].addEventListener('click', function () {
        if (panel && panel.kind === kind) {
          if (Q.toggle_opener) panel.close(false);
          else panel.input.focus({ preventScroll: true });
          return;
        }
        openPanel(kind);
      });
      lists[kind] = el('div', { class: 'perm-list' });
      panes[kind] = el('div', { class: 'perm-pane' + (kind === 'project' ? '' : ' hidden'), role: 'tabpanel', id: 'k-pane-' + kind,
        'aria-labelledby': 'k-tab-' + kind }, [
        el('div', { class: 'perm-head' }, [help(KINDS[kind][2]), el('div', { class: 'perm-anchor' }, [openers[kind]])]),
        lists[kind]]);
      drawPerms(kind);
    });
    function showTab(kind) {
      Object.keys(KINDS).forEach(function (t) {
        tabs[t].setAttribute('aria-selected', String(t === kind));
        tabs[t].classList.toggle('on', t === kind);
        panes[t].classList.toggle('hidden', t !== kind);
      });
    }
    // The panel: a focused search field and the permissions not added yet, as
    // plain buttons. Not a dialog: the rest of the form stays readable.
    function openPanel(kind) {
      closeAllOverlays();
      var opener = openers[kind];
      var have = {};
      k.perms[kind].forEach(function (p) { have[p.name] = 1; });
      var input = el('input', { type: 'text', role: 'combobox', 'aria-label': 'Search', 'aria-expanded': 'true',
        'aria-autocomplete': 'list', 'aria-controls': 'perm-list-' + kind, placeholder: 'Search permissions', autocomplete: 'off' });
      var items = d.permissions[kind].filter(function (n) { return !have[n]; }).map(function (n) {
        return el('button', { type: 'button', class: 'pp-item', text: n, onclick: function () { pick(n); } });
      });
      var none = el('div', { class: 'pp-none hidden', text: 'No permissions match.' });
      var node = el('div', { class: 'perm-panel', id: 'perm-panel-' + kind }, [
        el('div', { class: 'pp-search' }, [svg('search', 18), input]),
        el('div', { class: 'pp-list', id: 'perm-list-' + kind }, items.concat([none]))]);
      if (Q.portal_panel) {
        // A container before the app's root, as a portal library makes one.
        if (!R.portal) R.portal = el('div', { class: 'portal-root' });
        if (document.body.firstChild !== R.portal) document.body.insertBefore(R.portal, document.body.firstChild);
        R.portal.appendChild(node);
      } else {
        opener.parentNode.insertBefore(node, opener.nextSibling);
      }
      opener.setAttribute('aria-expanded', 'true');
      var me = { kind: kind, input: input, close: close };
      panel = me;
      var ovl = pushOverlay({ kind: 'panel', escape: function () { close(true); }, close: function () { close(false); }, reposition: placePanel });
      placePanel();
      // A press outside closes it; the opener is not outside, its own click decides.
      var off = closeOnOutside([node, opener], function () { close(false); });
      input.focus({ preventScroll: true });
      input.addEventListener('input', function () {
        var v = input.value.trim().toLowerCase(), n = 0;
        items.forEach(function (b) { var on = b.textContent.toLowerCase().indexOf(v) >= 0; b.classList.toggle('hidden', !on); if (on) n++; });
        none.classList.toggle('hidden', n > 0);
      });
      input.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        var first = items.filter(function (b) { return !b.classList.contains('hidden'); })[0];
        if (first && input.value.trim()) pick(first.textContent);
      });
      function placePanel() {
        var r = opener.getBoundingClientRect(), h = node.offsetHeight, below = window.innerHeight - r.bottom - 8;
        var up = !(below >= h || below >= r.top - 8);
        if (Q.portal_panel) {
          node.style.left = Math.max(8, r.right - node.offsetWidth) + 'px';
          node.style.top = (up ? r.top - h - 4 : r.bottom + 4) + 'px';
        } else node.classList.toggle('up', up);
      }
      function pick(n) {
        if (kind === 'project' && !k.perms.project.length) k.perms.project.push({ name: d.mandatory, level: 'Read-only', mandatory: true });
        k.perms[kind].push({ name: n, level: 'Read-only' });
        close(true);
        drawPerms(kind);
        saveDraft();
      }
      function close(refocus) {
        if (panel !== me) return;
        panel = null;
        off(); node.remove(); dropOverlay(ovl);
        opener.setAttribute('aria-expanded', 'false');
        if (refocus) opener.focus({ preventScroll: true });
      }
    }
    function drawPerms(kind) {
      var box = lists[kind], list = k.perms[kind];
      counts[kind].textContent = String(list.length);
      box.textContent = '';
      if (!list.length) { box.appendChild(el('p', { class: 'perm-empty', text: KINDS[kind][1] })); return; }
      list.forEach(function (p) {
        var ctl;
        if (p.mandatory) ctl = [el('span', { class: 'perm-fixed', text: 'Read-only (mandatory)' })];
        else {
          var access = selectField('perm-' + kind + '-' + slug(p.name), 'Access', d.levels, p.level,
            function (v) { p.level = v; saveDraft(); }, { optional: true });
          ctl = [access.node, el('button', { class: 'btn text', type: 'button', text: 'Remove', onclick: function () { removePerm(kind, p.name); } })];
        }
        box.appendChild(el('div', { class: 'perm-row', 'data-perm': p.name }, [
          el('div', { class: 'perm-name' }, [el('div', { class: 'pn', text: p.name }),
            p.mandatory ? el('div', { class: 'k-help', text: 'Read the names, labels and settings of the projects. Every key with project permissions has it.' }) : null]),
          el('div', { class: 'perm-ctl' }, ctl)]));
      });
    }
    function removePerm(kind, n) {
      k.perms[kind] = k.perms[kind].filter(function (p) { return p.name !== n; });
      // Metadata comes with the other project permissions and goes with the last of them.
      if (kind === 'project' && k.perms.project.every(function (p) { return p.mandatory; })) k.perms.project = [];
      drawPerms(kind);
      saveDraft();
    }

    // ─── bottom ──────────────────────────────────────────────────────────
    var genErr = el('div', { class: 'k-err', role: 'alert' });
    var gen = el('button', { class: 'btn primary', type: 'button', text: 'Generate key and request access' });
    gen.addEventListener('click', function () {
      gen.disabled = true;
      api('keys/generate', keyForm()).then(function (r) {
        gen.disabled = false;
        var errs = (r.data && r.data.errors) || {};
        k.f.name.error(errs.name || '');
        genErr.textContent = Object.keys(errs).filter(function (x) { return x !== 'name'; }).map(function (x) { return errs[x]; }).join(' ');
        if (r.status !== 200) { if (errs.name) k.f.name.ctl.scrollIntoView({ block: 'center' }); return; }
        var name = k.f.name.get();
        K = null;
        setCard({ title: 'New access key', body: [el('div', { class: 'kf' }, [el('div', { class: 'success', role: 'status' }, [
          el('h3', {}, [svg('checkCircle', 26), r.data.approval ? 'Access requested for ' + name : 'Key ' + name + ' generated']),
          el('p', { text: r.data.approval ? 'An administrator of ' + k.owner + ' must approve the key before it works. You will get an email when they do.'
            : 'The key works now.' }),
          el('a', { class: 'btn primary', href: 'page.html?p=api-key', 'data-spa': 'quick', text: 'Back to API keys' })])])] });
      });
    });
    var cancel = el('a', { class: 'btn text dark', href: 'page.html?p=api-key', 'data-spa': 'quick', text: 'Cancel' });

    var col = el('div', { class: 'kf' }, [
      el('p', { class: 'kf-lead', text: 'A key lets a program call the ' + CONSOLE + ' API with only the projects and permissions you give it.' }),
      k.f.name.node, help('A unique name, so you can tell this key from your others.'),
      k.f.description.node,
      owner.node, help('The owner pays for what the key does. Projects and permissions below belong to it.'),
      expiration.node, expHelp,
      msgBox,
      el('h2', { class: 'kf-h', text: 'Project access' }),
      radios, pickBox,
      el('h2', { class: 'kf-h', text: 'Permissions' }),
      help('Give the key only the permissions it needs. Each one is read-only unless you change it.'),
      el('div', { class: 'perm-box' }, [el('div', { class: 'perm-tabs', role: 'tablist', 'aria-label': 'Permission types' }, [tabs.project, tabs.org]),
        panes.project, panes.org]),
      el('div', { class: 'kf-foot' }, [gen, cancel]), genErr,
    ]);
    syncExpiry();
    syncOwner();
    setCard({ title: 'New access key', body: [col] });
  }

  // ─── console-wide search results ───────────────────────────────────────
  VIEWS.search = function (seq) {
    var q = params().get('q') || '';
    setCrumbs([[CONSOLE, 'page.html?p=home'], ['Search', 'search.html?q=' + enc(q)]]);
    setTitle('Search');
    R.gsInput.value = q;
    api('search?q=' + enc(q)).then(function (r) {
      if (!current(seq)) return;
      var res = r.data.results || [];
      setCard({ title: 'Search results', body: [el('div', { class: 'pad' }, [
        el('p', { text: res.length + ' results for "' + q + '" across ' + CONSOLE + ': resources, documentation and products.' }),
      ].concat(res.map(function (x) {
        var link = x.kind === 'Documentation' ? ext(x.title, x.href) : el('a', { href: x.href, 'data-spa': 'swap', text: x.title });
        return el('div', { class: 'sres' }, [el('div', { class: 'k', text: x.kind }), link, el('p', { text: x.snippet })]);
      })))] });
      shown('search');
    });
  };

  // ─── other pages of the console ────────────────────────────────────────
  function pageInfo(id) { var p = PAGES()[id]; return p || [id, '']; }
  var PAGES = function () { return {
    home: ['Welcome', 'Your ' + CONSOLE + ' dashboard shows the projects you use most. Choose ' + PLATFORM + ' from the navigation menu to work with models.'],
    dashboard: [PLATFORM, 'Build with models from Cirrolux and its partners. Open the model catalog to find a model, then enable it for your project.'],
    models: ['Models', 'Find models in the model catalog, tune them, and track what you deployed.'],
    tuning: ['Tuning', 'No tuning jobs in this project yet.'], evaluation: ['Evaluation', 'No evaluations in this project yet.'],
    registry: ['Model Registry', 'No models are registered in this project.'], endpoints: ['Endpoints', 'No endpoints in this project.'],
    batch: ['Batch Inference', 'No batch jobs in this project.'], throughput: ['Provisioned Throughput', 'No provisioned throughput orders.'],
    datasets: ['Datasets', 'No datasets in this project.'], features: ['Feature Store', 'No feature stores in this project.'],
    experiments: ['Experiments', 'No experiments in this project.'], training: ['Training', 'No training jobs in this project.'],
    ray: ['Ray', 'No Ray clusters in this project.'], pipelines: ['Pipelines', 'No pipeline runs in this project.'],
    metadata: ['Metadata', 'No metadata stores in this project.'], monitoring: ['Monitoring', 'No monitoring jobs in this project.'],
    'api-key': ['API keys', 'You have no access keys yet. Keys owned by ' + S_ORG() + ' work only after an administrator approves them.'],
    billing: ['Billing', 'Billing account Lumen Robotics main (0A1B2C-3D4E5F-6A7B8C) pays for every project in this organization.'],
    tutorials: ['Tutorials', 'Step-by-step tutorials open in the documentation.'], deploy: ['Deploy model', 'Choose a model in the model catalog first.'],
    studio: ['Studio', 'Studio is not available in this project.'], 'new-project': ['New project', 'An organization policy stops you from creating projects. Ask an administrator.'],
    resources: ['Manage resources', 'Resources of ' + S_ORG() + '.'], preferences: ['Preferences', 'Language: English. Time zone: UTC.'],
    account: ['Account', 'Signed in to ' + CONSOLE + '.'], compute: ['Compute', 'No instances in this project.'], storage: ['Storage', 'No buckets in this project.'],
    iam: ['IAM & Admin', 'You are an Owner of this project.'],
  }; };
  function S_ORG() { return S ? S.org : ''; }
  VIEWS.page = function () {
    var id = params().get('p') || 'home', p = pageInfo(id);
    setCrumbs([[PLATFORM, 'page.html?p=dashboard'], [p[0], 'page.html?p=' + enc(id)]]);
    setTitle(p[0]);
    var links = id === 'dashboard' || id === 'models' ? el('p', {}, [el('a', { href: 'catalog.html', 'data-spa': 'swap', text: 'Open the model catalog' }), ' · ',
      el('a', { href: 'agreements.html', 'data-spa': 'quick', text: 'Agreements' })])
      : id === 'api-key' ? el('p', {}, [el('a', { href: 'keys.html', 'data-spa': 'quick', text: 'New access key' })]) : null;
    setCard({ title: p[0], body: [el('div', { class: 'pad' }, [el('p', { text: p[1] }), links])] });
    shown('page');
  };

  // ─── documentation (a separate site, opened in a new tab) ──────────────
  var DOCS = {
    claude: 'Use Claude models on ' + PLATFORM, 'org-policy': 'Restrict internet access for models', logging: 'Log requests and responses',
    'acceptable-use': 'Acceptable use of partner models', privacy: 'Cirrolux Cloud Privacy Notice', 'model-catalog': 'About the model catalog',
  };
  function docsPage() {
    document.body.className = 'docs';
    var t = params().get('topic') || 'index';
    var title = DOCS[t] || t.replace(/-/g, ' ').replace(/^./, function (c) { return c.toUpperCase(); });
    document.title = title + ' | ' + CONSOLE + ' Documentation';
    document.body.appendChild(el('header', { class: 'docs-top' }, [svg('notebook', 22), CONSOLE + ' Documentation']));
    document.body.appendChild(el('main', { class: 'docs-main' }, [el('h1', { text: title }),
      el('p', { text: 'This page belongs to the ' + CONSOLE + ' documentation. It explains the feature in general terms; it cannot change anything in your projects.' }),
      el('p', { text: 'Close this tab to go back to the console.' })]));
    shown('docs');
  }

  // ─── boot ──────────────────────────────────────────────────────────────
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[data-spa]') : null;
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    go(a.getAttribute('href'), a.getAttribute('data-spa'));
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && overlays.length) { overlays[overlays.length - 1].escape(e); return; }
    var t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
    if (typing || !R.gsInput) return;
    if (e.key === '/') { e.preventDefault(); R.gsInput.focus(); }
    else if (e.key === '.') { e.preventDefault(); productMenu(); }
  });
  window.addEventListener('popstate', function () { pushed = Math.max(0, pushed - 1); closeAllOverlays(); schedule(T.swap); });

  function boot() {
    var page = (location.pathname.split('/').pop() || '').replace(/\.html$/, '');
    if (page === 'docs') { docsPage(); return; }
    if (page === 'index' || page === '') { location.replace('catalog.html' + location.search); return; }
    api('session?project=' + enc(params().get('project') || '')).then(function (r) {
      S = r.data;
      Q = S.quirks || {};
      T = Q.lazy_content ? { first: S.timings.first_ms, swap: S.timings.swap_ms, quick: 350 } : { first: 0, swap: 0, quick: 0 };
      buildShell();
      var u = new URL(location.href);
      if (S.selected && u.searchParams.get('project') !== S.selected.id) {
        u.searchParams.set('project', S.selected.id);
        history.replaceState(null, '', u.pathname + u.search);
      }
      var name = viewName();
      schedule(name === 'catalog' ? (S.selected ? T.first : 0) : name === 'model' ? Math.round(T.first * 0.6) : T.quick);
    });
  }
  boot();
})();
