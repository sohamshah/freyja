// Workspace: a menu bar (File, Edit, View, Help). A click opens a top menu.
// Inside View, "Appearance ▶" and "Security ▶" open their submenus on
// pointerenter, and the submenu's items are added to the DOM only then; a
// submenu closes 300 ms after the pointer leaves it. Quirk hover_only (on):
// hovering is the only way in. Off: a click on the parent (or Enter, or Right)
// opens it too and keeps it open until clicked again.
(function () {
  'use strict';
  var CLOSE_DELAY_MS = 300;
  var MENUS = [
    { id: 'file', label: 'File', items: [
      { id: 'new_policy', label: 'New policy' }, { id: 'open', label: 'Open…' },
      { id: 'duplicate', label: 'Duplicate' }, { sep: true },
      { id: 'export_csv', label: 'Export as CSV' }, { id: 'print', label: 'Print…' }, { sep: true },
      { id: 'sign_out', label: 'Sign out' },
    ] },
    { id: 'edit', label: 'Edit', items: [
      { id: 'undo', label: 'Undo' }, { id: 'redo', label: 'Redo' }, { sep: true },
      { id: 'cut', label: 'Cut' }, { id: 'copy', label: 'Copy' }, { id: 'paste', label: 'Paste' }, { sep: true },
      { id: 'select_all', label: 'Select all' }, { id: 'find', label: 'Find…' },
    ] },
    { id: 'view', label: 'View', items: [
      { id: 'zoom_in', label: 'Zoom in' }, { id: 'zoom_out', label: 'Zoom out' },
      { id: 'actual_size', label: 'Actual size' }, { sep: true },
      { id: 'toggle_sidebar', label: 'Show sidebar', check: 'sidebar' },
      { id: 'compact', label: 'Compact density', check: 'compact' }, { sep: true },
      { sub: 'appearance', label: 'Appearance', items: [
        { id: 'theme_light', label: 'Light', radio: 'light' }, { id: 'theme_dark', label: 'Dark', radio: 'dark' },
        { id: 'theme_system', label: 'Match system', radio: 'system' },
      ] },
      { sub: 'security', label: 'Security', items: [
        { id: 'show_passwords', label: 'Show passwords', check: 'show_passwords' },
        { id: 'mask_emails', label: 'Mask email addresses', check: 'mask_emails' },
        { id: 'audit_trail', label: 'Show audit trail', check: 'audit_trail' }, { sep: true },
        { id: 'lock', label: 'Lock console now' },
      ] },
    ] },
    { id: 'help', label: 'Help', items: [
      { id: 'shortcuts', label: 'Keyboard shortcuts' }, { id: 'docs', label: 'Documentation' },
      { id: 'release_notes', label: 'Release notes' }, { sep: true },
      { id: 'about', label: 'About Tessaly Console' },
    ] },
  ];

  TC.pages.workspace = function (s) {
    var el = TC.el, $ = TC.$, hoverOnly = !!(s.quirks || {}).hover_only;
    var bar = $('menubar'), settings = {}, open = null;

    function leaf(it) {
      var role = it.check ? 'menuitemcheckbox' : it.radio ? 'menuitemradio' : 'menuitem';
      var on = it.check ? !!settings[it.check] : it.radio ? settings.theme === it.radio : null;
      var e = el('div', {
        role: role, tabindex: '-1', class: 'mitem', 'data-item': it.id,
        'aria-checked': on === null ? null : String(on),
      }, [el('span', {}, [el('span', { class: 'tick', 'aria-hidden': 'true' }), it.label])]);
      e.addEventListener('click', function (ev) { ev.stopPropagation(); choose(it.id); });
      e.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); choose(it.id); }
      });
      return e;
    }

    function parent(it) {
      var wrap = el('div', { class: 'msub', role: 'none', 'data-sub': it.sub });
      var label = el('div', {
        role: 'menuitem', tabindex: '-1', class: 'mitem parent', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      }, [el('span', {}, [el('span', { class: 'tick', 'aria-hidden': 'true' }), it.label]),
        el('span', { class: 'arrow', 'aria-hidden': 'true', text: '▶' })]);
      wrap.appendChild(label);
      var st = { wrap: wrap, label: label, it: it, drop: null, timer: null, pinned: false };
      wrap.addEventListener('pointerenter', function () { clearTimeout(st.timer); openSub(st, 'hover'); });
      wrap.addEventListener('pointerleave', function () {
        if (st.pinned) return;
        clearTimeout(st.timer);
        st.timer = setTimeout(function () { closeSub(st); }, CLOSE_DELAY_MS);
      });
      label.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (hoverOnly) return;
        if (st.drop && st.pinned) closeSub(st); else openSub(st, 'click');
      });
      label.addEventListener('keydown', function (ev) {
        if (hoverOnly || !(ev.key === 'Enter' || ev.key === ' ' || ev.key === 'ArrowRight')) return;
        ev.preventDefault();
        openSub(st, 'click');
        var first = st.drop.querySelector('.mitem');
        if (first) first.focus();
      });
      return st;
    }

    function openSub(st, how) {
      if (how === 'click') st.pinned = true;
      if (st.drop) return;
      if (open) open.subs.forEach(function (o) { if (o !== st) closeSub(o); });
      var drop = el('div', { class: 'mdrop sub', role: 'menu', 'aria-label': st.it.label });
      st.it.items.forEach(function (x) {
        drop.appendChild(x.sep ? el('div', { class: 'msep', role: 'separator' }) : leaf(x));
      });
      st.wrap.appendChild(drop);
      st.wrap.classList.add('open');
      st.label.setAttribute('aria-expanded', 'true');
      st.drop = drop;
    }
    function closeSub(st) {
      clearTimeout(st.timer);
      st.pinned = false;
      if (!st.drop) return;
      st.drop.remove();
      st.drop = null;
      st.wrap.classList.remove('open');
      st.label.setAttribute('aria-expanded', 'false');
    }

    function openMenu(m, btn) {
      closeMenu();
      var drop = el('div', { class: 'mdrop', role: 'menu', 'aria-label': m.label, 'data-menu': m.id });
      var subs = [];
      m.items.forEach(function (it) {
        if (it.sep) drop.appendChild(el('div', { class: 'msep', role: 'separator' }));
        else if (it.sub) { var st = parent(it); subs.push(st); drop.appendChild(st.wrap); }
        else drop.appendChild(leaf(it));
      });
      btn.parentNode.appendChild(drop);
      btn.setAttribute('aria-expanded', 'true');
      open = { menu: m, btn: btn, drop: drop, subs: subs };
    }
    function closeMenu() {
      if (!open) return;
      open.subs.forEach(closeSub);
      open.drop.remove();
      open.btn.setAttribute('aria-expanded', 'false');
      open = null;
    }

    function choose(id) {
      closeMenu();
      TC.api('workspace/choose', { item: id }).then(function (r) {
        if (r.status === 200) { settings = r.data.settings; paint(); }
      });
    }

    function paint() {
      var rows = [
        ['Zoom', settings.zoom + '%'],
        ['Sidebar', settings.sidebar ? 'shown' : 'hidden'],
        ['Density', settings.compact ? 'compact' : 'comfortable'],
        ['Theme', settings.theme === 'system' ? 'match system' : settings.theme],
        ['Passwords', settings.show_passwords ? 'shown' : 'masked'],
        ['Email addresses', settings.mask_emails ? 'masked' : 'shown'],
        ['Audit trail', settings.audit_trail ? 'shown' : 'hidden'],
        ['Console', settings.locked ? 'locked' : 'unlocked'],
      ];
      var dl = $('viewSettings');
      dl.innerHTML = '';
      rows.forEach(function (x) { dl.appendChild(el('dt', { text: x[0] })); dl.appendChild(el('dd', { text: x[1] })); });
      var side = document.querySelector('.side');
      if (side) side.classList.toggle('hidden', !settings.sidebar);
      document.body.setAttribute('data-show-passwords', settings.show_passwords ? '1' : '0');
    }

    MENUS.forEach(function (m) {
      var btn = el('button', {
        type: 'button', role: 'menuitem', class: 'mbtn', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
        'data-menu': m.id, text: m.label,
      });
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (open && open.menu === m) closeMenu(); else openMenu(m, btn);
      });
      bar.appendChild(el('div', { class: 'mtop', role: 'none' }, [btn]));
    });
    document.addEventListener('click', function (ev) { if (open && !bar.contains(ev.target)) closeMenu(); });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && open) { var b = open.btn; closeMenu(); b.focus(); }
    });

    return TC.api('workspace').then(function (r) { settings = r.data.settings || {}; paint(); });
  };
})();
