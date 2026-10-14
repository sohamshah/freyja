// Release checklist: eight cards to reorder, then "Save order". How a card
// moves depends on the episode's mode (one task each):
//   menu     each card's "⋯" button opens Move up / Move down / Move to top
//   keyboard focus a card's handle, Enter (or Space) picks it up, Up/Down move
//            it, Enter drops it, Escape cancels; a live region says each move
//   drag     only a trusted pointer drag on the handle moves a card
// Every mode also takes a trusted pointer drag. The server keeps its own copy of
// the order, move by move; "Save order" posts the order the cards show.
// Quirks: unnamed_icons off names the ⋯ button and the handle for their card and
// gives the menu its roles; no_move_buttons off adds Move up / Move down buttons.
(function () {
  'use strict';
  var GRIP = '<svg width="12" height="18" viewBox="0 0 12 18" aria-hidden="true" focusable="false" fill="currentColor">' +
    '<circle cx="3" cy="3" r="1.6"/><circle cx="9" cy="3" r="1.6"/><circle cx="3" cy="9" r="1.6"/>' +
    '<circle cx="9" cy="9" r="1.6"/><circle cx="3" cy="15" r="1.6"/><circle cx="9" cy="15" r="1.6"/></svg>';
  var DOTS = '<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true" focusable="false" fill="currentColor">' +
    '<circle cx="3.5" cy="9" r="1.7"/><circle cx="9" cy="9" r="1.7"/><circle cx="14.5" cy="9" r="1.7"/></svg>';
  var HINTS = {
    menu: 'Reorder the steps, then save the order.',
    keyboard: 'Drag a card by its handle to move it. With the keyboard: focus a card’s handle, press Enter to pick the card up, Up or Down to move it, and Enter again to drop it. Escape puts it back.',
    drag: 'Drag a card by its handle to move it, then save the order.',
  };

  TC.pages.checklist = function (s) {
    var el = TC.el, $ = TC.$, Q = s.quirks || {}, mode = s.mode || 'menu';
    var named = !Q.unnamed_icons, buttons = !Q.no_move_buttons;
    var items = {}, order = [], base = [];
    var list = $('ccards'), live = $('dndLive');
    var lift = null, drag = null, menu = null, saves = 0;
    var queue = Promise.resolve();

    $('hint').textContent = HINTS[mode] || HINTS.menu;
    if (mode !== 'keyboard') $('dndHelp').remove();

    // One request at a time, so a save never overtakes the move before it.
    function call(name, body) {
      var p = queue.then(function () { return TC.api(name, body); });
      queue = p.catch(function () {});
      return p;
    }
    function title(id) { return items[id].title; }
    function say(t) { live.textContent = t; }
    function cardEl(id) { return list.querySelector('.ccard[data-id="' + id + '"]'); }
    function cards() { return Array.prototype.slice.call(list.querySelectorAll('.ccard')); }
    function domOrder() { return cards().map(function (c) { return c.getAttribute('data-id'); }); }
    function status() {
      $('orderState').textContent = order.join() === base.join() ? 'No unsaved changes' : 'Unsaved changes';
    }

    function render(focus) {
      closeMenu();
      list.innerHTML = '';
      order.forEach(function (id, i) { list.appendChild(card(id, i)); });
      status();
      if (focus) {
        var f = list.querySelector('.ccard[data-id="' + focus.id + '"] ' + focus.sel);
        if (f && !f.disabled) f.focus();
      }
    }

    function card(id, i) {
      var it = items[id], n = order.length;
      var li = el('li', { class: 'ccard', 'data-id': id });
      var handle;
      if (mode === 'keyboard') {
        handle = el('button', {
          type: 'button', class: 'handle', 'aria-roledescription': 'sortable',
          'aria-describedby': 'dndHelp', 'aria-label': named ? 'Reorder ' + it.title : null,
        });
        handle.addEventListener('keydown', function (e) { onKey(e, id); });
        handle.addEventListener('blur', function () { if (lift && lift.id === id) cancel(true); });
      } else {
        handle = el('span', { class: 'handle', 'aria-hidden': 'true' });
      }
      handle.innerHTML = GRIP;
      handle.addEventListener('pointerdown', function (e) { startDrag(e, id, li); });
      li.appendChild(handle);
      li.appendChild(el('div', { class: 'cbody' }, [
        el('p', { class: 'ctitle', text: it.title }),
        el('p', { class: 'muted cmeta', text: it.owner + ' · ' + it.estimate }),
      ]));
      if (buttons) {
        li.appendChild(el('button', {
          type: 'button', class: 'btn small moveup', 'aria-label': 'Move ' + it.title + ' up',
          disabled: i === 0, text: 'Move up',
          onclick: function () { move(id, i - 1, 'button', { id: id, sel: '.moveup' }); },
        }));
        li.appendChild(el('button', {
          type: 'button', class: 'btn small movedown', 'aria-label': 'Move ' + it.title + ' down',
          disabled: i === n - 1, text: 'Move down',
          onclick: function () { move(id, i + 1, 'button', { id: id, sel: '.movedown' }); },
        }));
      }
      if (mode === 'menu') {
        var b = el('button', {
          type: 'button', class: 'card-act', 'aria-label': named ? 'Actions for ' + it.title : null,
          'aria-haspopup': named ? 'menu' : null, 'aria-expanded': named ? 'false' : null,
        });
        b.innerHTML = DOTS;
        b.addEventListener('click', function (e) { e.stopPropagation(); toggleMenu(id, li, b); });
        li.appendChild(b);
      }
      return li;
    }

    // The page shows a move at once; the server applies the same move to its copy.
    function move(id, to, via, focus, msg) {
      var from = order.indexOf(id);
      if (to < 0 || to >= order.length || to === from) return;
      order.splice(from, 1);
      order.splice(to, 0, id);
      render(focus);
      say(msg || title(id) + ' moved to position ' + (to + 1) + ' of ' + order.length + '.');
      call('checklist/move', { id: id, to: to, via: via }).then(function (r) {
        if (r.status === 200 && r.data.order.join() === order.join()) return;
        return call('checklist').then(function (g) { order = g.data.order.slice(); render(focus); });
      });
    }

    // ─── the ⋯ menu ────────────────────────────────────────────────────
    function toggleMenu(id, li, b) {
      if (menu && menu.id === id) { closeMenu(); return; }
      closeMenu();
      var i = order.indexOf(id), n = order.length;
      var m = el('div', { class: 'popmenu', role: named ? 'menu' : null, 'aria-label': named ? 'Actions for ' + title(id) : null });
      [['Move up', i - 1, i === 0], ['Move down', i + 1, i === n - 1], ['Move to top', 0, i === 0]].forEach(function (x) {
        m.appendChild(el('button', {
          type: 'button', role: named ? 'menuitem' : null, disabled: x[2], text: x[0],
          onclick: function (e) { e.stopPropagation(); closeMenu(); move(id, x[1], 'menu', { id: id, sel: '.card-act' }); },
        }));
      });
      li.appendChild(m);
      if (named) b.setAttribute('aria-expanded', 'true');
      menu = { id: id, el: m, btn: b };
    }
    function closeMenu() {
      if (!menu) return;
      menu.el.remove();
      if (named && menu.btn.isConnected) menu.btn.setAttribute('aria-expanded', 'false');
      menu = null;
    }
    document.addEventListener('click', function (e) { if (menu && !menu.el.contains(e.target)) closeMenu(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && menu) { var b = menu.btn; closeMenu(); b.focus(); }
    });

    // ─── keyboard: lift, move, drop ────────────────────────────────────
    function onKey(e, id) {
      var k = e.key, act = k === 'Enter' || k === ' ' || k === 'Spacebar';
      if (!lift) {
        if (act) { e.preventDefault(); startLift(id); }
        return;
      }
      if (lift.id !== id) return;
      if (k === 'ArrowUp' || k === 'ArrowDown') { e.preventDefault(); shift(k === 'ArrowUp' ? -1 : 1); }
      else if (act) { e.preventDefault(); drop(); }
      else if (k === 'Escape') { e.preventDefault(); cancel(false); }
    }
    function startLift(id) {
      var at = order.indexOf(id);
      lift = { id: id, from: at, at: at };
      var li = cardEl(id);
      li.classList.add('lifted');
      li.querySelector('.handle').setAttribute('aria-pressed', 'true');
      say('Picked up ' + title(id) + '. It is in position ' + (at + 1) + ' of ' + order.length +
        '. Use the Up and Down arrow keys to move it, Enter to drop it, Escape to cancel.');
    }
    // Moves the neighbour, not the lifted card, so the handle keeps its focus.
    function shift(d) {
      var to = lift.at + d, li = cardEl(lift.id);
      if (to < 0 || to >= order.length) {
        say(title(lift.id) + ' is already at the ' + (d < 0 ? 'top.' : 'bottom.'));
        return;
      }
      if (d < 0) list.insertBefore(li.previousElementSibling, li.nextElementSibling);
      else list.insertBefore(li.nextElementSibling, li);
      lift.at = to;
      say(title(lift.id) + ' moved to position ' + (to + 1) + ' of ' + order.length + '.');
    }
    function drop() {
      var l = lift;
      lift = null;
      var msg = title(l.id) + ' dropped at position ' + (l.at + 1) + ' of ' + order.length + '.';
      if (l.at === l.from) { render({ id: l.id, sel: '.handle' }); say(msg); return; }
      move(l.id, l.at, 'keyboard', { id: l.id, sel: '.handle' }, msg);
    }
    function cancel(quietly) {
      var l = lift;
      lift = null;
      render(quietly ? null : { id: l.id, sel: '.handle' });
      say('Movement cancelled. ' + title(l.id) + ' is back in position ' + (l.from + 1) + ' of ' + order.length + '.');
    }

    // ─── pointer drag: trusted events only ─────────────────────────────
    function startDrag(e, id, li) {
      if (!e.isTrusted || e.button !== 0 || lift) return;
      e.preventDefault();  // no text selection; this also stops the press from focusing...
      var h = e.currentTarget;
      if (h && h.tagName === 'BUTTON') h.focus({ preventScroll: true });  // ...so focus it here
      closeMenu();
      drag = { id: id, li: li, y0: e.clientY, on: false };
    }
    function others(li) { return cards().filter(function (c) { return c !== li; }); }
    function slot(li, y) {
      return others(li).filter(function (c) { var r = c.getBoundingClientRect(); return r.top + r.height / 2 < y; }).length;
    }
    function mark(li, to) {
      cards().forEach(function (c) { c.classList.remove('drop-before', 'drop-after'); });
      if (to < 0) return;
      var o = others(li);
      if (to < o.length) o[to].classList.add('drop-before');
      else if (o.length) o[o.length - 1].classList.add('drop-after');
    }
    document.addEventListener('pointermove', function (e) {
      if (!drag || !e.isTrusted) return;
      var dy = e.clientY - drag.y0;
      if (!drag.on) {
        if (Math.abs(dy) < 4) return;
        drag.on = true;
        drag.li.classList.add('dragging');
      }
      drag.li.style.transform = 'translateY(' + dy + 'px)';
      mark(drag.li, slot(drag.li, e.clientY));
    });
    function endDrag(e, commit) {
      if (!drag || !e.isTrusted) return;
      var d = drag;
      drag = null;
      d.li.classList.remove('dragging');
      d.li.style.transform = '';
      mark(d.li, -1);
      if (!commit || !d.on) return;
      var to = slot(d.li, e.clientY);
      if (to !== order.indexOf(d.id)) move(d.id, to, 'drag');
    }
    document.addEventListener('pointerup', function (e) { endDrag(e, true); });
    document.addEventListener('pointercancel', function (e) { endDrag(e, false); });

    $('saveOrder').addEventListener('click', function () {
      if (lift) cancel(true);
      closeMenu();
      call('checklist/save', { order: domOrder() }).then(function (r) {
        if (r.status !== 200) return;
        base = r.data.saved.slice();
        status();
        list.setAttribute('data-saves', String(++saves));
      });
    });

    return TC.api('checklist').then(function (r) {
      r.data.items.forEach(function (it) { items[it.id] = it; });
      order = r.data.order.slice();
      base = (r.data.saved || r.data.order).slice();
      render(null);
    });
  };
})();
