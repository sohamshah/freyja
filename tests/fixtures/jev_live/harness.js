// Shared by the jev live fixtures: tags every page event with the scenario's
// ?run= id and posts it to the harness server, and carries ?run= across links.
(function () {
  var run = new URLSearchParams(location.search).get('run') || '';
  window.H = {
    run: run,
    log: function (event, data) {
      var body = JSON.stringify(Object.assign({ run: run, page: location.pathname, event: event }, data || {}));
      try { navigator.sendBeacon('/event', body); } catch (e) { /* ignore */ }
    },
    withRun: function (href) {
      var u = new URL(href, location.href);
      if (run && u.origin === location.origin) u.searchParams.set('run', run);
      return u.toString();
    },
  };
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (a && run && a.origin === location.origin && !/[?&]run=/.test(a.getAttribute('href'))) a.href = H.withRun(a.href);
  }, true);
  H.log('load', { title: document.title });
})();
