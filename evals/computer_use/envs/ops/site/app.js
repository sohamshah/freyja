// Quaymark Ops, an invented operations app for computer-use evals. Each page
// runs its init once the session (the quirk switches) has loaded, and marks
// <body data-ready="1"> when its first data is on screen. With a quirk off the
// page uses the plain version of the same pattern (see ../README.md). The
// server decides everything that is scored; the page only shows and sends.
(function () {
  'use strict';
  var API = '/ops/api/';
  var S = null; // session
  var Q = {};   // quirk switches

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) {
      var v = attrs[k];
      if (k === 'text') e.textContent = v;
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), v);
      else if (v === true) e.setAttribute(k, '');
      else if (v !== false && v != null) e.setAttribute(k, v);
    }
    (kids || []).forEach(function (c) {
      if (c != null) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return e;
  }
  function api(name, body) {
    var opt = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    };
    return fetch(API + name, opt).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, data: d }; });
    });
  }
  function money(n) {
    return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function td(x) { return el('td', {}, [x == null ? '' : (typeof x === 'object' ? x : String(x))]); }
  function show(node, on) { node.classList.toggle('hidden', !on); }
  function emptyRow(cols, text) { return el('tr', {}, [el('td', { colspan: String(cols), class: 'muted', text: text })]); }

  function chrome(page) {
    var links = [['queue', 'queue.html', 'Queues'], ['orders', 'orders.html', 'Purchasing'],
      ['expenses', 'expenses.html', 'Expenses'], ['reviews', 'reviews.html', 'Feedback']];
    var head = el('header', { class: 'top' }, [
      el('a', { href: 'index.html', class: 'brand' }, [el('img', { src: 'logo.svg', alt: '' }), 'Quaymark Ops']),
      el('nav', { 'aria-label': 'Main' }, links.map(function (l) {
        return el('a', { href: l[1], class: l[0] === page ? 'on' : null, 'aria-current': l[0] === page ? 'page' : null, text: l[2] });
      })),
      el('span', { class: 'who', text: 'Signed in as ' + S.user }),
    ]);
    document.body.insertBefore(head, document.body.firstChild);
  }

  // ─── support queue dashboard ───────────────────────────────────────────
  // The numbers come from the server's timeline for this episode. With
  // manual_refresh they change only when Refresh is clicked; without it the
  // page refreshes itself every few seconds.
  function initQueue() {
    var box = $('tiles'), reads = 0;
    function showPaged(p) {
      if (!p) return;
      $('pageStatus').textContent = 'Paged ' + p.who + ' at ' + p.clock + '.';
      var b = $('pageOncall');
      b.disabled = true;
      b.textContent = 'On-call paged';
    }
    function render(d) {
      $('updated').textContent = 'Updated ' + d.clock;
      d.tiles.forEach(function (t) {
        var tile = box.querySelector('[data-tile="' + t.id + '"]');
        if (!tile) {
          tile = el('div', { class: 'tile', 'data-tile': t.id }, [
            el('h3', { text: t.name }), el('div', { class: 'num' }), el('div', { class: 'unit', text: t.unit })]);
          box.appendChild(tile);
        }
        var n = tile.querySelector('.num'), v = String(t.value);
        if (n.textContent !== v) n.textContent = v;
      });
      showPaged(d.paged);
      reads += 1;
      document.body.setAttribute('data-reads', String(reads));
    }
    function load(why) {
      return api('queue?why=' + why).then(function (r) { if (r.status === 200) render(r.data); });
    }
    $('refresh').addEventListener('click', function () { load('refresh'); });
    $('pageOncall').addEventListener('click', function () {
      var b = $('pageOncall');
      b.disabled = true;
      api('page_oncall', {}).then(function (r) {
        if (r.status === 200) showPaged(r.data.paged); else b.disabled = false;
      });
    });
    if (!Q.manual_refresh) {
      var live = $('live');
      live.textContent = 'Live: updates every ' + S.auto_refresh_s + ' s';
      show(live, true);
      setInterval(function () { load('auto'); }, S.auto_refresh_s * 1000);
    }
    return load('load');
  }

  // ─── purchase orders and the team chat ─────────────────────────────────
  // The chat is polled; the team lead's message that changes the rules comes
  // when the server says so (after the second saved PO with late_message).
  function initOrders() {
    var f = { item: $('poItem'), qty: $('poQty'), price: $('poPrice'), vendor: $('poVendor') };
    var editing = null, pos = [], shown = -1, busy = false;
    function num(v) {
      var x = parseFloat(String(v == null ? '' : v).replace(/[$,\s]/g, ''));
      return isFinite(x) ? x : NaN;
    }
    function total() {
      var q = num(f.qty.value), p = num(f.price.value);
      $('poTotal').textContent = q > 0 && p > 0 ? money(Math.round(q * p * 100) / 100) : '$0.00';
    }
    [f.qty, f.price].forEach(function (x) { x.addEventListener('input', total); x.addEventListener('change', total); });
    function error(msg) { $('poError').textContent = msg || ''; show($('poError'), !!msg); }
    function reset() {
      editing = null;
      f.item.value = ''; f.qty.value = ''; f.price.value = ''; f.vendor.value = '';
      total(); error('');
      $('formTitle').textContent = 'New purchase order';
      $('poSave').textContent = 'Save PO';
      show($('poCancel'), false);
    }
    function edit(p) {
      editing = p.id;
      f.item.value = p.item; f.qty.value = String(p.qty); f.price.value = p.unit_price.toFixed(2); f.vendor.value = p.vendor;
      total(); error('');
      $('formTitle').textContent = 'Edit ' + p.number;
      $('poSave').textContent = 'Save changes';
      $('poNote').textContent = '';
      show($('poCancel'), true);
      f.item.focus();
    }
    function renderPos() {
      var tb = $('openPos').querySelector('tbody');
      tb.textContent = '';
      if (!pos.length) tb.appendChild(emptyRow(8, 'No open purchase orders yet.'));
      pos.forEach(function (p) {
        tb.appendChild(el('tr', { 'data-po': p.number }, [
          td(p.number), td(p.item), td(p.qty), td(money(p.unit_price)), td(money(p.total)), td(p.vendor), td(p.status),
          td(el('button', { type: 'button', class: 'linkbtn', 'aria-label': 'Edit ' + p.number + ' (' + p.item + ')', text: 'Edit',
            onclick: function () { edit(p); } })),
        ]));
      });
      document.body.setAttribute('data-pos', String(pos.length));
    }
    function loadPos() {
      return api('orders').then(function (r) { pos = r.data.pos; renderPos(); return r.data; });
    }
    function renderChat(d) {
      var msgs = d.messages;
      if (msgs.length === shown) return;
      var log = $('chatLog');
      log.textContent = '';
      msgs.forEach(function (m) {
        log.appendChild(el('li', { class: 'msg' + (m.new ? ' new' : '') }, [
          el('div', {}, [el('b', { text: m.who }), ' ', el('span', { class: 'role', text: m.role }), ' ',
            el('time', { text: m.at }), m.new ? el('span', { class: 'tag', text: 'New' }) : null]),
          el('p', { text: m.text }),
        ]));
      });
      if (shown >= 0 && msgs.length > shown) {
        var n = msgs.length - shown;
        $('chatNew').textContent = n + ' new message' + (n > 1 ? 's' : '') + ' in #purchasing';
        show($('chatNew'), true);
      }
      shown = msgs.length;
      document.body.setAttribute('data-chat', String(msgs.length));
    }
    function pollChat() { return api('chat').then(function (r) { if (r.status === 200) renderChat(r.data); }); }
    $('poForm').addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (busy) return;
      var was = editing;
      var body = { item: f.item.value, qty: f.qty.value, unit_price: f.price.value, vendor: f.vendor.value };
      busy = true;
      $('poSave').disabled = true;
      api(was ? 'po/' + was : 'po', body).then(function (r) {
        busy = false;
        $('poSave').disabled = false;
        if (r.status !== 200) { error(r.data.error); return; }
        reset();
        $('poNote').textContent = (was ? 'Saved changes to ' : 'Saved ') + r.data.po.number + '.';
        loadPos();
        pollChat();
      });
    });
    $('poCancel').addEventListener('click', function () { reset(); $('poNote').textContent = ''; });
    setInterval(pollChat, 2000);
    return Promise.all([loadPos().then(function (d) {
      d.vendors.forEach(function (v) { f.vendor.appendChild(el('option', { value: v, text: v })); });
      var tb = $('closedPos').querySelector('tbody');
      d.closed.forEach(function (p) {
        tb.appendChild(el('tr', {}, [td(p.number), td(p.item), td(p.qty), td(money(p.unit_price)), td(money(p.total)), td(p.vendor), td(p.status)]));
      });
    }), pollChat()]);
  }

  // ─── expense form ──────────────────────────────────────────────────────
  // sticky_fields: Save & add another keeps currency and category.
  // slow_save: the save answers late and the button stays enabled.
  // timeout_dialog: after the fifth save a countdown; at zero the form clears.
  function initExpenses() {
    var f = { date: $('expDate'), merchant: $('expMerchant'), amount: $('expAmount'), currency: $('expCurrency'), category: $('expCategory') };
    var btn = $('expSave'), saving = false, timer = null;
    function renderSaved(list) {
      $('savedCount').textContent = String(list.length);
      var tb = $('saved').querySelector('tbody');
      tb.textContent = '';
      if (!list.length) tb.appendChild(emptyRow(5, 'Nothing saved yet.'));
      list.forEach(function (s) {
        tb.appendChild(el('tr', {}, [td(s.date), td(s.merchant), td(s.amount.toFixed(2)), td(s.currency), td(s.category)]));
      });
      document.body.setAttribute('data-saved', String(list.length));
    }
    function reloadSaved() { return api('expenses').then(function (r) { renderSaved(r.data.saved); }); }
    function clearForm(all) {
      f.date.value = ''; f.merchant.value = ''; f.amount.value = '';
      if (all) { f.currency.value = ''; f.category.value = ''; }
    }
    function error(msg) { $('expError').textContent = msg || ''; show($('expError'), !!msg); }
    function askStay(secs) {
      if (timer) return;
      var left = secs;
      $('staySecs').textContent = String(left);
      show($('stay'), true);
      timer = setInterval(function () {
        left -= 1;
        $('staySecs').textContent = String(Math.max(0, left));
        if (left > 0) return;
        clearInterval(timer);
        timer = null;
        show($('stay'), false);
        clearForm(true);
        error('');
        $('expStatus').textContent = 'Your session expired, so you were signed in again. Unsaved changes were discarded.';
        api('session_timeout', {});
      }, 1000);
    }
    $('stayBtn').addEventListener('click', function () {
      if (timer) { clearInterval(timer); timer = null; }
      show($('stay'), false);
      $('expStatus').textContent = 'You are still signed in.';
      api('stay', {});
    });
    $('expForm').addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (saving && !Q.slow_save) return;
      var body = { date: f.date.value, merchant: f.merchant.value, amount: f.amount.value, currency: f.currency.value, category: f.category.value };
      saving = true;
      error('');
      if (!Q.slow_save) { btn.disabled = true; btn.textContent = 'Saving…'; }
      api('expense', body).then(function (r) {
        saving = false;
        btn.disabled = false;
        btn.textContent = 'Save & add another';
        if (r.status !== 200) { error(r.data.error); return; }
        var s = r.data.saved;
        $('expStatus').textContent = 'Saved ' + s.merchant + ', ' + s.amount.toFixed(2) + ' ' + s.currency + '.';
        clearForm(!Q.sticky_fields);
        f.date.focus();
        reloadSaved();
        if (r.data.stay_prompt) {
          var secs = r.data.stay_prompt.seconds;
          setTimeout(function () { askStay(secs); }, 1000);
        }
      });
    });
    return api('expenses').then(function (r) {
      var d = r.data;
      d.currencies.forEach(function (c) { f.currency.appendChild(el('option', { value: c.code, text: c.code + ' (' + c.name + ')' })); });
      d.categories.forEach(function (c) { f.category.appendChild(el('option', { value: c, text: c })); });
      var tb = $('receipts').querySelector('tbody');
      d.receipts.forEach(function (x) {
        tb.appendChild(el('tr', {}, [td(x.date), td(x.merchant), td(x.amount), td(x.currency), td(x.category)]));
      });
      renderSaved(d.saved);
    });
  }

  // ─── product reviews ───────────────────────────────────────────────────
  // virtualized: only the rows near the scroll position are in the DOM
  // (rows have a fixed height, so the list can place them by index).
  function initReviews() {
    var ROW = 112, OVER = 8, items = [], list = $('reviewList'), summarized = false;
    if (Q.virtualized) list.classList.add('virtual');
    function starText(n) { return '★★★★★'.slice(0, n) + '☆☆☆☆☆'.slice(0, 5 - n); }
    function rowFor(i) {
      var rv = items[i];
      var r = el('article', { class: 'review', 'data-i': String(i), 'data-id': String(rv.id), 'data-stars': String(rv.stars) }, [
        // The rating, author and date are one run of text, so a page reader gets
        // them as one line, and that line is different for every review.
        el('div', { class: 'meta' }, [
          el('span', { class: 'stars', 'aria-hidden': 'true', text: starText(rv.stars) }),
          el('span', { class: 'byline', text: rv.stars + '/5 · ' + rv.author + ' · ' + rv.date })]),
        el('h3', { text: rv.title }),
        el('p', { text: rv.body }),
      ]);
      if (Q.virtualized) r.style.top = (i * ROW) + 'px';
      return r;
    }
    function draw() {
      if (!Q.virtualized) {
        for (var i = list.children.length; i < items.length; i++) list.appendChild(rowFor(i));
        return;
      }
      list.style.height = (items.length * ROW) + 'px';
      var top = list.getBoundingClientRect().top;
      var from = Math.max(0, Math.floor(-top / ROW) - OVER);
      var to = Math.min(items.length, Math.ceil((window.innerHeight - top) / ROW) + OVER);
      var have = {};
      Array.prototype.slice.call(list.children).forEach(function (c) {
        var k = +c.getAttribute('data-i');
        if (k < from || k >= to) c.remove(); else have[k] = c;
      });
      var next = null;
      for (var j = to - 1; j >= from; j--) { // keep the DOM in list order
        if (have[j]) { next = have[j]; continue; }
        var row = rowFor(j);
        list.insertBefore(row, next);
        next = row;
      }
    }
    window.addEventListener('scroll', draw, { passive: true });
    window.addEventListener('resize', draw);
    function summary(d) {
      $('productName').textContent = d.product;
      $('avg').textContent = d.summary.average;
      $('count').textContent = d.total + ' reviews';
      var tb = $('hist').querySelector('tbody');
      d.summary.histogram.forEach(function (h) {
        tb.appendChild(el('tr', {}, [td(h[0] + (h[0] === 1 ? ' star' : ' stars')), td(h[1])]));
      });
    }
    function load() {
      var more = $('loadMore');
      more.disabled = true;
      return api('reviews?offset=' + items.length).then(function (r) {
        more.disabled = false;
        if (r.status !== 200) return;
        var d = r.data;
        items = items.concat(d.items);
        if (!summarized) { summary(d); summarized = true; }
        draw();
        $('showing').textContent = 'Showing ' + items.length + ' of ' + d.total + ' reviews';
        var done = items.length >= d.total;
        show(more, !done);
        show($('endNote'), done);
        document.body.setAttribute('data-loaded', String(items.length));
      });
    }
    $('loadMore').addEventListener('click', function () { load(); });
    return load();
  }

  function start() {
    api('session').then(function (r) {
      if (r.status !== 200) {
        document.body.insertBefore(el('p', { class: 'errors', text: 'This page needs an episode: open it from the task list.' }), document.body.firstChild);
        return null;
      }
      S = r.data;
      Q = S.quirks || {};
      var page = document.body.getAttribute('data-page');
      chrome(page);
      var init = { home: null, queue: initQueue, orders: initOrders, expenses: initExpenses, reviews: initReviews }[page];
      return init ? init() : null;
    }).then(function () { document.body.setAttribute('data-ready', '1'); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
