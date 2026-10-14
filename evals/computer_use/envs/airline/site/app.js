// Mock budget-airline booking site. Each page calls init<Page>() once the
// session (booking state + quirk switches) has loaded. Quirks reproduce, one by
// one, the page patterns that stopped a real booking run; with a quirk off the
// page uses the accessible version of the same widget.
(function () {
  'use strict';
  var API = '/airline/api/';
  var S = null; // session view
  var Q = {};   // quirk switches

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) {
      var v = attrs[k];
      if (k === 'text') e.textContent = v;
      else if (k === 'html') e.innerHTML = v;
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), v);
      else if (v === true) e.setAttribute(k, '');
      else if (v !== false && v != null) e.setAttribute(k, v);
    }
    (kids || []).forEach(function (c) { if (c != null) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return e;
  }
  function api(name, body) {
    var opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    return fetch(API + name, opt).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, data: d }; });
    });
  }
  // The real flow sends you home when the session is gone or a page is out of order.
  function redirected(res) {
    if (res.status === 440 || res.status === 409) { location.href = res.data.go; return true; }
    return false;
  }
  function money(n) { return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function fmtDate(iso, comma) {
    var d = new Date(iso + 'T12:00:00');
    var m = d.toLocaleString('en-US', { month: 'short' });
    return m + ' ' + String(d.getDate()).padStart(2, '0') + (comma ? ', ' : ' ') + d.getFullYear();
  }
  function iso(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  function pax(i) { var p = (S.passengers || [])[i] || {}; return (p.first || 'Passenger') + ' ' + (p.last || String(i + 1)); }
  function city(code) { return ((S && S.cities) || {})[code] || code; }

  // ─── chrome shared by every page ───────────────────────────────────────
  function chrome(step) {
    var head = el('header', { class: 'top' }, [
      el('a', { href: 'index.html', class: 'logo' }, [el('img', { src: 'logo.svg', alt: '' })]),
      el('a', { href: 'index.html', text: 'Book' }), el('a', { href: '#', text: 'My Trips' }),
      el('a', { href: '#', text: 'Travel Info & Help' }), el('a', { href: '#', text: 'Deals' }),
      el('a', { href: '#', text: 'Kestrel Miles' }), el('span', { class: 'acct', text: 'Hi, log in | sign up' }),
    ]);
    document.body.insertBefore(head, document.body.firstChild);
    if (step) {
      var steps = ['FLIGHT', 'INFO', 'SEATS', 'BAGS', 'ADD-ONS', 'PAYMENT'];
      var nav = el('nav', { class: 'progress' }, steps.map(function (s) {
        return el('a', { href: '#', class: s === step ? 'on' : '', text: s });
      }));
      nav.appendChild(el('a', { href: '#', 'aria-label': 'press enter to open shopping cart', text: S ? money(S.total) : '' }));
      head.after(nav);
    }
    if (Q.hidden_templates) {
      ['PASSWORD RESET', 'CONFIRM IDENTITY', 'SIGN UP', 'UPDATE PASSENGER INFORMATION', 'ACCOUNT SIGN IN', 'CANCELLATIONS']
        .forEach(function (t) {
          document.body.appendChild(el('div', { class: 'tpl-modal' }, [el('h3', { text: t }), el('button', { type: 'button', text: 'Close' })]));
        });
    }
    if (Q.cookie_banner && !sessionStorage.getItem('cookies')) {
      var c = el('div', { class: 'cookie' }, [
        el('p', { text: 'We use cookies and similar tracking technologies to enhance your browsing experience, analyze traffic to our site, customize content and advertising, and provide social media features.' }),
        el('button', { class: 'btn secondary', 'aria-label': 'customize cookie usage', text: 'Manage Cookies' }),
        el('button', { class: 'btn', 'aria-label': 'allow cookies', text: 'Accept All Cookies', onclick: function () { sessionStorage.setItem('cookies', '1'); c.remove(); } }),
      ]);
      document.body.appendChild(c);
    }
  }

  // ─── home: search form ─────────────────────────────────────────────────
  function initHome() {
    chrome(null);
    var f = { origin: null, dest: null, depart: null, ret: null, adults: 1, children: 0, infants: 0, trip: 'roundtrip' };
    if (S.expired || /expired=1/.test(location.search)) {
      $('notice').textContent = 'Your session has expired. Please start a new search.';
      $('notice').classList.remove('hidden');
    }
    // Trip type: radios drawn as custom dots (the native input has no size).
    document.querySelectorAll('input[name=trip]').forEach(function (r) {
      r.addEventListener('change', function () { f.trip = r.value; $('returnDate').disabled = f.trip === 'oneway' || !f.dest; });
    });
    var origin = $('origin'), dest = $('destination');
    autocomplete(origin, function () { return ''; }, function (a) {
      f.origin = a; dest.disabled = !a;
      if (a) { dest.focus(); }
      dates();
    });
    autocomplete(dest, function () { return f.origin ? f.origin.code : ''; }, function (a) { f.dest = a; dates(); });
    function dates() {
      var on = !!(f.origin && f.dest);
      $('departureDate').disabled = !on;
      $('returnDate').disabled = !on || f.trip === 'oneway';
    }
    datepicker($('departureDate'), $('departureDateIcon'), function (d) {
      f.depart = d; $('departureDate').value = fmtDate(d, true);
      if (f.trip !== 'oneway') {
        // The site moves on to the return date and shows a provisional value.
        var next = new Date(d + 'T12:00:00'); next.setDate(next.getDate() + 1);
        $('returnDate').value = fmtDate(iso(next), false);
        setTimeout(function () { openPicker($('returnDate'), function (r) { f.ret = r; $('returnDate').value = fmtDate(r, true); }, d); }, 50);
      }
    });
    datepicker($('returnDate'), $('returnDateIcon'), function (d) { f.ret = d; $('returnDate').value = fmtDate(d, true); }, function () { return f.depart; });
    travelers(f);
    var search = $('btnSearch');
    if (!Q.image_search) {
      var b = el('button', { type: 'button', class: 'btn', id: 'btnSearch2', text: 'Search' });
      search.replaceWith(b);
      search = b;
    }
    search.addEventListener('click', function (e) {
      e.preventDefault();
      var err = !f.origin ? 'Please select a departure city.' : !f.dest ? 'Please select an arrival city.'
        : !f.depart ? 'Please select a departure date.' : (f.trip !== 'oneway' && !f.ret) ? 'Please select a return date.' : '';
      if (err) { showErr(err); return; }
      api('search', { origin: f.origin.code, dest: f.dest.code, depart: f.depart, return: f.trip === 'oneway' ? null : f.ret,
                      adults: f.adults, trip: f.trip }).then(function (res) {
        if (res.status !== 200) { showErr(res.data.error); return; }
        location.href = res.data.go;
      });
    });
    function showErr(t) { $('formError').textContent = t; $('formError').classList.remove('hidden'); }
    if (Q.promo_popup && !sessionStorage.getItem('promo')) setTimeout(promo, 1200);
  }

  // A promotion drawn inside an open shadow root over the whole page. Its close
  // button is an icon with no name; Escape also closes it.
  function promo() {
    var host = el('div', { id: 'promo-instance' });
    var root = el('div', { id: 'promo-root' }, [host]);
    document.body.appendChild(root);
    var sr = host.attachShadow({ mode: 'open' });
    sr.innerHTML = '<style>.ov{position:fixed;inset:0;background:rgba(10,30,20,.55);backdrop-filter:blur(2px);z-index:5000;display:flex;align-items:center;justify-content:center}' +
      '.card{width:480px;background:linear-gradient(135deg,#0b5d34,#2e8b57);color:#fff;border-radius:10px;padding:34px;position:relative;font:16px sans-serif}' +
      '.x{position:absolute;right:12px;top:10px;background:none;border:0;cursor:pointer}.cta{display:inline-block;margin-top:16px;background:#b6f36c;color:#0b3d22;padding:10px 22px;border-radius:20px;font-weight:700;text-decoration:none}</style>' +
      '<div class="ov"><div class="card"><button class="x"><svg width="20" height="20" viewBox="0 0 20 20"><path d="M3 3 L17 17 M17 3 L3 17" stroke="#fff" stroke-width="2"/></svg></button>' +
      '<h2>Earn up to 60,000 Miles</h2><p>after qualifying account activity. That\'s enough for up to 12 one-way award flights.</p>' +
      '<a class="cta" href="#">PRE-QUALIFY NOW</a><p style="font-size:11px">Terms Apply</p></div></div>';
    function close() { sessionStorage.setItem('promo', '1'); root.remove(); document.removeEventListener('keydown', esc, true); }
    function esc(e) { if (e.key === 'Escape') close(); }
    sr.querySelector('.x').addEventListener('click', close);
    document.addEventListener('keydown', esc, true);
  }

  // Airport suggestions in the style of a well-known autocomplete widget: list
  // items with no role, no pointer cursor and a tabindex=-1 wrapper; a live
  // region says to use the arrow keys. Typing alone commits nothing.
  function autocomplete(input, exclude, onCommit) {
    var acc = !Q.autocomplete;
    var menu = el('ul', { class: 'ui-menu ui-widget ui-autocomplete' + (acc ? ' accessible' : ''), tabindex: '0', style: 'display:none' });
    var live = el('div', { role: 'status', 'aria-live': 'assertive', class: 'ui-helper-hidden-accessible' });
    document.body.appendChild(menu);
    document.body.appendChild(live);
    if (acc) {
      menu.setAttribute('role', 'listbox');
      input.setAttribute('role', 'combobox');
      input.setAttribute('aria-expanded', 'false');
      input.setAttribute('aria-autocomplete', 'list');
    }
    // What the real widget does to every field it attaches to.
    input.classList.add('ui-autocomplete-input');
    input.setAttribute('autocomplete', 'off');
    var items = [], active = -1, timer = null;
    input.addEventListener('input', function () {
      onCommit(null);
      clearTimeout(timer);
      timer = setTimeout(search, 300);
    });
    input.addEventListener('keydown', function (e) {
      if (menu.style.display === 'none') return;
      if (e.key === 'ArrowDown') { e.preventDefault(); setActive(Math.min(active + 1, items.length - 1)); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(Math.max(active - 1, 0)); }
      else if (e.key === 'Enter') { if (active >= 0) { e.preventDefault(); select(items[active]); } }
      else if (e.key === 'Tab') { if (active >= 0) select(items[active]); else close(); }
      else if (e.key === 'Escape') { close(); }
    });
    input.addEventListener('blur', function () { setTimeout(close, 200); });
    function search() {
      var term = input.value.trim();
      if (term.length < 2) { close(); return; }
      fetch(API + 'airports?q=' + encodeURIComponent(term) + (exclude() ? '&from=' + exclude() : ''))
        .then(function (r) { return r.json(); }).then(render);
    }
    function render(list) {
      items = list; active = -1; menu.innerHTML = '';
      var country = null;
      list.forEach(function (a, i) {
        if (a.country !== country) { country = a.country; menu.appendChild(el('li', { class: 'ui-autocomplete-category', text: country })); }
        var wrap = el('div', { class: 'station-container ui-menu-item-wrapper', tabindex: '-1', id: 'ui-id-' + (i + 3) }, [
          el('img', { class: 'station-img', src: 'pin.svg' }),
          el('div', { class: 'city-state-container' }, [el('div', { class: 'city' }, [el('span', { text: a.city })]),
            el('div', { class: 'state' }, [el('span', { text: a.state + ' (' + a.code + ')' })])]),
        ]);
        var li = el('li', { class: 'ui-menu-item', 'aria-label': a.country + ' : ' + a.city + ' (' + a.code + ')', 'data-i': i }, [wrap]);
        if (acc) { li.setAttribute('role', 'option'); li.removeAttribute('aria-label'); }
        menu.appendChild(li);
      });
      var r = input.getBoundingClientRect();
      menu.style.left = (r.left + scrollX) + 'px';
      menu.style.top = (r.bottom + scrollY) + 'px';
      menu.style.width = Math.max(r.width, 300) + 'px';
      menu.style.display = list.length ? 'block' : 'none';
      if (acc) input.setAttribute('aria-expanded', list.length ? 'true' : 'false');
      live.textContent = list.length ? list.length + (list.length === 1 ? ' result is' : ' results are') +
        ' available, use up and down arrow keys to navigate.' : 'No search results.';
    }
    // The widget keeps focus in the field while you press an item.
    menu.addEventListener('mousedown', function (e) { e.preventDefault(); });
    menu.addEventListener('click', function (e) {
      var li = e.target.closest && e.target.closest('li.ui-menu-item');
      if (li) select(items[+li.getAttribute('data-i')]);
    });
    function setActive(i) {
      active = i;
      menu.querySelectorAll('.ui-menu-item-wrapper').forEach(function (w, j) { w.classList.toggle('ui-state-active', j === i); });
      if (i >= 0) input.value = items[i].city + ', ' + items[i].state + ' (' + items[i].code + ')';
    }
    function select(a) {
      input.value = a.city + ', ' + a.state + ' (' + a.code + ')';
      close();
      onCommit(a);
    }
    function close() { menu.style.display = 'none'; if (acc) input.setAttribute('aria-expanded', 'false'); }
  }

  // Two months side by side; days are links named by their number.
  var picker = null;
  function datepicker(input, icon, onPick, minDate) {
    function open() { if (!input.disabled) openPicker(input, onPick, minDate ? minDate() : null); }
    input.addEventListener('click', open);
    input.addEventListener('focus', open);
    icon.addEventListener('click', function () { input.focus(); open(); });
  }
  function openPicker(input, onPick, min) {
    closePicker();
    var today = new Date(S.today + 'T12:00:00');
    var floor = min ? new Date(min + 'T12:00:00') : today;
    var month = new Date(floor.getFullYear(), floor.getMonth(), 1);
    var box = el('div', { class: 'ui-datepicker' });
    function render() {
      box.innerHTML = '';
      box.appendChild(el('a', { href: '#', class: 'nav ui-datepicker-prev', text: 'Prev', onclick: function (e) { e.preventDefault(); month.setMonth(month.getMonth() - 1); render(); } }));
      box.appendChild(el('a', { href: '#', class: 'nav ui-datepicker-next', text: 'Next', onclick: function (e) { e.preventDefault(); month.setMonth(month.getMonth() + 1); render(); } }));
      for (var m = 0; m < 2; m++) {
        var first = new Date(month.getFullYear(), month.getMonth() + m, 1);
        var wrap = el('div', { class: 'month' }, [el('div', { class: 'ui-datepicker-title', text: first.toLocaleString('en-US', { month: 'long' }) + ' ' + first.getFullYear() })]);
        var t = el('table', { class: 'ui-datepicker-calendar' });
        t.appendChild(el('thead', {}, [el('tr', {}, ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'].map(function (d) { return el('th', { text: d }); }))]));
        var tb = el('tbody'), tr = el('tr');
        for (var i = 0; i < first.getDay(); i++) tr.appendChild(el('td'));
        var days = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
        for (var d = 1; d <= days; d++) {
          var date = new Date(first.getFullYear(), first.getMonth(), d, 12);
          if (date < floor) tr.appendChild(el('td', { class: 'ui-state-disabled' }, [el('span', { text: String(d) })]));
          else {
            (function (dt) {
              tr.appendChild(el('td', { 'data-handler': 'selectDay' }, [el('a', { href: '#', class: 'ui-state-default', text: String(dt.getDate()),
                onclick: function (e) { e.preventDefault(); closePicker(); onPick(iso(dt)); } })]));
            })(date);
          }
          if (date.getDay() === 6) { tb.appendChild(tr); tr = el('tr'); }
        }
        if (tr.children.length) tb.appendChild(tr);
        t.appendChild(tb);
        wrap.appendChild(t);
        box.appendChild(wrap);
      }
    }
    render();
    var r = input.getBoundingClientRect();
    box.style.left = (r.left + scrollX) + 'px';
    box.style.top = (r.bottom + scrollY + 2) + 'px';
    document.body.appendChild(box);
    picker = box;
    setTimeout(function () { document.addEventListener('mousedown', outside, true); }, 0);
    function outside(e) {
      if (picker && !picker.contains(e.target) && e.target !== input) { closePicker(); }
    }
    box._outside = outside;
  }
  function closePicker() {
    if (!picker) return;
    document.removeEventListener('mousedown', picker._outside, true);
    picker.remove();
    picker = null;
  }

  // Travelers: a read-only input that opens a panel of add/remove buttons. The
  // panel covers the fields below it; Escape leaves it open.
  function travelers(f) {
    var input = $('passengersInput');
    if (!Q.readonly_travelers) {
      var sel = el('select', { id: 'passengersSelect', 'aria-label': 'Travelers' });
      for (var n = 1; n <= 6; n++) sel.appendChild(el('option', { value: n, text: n + (n === 1 ? ' Adult' : ' Adults') }));
      sel.addEventListener('change', function () { f.adults = +sel.value; });
      input.replaceWith(sel);
      return;
    }
    var pop = null;
    function label() { input.value = f.adults + (f.adults === 1 ? ' Adult' : ' Adults') + (f.children ? ', ' + f.children + ' Child' : ''); }
    function row(name, key, min) {
      var count = el('span', { text: String(f[key]) });
      return el('div', { class: 'prow' }, [el('span', { text: name }), el('span', {}, [
        el('button', { type: 'button', 'aria-label': 'remove ' + key.replace(/s$/, '').replace('childre', 'child'), text: '−',
          onclick: function () { if (f[key] > min) { f[key]--; count.textContent = f[key]; label(); } } }),
        ' ', count, ' ',
        el('button', { type: 'button', 'aria-label': 'add ' + key.replace(/s$/, '').replace('childre', 'child'), text: '+',
          onclick: function () { if (f[key] < 6) { f[key]++; count.textContent = f[key]; label(); } } }),
      ])]);
    }
    function open() {
      var r = input.getBoundingClientRect();
      pop = el('div', { class: 'passengers-popup' }, [row('Adults', 'adults', 1), row('Children', 'children', 0), row('Lap Infants', 'infants', 0),
        el('p', { class: 'muted', text: 'Low fares on select flights.' })]);
      pop.style.left = (r.left + scrollX) + 'px';
      pop.style.top = (r.bottom + scrollY) + 'px';
      document.body.appendChild(pop);
      input.setAttribute('aria-expanded', 'true');
    }
    function close() { if (pop) { pop.remove(); pop = null; input.setAttribute('aria-expanded', 'false'); } }
    input.addEventListener('click', function (e) {
      // trusted_toggle: as on the real site, only a person's click closes it.
      if (pop) { if (!Q.trusted_toggle || e.isTrusted) close(); } else open();
    });
    // A press outside or Escape leaves it open; it closes when its field is
    // clicked again or another form field takes focus. With trusted_toggle, as on
    // the real site, focus moving elsewhere leaves it open too.
    document.addEventListener('focusin', function (e) {
      if (Q.trusted_toggle) return;
      if (pop && e.target !== input && !pop.contains(e.target) && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) close();
    });
  }

  // ─── flight select ─────────────────────────────────────────────────────
  function initSelect() {
    chrome('FLIGHT');
    api('flights').then(function (res) {
      if (redirected(res)) return;
      var data = res.data, sel = { out: null, ret: null };
      var s = data.search;
      $('summary').innerHTML = '';
      $('summary').append(el('span', { text: s.origin }), el('span', { text: s.dest }),
        el('span', { text: data.adults + ' adults' }), el('a', { href: 'index.html', text: 'edit' }),
        el('span', { class: 'total', id: 'cartTotal', text: money(S.total) }));
      section('out', $('departList'), data.out, s.depart);
      section('ret', $('returnList'), data.ret, s['return']);
      function section(leg, box, flights, day) {
        var rib = el('div', { class: 'ribbon' });
        var d0 = new Date(day + 'T12:00:00');
        for (var k = -2; k <= 2; k++) {
          var dd = new Date(d0); dd.setDate(d0.getDate() + k);
          var price = 300 + ((dd.getDate() * 37) % 260);
          rib.appendChild(el('button', { type: 'button', class: k === 0 ? 'on' : '', 'aria-label': dd.toLocaleString('en-US', { weekday: 'long' }) + ' ' + (dd.getMonth() + 1) + '/' + dd.getDate() + ' starting at $' + price },
            [el('div', { text: dd.toLocaleString('en-US', { weekday: 'short' }) + ' ' + (dd.getMonth() + 1) + '/' + dd.getDate() }), el('div', { text: '$' + price })]));
        }
        box.appendChild(rib);
        box.appendChild(el('div', { class: 'legend' }, [el('span', { text: '' })].concat(data.bundles.map(function (b) { return el('span', { text: b.name }); }))));
        var list = el('div', { class: 'results' + (Q.nested_scroll ? ' nested' : '') });
        flights.forEach(function (f) {
          var row = el('div', { class: 'frow' });
          var details = el('div', { class: 'details hidden', text: 'Flight ' + f.flight + ' · ' + f.legs.join(' → ') + ' · ' + f.duration + ' · Seats: Legroom Plus rows 1-3' });
          row.appendChild(el('div', {}, [
            el('div', { class: 'times', text: f.dep + ' ' + f.legs[0] + '  →  ' + f.arr + ' ' + f.legs[f.legs.length - 1] + (f.next_day ? ' (+1 day)' : '') }),
            el('div', { class: 'meta', text: f.duration + ' | ' + f.stops + ' ' }),
            el('a', { href: '#0', text: '+Details', onclick: function (e) { e.preventDefault(); details.classList.toggle('hidden'); } }),
          ]));
          data.bundles.forEach(function (b) {
            var fare = f.fares[b.id];
            var box2;
            if (Q.hidden_radio_fares) {
              var aria = b.id === 'business' ? 'Biz fare' : 'Standard fare $' + f.fares.basic.standard + ' departs at ' + f.dep + ' arrives at ' + f.arr + ' duration ' + f.duration + ' ' + f.stops;
              box2 = el('div', { class: 'farebox' }, [
                el('label', { class: 'customRadio' }, [el('span', { hidden: true, text: 'Select standard fare' }),
                  el('input', { type: 'radio', name: 'fare-' + leg, class: 'zero', 'aria-label': aria })]),
                el('div', { class: 'dd', text: '$' + fare.club_saver + ' Club Saver' }),
                el('div', { class: 'std', text: '$' + fare.standard }), el('div', { class: 'muted', text: 'Standard' }),
              ]);
            } else {
              box2 = el('button', { type: 'button', class: 'farebox', 'aria-label': b.name + ' at $' + fare.standard + ' standard ($' + fare.club_saver + ' Club Saver) for the ' + f.dep + ' flight ' + f.flight }, [
                el('div', { class: 'dd', text: '$' + fare.club_saver + ' Club Saver' }), el('div', { class: 'std', text: '$' + fare.standard }), el('div', { class: 'muted', text: 'Standard' })]);
            }
            box2.addEventListener('click', function () {
              list.querySelectorAll('.farebox').forEach(function (x) { x.classList.remove('selected'); });
              box2.classList.add('selected');
              var r = box2.querySelector('input'); if (r) r.checked = true;
              sel[leg] = { flight_id: f.id, bundle: b.id, std: fare.standard, dd: fare.club_saver };
              bar();
            });
            row.appendChild(box2);
          });
          row.appendChild(details);
          list.appendChild(row);
        });
        box.appendChild(list);
      }
      function bar() {
        if (!sel.out || !sel.ret) return;
        var std = sel.out.std + sel.ret.std, dd = sel.out.dd + sel.ret.dd;
        $('fareBar').innerHTML = '';
        $('fareBar').append(
          el('div', { class: 'opt' }, [el('div', {}, [el('div', { text: 'Club Saver' }), el('strong', { text: money(dd + 47 * 2) }), el('span', { class: 'muted', text: ' per person' })]),
            el('button', { type: 'button', class: 'btn secondary', text: 'CONTINUE', onclick: function () { go('club_saver', 'accepted'); } })]),
          el('div', { class: 'opt' }, [el('div', {}, [el('div', { text: 'Standard Price' }), el('strong', { text: money(std + 47 * 2) }), el('span', { class: 'muted', text: ' per person. No Club Saver today? Enjoy our standard fare.' })]),
            el('button', { type: 'button', class: 'btn', text: 'CONTINUE', onclick: standard })]));
        $('fareBar').classList.remove('hidden');
        $('cartTotal').textContent = money((std + 47 * 2) * data.adults);
      }
      function standard() {
        if (!Q.upsell_modal) { go('standard', 'declined'); return; }
        // An upsell added at the end of a long page, with no dialog role.
        var ov = el('div', { class: 'overlay' }, [el('div', { class: 'box' }, [
          el('button', { type: 'button', class: 'close', 'aria-label': 'Close', text: '×', onclick: function () { ov.remove(); } }),
          el('h2', { text: 'Purchase GoPass™ Pass' }),
          el('p', { text: 'Fly as much as you want for one low price. Sign up now and save on this trip.' }),
          el('button', { type: 'button', class: 'btn', text: 'SIGN UP', onclick: function () { go('standard', 'accepted'); } }), ' ',
          el('a', { href: '#', text: 'No thanks, continue with standard', onclick: function (e) { e.preventDefault(); go('standard', 'declined'); } }),
          el('p', {}, [el('a', { href: '#', text: 'Learn more and read the GoPass™ FAQs' })]),
        ])]);
        document.body.appendChild(ov);
      }
      function go(fare, membership) {
        api('select', { out: sel.out, ret: sel.ret, fare: fare, membership: membership }).then(function (r) {
          if (redirected(r)) return;
          if (r.status !== 200) { alertBox(r.data.error); return; }
          location.href = r.data.go;
        });
      }
    });
  }

  function alertBox(t) {
    var e = $('pageErrors');
    if (!e) return;
    e.textContent = 'ERRORS | Error: ' + t;
    e.classList.remove('hidden');
    e.scrollIntoView({ block: 'center' });
  }

  // ─── passengers ────────────────────────────────────────────────────────
  function initPassengers() {
    chrome('INFO');
    var n = S.adults || 2;
    var model = { p: [], c: { country: 'United States of America' } };
    var commit = Q.change_commit ? ['change'] : ['input', 'change'];
    function bind(input, obj, key) {
      commit.forEach(function (ev) { input.addEventListener(ev, function () { obj[key] = input.value; if (obj === model.p[0]) mirror(); }); });
    }
    function field(label, attrs, obj, key) {
      var input = attrs.options ? el('select', {}, attrs.options.map(function (o) { return el('option', { value: o, text: o || 'Select' }); })) : el('input', attrs);
      bind(input, obj, key);
      return el('label', {}, [label, input]);
    }
    var box = $('paxForms');
    for (var i = 0; i < n; i++) {
      var p = {}; model.p.push(p);
      box.appendChild(el('div', { class: 'card pform' }, [
        el('h2', { text: 'Passenger ' + (i + 1) + ' (Adult)' }),
        el('p', { class: 'muted' }, ['KESTREL Miles # ', el('a', { href: '#', text: 'Sign Up' })]),
        el('div', { class: 'grid' }, [
          field('First Name *', { type: 'text' }, p, 'first'), field('Middle Name', { type: 'text' }, p, 'middle'),
          field('Last Name *', { type: 'text' }, p, 'last'),
          field('Suffix', { options: ['', 'Jr.', 'Sr.', 'II', 'III'] }, p, 'suffix'),
          field('Gender *', { options: ['', 'Male', 'Female', 'Undisclosed'] }, p, 'gender'),
          field('Date of Birth * (mm/dd/yyyy)', { type: 'text', placeholder: 'mm/dd/yyyy' }, p, 'dob'),
        ]),
        el('p', { class: 'muted' }, [el('a', { href: '#', text: 'Add Additional Services & Information' }), ' (Known Traveler Number/TSA Pre✓®, redress)']),
      ]));
    }
    var c = model.c;
    var first = el('input', { type: 'text' }), last = el('input', { type: 'text' });
    bind(first, c, 'first'); bind(last, c, 'last');
    var same = el('input', { type: 'checkbox', checked: true });
    function mirror() { if (same.checked) { first.value = model.p[0].first || ''; last.value = model.p[0].last || ''; c.first = first.value; c.last = last.value; } }
    same.addEventListener('change', mirror);
    box.appendChild(el('div', { class: 'card pform' }, [
      el('h2', { text: 'Primary Contact' }),
      el('p', { class: 'muted', text: 'Please let us know the best way to reach you during travel, for important updates about your trip.' }),
      el('label', { style: 'flex-direction:row;align-items:center' }, [same, ' I am the first passenger']),
      el('div', { class: 'grid' }, [
        el('label', {}, ['First Name *', first]), el('label', {}, ['Last Name *', last]),
        field('Email Address *', { type: 'email' }, c, 'email'),
        field('Mobile Number *', { type: 'tel' }, c, 'phone'),
        field('Country of Residence *', { options: ['United States of America', 'Canada', 'Mexico', 'United Kingdom'] }, c, 'country'),
        field('Zip code *', { type: 'text' }, c, 'zip'),
      ]),
    ]));
    $('btnContinue').addEventListener('click', function () {
      // No preventDefault: the address gains #0 the way the real link does.
      var missing = [];
      model.p.forEach(function (p, i) { ['first', 'last', 'gender', 'dob'].forEach(function (k) { if (!p[k]) missing.push('Passenger ' + (i + 1) + ' ' + k); }); });
      ['email', 'phone', 'zip'].forEach(function (k) { if (!c[k]) missing.push('contact ' + k); });
      if (missing.length) { alertBox('Please complete required fields: ' + missing.join(', ')); return; }
      api('passengers', { passengers: model.p, contact: c }).then(function (r) {
        if (redirected(r)) return;
        if (r.status !== 200) { alertBox(r.data.error); return; }
        location.href = r.data.go;
      });
    });
  }

  // ─── seats ─────────────────────────────────────────────────────────────
  function initSeats() {
    chrome('SEATS');
    var active = { leg: 'out', pax: 0 };
    var data = null;
    function load() { return api('seatmap').then(function (r) { if (redirected(r)) return; data = r.data; S.seats = data.seats; render(); }); }
    function render() {
      var who = $('seatWho'); who.innerHTML = '';
      var o = city(data.search.origin), d = city(data.search.dest);
      [['out', 'DEPARTING', o + ' to ' + d], ['ret', 'RETURNING', d + ' to ' + o]].forEach(function (L) {
        who.appendChild(el('h2', { text: L[1] + ' ' + L[2] }));
        data.passengers.forEach(function (p, i) {
          var seat = data.seats[L[0] + ':' + i];
          var name = p.first + ' ' + p.last;
          who.appendChild(el('div', { class: 'prow' }, [
            seat ? el('button', { type: 'button', class: 'btn secondary', text: name + ' ' + seat }) : null,
            el('a', { href: '#', text: (seat ? 'Update' : 'Select') + ' seat for ' + name + ' for flight ' + L[2],
              onclick: function (e) { e.preventDefault(); active = { leg: L[0], pax: i }; render(); } }),
          ]));
        });
      });
      var p = data.passengers[active.pax];
      $('seatHead').textContent = (active.leg === 'out' ? 'DEPARTING ' + o + ' to ' + d : 'RETURNING ' + d + ' to ' + o) +
        ' — selecting a seat for ' + p.first + ' ' + p.last;
      var map = $('seatMap'); map.innerHTML = '';
      data[active.leg].rows.forEach(function (row) {
        row.forEach(function (c, j) {
          if (j === 3) map.appendChild(el('div', { class: 'aisle', text: c.id.replace(/\D/g, '') }));
          var pos = 'ABCDEF'.indexOf(c.id.slice(-1));
          var side = pos === 0 || pos === 5 ? ' window' : pos === 2 || pos === 3 ? ' aisle seat' : '';
          var kind = c.cls === 'legroom' ? 'legroom' : c.cls === 'preferred' ? 'preferr' : 'standard';
          var mine = c.who !== undefined && c.who !== null;
          var label = c.taken && !mine ? 'seat number ' + c.id + ' not available' : 'seat: ' + c.id + ' ' + kind + side + (c.id === '1D' || c.id === '1F' ? ' Near Lavatory' : '');
          map.appendChild(el('button', { type: 'button', class: c.cls + (mine ? ' mine' : ''), 'aria-label': label, disabled: c.taken && !mine, text: mine ? (data.passengers[+c.who.split(':')[1]] || {}).first[0] + '' : c.id.slice(-1),
            onclick: function () { pick(c.id); } }));
        });
      });
      $('seatTotal').textContent = money(S.total);
    }
    function pick(id) {
      api('seat', { leg: active.leg, pax: active.pax, seat: id }).then(function (r) {
        if (redirected(r)) return;
        if (r.status === 200) {
          S.total = r.data.total;
          if (!Q.active_passenger) {
            // Accessible version: move on to the next passenger without a seat.
            for (var k = 0; k < data.passengers.length; k++) if (!r.data.seats[active.leg + ':' + k]) { active.pax = k; break; }
          }
        }
        load();
      });
    }
    $('btnSeatsContinue').addEventListener('click', function () {
      api('continue', { from: 'seats' }).then(function (r) { if (!redirected(r) && r.status === 200) location.href = r.data.go; });
    });
    load();
  }

  // ─── bags ──────────────────────────────────────────────────────────────
  function initBags() {
    chrome('BAGS');
    var box = $('bagList');
    (S.passengers || []).forEach(function (p) {
      box.appendChild(el('div', { class: 'card' }, [el('h2', { text: p.first + ' ' + p.last }),
        el('p', { text: '1x Carry-On + Priority Boarding (INCLUDED) · 2x 40lb Checked Bags (INCLUDED) increased to 41-50 lbs (INCLUDED) · 1 free personal item' }),
        el('label', {}, [el('input', { type: 'checkbox', checked: true }), ' Same for return flight'])]));
    });
    $('bagTotal').textContent = 'Bag Total: $0';
    $('btnBagsContinue').addEventListener('click', function () {
      api('continue', { from: 'bags' }).then(function (r) { if (!redirected(r) && r.status === 200) location.href = r.data.go; });
    });
  }

  // ─── add-ons ───────────────────────────────────────────────────────────
  function initExtras() {
    chrome('ADD-ONS');
    var offers = $('offers');
    if (Q.iframe_offers) {
      var other = location.protocol + '//' + (location.hostname === '127.0.0.1' ? 'localhost' : '127.0.0.1') + ':' + location.port;
      offers.appendChild(el('iframe', { class: 'offer-frame', src: other + '/airline/offer.html?ep=' + S.ep, title: '' }));
    } else {
      ['disruption', 'cfar'].forEach(function (k) {
        var name = k === 'disruption' ? 'Disruption Assistance' : 'Cancel For Any Reason';
        offers.appendChild(el('div', { class: 'card' }, [el('h2', { text: name.toUpperCase() }), el('div', { class: 'choice' }, [
          el('label', {}, [el('input', { type: 'radio', name: k, onchange: function () { send(k, 'yes'); } }), ' Add ' + name]),
          el('label', {}, [el('input', { type: 'radio', name: k, onchange: function () { send(k, 'no'); } }), ' No thanks – I don\'t want to add this option']),
        ])]));
      });
    }
    function send(kind, choice) { fetch(API + 'offer?ep=' + S.ep, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: kind, choice: choice }) }); }
    var cars = $('cars');
    for (var i = 0; i < 16; i++) {
      var price = (365.97 + i * 23.31).toFixed(2);
      cars.appendChild(el('div', { class: 'car' }, [el('strong', { text: '$' + price }), ' ', el('a', { href: '#', text: 'view details' }),
        el('p', { class: 'muted', text: 'Compact · 4 Doors | 5 Seats | Automatic · Unlimited Miles!' }), el('button', { type: 'button', class: 'btn secondary', text: 'ADD THIS CAR!' })]));
    }
    var self = null;
    document.querySelectorAll('input[name=selfservice]').forEach(function (r) { r.addEventListener('change', function () { self = r.value; }); });
    $('btnExtrasContinue').addEventListener('click', function () {
      api('extras', { self_service: self }).then(function (r) {
        if (redirected(r)) return;
        if (r.status !== 200) { alertBox(r.data.error); return; }
        location.href = r.data.go;
      });
    });
  }

  // ─── payment ───────────────────────────────────────────────────────────
  function initPayment() {
    chrome('PAYMENT');
    var sel = S.selection || {};
    var lines = [
      ['PASSENGER(S)', (S.passengers || []).map(function (p, i) { return (i + 1) + '- ' + p.first + ' ' + p.last; }).join('  ')],
      ['BUNDLE IT', 'Departing: ' + ((sel.out || {}).bundle || '') + ' · Returning: ' + ((sel.ret || {}).bundle || '')],
      ['SEATS', Object.keys(S.seats || {}).sort().map(function (k) { return k + ' ' + S.seats[k]; }).join(', ')],
      ['EXTRAS', 'Disruption Assistance: ' + (S.extras.disruption || '-') + ' · Cancel For Any Reason: ' + (S.extras.cfar || '-') + ' · Self-Service: ' + (S.extras.self_service || '-')],
    ];
    var cart = $('cartLines');
    lines.forEach(function (l) {
      var body = el('div', { class: 'hidden muted', text: l[1] });
      cart.appendChild(el('div', {}, [el('strong', { text: l[0] + ' ' }), el('a', { href: '#0', text: '[view]', onclick: function () { body.classList.toggle('hidden'); } }), body]));
    });
    $('payTotal').textContent = 'Total Payment Due: ' + money(S.total);
    $('payZip').value = (S.contact || {}).zip || '';
    $('payEmail').value = (S.contact || {}).email || '';
    $('btnPurchase').addEventListener('click', function () {
      if (!$('terms').checked) { alertBox('Please accept the terms.'); return; }
      api('purchase', {}).then(function (r) { if (!redirected(r) && r.status === 200) location.href = r.data.go; });
    });
  }

  function start() {
    fetch(API + 'session').then(function (r) { return r.json(); }).then(function (s) {
      S = s; Q = s.quirks || {};
      var page = document.body.getAttribute('data-page');
      if (s.expired && page !== 'home') { location.href = 'index.html?expired=1'; return; }
      ({ home: initHome, select: initSelect, passengers: initPassengers, seats: initSeats, bags: initBags,
         extras: initExtras, payment: initPayment, confirm: function () { chrome(null); } })[page]();
      document.body.setAttribute('data-ready', '1');
    });
  }
  window.KS = { api: api };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
