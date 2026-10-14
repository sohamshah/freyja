// Tessaly Console, an invented admin console: the shell and helpers every page
// shares. A page script registers TC.pages.<name>(session) and returns a promise;
// the shell loads the episode's session (the quirk switches and the checklist
// mode) first, builds the sidebar, runs the page, then marks the body ready.
// Every pattern here is reimplemented from its description (see README.md).
(function () {
  'use strict';
  var API = '/widgets/api/';

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

  function api(name, body) {
    var opt = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    };
    return fetch(API + name, opt).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, data: d }; },
        function () { return { status: r.status, data: {} }; });
    });
  }

  function money(n) { return '$' + Number(n).toLocaleString('en-US'); }

  // An invented mark: a rounded square with a bar and a stem.
  var LOGO = '<svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">' +
    '<rect x="2" y="2" width="20" height="20" rx="6" fill="#8b80ee"/>' +
    '<path d="M7 8.5h10M12 8.5v8" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg>';

  var NAV = [
    ['index', 'index.html', 'Overview'],
    ['catalog', 'catalog.html', 'Products'],
    ['members', 'members.html', 'Members'],
    ['notifications', 'notifications.html', 'Notifications'],
    ['checklist', 'checklist.html', 'Release checklist'],
    ['signup', 'signup.html', 'New account'],
    ['workspace', 'workspace.html', 'Workspace'],
  ];

  function shell(page) {
    var active = page === 'product' ? 'catalog' : page === 'account' ? 'signup' : page;
    var brand = el('a', { class: 'brand', href: 'index.html' });
    brand.innerHTML = LOGO + '<span>Tessaly Console</span>';
    var side = el('nav', { class: 'side', 'aria-label': 'Console' }, [
      brand,
      el('ul', {}, NAV.map(function (n) {
        return el('li', {}, [el('a', { href: n[1], class: n[0] === active ? 'on' : null,
          'aria-current': n[0] === active ? 'page' : null, text: n[2] })]);
      })),
      el('div', { class: 'who', text: 'Signed in as Ada Lovelace · Tessaly Ops' }),
    ]);
    var app = document.querySelector('.app');
    app.insertBefore(side, app.firstChild);
  }

  var TC = window.TC = { api: api, el: el, $: $, money: money, pages: {}, session: null };

  function start() {
    api('session').then(function (r) {
      var s = r.data || {};
      TC.session = s;
      var page = document.body.getAttribute('data-page');
      shell(page);
      var init = TC.pages[page];
      return Promise.resolve(init ? init(s) : null);
    }).then(function () {
      document.body.setAttribute('data-ready', '1');
    }, function (err) {
      document.body.setAttribute('data-ready', '1');
      document.body.setAttribute('data-error', String(err && err.message || err));
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else setTimeout(start, 0);
})();
