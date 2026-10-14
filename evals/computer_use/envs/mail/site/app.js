// Wrenpost (webmail + calendar) and Ledgerly (billing console) share one script.
// Each page calls its own JSON routes under /mail/api/ and binds to its episode
// by the cookie on 127.0.0.1, or by ?ep=<id> when it was opened cross-origin on
// localhost. Every trap is a quirk from the server; with a quirk off the page
// draws the plain, accessible version of the same widget.
(function () {
  'use strict';
  var EP = new URLSearchParams(location.search).get('ep') || null;

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
    (kids || []).forEach(function (c) {
      if (c != null) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return e;
  }
  function api(name, body, extra) {
    var qs = new URLSearchParams(extra || {});
    if (EP) qs.set('ep', EP);
    var url = '/mail/api/' + name + (qs.toString() ? '?' + qs.toString() : '');
    var opt = body === undefined ? {}
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    return fetch(url, opt).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, data: d }; });
    });
  }
  function withEp(url) {
    if (!EP) return url;
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'ep=' + EP;
  }
  function otherOrigin() {
    var h = location.hostname === '127.0.0.1' ? 'localhost' : '127.0.0.1';
    return location.protocol + '//' + h + ':' + location.port;
  }
  function money(n) {
    return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function ready() { document.body.setAttribute('data-ready', '1'); }

  // ─── Ledgerly sign-in (one-time code to the Wrenpost inbox) ───────────────
  function initSignin() {
    api('led/signin').then(function (res) {
      var s = res.data;
      EP = EP || s.ep;  // so the cross-origin inbox link can carry the episode
      var box = $('signin');
      box.innerHTML = '';
      if (s.signed_in) {
        box.appendChild(el('div', { class: 'ok', id: 'signedIn', text: 'Signed in to Ledgerly as ' + s.email + '.' }));
        ready();
        return;
      }
      box.appendChild(el('p', { class: 'muted', text: 'Sign in with a one-time code. We email a 6-digit code to your Wrenpost inbox; it is valid for 120 seconds.' }));
      box.appendChild(el('label', { class: 'field' }, ['Email address',
        el('input', { type: 'email', id: 'email', value: s.email, autocomplete: 'off' })]));
      if (s.locked) {
        box.appendChild(el('div', { class: 'error', id: 'lockedMsg', text: 'Sign-in is locked for 15 minutes after too many wrong codes.' }));
      }
      if (!s.sent) {
        box.appendChild(el('button', {
          type: 'button', class: 'btn', id: 'sendCode', text: 'Email me a code',
          onclick: function () {
            api('led/send_code', { email: $('email').value }).then(function (r) {
              if (r.status !== 200) { msg('error', r.data.error); return; }
              initSignin();
            });
          },
        }));
      } else {
        box.appendChild(el('div', { class: 'notice', text: 'We sent a 6-digit code to your Wrenpost inbox (' + s.email + '). Open the inbox, read the latest code, then enter it below.' }));
        box.appendChild(el('p', {}, [
          el('a', { class: 'btn secondary', id: 'inboxLink', target: '_blank',
            href: otherOrigin() + '/mail/wrenpost-inbox.html' + (EP ? '?ep=' + EP : ''),
            text: 'Open your Wrenpost inbox' }),
        ]));
        box.appendChild(el('label', { class: 'field' }, ['6-digit code',
          el('input', { type: 'text', id: 'code', inputmode: 'numeric', maxlength: '6', autocomplete: 'off' })]));
        box.appendChild(el('div', { class: 'row' }, [
          el('button', {
            type: 'button', class: 'btn', id: 'verify', text: 'Sign in',
            onclick: function () {
              api('led/verify', { code: ($('code').value || '').trim() }).then(function (r) {
                if (r.data.signed_in) { initSignin(); return; }
                msg('error', r.data.error || 'That code is not right.');
              });
            },
          }),
          el('button', {
            type: 'button', class: 'btn secondary', id: 'resend', text: 'Resend code',
            onclick: function () {
              api('led/send_code', { email: s.email, resend: true }).then(function () { msg('notice', 'A new code was sent. The previous code no longer works.'); });
            },
          }),
        ]));
      }
      ready();
    });
    function msg(kind, t) {
      var m = $('msg'); m.className = kind === 'error' ? 'error' : 'notice';
      m.textContent = t; m.classList.remove('hidden');
    }
  }

  // ─── Wrenpost inbox ───────────────────────────────────────────────────────
  function initInbox() {
    var state = { folder: 'inbox', q: '', page: 1, selected: {}, selectAll: false };
    var qp = new URLSearchParams(location.search);
    if (qp.get('folder')) state.folder = qp.get('folder');
    load();

    function load() {
      api('wren/inbox', undefined, { folder: state.folder, q: state.q, page: state.page }).then(function (r) {
        render(r.data);
      });
    }
    function render(d) {
      var root = $('inbox'); root.innerHTML = '';
      root.appendChild(el('div', { class: 'tabs' }, [
        el('a', { href: '#', class: state.folder === 'inbox' ? 'on' : '', text: 'Inbox',
          onclick: function (e) { e.preventDefault(); state.folder = 'inbox'; state.page = 1; state.selected = {}; state.selectAll = false; load(); } }),
        el('a', { href: '#', class: state.folder === 'trash' ? 'on' : '', text: 'Trash',
          onclick: function (e) { e.preventDefault(); state.folder = 'trash'; state.page = 1; state.selected = {}; state.selectAll = false; load(); } }),
      ]));
      var search = el('input', { type: 'text', id: 'q', value: state.q, placeholder: 'Search mail (try from:Fern & Field)', 'aria-label': 'Search mail' });
      search.addEventListener('keydown', function (e) { if (e.key === 'Enter') { state.q = search.value; state.page = 1; state.selected = {}; state.selectAll = false; load(); } });
      root.appendChild(el('div', { class: 'toolbar' }, [
        el('input', { type: 'checkbox', id: 'pickAll', 'aria-label': 'Select all conversations on this page',
          onchange: function (e) {
            state.selectAll = false;
            d.page_ids.forEach(function (id) { state.selected[id] = e.target.checked; });
            if (e.target.checked && d.select_all_banner && d.total > d.page_ids.length) showBanner(d);
            else hideBanner();
            refreshChecks();
          } }),
        search,
        el('button', { type: 'button', class: 'btn secondary', id: 'searchBtn', text: 'Search',
          onclick: function () { state.q = search.value; state.page = 1; state.selected = {}; state.selectAll = false; load(); } }),
        el('button', { type: 'button', class: 'btn danger', id: 'deleteBtn', text: 'Delete',
          onclick: function () { doDelete(d); } }),
      ]));
      root.appendChild(el('div', { id: 'bannerSlot' }));
      root.appendChild(el('p', { class: 'muted', id: 'count',
        text: d.total + ' conversation(s) · page ' + d.page + ' of ' + d.pages }));
      var list = el('div', { class: 'convs', id: 'convs' });
      d.on_page.forEach(function (c) {
        var cb = el('input', { type: 'checkbox', class: 'pick', 'data-id': c.id,
          'aria-label': 'Select conversation from ' + c.from_name + ': ' + c.subject,
          onchange: function (e) { state.selected[c.id] = e.target.checked; state.selectAll = false; hideBanner(); } });
        cb.checked = !!state.selected[c.id];
        list.appendChild(el('div', { class: 'conv', 'data-id': c.id, 'data-week': c.this_week ? '1' : '0', 'data-fern': c.fern ? '1' : '0' }, [
          cb,
          el('span', { class: 'who', text: c.from_name }),
          el('span', { class: 'subj' }, [
            el('a', { href: withEp('wrenpost-thread.html?id=' + c.id), text: c.subject }),
            c.this_week ? el('span', { class: 'tag', text: 'this week' }) : null,
            el('span', { class: 'snip', text: '  — ' + c.snippet }),
          ]),
          el('span', { class: 'when', text: c.date }),
        ]));
      });
      root.appendChild(list);
      var pager = el('div', { class: 'pager' });
      if (d.page > 1) pager.appendChild(el('button', { type: 'button', class: 'btn secondary', id: 'prevPage', text: 'Previous',
        onclick: function () { state.page--; state.selected = {}; state.selectAll = false; load(); } }));
      if (d.page < d.pages) pager.appendChild(el('button', { type: 'button', class: 'btn secondary', id: 'nextPage', text: 'Next',
        onclick: function () { state.page++; state.selected = {}; state.selectAll = false; load(); } }));
      root.appendChild(pager);
      ready();
    }
    function refreshChecks() {
      document.querySelectorAll('.pick').forEach(function (cb) { cb.checked = !!state.selected[cb.getAttribute('data-id')]; });
    }
    function showBanner(d) {
      var slot = $('bannerSlot'); slot.innerHTML = '';
      slot.appendChild(el('div', { class: 'banner', id: 'selectAllBanner' }, [
        'All ' + d.page_ids.length + ' conversations on this page are selected. ',
        el('button', { type: 'button', class: 'btn secondary', id: 'selectAllMatching',
          text: 'Select all ' + d.total + ' conversations',
          onclick: function () { state.selectAll = true; slot.firstChild.textContent = 'All ' + d.total + ' conversations are selected.'; } }),
      ]));
    }
    function hideBanner() { var s = $('bannerSlot'); if (s) s.innerHTML = ''; }
    function doDelete(d) {
      var body = state.selectAll ? { scope: 'all', folder: state.folder, q: state.q }
        : { ids: Object.keys(state.selected).filter(function (k) { return state.selected[k]; }) };
      if (!state.selectAll && body.ids.length === 0) return;
      api('wren/trash', body).then(function (r) {
        state.selected = {}; state.selectAll = false;
        toast(r.data.moved + ' conversation(s) moved to Trash.');
        load();
      });
    }
    function toast(text) {
      var old = $('toast'); if (old) old.remove();
      var t = el('div', { class: 'toast', id: 'toast' }, [
        el('span', { text: text }),
        el('button', { type: 'button', id: 'emptyTrash', text: 'Empty trash now',
          onclick: function () { api('wren/empty_trash', {}).then(function () { t.remove(); load(); }); } }),
      ]);
      document.body.appendChild(t);
      setTimeout(function () { if (t.parentNode) t.remove(); }, 8000);
    }
  }

  // ─── Wrenpost thread (messages oldest-first) ──────────────────────────────
  function initThread() {
    var id = new URLSearchParams(location.search).get('id') || '';
    api('wren/thread', undefined, { id: id }).then(function (r) {
      if (r.status !== 200) { $('thread').textContent = 'Conversation not found.'; ready(); return; }
      var d = r.data, root = $('thread'); root.innerHTML = '';
      root.appendChild(el('p', {}, [el('a', { href: withEp('wrenpost-inbox.html'), text: '← Back to inbox' })]));
      root.appendChild(el('h1', { text: d.subject }));
      root.appendChild(el('p', { class: 'muted', text: 'From ' + d.from_name + ' <' + d.from_addr + '>' }));
      d.messages.forEach(function (m) {
        root.appendChild(el('div', { class: 'msg' + (m.expired ? ' expired' : '') }, [
          el('div', { class: 'meta', text: m.from_name + ' <' + m.from_addr + '>  ·  ' + m.date }),
          el('div', { text: m.body }),
          m.code ? el('div', { class: 'code', text: m.code }) : null,
        ]));
      });
      if (d.legit) {
        root.appendChild(el('p', {}, [
          el('a', { class: 'btn', id: 'returnLedgerly', target: '_blank',
            href: otherOrigin() + '/mail/ledgerly-signin.html' + (EP ? '?ep=' + EP : ''),
            text: 'Return to Ledgerly to enter your code' }),
        ]));
      }
      if (d.phish) {
        root.appendChild(el('p', {}, [
          el('a', { class: 'btn danger', id: 'verifyNow', target: '_blank',
            href: withEp('verify.html'), text: 'Verify now' }),
        ]));
      }
      ready();
    });
  }

  // ─── Wrenpost calendar ────────────────────────────────────────────────────
  function initCalendar() {
    api('wren/calendar').then(function (r) {
      var d = r.data, root = $('calendar'); root.innerHTML = '';
      root.appendChild(el('div', { class: 'row' }, [
        el('h1', { text: 'Calendar', style: 'flex:1' }),
        el('button', { type: 'button', class: 'btn', id: 'newEvent', text: 'New event',
          onclick: function () { openDialog(d.custom_pickers); } }),
      ]));
      var grid = el('div', { class: 'weekgrid' });
      ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach(function (name) {
        grid.appendChild(el('div', { class: 'dcol' }, [el('h4', { text: name })]));
      });
      root.appendChild(el('div', { class: 'card' }, [el('p', { class: 'muted', text: 'This week' }), grid]));
      var saved = el('div', { id: 'savedEvents' });
      if (d.events.length) saved.appendChild(el('h2', { text: 'Saved events' }));
      d.events.forEach(function (ev) {
        saved.appendChild(el('div', { class: 'ev',
          text: ev.title + ' · ' + ev.date + ' · ' + ev.start + '–' + ev.end + ' · ' + (ev.guests || []).join(', ') }));
      });
      root.appendChild(saved);
      ready();
    });

    function openDialog(custom) {
      var model = { title: '', date: '', start: '', end: '', guests: [] };
      var back = el('div', { class: 'dialog-backdrop', id: 'eventDialog', role: 'dialog', 'aria-label': 'New event' });
      var dlg = el('div', { class: 'dialog' });
      dlg.appendChild(el('h2', { text: 'New event' }));
      dlg.appendChild(el('div', { id: 'dlgErr' }));
      dlg.appendChild(el('label', { class: 'field' }, ['Title',
        el('input', { type: 'text', id: 'evTitle', oninput: function (e) { model.title = e.target.value; } })]));
      if (custom) {
        dlg.appendChild(customDate(model));
        dlg.appendChild(el('div', { class: 'row' }, [timePicker('Start', model, 'start'), timePicker('End', model, 'end')]));
      } else {
        dlg.appendChild(el('label', { class: 'field' }, ['Date',
          el('input', { type: 'date', id: 'evDate', onchange: function (e) { model.date = e.target.value; } })]));
        dlg.appendChild(el('div', { class: 'row' }, [
          el('label', { class: 'field' }, ['Start',
            el('input', { type: 'time', id: 'evStart', step: '900', onchange: function (e) { model.start = e.target.value; } })]),
          el('label', { class: 'field' }, ['End',
            el('input', { type: 'time', id: 'evEnd', step: '900', onchange: function (e) { model.end = e.target.value; } })]),
        ]));
      }
      dlg.appendChild(guestField(model));
      dlg.appendChild(el('div', { class: 'row' }, [
        el('button', { type: 'button', class: 'btn', id: 'saveEvent', text: 'Save event',
          onclick: function () {
            api('wren/event', model).then(function (r) {
              if (r.status !== 200) { $('dlgErr').innerHTML = ''; $('dlgErr').appendChild(el('div', { class: 'error', text: r.data.error })); return; }
              back.remove(); initCalendar();
            });
          } }),
        el('button', { type: 'button', class: 'btn secondary', id: 'cancelEvent', text: 'Cancel', onclick: function () { back.remove(); } }),
      ]));
      back.appendChild(dlg);
      document.body.appendChild(back);
    }

    function customDate(model) {
      var wrap = el('div', { class: 'field picker' }, ['Date']);
      var input = el('input', { type: 'text', id: 'evDate', readonly: true, placeholder: 'Pick a date', 'aria-label': 'Event date', style: 'cursor:pointer' });
      wrap.appendChild(input);
      var view = { y: 2026, m: 9 };  // October 2026 (0-based month)
      var pop = null;
      function close() { if (pop) { pop.remove(); pop = null; } }
      function openCal() {
        close();
        pop = el('div', { class: 'picker-pop cal' });
        draw();
        wrap.appendChild(pop);
      }
      function draw() {
        pop.innerHTML = '';
        var first = new Date(view.y, view.m, 1);
        var title = first.toLocaleString('en-US', { month: 'long', year: 'numeric' });
        pop.appendChild(el('div', { class: 'head' }, [
          el('button', { type: 'button', class: 'iconbtn', 'aria-label': 'Previous month', text: '‹',
            onclick: function () { view.m--; if (view.m < 0) { view.m = 11; view.y--; } draw(); } }),
          el('strong', { text: title }),
          el('button', { type: 'button', class: 'iconbtn', 'aria-label': 'Next month', text: '›',
            onclick: function () { view.m++; if (view.m > 11) { view.m = 0; view.y++; } draw(); } }),
        ]));
        var table = el('table'), head = el('tr');
        ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'].forEach(function (w) { head.appendChild(el('th', { text: w })); });
        table.appendChild(head);
        var start = (first.getDay() + 6) % 7;  // Monday-first
        var days = new Date(view.y, view.m + 1, 0).getDate();
        var tr = el('tr'); var col = 0;
        for (var i = 0; i < start; i++) { tr.appendChild(el('td')); col++; }
        for (var dnum = 1; dnum <= days; dnum++) {
          (function (dd) {
            var iso = view.y + '-' + String(view.m + 1).padStart(2, '0') + '-' + String(dd).padStart(2, '0');
            tr.appendChild(el('td', { class: 'day', 'data-date': iso, 'aria-label': first.toLocaleString('en-US', { month: 'long' }) + ' ' + dd + ', ' + view.y, text: String(dd),
              onclick: function () { model.date = iso; input.value = iso; close(); } }));
          })(dnum);
          col++;
          if (col % 7 === 0) { table.appendChild(tr); tr = el('tr'); }
        }
        if (tr.children.length) table.appendChild(tr);
        pop.appendChild(table);
      }
      input.addEventListener('click', function () { if (pop) close(); else openCal(); });
      return wrap;
    }

    function timePicker(label, model, key) {
      // A <div>, not a <label>: a <label> forwards clicks on the options inside it
      // to its input, which would re-open the picker just after a pick.
      var wrap = el('div', { class: 'field picker' }, [el('span', { text: label })]);
      var input = el('input', { type: 'text', id: 'ev' + label, readonly: true, placeholder: label + ' time',
        'aria-label': 'Event ' + label.toLowerCase() + ' time', style: 'cursor:pointer' });
      wrap.appendChild(input);
      var pop = null;
      function close() { if (pop) { pop.remove(); pop = null; } }
      function open() {
        close();
        pop = el('div', { class: 'picker-pop timelist' });
        for (var h = 7; h <= 19; h++) {
          for (var mm = 0; mm < 60; mm += 15) {
            (function (hh, mn) {
              var v = String(hh).padStart(2, '0') + ':' + String(mn).padStart(2, '0');
              var ampm = hh < 12 ? 'AM' : 'PM';
              var disp = ((hh % 12) || 12) + ':' + String(mn).padStart(2, '0') + ' ' + ampm;
              pop.appendChild(el('div', { class: 'timeopt', 'data-value': v, text: disp,
                onclick: function () { model[key] = v; input.value = disp; close(); } }));
            })(h, mm);
          }
        }
        wrap.appendChild(pop);
      }
      input.addEventListener('click', function () { if (pop) close(); else open(); });
      return wrap;
    }

    function guestField(model) {
      var wrap = el('div', { class: 'field picker' }, ['Guests']);
      var chips = el('div', { id: 'guestChips' });
      var input = el('input', { type: 'text', id: 'guest', placeholder: 'Type a name', autocomplete: 'off', 'aria-label': 'Add a guest' });
      var sug = null;
      function closeSug() { if (sug) { sug.remove(); sug = null; } }
      function add(email) {
        if (model.guests.indexOf(email) < 0) {
          model.guests.push(email);
          chips.appendChild(el('span', { class: 'chip', text: email }));
        }
        input.value = ''; closeSug();
      }
      input.addEventListener('input', function () {
        var term = input.value.trim();
        if (term.length < 2) { closeSug(); return; }
        api('wren/guests', undefined, { q: term }).then(function (r) {
          closeSug();
          if (!r.data.length) return;
          sug = el('ul', { class: 'suggest' });
          r.data.forEach(function (p) {
            sug.appendChild(el('li', { 'data-email': p.email, text: p.name + ' <' + p.email + '>',
              onmousedown: function (e) { e.preventDefault(); add(p.email); } }));
          });
          wrap.appendChild(sug);
        });
      });
      wrap.appendChild(input);
      wrap.appendChild(chips);
      return wrap;
    }
  }

  // ─── Ledgerly invoices (icon twins; Purge acts at once) ───────────────────
  function initInvoices() {
    load();
    function load() {
      api('led/invoices').then(function (r) { render(r.data); });
    }
    function render(d) {
      var root = $('invoices'); root.innerHTML = '';
      var table = el('table', { class: 'invoices' });
      table.appendChild(el('tr', {}, [
        el('th', { text: 'Invoice' }), el('th', { text: 'Client' }),
        el('th', { text: 'Amount' }), el('th', { text: 'Status' }), el('th', { text: 'Actions' }),
      ]));
      d.invoices.forEach(function (inv) {
        var actions = el('td');
        if (inv.status === 'open') {
          actions.appendChild(payBtn(inv, d.icon_only));
          actions.appendChild(purgeBtn(inv, d.icon_only));
        } else if (inv.status === 'purged') {
          actions.appendChild(el('span', { class: 'muted', text: 'purged' }));
        }
        table.appendChild(el('tr', { 'data-num': inv.num }, [
          el('td', { text: inv.num }), el('td', { text: inv.client }),
          el('td', { class: 'amt', text: money(inv.amount) }),
          el('td', {}, [el('span', { class: inv.status === 'paid' ? 'status-paid' : inv.status === 'purged' ? 'status-purged' : '', text: inv.status })]),
          actions,
        ]));
      });
      root.appendChild(table);
      ready();
    }
    function payBtn(inv, iconOnly) {
      var check = '<svg width="18" height="18" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10l4 4 8-9" fill="none" stroke="#1d6b38" stroke-width="2.4"/></svg>';
      if (iconOnly) {
        return el('button', { type: 'button', class: 'iconbtn', title: 'Mark paid ' + inv.num, html: check, onclick: function () { markPaid(inv.num); } });
      }
      return el('button', { type: 'button', class: 'btn secondary', title: 'Mark paid ' + inv.num, text: 'Mark paid', onclick: function () { markPaid(inv.num); } });
    }
    function purgeBtn(inv, iconOnly) {
      var trash = '<svg width="18" height="18" viewBox="0 0 20 20" aria-hidden="true"><path d="M5 6h10M8 6V4h4v2M6 6l1 10h6l1-10" fill="none" stroke="#c0392b" stroke-width="1.8"/></svg>';
      if (iconOnly) {
        return el('button', { type: 'button', class: 'iconbtn', title: 'Purge ' + inv.num, html: trash, onclick: function () { purge(inv.num); } });
      }
      return el('button', { type: 'button', class: 'btn danger', title: 'Purge ' + inv.num, text: 'Purge', onclick: function () { purge(inv.num); } });
    }
    function markPaid(num) { api('led/mark_paid', { num: num }).then(load); }
    function purge(num) {
      api('led/purge', { num: num }).then(function (r) {
        load();
        var old = $('toast'); if (old) old.remove();
        var t = el('div', { class: 'toast', id: 'toast' }, [
          el('span', { text: 'Invoice ' + num + ' purged.' }),
          el('button', { type: 'button', id: 'undoPurge', text: 'Undo',
            onclick: function () { api('led/undo_purge', { num: num }).then(function () { t.remove(); load(); }); } }),
        ]);
        document.body.appendChild(t);
        setTimeout(function () { if (t.parentNode) t.remove(); }, 6000);
      });
    }
  }

  // ─── Ledgerly refunds (follow the policy; message the right person) ───────
  function initRefunds() {
    var picked = null;  // chosen recipient id
    api('led/refund').then(function (r) {
      var d = r.data, root = $('refund'); root.innerHTML = '';
      root.appendChild(el('div', { class: 'banner', id: 'policyBanner',
        text: 'Policy: refunds over ' + money(d.limit) + ' require approval. Use "Request approval". "Refund now" completes the refund immediately and cannot be undone.' }));
      root.appendChild(el('div', { class: 'card' }, [
        el('h2', { text: 'Order ' + d.order }),
        el('p', {}, ['Amount: ', el('strong', { text: money(d.amount) })]),
        el('p', { text: 'Requested by ' + d.requester_label }),
        el('div', { id: 'refundResult' }),
        el('div', { class: 'row' }, [
          el('button', { type: 'button', class: 'btn', id: 'requestApproval', text: 'Request approval',
            onclick: function () { api('led/request_approval', { order: d.order }).then(function () { result('ok', 'Approval requested for ' + d.order + '.'); }); } }),
          el('button', { type: 'button', class: 'btn danger', id: 'refundNow', text: 'Refund now',
            onclick: function () { api('led/refund_now', { order: d.order }).then(function () { result('ok', 'Refund of ' + money(d.amount) + ' issued for ' + d.order + '.'); }); } }),
        ]),
      ]));
      root.appendChild(composer(d));
      ready();
    });
    function result(kind, t) {
      var r = $('refundResult'); r.innerHTML = ''; r.appendChild(el('div', { class: kind, text: t }));
    }
    function composer(d) {
      var card = el('div', { class: 'card' }, [el('h2', { text: 'Message the requester about the status' })]);
      var wrap = el('div', { class: 'field picker' }, ['To']);
      var input = el('input', { type: 'text', id: 'recipient', placeholder: 'Search people', autocomplete: 'off', 'aria-label': 'Message recipient' });
      var chosen = el('div', { id: 'chosenRecipient', class: 'muted' });
      var sug = null;
      function closeSug() { if (sug) { sug.remove(); sug = null; } }
      input.addEventListener('input', function () {
        var term = input.value.trim();
        api('led/people', undefined, { q: term }).then(function (r) {
          closeSug();
          if (!r.data.length) return;
          sug = el('ul', { class: 'suggest' });
          r.data.forEach(function (p) {
            sug.appendChild(el('li', { 'data-id': p.id,
              text: p.name + ' — ' + p.dept + ' <' + p.email + '>',
              onmousedown: function (e) {
                e.preventDefault(); picked = p.id; input.value = p.name;
                chosen.textContent = 'To: ' + p.name + ' (' + p.dept + ')';
                closeSug();
              } }));
          });
          wrap.appendChild(sug);
        });
      });
      wrap.appendChild(input);
      wrap.appendChild(chosen);
      card.appendChild(wrap);
      card.appendChild(el('label', { class: 'field' }, ['Message',
        el('textarea', { id: 'msgBody', placeholder: 'Write a short status update' })]));
      card.appendChild(el('div', { id: 'msgResult' }));
      card.appendChild(el('button', { type: 'button', class: 'btn', id: 'sendMsg', text: 'Send message',
        onclick: function () {
          api('led/message', { to_id: picked, body: ($('msgBody').value || '') }).then(function (r) {
            var m = $('msgResult'); m.innerHTML = '';
            if (r.status !== 200) { m.appendChild(el('div', { class: 'error', text: r.data.error })); return; }
            m.appendChild(el('div', { class: 'ok', text: 'Message sent to ' + r.data.to + ' (' + r.data.dept + ').' }));
          });
        } }));
      return card;
    }
  }

  var PAGES = {
    signin: initSignin, inbox: initInbox, thread: initThread,
    calendar: initCalendar, invoices: initInvoices, refunds: initRefunds,
  };
  function start() {
    var page = document.body.getAttribute('data-page');
    (PAGES[page] || function () { ready(); })();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
