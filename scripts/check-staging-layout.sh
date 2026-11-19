#!/usr/bin/env bash
# Checks the path layout of one Cloudoptics origin (docs/14 §7.2, SITE-13/14):
#   /            the public site            /app/   the web app
#   /demo/       the demo (noindex)         /api    the API
#   /cfn/*.yaml  the templates (from the site), and real 404s with old app paths forwarded.
# Usage: scripts/check-staging-layout.sh https://cloudoptics.cloudboosta.com
set -euo pipefail
base="${1:?usage: $0 https://host}"
fail=0
check() { # path, expected status, optional grep pattern in the body
  local body code
  body=$(mktemp)
  code=$(curl -s -o "$body" -w '%{http_code}' "$base$1")
  if [ "$code" != "$2" ]; then
    echo "FAIL $1: HTTP $code, expected $2"; fail=1
  elif [ -n "${3:-}" ] && ! grep -q -- "$3" "$body"; then
    echo "FAIL $1: body does not contain $3"; fail=1
  else
    echo "ok   $1 ($code)"
  fi
  rm -f "$body"
}
check /api/readyz 200
check / 200 'rel="canonical"'
check /app/ 200 '/app/assets/'
check /app/overview 200 '/app/assets/'
check /demo/ 200 'name="robots" content="noindex'
check /cfn/cloudoptics-access.yaml 200 'AWSTemplateFormatVersion'
check /cfn/scheduler/cloudoptics-scheduler.yaml 200 'AWSTemplateFormatVersion'
# Old app paths are not site pages: the 404 page answers and forwards them in the browser.
check /overview 404 'legacy-redirect.js'
check /no-such-page 404 'noindex'
exit $fail
