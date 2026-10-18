// Casa Alameda, an invented resort's booking engine for computer-use evals.
// The pages show what the server says and send what the person does; the
// server decides everything that is scored. With a quirk off a page uses the
// plain version of its pattern (see ../README.md).
(function () {
  'use strict';
  var API = '/hotel/api/';
  var Q = {}, HOTEL = '';
  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
    'October', 'November', 'December'];

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
  function longDate(iso) {
    var p = iso.split('-');
    return MONTHS[+p[1] - 1] + ' ' + (+p[2]) + ' ' + p[0];
  }
  function us(iso) { var p = iso.split('-'); return p[1] + '/' + p[2] + '/' + p[0]; }

  function chrome(step) {
    document.body.insertBefore(el('header', { class: 'top' }, [
      el('a', { href: 'book.html', class: 'brand', text: HOTEL }),
      el('a', { href: '#', text: 'Hotel Site' }), el('a', { href: '#', text: 'English' }),
      el('a', { href: '#', text: "Let's talk" }),
    ]), document.body.firstChild);
    var steps = ['Select Dates', 'Rooms & Rates', 'Extras', 'Review & Checkout', 'Confirmation'];
    $('steps').replaceChildren.apply($('steps'), steps.map(function (s, i) {
      return el('li', { class: i === step ? 'on' : null, text: s });
    }));
  }

  function searchForm(s, onDone) {
    var err = el('p', { class: 'errors' });
    var f = el('form', { class: 'search', 'aria-label': 'Change your search' }, [
      el('label', {}, ['Check-in (MM/DD/YYYY)', el('input', { id: 'checkin', name: 'checkin', value: us(s.checkin) })]),
      el('label', {}, ['Check-out (MM/DD/YYYY)', el('input', { id: 'checkout', name: 'checkout', value: us(s.checkout) })]),
      el('label', {}, ['Adults', el('select', { id: 'adults', name: 'adults' }, [1, 2, 3].map(function (n) {
        return el('option', { value: String(n), selected: n === s.adults, text: String(n) });
      }))]),
      el('label', {}, ['Group or promo code', el('input', { id: 'code', name: 'code', value: s.code || '' })]),
      el('button', { type: 'submit', id: 'update', text: onDone ? 'UPDATE SEARCH' : 'BOOK HOTEL' }),
    ]);
    f.addEventListener('submit', function (ev) {
      ev.preventDefault();
      api('search', { checkin: $('checkin').value, checkout: $('checkout').value, adults: $('adults').value,
        code: $('code').value }).then(function (r) {
        if (r.status !== 200) { err.textContent = r.data.error; return; }
        if (onDone) onDone(); else location.href = 'rooms.html';
      });
    });
    return [f, err];
  }

  // ─── results ──────────────────────────────────────────────────────────
  var open = 'deluxe';
  function initRooms() {
    chrome(1);
    api('results').then(function (r) { render(r.data); });
  }

  function groupPolicy(d) {
    var parts = [
      'Room rates are quoted in U.S. dollars, per room, per night, based on the European Plan (no meals or beverages included, except for the hosted breakfast on the mornings of the 22nd, 23rd and 24th). All taxes and service charges are included.',
      'The minimum check-in age is 18 years. Up to two children ages 0 to 4 stay free when sharing a room with their paying parents.',
      'Reservations may be cancelled free of charge up to 21 days prior to arrival. Cancellations made between 20 and 0 days before arrival incur a fee equal to one night at the group rate. In the event of a no-show, all unused room nights are charged, and no refunds or credits are issued. This also applies to late arrivals and early departures.',
    ];
    if (Q.long_policy) {
      // The real policy went on for paragraphs before the clause a guest needs.
      for (var i = 1; i <= 14; i++) {
        parts.push('Clause ' + i + ': guests agree to the resort rules on noise, pool hours, pets in common areas, ' +
          'smoking, visitors, parking, lost property, damage to rooms, linen, minibar use, spa bookings, ' +
          'restaurant reservations, wedding venue access, transport from the airport and luggage storage, ' +
          'which the front desk can provide in writing on request at any time during the stay.');
      }
    }
    parts.push('A deposit of ' + d.deposit + ' is charged ' + d.deposit_days + ' days before arrival and is refundable until the free cancellation date.');
    parts.push('The group rate is available through October 21, 2026, or until all rooms in the group block are reserved, whichever comes first.');
    return parts.join(' ');
  }

  function rate(room, p, d) {
    var policy = el('p', { class: 'policy closed', text: p.id === 'group' ? groupPolicy(d)
      : 'Our room only flexible rates are based on 2 adults and up to 2 children age 12 or under. Free cancellation up to 24 hours before arrival. No deposit; the stay is charged at check-in.' });
    var more = el('a', { href: '#', class: 'more', text: '> VIEW POLICIES', onclick: function (ev) {
      ev.preventDefault();
      var shut = policy.classList.toggle('closed');
      more.textContent = shut ? '> VIEW POLICIES' : '> HIDE POLICIES';
      if (!shut) api('opened', { room: room.id, part: 'policy:' + p.id });
    } });
    var sel = el('button', { type: 'button', text: 'SELECT', onclick: function () {
      api('select', { room: room.id, plan: p.id }).then(function (r) {
        $('steps').children[3].className = 'on';
        $('checkout').classList.remove('hidden');
        $('checkout').textContent = 'Review & Checkout: total ' + r.data.total.toFixed(2) + ' USD including taxes. Sign in to continue.';
      });
    } });
    return el('div', { class: 'rate' }, [
      el('div', {}, [
        el('h4', { text: p.name }),
        el('div', { text: 'Free cancellation up to ' + p.days + ' day(s) before arrival' }),
        more,
      ]),
      el('div', { class: 'price' }, [el('b', { text: String(p.price) }), ' Per Night',
        Q.from_prices ? el('div', { text: 'Excluding Taxes & Fees' }) : el('div', { text: 'average per night' }), sel]),
      policy,
    ]);
  }

  function render(d) {
    var s = d.search, main = $('main');
    var summary = el('div', { class: 'summary' }, [
      el('span', { text: 'Adults: ' + s.adults }), el('span', { text: 'Rooms: 1' }),
      el('span', { text: 'Hotel Nights: ' + d.nights }),
      el('span', { text: 'Dates: ' + longDate(s.checkin) + ' - ' + longDate(s.checkout) }),
      el('span', { text: 'Code: ' + (s.code || 'none') }),
      el('a', { href: '#', id: 'modify', text: 'MODIFY', onclick: function (ev) {
        ev.preventDefault();
        $('modifyBox').classList.toggle('hidden');
      } }),
    ]);
    var box = el('div', { id: 'modifyBox', class: 'hidden' }, searchForm(s, function () {
      api('results').then(function (r) { render(r.data); });
    }));
    var cards = el('div', { class: 'rooms' }, d.rooms.map(function (room) {
      var on = room.id === open;
      return el('div', { class: 'room-card' }, [
        el('div', { class: 'room-head' }, [
          room.left ? el('div', { class: 'left', text: 'Only ' + room.left + ' Room(s) left' }) : null,
          el('h3', { text: room.name }),
          el('div', { class: 'meta', text: '3 max · 42 m²' }),
          el('div', { class: 'from' }, [Q.from_prices ? 'From ' : '', el('b', { text: String(room.from) }),
            Q.from_prices ? '' : ' average per night']),
        ]),
        el('div', { class: 'room-foot' }, [el('a', { href: '#', class: 'toggle',
          text: Q.toggle_labels ? (on ? 'VIEWING' : 'VIEW DETAILS') : (on ? 'HIDE DETAILS of ' + room.name : 'VIEW DETAILS of ' + room.name),
          onclick: function (ev) {
            ev.preventDefault();
            open = room.id;
            api('opened', { room: room.id, part: 'details' });
            render(d);
          } })]),
      ]);
    }));
    var panels = d.rooms.map(function (room) {
      var on = room.id === open;
      return el('section', { class: 'panel' + (on ? '' : (Q.stacked_panels ? ' closed' : ' gone')), 'aria-hidden': null }, [
        el('h3', { text: room.name }),
        el('p', { text: room.about }),
      ].concat(room.plans.map(function (p) { return rate(room, p, d); })));
    });
    main.replaceChildren.apply(main, [summary, box, cards].concat(panels, [el('p', { id: 'checkout', class: 'hidden' })]));
    document.body.setAttribute('data-ready', '1');
  }

  // ─── entry page ───────────────────────────────────────────────────────
  function initBook() {
    chrome(0);
    api('book_page', {});
    var s = { checkin: '2027-01-01', checkout: '2027-01-02', adults: 2, code: '' };
    var main = $('main');
    main.replaceChildren.apply(main, [el('h1', { text: 'Book your stay' })].concat(searchForm(s, null)));
    document.body.setAttribute('data-ready', '1');
  }

  api('session').then(function (r) {
    if (r.status !== 200) {
      document.body.insertBefore(el('p', { class: 'errors', text: 'This page needs an episode: open it from the task list.' }), document.body.firstChild);
      return;
    }
    Q = r.data.quirks; HOTEL = r.data.hotel;
    var page = document.body.getAttribute('data-page');
    if (page === 'rooms') initRooms(); else initBook();
  });
})();
