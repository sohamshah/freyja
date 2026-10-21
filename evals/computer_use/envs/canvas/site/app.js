// Ledger: the whole interface is drawn on one canvas. Every control is a
// region in `regions`, rebuilt on each render and hit-tested on click; the
// keyboard goes to the canvas itself (it has the focus after a click), so the
// page has no DOM control and no DOM text. `window.__ledger` is for the
// privileged oracle only: it answers where a region is.
(function () {
  'use strict';
  var API = '/canvas/api/';
  var canvas = document.getElementById('app');
  var ctx = canvas.getContext('2d');
  var FONT = '-apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif';
  var SIDEBAR = 200, TOP = 56;

  var model = null;        // the server's state: orders, settings, timezones, quirks
  var page = 'orders';
  var drawer = null;       // order id whose drawer is open
  var confirming = null;   // order id the confirmation sheet asks about
  var draft = null;        // settings being edited
  var focus = null;        // 'name' while the business name field has the keyboard
  var selectAll = false;   // the next typed character replaces the field
  var dropdown = false;    // the timezone list is open
  var toast = null;        // {text, until}
  var regions = [];

  function api(name, body) {
    var opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
    return fetch(API + name, opt).then(function (r) { return r.json(); });
  }

  function load() {
    return api('state').then(function (m) {
      model = m;
      if (draft === null) draft = Object.assign({}, m.settings);
      render();
    });
  }

  function money(cents) {
    var d = Math.floor(cents / 100), c = cents % 100;
    return '$' + d.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + (c < 10 ? '0' : '') + c;
  }

  function quirk(name) { return !!(model && model.quirks && model.quirks[name]); }

  // ─── drawing helpers ───
  function rect(x, y, w, h, fill, stroke, r) {
    r = r || 0;
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke(); }
  }

  function text(s, x, y, o) {
    o = o || {};
    ctx.font = (o.weight || 400) + ' ' + (o.size || 14) + 'px ' + FONT;
    ctx.fillStyle = o.color || '#111827';
    ctx.textAlign = o.align || 'left';
    ctx.textBaseline = o.baseline || 'middle';
    ctx.fillText(s, x, y);
  }

  function region(id, x, y, w, h, data) {
    regions.push({ id: id, x: x, y: y, w: w, h: h, data: data || null });
  }

  function button(id, label, x, y, w, h, kind) {
    var fill = kind === 'primary' ? '#2563eb' : kind === 'danger' ? '#dc2626' : '#ffffff';
    var color = kind === 'primary' || kind === 'danger' ? '#ffffff' : '#111827';
    rect(x, y, w, h, fill, kind ? null : '#d1d5db', 6);
    text(label, x + w / 2, y + h / 2, { align: 'center', color: color, weight: 500 });
    region(id, x, y, w, h);
  }

  function toggle(id, label, x, y, on) {
    var small = quirk('small_controls');
    var w = small ? 34 : 48, h = small ? 18 : 26, k = h - 4;
    rect(x, y, w, h, on ? '#16a34a' : '#9ca3af', null, h / 2);
    rect(on ? x + w - k - 2 : x + 2, y + 2, k, k, '#ffffff', null, k / 2);
    text(label, x + w + 12, y + h / 2);
    region(id, x, y, w, h);
  }

  // ─── pages ───
  function drawChrome(W, H) {
    rect(0, 0, W, H, '#f3f4f6');
    rect(0, 0, W, TOP, '#111827');
    text('Ledger', 24, TOP / 2, { color: '#ffffff', size: 18, weight: 500 });
    text(model.settings.name, W - 24, TOP / 2, { color: '#d1d5db', align: 'right' });
    rect(0, TOP, SIDEBAR, H - TOP, '#ffffff', null);
    ctx.fillStyle = '#e5e7eb'; ctx.fillRect(SIDEBAR - 1, TOP, 1, H - TOP);
    var items = [['orders', 'Orders'], ['customers', 'Customers'], ['settings', 'Settings']];
    items.forEach(function (it, i) {
      var y = TOP + 16 + i * 44;
      if (page === it[0]) rect(8, y, SIDEBAR - 16, 36, '#eff6ff', null, 6);
      text(it[1], 24, y + 18, { color: page === it[0] ? '#1d4ed8' : '#374151', weight: page === it[0] ? 500 : 400 });
      region('nav:' + it[0], 8, y, SIDEBAR - 16, 36);
    });
  }

  function drawOrders(W, H) {
    var x0 = SIDEBAR + 32, y = TOP + 28;
    text('Orders', x0, y, { size: 22, weight: 500 });
    y += 36;
    var cols = [0, 90, 310, 440, 560];
    var rowH = quirk('small_controls') ? 36 : 48;
    var width = 660;
    rect(x0, y, width, rowH * (model.orders.length + 1) + 1, '#ffffff', '#e5e7eb', 6);
    ['Order', 'Customer', 'Date', 'Amount', 'Status'].forEach(function (h, i) {
      text(h, x0 + 16 + cols[i], y + rowH / 2, { color: '#6b7280', size: 12, weight: 500 });
    });
    var open = 0, total = 0;
    model.orders.forEach(function (o, i) {
      var ry = y + rowH * (i + 1);
      ctx.fillStyle = '#e5e7eb'; ctx.fillRect(x0, ry, width, 1);
      if (drawer === o.id) rect(x0 + 1, ry + 1, width - 2, rowH - 1, '#eff6ff');
      text('#' + o.id, x0 + 16 + cols[0], ry + rowH / 2);
      text(o.customer, x0 + 16 + cols[1], ry + rowH / 2);
      text(o.date, x0 + 16 + cols[2], ry + rowH / 2, { color: '#6b7280' });
      text(money(o.cents), x0 + 16 + cols[3], ry + rowH / 2);
      var pill = o.status === 'open' ? ['#dbeafe', '#1e40af'] : o.status === 'shipped' ? ['#dcfce7', '#166534'] : ['#fee2e2', '#991b1b'];
      rect(x0 + 16 + cols[4], ry + rowH / 2 - 10, 72, 20, pill[0], null, 10);
      text(o.status, x0 + 16 + cols[4] + 36, ry + rowH / 2, { align: 'center', size: 12, color: pill[1], weight: 500 });
      region('row:' + o.id, x0, ry, width, rowH, o);
      if (o.status === 'open') { open += 1; total += o.cents; }
    });
    var fy = y + rowH * (model.orders.length + 1) + 24;
    text('Open orders: ' + open + '  ·  total ' + money(total), x0, fy, { color: '#374151' });
    if (drawer !== null) drawDrawer(W, H);
  }

  function drawDrawer(W, H) {
    var o = model.orders.filter(function (x) { return x.id === drawer; })[0];
    if (!o) { drawer = null; return; }
    var w = 360, x = W - w;
    rect(x, TOP, w, H - TOP, '#ffffff');
    ctx.fillStyle = '#e5e7eb'; ctx.fillRect(x, TOP, 1, H - TOP);
    region('drawer', x, TOP, w, H - TOP);  // swallows clicks on the panel's blank space
    text('Order #' + o.id, x + 24, TOP + 32, { size: 18, weight: 500 });
    button('drawer:close', '✕', x + w - 44, TOP + 16, 28, 28);
    var y = TOP + 76;
    [['Customer', o.customer], ['Date', o.date], ['Amount', money(o.cents)], ['Status', o.status]].forEach(function (kv) {
      text(kv[0], x + 24, y, { color: '#6b7280', size: 12 });
      text(kv[1], x + 24, y + 20);
      y += 48;
    });
    if (o.status === 'open') {
      button('drawer:ship', 'Mark shipped', x + 24, y + 8, 150, 36, 'primary');
      button('drawer:cancel', 'Cancel order', x + 186, y + 8, 150, 36, 'danger');
    } else {
      text('This order is ' + o.status + '.', x + 24, y + 26, { color: '#6b7280' });
    }
    if (confirming !== null) drawConfirm(W, H, o);
  }

  function drawConfirm(W, H, o) {
    rect(0, 0, W, H, 'rgba(17, 24, 39, 0.45)');
    region('sheet:backdrop', 0, 0, W, H);
    var w = 420, h = 170, x = (W - w) / 2, y = (H - h) / 2;
    rect(x, y, w, h, '#ffffff', null, 10);
    region('sheet', x, y, w, h);
    text('Ship order #' + o.id + '?', x + 24, y + 36, { size: 18, weight: 500 });
    text(o.customer + ' will get a shipping notice. This cannot be undone.', x + 24, y + 72, { color: '#4b5563' });
    button('confirm:keep', 'Keep open', x + w - 268, y + h - 56, 120, 36);
    button('confirm:ship', 'Ship', x + w - 136, y + h - 56, 112, 36, 'primary');
  }

  function drawCustomers() {
    var x0 = SIDEBAR + 32, y = TOP + 28;
    text('Customers', x0, y, { size: 22, weight: 500 });
    var seen = {};
    var names = [];
    model.orders.forEach(function (o) { if (!seen[o.customer]) { seen[o.customer] = 1; names.push(o.customer); } });
    y += 36;
    rect(x0, y, 420, 44 * names.length + 1, '#ffffff', '#e5e7eb', 6);
    names.forEach(function (n, i) {
      text(n, x0 + 16, y + 44 * i + 22);
      ctx.fillStyle = '#e5e7eb'; ctx.fillRect(x0, y + 44 * (i + 1), 420, 1);
    });
  }

  function drawSettings(W, H) {
    var x0 = SIDEBAR + 32, y = TOP + 28;
    text('Settings', x0, y, { size: 22, weight: 500 });
    y += 48;
    // business name
    text('Business name', x0, y, { color: '#6b7280', size: 12, weight: 500 });
    y += 16;
    var fw = 360, fh = 36;
    rect(x0, y, fw, fh, '#ffffff', focus === 'name' ? '#2563eb' : '#d1d5db', 6);
    if (focus === 'name' && selectAll && draft.name) rect(x0 + 10, y + 8, Math.min(fw - 20, ctx.measureText(draft.name).width + 2), fh - 16, '#bfdbfe');
    text(draft.name, x0 + 12, y + fh / 2);
    if (focus === 'name' && !selectAll) {
      ctx.font = '400 14px ' + FONT;
      var cx = x0 + 12 + ctx.measureText(draft.name).width + 1;
      ctx.fillStyle = '#111827'; ctx.fillRect(cx, y + 9, 1, fh - 18);
    }
    region('field:name', x0, y, fw, fh);
    y += fh + 24;
    // timezone
    text('Timezone', x0, y, { color: '#6b7280', size: 12, weight: 500 });
    y += 16;
    rect(x0, y, fw, fh, '#ffffff', '#d1d5db', 6);
    text(draft.timezone, x0 + 12, y + fh / 2);
    text(dropdown ? '▴' : '▾', x0 + fw - 18, y + fh / 2, { align: 'center', color: '#6b7280' });
    region('dropdown:timezone', x0, y, fw, fh);
    var ddY = y + fh + 4;
    y += fh + 28;
    // notifications
    text('Notifications', x0, y, { color: '#6b7280', size: 12, weight: 500 });
    y += 20;
    var step = quirk('small_controls') ? 34 : 42;
    toggle('toggle:email', 'Email alerts', x0, y, draft.email); y += step;
    toggle('toggle:sms', 'SMS alerts', x0, y, draft.sms); y += step;
    toggle('toggle:digest', 'Weekly digest', x0, y, draft.digest); y += step;
    y += 16;
    button('save', 'Save changes', x0, y, 140, 36, 'primary');
    var dirty = JSON.stringify(draft) !== JSON.stringify(model.settings);
    if (dirty) text('Unsaved changes', x0 + 156, y + 18, { color: '#b45309' });
    if (dropdown) {
      var n = model.timezones.length;
      rect(x0, ddY, fw, 32 * n + 2, '#ffffff', '#d1d5db', 6);
      region('dropdown:list', x0, ddY, fw, 32 * n + 2);
      model.timezones.forEach(function (tz, i) {
        var oy = ddY + 1 + 32 * i;
        if (tz === draft.timezone) rect(x0 + 1, oy, fw - 2, 32, '#eff6ff');
        text(tz, x0 + 12, oy + 16);
        region('option:' + tz, x0, oy, fw, 32, tz);
      });
    }
  }

  function drawToast(W) {
    if (!toast) return;
    if (Date.now() > toast.until) { toast = null; return; }
    var w = 220, h = 40, x = (W - w) / 2, y = TOP + 16;
    rect(x, y, w, h, '#111827', null, 8);
    text(toast.text, x + w / 2, y + h / 2, { color: '#ffffff', align: 'center' });
    setTimeout(render, toast.until - Date.now() + 20);
  }

  function render() {
    if (!model) return;
    var dpr = window.devicePixelRatio || 1;
    var W = window.innerWidth, H = window.innerHeight;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    regions = [];
    drawChrome(W, H);
    if (page === 'orders') drawOrders(W, H);
    else if (page === 'customers') drawCustomers();
    else drawSettings(W, H);
    drawToast(W);
  }

  // ─── input ───
  function hit(x, y) {
    for (var i = regions.length - 1; i >= 0; i--) {
      var r = regions[i];
      if (x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h) return r;
    }
    return null;
  }

  function act(r) {
    var id = r ? r.id : '';
    var blurred = focus !== null && id !== 'field:name';
    if (blurred) { focus = null; selectAll = false; }
    if (dropdown && id.indexOf('option:') !== 0 && id !== 'dropdown:timezone') dropdown = false;
    if (id.indexOf('nav:') === 0) {
      page = id.slice(4); drawer = null; confirming = null; dropdown = false;
      api('nav', { page: page });
    } else if (id.indexOf('row:') === 0) {
      drawer = r.data.id;
    } else if (id === 'drawer:close') {
      drawer = null;
    } else if (id === 'drawer:ship') {
      if (quirk('confirm_sheet')) confirming = drawer;
      else ship(drawer);
    } else if (id === 'drawer:cancel') {
      api('cancel', { id: drawer }).then(load);
    } else if (id === 'confirm:ship') {
      ship(confirming);
    } else if (id === 'confirm:keep' || id === 'sheet:backdrop') {
      confirming = null;
    } else if (id === 'field:name') {
      focus = 'name'; selectAll = false;
    } else if (id === 'dropdown:timezone') {
      dropdown = !dropdown;
    } else if (id.indexOf('option:') === 0) {
      draft.timezone = r.data; dropdown = false;
    } else if (id.indexOf('toggle:') === 0) {
      var k = id.slice(7); draft[k] = !draft[k];
    } else if (id === 'save') {
      api('save_settings', draft).then(function () {
        toast = { text: 'Settings saved', until: Date.now() + 2500 };
        return load();
      });
    }
    render();
  }

  function ship(id) {
    confirming = null;
    api('ship', { id: id }).then(function () {
      toast = { text: 'Order #' + id + ' shipped', until: Date.now() + 2500 };
      return load();
    });
  }

  canvas.addEventListener('mousedown', function (e) {
    canvas.focus();
    var b = canvas.getBoundingClientRect();
    act(hit(e.clientX - b.left, e.clientY - b.top));
  });

  canvas.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (confirming !== null) confirming = null;
      else if (dropdown) dropdown = false;
      else if (focus) { focus = null; selectAll = false; }
      else if (drawer !== null) drawer = null;
      render();
      return;
    }
    if (focus !== 'name') return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'a') { selectAll = true; e.preventDefault(); render(); return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'Backspace') {
      draft.name = selectAll ? '' : draft.name.slice(0, -1);
      selectAll = false;
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      focus = null; selectAll = false;
    } else if (e.key.length === 1) {
      draft.name = (selectAll ? '' : draft.name) + e.key;
      selectAll = false;
    } else {
      return;
    }
    e.preventDefault();
    render();
  });

  window.addEventListener('resize', render);

  window.__ledger = {
    find: function (id) {
      var r = regions.filter(function (x) { return x.id === id; })[0];
      return r ? [r.x + r.w / 2, r.y + r.h / 2] : null;
    },
    ids: function () { return regions.map(function (x) { return x.id; }); },
    page: function () { return page; },
    draft: function () { return draft; },
  };

  load();
})();
