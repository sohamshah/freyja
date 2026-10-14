// New customer account: a long form with real-time checks and conditional parts.
//  - Choosing Business renames the name field and adds a required VAT ID field
//    at the end of the form, below the fold.
//  - About 2 s after the postcode field loses focus, an address lookup fills
//    City, replacing whatever it held.
//  - Create account waits ~2 s on the server, then the button returns to normal.
// Quirk late_errors (on): errors are listed only in a banner at the top of the
// page. Off: the VAT field is checked as you type and every error shows inline
// under its field with aria-invalid.
(function () {
  'use strict';
  var LOOKUP_DELAY_MS = 2000;

  TC.pages.signup = function (s) {
    var el = TC.el, $ = TC.$, late = !!(s.quirks || {}).late_errors;
    var form = $('signupForm'), btn = $('createBtn'), banner = $('formErrors');
    var vat = null, lookupTimer = null, busy = false;

    function kind() { return form.querySelector('input[name=acct]:checked').value; }

    // ─── inline errors (late_errors off) ───────────────────────────────
    function setInline(input, msg) {
      if (!input) return;
      var id = 'err-' + input.id, p = $(id);
      if (!msg) {
        if (p) p.remove();
        input.removeAttribute('aria-invalid');
        input.removeAttribute('aria-describedby');
        return;
      }
      if (!p) {
        p = el('p', { class: 'ferr', id: id });
        input.parentNode.appendChild(p);
      }
      p.textContent = msg;
      input.setAttribute('aria-invalid', 'true');
      input.setAttribute('aria-describedby', id);
    }

    // Email is checked on leaving the field, in both versions of the form.
    var email = $('email');
    email.addEventListener('blur', function () {
      var v = email.value.trim();
      setInline(email, v && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) ? 'Enter a valid email address' : null);
    });
    email.addEventListener('input', function () {
      if (/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.value.trim())) setInline(email, null);
    });

    // ─── Business adds the VAT ID field at the end ─────────────────────
    function onKind() {
      var biz = kind() === 'business';
      $('nameLabel').textContent = biz ? 'Company name' : 'Full name';
      if (biz && !vat) {
        vat = el('input', { type: 'text', id: 'vat', name: 'vat', autocomplete: 'off', 'aria-required': 'true' });
        $('vatSlot').appendChild(el('div', { class: 'field', id: 'vatField' }, [
          el('label', { class: 'lab', for: 'vat', text: 'VAT ID' }),
          el('p', { class: 'muted', text: 'Required for business accounts: the company’s UK VAT registration number.' }),
          vat,
        ]));
        if (!late) {
          vat.addEventListener('input', function () {
            setInline(vat, /\s/.test(vat.value) ? 'VAT ID: remove the spaces' : null);
          });
          vat.addEventListener('blur', function () {
            var v = vat.value;
            if (/\s/.test(v)) setInline(vat, 'VAT ID: remove the spaces');
            else if (v && !/^GB\d{9}$/.test(v)) setInline(vat, 'VAT ID: use GB followed by 9 digits');
          });
        }
      } else if (!biz && vat) {
        $('vatField').remove();
        vat = null;
      }
    }
    form.querySelectorAll('input[name=acct]').forEach(function (r) { r.addEventListener('change', onKind); });

    // ─── the postcode lookup, ~2 s after the field loses focus ─────────
    var postcode = $('postcode'), city = $('city');
    postcode.addEventListener('blur', function () {
      clearTimeout(lookupTimer);
      var code = postcode.value.trim();
      if (!code) return;
      lookupTimer = setTimeout(function () {
        TC.api('postcode?code=' + encodeURIComponent(code)).then(function (r) {
          if (r.status !== 200) return;
          city.value = r.data.city;
          city.dispatchEvent(new Event('input', { bubbles: true }));
          city.dispatchEvent(new Event('change', { bubbles: true }));
          document.body.setAttribute('data-lookup', r.data.city);
        });
      }, LOOKUP_DELAY_MS);
    });

    // ─── submit ────────────────────────────────────────────────────────
    function clearErrors() {
      banner.classList.add('hidden');
      banner.innerHTML = '';
      form.querySelectorAll('[aria-invalid=true]').forEach(function (i) { setInline(i, null); });
    }
    function showErrors(errors) {
      var loose = [];
      errors.forEach(function (e) {
        var input = e.field && form.querySelector('[name="' + e.field + '"]');
        if (!late && input && input.type !== 'radio') setInline(input, e.message);
        else loose.push(e.message);
      });
      if (!loose.length) return;
      banner.innerHTML = '';
      banner.appendChild(el('strong', { text: 'We could not create this account.' }));
      banner.appendChild(el('ul', {}, loose.map(function (m) { return el('li', { text: m }); })));
      banner.classList.remove('hidden');
    }
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (busy) return;
      busy = true;
      btn.disabled = true;
      btn.textContent = 'Creating account…';
      var body = {
        type: kind(), name: $('name').value, email: email.value, phone: $('phone').value,
        address1: $('address1').value, address2: $('address2').value, postcode: postcode.value,
        city: city.value, country: $('country').value, team_size: $('teamSize').value,
        source: $('source').value, marketing: $('marketing').checked, vat: vat ? vat.value : '',
      };
      TC.api('signup', body).then(function (r) {
        busy = false;
        btn.disabled = false;
        btn.textContent = 'Create account';
        if (r.status === 200 && r.data.go) { location.href = r.data.go; return; }
        clearErrors();
        showErrors(r.data.errors || [{ field: '', message: r.data.error || 'Something went wrong. Try again.' }]);
        document.body.setAttribute('data-attempts', String(+(document.body.getAttribute('data-attempts') || 0) + 1));
      });
    });
  };
})();
