#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
umask 077

for file in .env backend.env caddy.env monitor.env; do
    if [[ -e "$file" ]]; then
        echo "Refusing to overwrite $file; rotate existing credentials explicitly." >&2
        exit 1
    fi
done
command -v openssl >/dev/null
cp .env.example .env
cp backend.env.example backend.env
cp caddy.env.example caddy.env
cp monitor.env.example monitor.env
{
    printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 32)"
    printf 'REDIS_PASSWORD=%s\n' "$(openssl rand -hex 32)"
} >> .env
{
    printf 'JWT_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'OAUTH_STATE_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'TELEMETRY_INGEST_KEY=%s\n' "$(openssl rand -hex 32)"
} >> backend.env
echo "Created owner-only .env, backend.env, caddy.env and monitor.env. Complete provider and alert settings before deployment."
