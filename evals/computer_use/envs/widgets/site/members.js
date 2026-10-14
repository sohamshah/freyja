// Members: 2,000 rows, sorted by email, in a table body that scrolls on its own
// (the page itself never scrolls). The table is virtualized: only the rows near
// the visible window are in the DOM (about 25), keyed by row, so a row that is
// not near the window cannot be found in the page at all. Each row has a
// Suspend (or Reactivate) button. Quirk no_search off: a filter box sits above.
(function () {
  'use strict';
  var ROW = 36, OVERSCAN = 3;

  TC.pages.members = function (s) {
    var el = TC.el, $ = TC.$, Q = s.quirks || {};
    var scroller = $('memberScroll'), spacer = $('memberSpacer'), foot = $('memberFoot');
    var all = [], rows = [], nodes = new Map(), query = '';

    function rowFor(m, i) {
      var act = m.status === 'suspended'
        ? el('button', { type: 'button', class: 'btn small act', text: 'Reactivate', onclick: function () { change(m, 'reactivate'); } })
        : el('button', { type: 'button', class: 'btn small act', text: 'Suspend', onclick: function () { change(m, 'suspend'); } });
      var r = el('div', {
        class: 'mrow', role: 'row', 'aria-rowindex': String(i + 2),
        'data-email': m.email, 'data-status': m.status,
      }, [
        el('div', { role: 'cell', text: m.name }),
        el('div', { role: 'cell', text: m.email }),
        el('div', { role: 'cell', text: m.role }),
        el('div', { role: 'cell' }, [el('span', { class: 'badge ' + m.status, text: m.status.charAt(0).toUpperCase() + m.status.slice(1) })]),
        el('div', { role: 'cell', text: m.last }),
        el('div', { role: 'cell' }, [act]),
      ]);
      r.style.top = (i * ROW) + 'px';
      return r;
    }

    function render(force) {
      var top = scroller.scrollTop, h = scroller.clientHeight || 600;
      var first = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
      var last = Math.min(rows.length - 1, Math.ceil((top + h) / ROW) + OVERSCAN);
      nodes.forEach(function (n, i) {
        if (force || i < first || i > last) { n.remove(); nodes.delete(i); }
      });
      // Rows stay in the DOM in row order, as keyed list renderers keep them.
      var next = null;
      for (var i = last; i >= first; i--) {
        if (!nodes.has(i)) {
          var n = rowFor(rows[i], i);
          nodes.set(i, n);
          spacer.insertBefore(n, next);
        }
        next = nodes.get(i);
      }
      var a = Math.min(rows.length, Math.floor(top / ROW) + 1), b = Math.min(rows.length, Math.ceil((top + h) / ROW));
      var total = all.length.toLocaleString('en-US');
      foot.textContent = (query
        ? rows.length.toLocaleString('en-US') + ' of ' + total + ' members match'
        : total + ' members, sorted by email (A to Z)') +
        (rows.length ? ' · showing rows ' + a.toLocaleString('en-US') + '–' + b.toLocaleString('en-US') : ' · no rows');
    }

    function relayout() {
      spacer.style.height = (rows.length * ROW) + 'px';
      render(true);
    }

    function change(m, how) {
      TC.api('members/' + how, { id: m.id }).then(function (r) {
        if (r.status !== 200) return;
        m.status = r.data.member.status;
        render(true);
      });
    }

    if (!Q.no_search) {
      var f = el('input', { type: 'search', id: 'memberFilter', placeholder: 'Filter by name or email', 'aria-label': 'Filter members by name or email' });
      f.addEventListener('input', function () {
        query = f.value.trim().toLowerCase();
        rows = query ? all.filter(function (m) { return (m.name + ' ' + m.email).toLowerCase().indexOf(query) >= 0; }) : all;
        scroller.scrollTop = 0;
        relayout();
      });
      $('filterSlot').appendChild(el('div', { class: 'filterbox' }, [f]));
    }

    scroller.addEventListener('scroll', function () { render(false); });
    window.addEventListener('resize', function () { render(true); });

    return TC.api('members').then(function (r) {
      all = r.data.members || [];
      rows = all;
      // For a privileged solver only (page scripts in an isolated world cannot see it).
      TC.memberIndex = function (email) { return rows.findIndex(function (m) { return m.email === email; }); };
      relayout();
    });
  };
})();
