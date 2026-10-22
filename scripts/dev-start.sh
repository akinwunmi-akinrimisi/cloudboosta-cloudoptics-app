#!/usr/bin/env bash
# Starts PostgreSQL + migrations + API in Docker, then the web dev server against it.
# In Codespaces the web app calls the forwarded API URL; locally, http://localhost:8000.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -n "${CODESPACE_NAME:-}" ]]; then
  domain="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
  api_url="https://${CODESPACE_NAME}-8000.${domain}"
  web_url="https://${CODESPACE_NAME}-5173.${domain}"
else
  api_url="http://localhost:8000"
  web_url="http://localhost:5173"
fi

export CLOUDOPTICS_CORS_ORIGINS="[\"${web_url}\"]"
docker compose up -d --build api
printf 'VITE_API_URL=%s\n' "$api_url" > apps/web/.env.local
nohup pnpm --filter @cloudoptics/web dev --host 0.0.0.0 > /tmp/web.log 2>&1 &
echo "API: ${api_url}/docs   Web: ${web_url}"
