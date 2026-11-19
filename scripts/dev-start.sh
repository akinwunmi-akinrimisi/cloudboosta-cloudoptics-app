#!/usr/bin/env bash
# Starts PostgreSQL + migrations + API in Docker, then the web dev server against it.
# In Codespaces the web app calls the API through the web dev server's /api proxy, so the
# forwarded API port stays private; locally it calls http://localhost:8000.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -n "${CODESPACE_NAME:-}" ]]; then
  domain="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
  api_url="https://${CODESPACE_NAME}-8000.${domain}"
  web_url="https://${CODESPACE_NAME}-5173.${domain}"
  web_api_url="/api"
else
  api_url="http://localhost:8000"
  web_url="http://localhost:5173"
  web_api_url="$api_url"
fi

# The API runs with dev sign-in. Sign its tokens with a random secret kept on this machine
# rather than the development secret committed to the repository.
secret_file="${HOME}/.config/cloudoptics/dev-auth-secret"
if [[ ! -s "$secret_file" ]]; then
  mkdir -p "$(dirname "$secret_file")"
  (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$secret_file")
fi
CLOUDOPTICS_DEV_AUTH_SECRET="$(cat "$secret_file")"
export CLOUDOPTICS_DEV_AUTH_SECRET

export CLOUDOPTICS_CORS_ORIGINS="[\"${web_url}\"]"
docker compose up -d --build api
printf 'VITE_API_URL=%s\n' "$web_api_url" > apps/web/.env.local
nohup pnpm --filter @cloudoptics/web dev --host 0.0.0.0 > /tmp/web.log 2>&1 &
echo "API: ${api_url}/docs   Web: ${web_url}"
