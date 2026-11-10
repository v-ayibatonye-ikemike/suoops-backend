#!/usr/bin/env bash
set -euo pipefail
repo="$(cd -- "$(dirname -- "$0")/.." && pwd)"
deploy="$repo/deploy/vps"
command -v jq >/dev/null
temp_dir="$(mktemp -d)"
cleanup() {
    rm -f -- "$temp_dir/.env" "$temp_dir/backend.env" "$temp_dir/.env.example" \
        "$temp_dir/backend.env.example" "$temp_dir/caddy.env" "$temp_dir/caddy.env.example" "$temp_dir/init-secrets.sh" \
        "$temp_dir/init-backup-secrets.sh" "$temp_dir/restic.env" "$temp_dir/monitor.env" "$temp_dir/monitor.env.example"
    rm -f -- "$temp_dir/docker-compose.yml"
    rmdir -- "$temp_dir"
}
trap cleanup EXIT

bash -n "$deploy/init-secrets.sh" "$deploy/init-backup-secrets.sh" \
    "$deploy/backup.sh" "$deploy/heartbeat.sh" "$repo/tests/test_vps_runtime.sh"
cp "$deploy/init-secrets.sh" "$deploy/.env.example" \
    "$deploy/backend.env.example" "$deploy/caddy.env.example" "$deploy/monitor.env.example" "$deploy/docker-compose.yml" \
    "$deploy/init-backup-secrets.sh" "$temp_dir/"
bash "$temp_dir/init-secrets.sh"
if bash "$temp_dir/init-secrets.sh"; then
    echo "Secret initialization unexpectedly overwrote existing files" >&2
    exit 1
fi
for variable in POSTGRES_PASSWORD REDIS_PASSWORD; do
    grep -Eq "^${variable}=[0-9a-f]{64}$" "$temp_dir/.env"
done
for variable in JWT_SECRET OAUTH_STATE_SECRET TELEMETRY_INGEST_KEY; do
    grep -Eq "^${variable}=[0-9a-f]{64}$" "$temp_dir/backend.env"
done
# GNU and BSD stat use different format flags.
for file in .env backend.env caddy.env monitor.env; do
    mode="$(stat -c '%a' "$temp_dir/$file" 2>/dev/null || stat -f '%Lp' "$temp_dir/$file")"
    [[ "$mode" == 600 ]]
done

printf '%s\n' "RESTIC_REPOSITORY='s3:https://example.com/test'" \
    "AWS_ACCESS_KEY_ID='test-only'" "AWS_SECRET_ACCESS_KEY='test-only'" \
    "RESTIC_PASSWORD=''" > "$temp_dir/restic.env"
bash "$temp_dir/init-backup-secrets.sh"
grep -Eq '^RESTIC_PASSWORD=[0-9a-f]{64}$' "$temp_dir/restic.env"
original_checksum="$(cksum "$temp_dir/restic.env")"
if bash "$temp_dir/init-backup-secrets.sh"; then
    echo "Backup secret initialization unexpectedly replaced its password" >&2
    exit 1
fi
[[ "$(cksum "$temp_dir/restic.env")" == "$original_checksum" ]]
mode="$(stat -c '%a' "$temp_dir/restic.env" 2>/dev/null || stat -f '%Lp' "$temp_dir/restic.env")"
[[ "$mode" == 600 ]]

export POSTGRES_PASSWORD=1111111111111111111111111111111111111111111111111111111111111111
export REDIS_PASSWORD=2222222222222222222222222222222222222222222222222222222222222222
export ACME_EMAIL=operator@example.com
printf '%s\n' 'VPS_TEST_LITERAL=dollar$TOKEN#literal' >> "$temp_dir/backend.env"
printf '%s\n' 'CF_API_TOKEN=test-only' >> "$temp_dir/caddy.env"
docker compose --project-directory "$temp_dir" --env-file "$temp_dir/.env" \
    -f "$temp_dir/docker-compose.yml" --profile tools config --format json |
    jq -e '
      (.services | keys | length == 9) and
      (.services | to_entries | map(select(.value.ports != null) | .key) == ["caddy"]) and
      (.services.caddy.build.dockerfile == "Caddy.Dockerfile") and
      (.services.caddy.environment.CF_API_TOKEN == "test-only") and
      (.services.api.environment.CF_API_TOKEN == null) and
      (.services.worker.environment.CF_API_TOKEN == null) and
      (.services.support.build.args.NODE_VERSION == "24") and
      (.networks.data.internal == true) and
      (.services.db.volumes[0].type == "volume") and
      (.services.db.image == "postgres:18-bookworm") and
      (.services.db.volumes[0].target == "/var/lib/postgresql") and
      (.services.db.environment.PGDATA == "/var/lib/postgresql/18/docker") and
      (.services.db.command == ["postgres", "-c", "shared_preload_libraries=pg_stat_statements"]) and
      (.services.redis.volumes[0].type == "volume") and
      (.services.redis.image == "redis:8.2.10-bookworm") and
      (.services.api.environment.ENV == "prod") and
      (.services.api.environment.CORS_ALLOW_ORIGIN_REGEX == "") and
      (.services.api.environment.TRUSTED_PROXY_HOPS == "1") and
      (.services.api.environment.VPS_TEST_LITERAL == "dollar$$TOKEN#literal") and
      (.services.worker.command | index("--beat") == null) and
      (.services.beat.command | index("beat") != null) and
      (.services.migrate.profiles == ["tools"])
    ' >/dev/null
docker compose --project-directory "$temp_dir" \
    -f "$repo/tests/fixtures/vps-env-probe.yml" run --rm --no-deps env-probe
bash "$repo/tests/test_vps_heartbeat.sh"
echo "VPS configuration, private ports, scheduler isolation and secret initialization passed."
