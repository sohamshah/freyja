// Catalog: a two-thumb price slider whose range reaches the results only when
// "Apply (n results)" posts it (a committed filter). Each thumb is a focusable
// div role=slider: Up/Right and Down/Left move it one $10 step, Page Up/Down
// five steps, Home/End to its limit, and a pointer drag moves it too. The thumbs
// cannot cross. Quirk slider_only off: Min and Max number inputs sit beside it.
(function () {
  'use strict';
  TC.pages.catalog = function (s) {
    var el = TC.el, $ = TC.$, Q = s.quirks || {};
    var B = { lo: 0, hi: 500, step: 10 };
    var val = { min: 0, max: 500 };
    var prices = [], sort = 'featured', drag = null, inputs = null;
    var box = $('priceRange'), fill = $('rangeFill');
    var thumbs = {};
    ['min', 'max'].forEach(function (w) {
      var t = el('div', {
        class: 'thumb', id: w === 'min' ? 'thumbMin' : 'thumbMax', role: 'slider', tabindex: '0',
        'aria-label': w === 'min' ? 'Minimum price' : 'Maximum price', 'aria-orientation': 'horizontal',
      });
      t.addEventListener('keydown', function (e) {
        var d = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1, PageUp: 5, PageDown: -5 }[e.key];
        if (d) { e.preventDefault(); set(w, val[w] + d * B.step); return; }
        if (e.key === 'Home') { e.preventDefault(); set(w, w === 'min' ? B.lo : val.min); }
        else if (e.key === 'End') { e.preventDefault(); set(w, w === 'min' ? val.max : B.hi); }
      });
      t.addEventListener('pointerdown', function (e) {
        if (e.button !== 0) return;
        e.preventDefault();
        t.focus();
        drag = { w: w, x: e.clientX, v: val[w], width: box.getBoundingClientRect().width || 1 };
      });
      t.addEventListener('focus', function () {
        thumbs.min.style.zIndex = w === 'min' ? '2' : '1';
        thumbs.max.style.zIndex = w === 'max' ? '2' : '1';
      });
      thumbs[w] = t;
      box.appendChild(t);
    });
    document.addEventListener('pointermove', function (e) {
      if (!drag) return;
      set(drag.w, drag.v + (e.clientX - drag.x) / drag.width * (B.hi - B.lo));
    });
    document.addEventListener('pointerup', function () { drag = null; });
    document.addEventListener('pointercancel', function () { drag = null; });

    if (!Q.slider_only) {
      inputs = {};
      var row = el('div', { class: 'minmax' });
      ['min', 'max'].forEach(function (w) {
        var input = el('input', { type: 'number', id: w === 'min' ? 'minPrice' : 'maxPrice', min: '0', max: '500', step: '10', inputmode: 'numeric' });
        input.addEventListener('input', function () {
          var n = parseFloat(input.value);
          if (isFinite(n)) set(w, n, true);
        });
        input.addEventListener('change', function () { input.value = val[w]; });
        inputs[w] = input;
        row.appendChild(el('label', {}, [w === 'min' ? 'Min price ($)' : 'Max price ($)', input]));
      });
      $('minmaxSlot').appendChild(row);
    }

    function snap(v) { return Math.round(v / B.step) * B.step; }
    function set(w, v, typing) {
      v = snap(v);
      v = w === 'min' ? Math.max(B.lo, Math.min(v, val.max)) : Math.min(B.hi, Math.max(v, val.min));
      if (v !== val[w]) { val[w] = v; }
      paint(typing ? w : null);
    }
    function pct(v) { return (v - B.lo) / (B.hi - B.lo) * 100; }
    function count() {
      return prices.filter(function (p) { return p >= val.min && p <= val.max; }).length;
    }
    function paint(typing) {
      ['min', 'max'].forEach(function (w) {
        var t = thumbs[w];
        t.style.left = pct(val[w]) + '%';
        t.setAttribute('aria-valuenow', String(val[w]));
        t.setAttribute('aria-valuetext', TC.money(val[w]));
        t.setAttribute('aria-valuemin', String(w === 'min' ? B.lo : val.min));
        t.setAttribute('aria-valuemax', String(w === 'min' ? val.max : B.hi));
        if (inputs && typing !== w) inputs[w].value = String(val[w]);
      });
      fill.style.left = pct(val.min) + '%';
      fill.style.width = (pct(val.max) - pct(val.min)) + '%';
      $('priceReadout').textContent = TC.money(val.min) + ' – ' + TC.money(val.max);
      var n = count();
      $('applyBtn').textContent = 'Apply (' + n + (n === 1 ? ' result)' : ' results)');
    }

    function render(d) {
      var f = d.filter;
      var grid = $('productGrid');
      grid.innerHTML = '';
      d.products.forEach(function (p) {
        grid.appendChild(el('article', { class: 'product', 'data-id': p.id, 'data-price': String(p.price) }, [
          el('a', { class: 'pname', href: 'product.html?id=' + p.id, text: p.name }),
          el('p', { class: 'muted', text: p.category }),
          el('p', { class: 'price', text: TC.money(p.price) }),
        ]));
      });
      $('resultCount').textContent = f
        ? d.products.length + ' products priced ' + TC.money(f.min) + ' to ' + TC.money(f.max)
        : 'All ' + d.products.length + ' products';
      document.body.setAttribute('data-applied', f ? f.min + '-' + f.max : '');
    }

    $('applyBtn').addEventListener('click', function () {
      TC.api('catalog/filter', { min: val.min, max: val.max, sort: sort }).then(function (r) {
        if (r.status === 200) render(r.data);
      });
    });
    $('sort').addEventListener('change', function () {
      sort = $('sort').value;
      TC.api('catalog?sort=' + encodeURIComponent(sort)).then(function (r) { if (r.status === 200) render(r.data); });
    });

    return TC.api('catalog').then(function (r) {
      var d = r.data;
      B = d.bounds || B;
      prices = d.prices || [];
      if (d.filter) val = { min: d.filter.min, max: d.filter.max };
      render(d);
      paint();
    });
  };
})();
