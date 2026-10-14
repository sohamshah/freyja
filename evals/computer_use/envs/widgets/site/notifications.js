// Notifications: two cards built from the same template, "Personal" and
// "Team". Each has a "Weekly digest" switch (role=switch) and a "Save" button
// that saves that card only. The DOM holds Personal first; CSS order shows Team
// first. Quirk twin_labels off: the controls name their card ("Team weekly
// digest", "Save team settings").
(function () {
  'use strict';
  var ROWS = [
    ['weekly_digest', 'Weekly digest', 'A summary of the week’s activity, every Monday.'],
    ['mentions', 'Mentions and replies', 'When someone mentions you or replies to you.'],
    ['product_updates', 'Product updates', 'New features and changes to Tessaly.'],
  ];
  var CARDS = [
    ['personal', 'Personal', 'Emails sent to you: Ada Lovelace, ada@example.com.'],
    ['team', 'Team', 'Emails sent to everyone in the Tessaly Ops workspace (14 members).'],
  ];

  TC.pages.notifications = function (s) {
    var el = TC.el, twin = !!(s.quirks || {}).twin_labels;
    return TC.api('notifications').then(function (r) {
      var box = TC.$('cards');
      CARDS.forEach(function (c) {
        var scope = c[0], word = c[1];
        var saved = Object.assign({}, r.data[scope]);
        var cur = Object.assign({}, saved);
        var state = el('span', { class: 'muted state' });
        var card = el('section', { class: 'card ncard', 'data-scope': scope, 'aria-labelledby': 'h-' + scope }, [
          el('h2', { id: 'h-' + scope, text: word }),
          el('p', { class: 'muted', text: c[2] }),
        ]);
        ROWS.forEach(function (row) {
          var key = row[0], lid = 'lbl-' + scope + '-' + key;
          var sw = el('button', {
            type: 'button', role: 'switch', class: 'switch', 'data-key': key,
            'aria-checked': cur[key] ? 'true' : 'false', 'aria-labelledby': lid,
          }, [el('span', { class: 'knob' })]);
          sw.addEventListener('click', function () {
            cur[key] = !cur[key];
            sw.setAttribute('aria-checked', cur[key] ? 'true' : 'false');
            dirty();
          });
          var label = twin ? row[1] : word + ' ' + row[1].charAt(0).toLowerCase() + row[1].slice(1);
          card.appendChild(el('div', { class: 'srow' }, [
            el('div', {}, [el('span', { class: 'slabel', id: lid, text: label }), el('span', { class: 'muted', text: row[2] })]),
            sw,
          ]));
        });
        var save = el('button', { type: 'button', class: 'btn primary save', text: twin ? 'Save' : 'Save ' + word.toLowerCase() + ' settings' });
        save.addEventListener('click', function () {
          save.disabled = true;
          TC.api('notifications/save', { scope: scope, settings: cur }).then(function (res) {
            save.disabled = false;
            if (res.status !== 200) return;
            saved = Object.assign({}, res.data.settings);
            card.setAttribute('data-saved', '1');
            dirty();
          });
        });
        card.appendChild(el('div', { class: 'foot' }, [state, save]));
        function dirty() {
          var d = ROWS.some(function (row) { return cur[row[0]] !== saved[row[0]]; });
          state.textContent = d ? 'Unsaved changes' : 'No unsaved changes';
        }
        dirty();
        box.appendChild(card);
      });
    });
  };
})();
