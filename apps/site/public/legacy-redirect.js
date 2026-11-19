// Before SITE-13 the app was served at the root (/overview, /settings/connections, ...). Sent
// emails and bookmarks still use those addresses, so the site's 404 page runs this before it
// renders and forwards them into the app under /app, keeping the query and the hash.
;(function (location) {
  var APP_ROUTES = ['sign-in', 'onboarding', 'overview', 'costs', 'recommendations', 'anomalies', 'budgets', 'commitments', 'reports', 'settings', 'staff']
  var match = /^\/([a-z-]+)(\/.*)?$/.exec(location.pathname)
  if (match && APP_ROUTES.indexOf(match[1]) !== -1) location.replace('/app' + location.pathname + location.search + location.hash)
})(window.location)
