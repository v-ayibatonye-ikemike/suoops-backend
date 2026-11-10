#!/usr/bin/env bash
set -euo pipefail
repo="$(cd -- "$(dirname -- "$0")/.." && pwd)"
deploy="$repo/deploy/vps"
: "${RELEASE_TAG:?Set RELEASE_TAG to the images being tested}"
export RELEASE_TAG ACME_EMAIL=operator@example.com
if [[ -n "${RESTIC_TEST_ENV:-}" ]]; then
    [[ "$RESTIC_TEST_ENV" = /* && -f "$RESTIC_TEST_ENV" ]] ||
        { echo "RESTIC_TEST_ENV must name an existing absolute credential-file path." >&2; exit 1; }
    command -v restic >/dev/null
fi
umask 077
temp_dir="$(mktemp -d /tmp/suoops-vps-runtime.XXXXXXXX)"
cp "$deploy/docker-compose.yml" "$deploy/Caddyfile" "$deploy/init-secrets.sh" \
    "$deploy/.env.example" "$deploy/backend.env.example" "$deploy/caddy.env.example" "$deploy/monitor.env.example" "$temp_dir/"
bash "$temp_dir/init-secrets.sh"

compose() {
    docker compose --project-directory "$temp_dir" --env-file "$temp_dir/.env" \
        -p suoops-vps-runtime-test -f "$temp_dir/docker-compose.yml" \
        -f "$repo/tests/fixtures/vps-runtime.yml" "$@"
}
started=false
cleanup() {
    result=$?
    if [[ "$started" == true ]]; then
        if [[ "$result" != 0 ]]; then
            compose logs --tail=60 >&2
        fi
        compose down --volumes
    fi
    rm -f -- "$temp_dir/.env" "$temp_dir/backend.env" "$temp_dir/.env.example" \
        "$temp_dir/backend.env.example" "$temp_dir/caddy.env" "$temp_dir/caddy.env.example" "$temp_dir/init-secrets.sh" \
        "$temp_dir/docker-compose.yml" "$temp_dir/Caddyfile" \
        "$temp_dir/backup.sh" "$temp_dir/.backup.lock" "$temp_dir/restic.env" "$temp_dir/monitor.env" "$temp_dir/monitor.env.example"
    if [[ -d "$temp_dir/restore" ]]; then
        rm -rf -- "$temp_dir/restore"
    fi
    rmdir -- "$temp_dir"
    exit "$result"
}
trap cleanup EXIT
if [[ -n "$(docker ps -aq --filter label=com.docker.compose.project=suoops-vps-runtime-test)" ]] ||
    [[ -n "$(docker volume ls -q --filter label=com.docker.compose.project=suoops-vps-runtime-test)" ]]; then
    echo "Refusing to reuse an existing runtime-test project." >&2
    exit 1
fi
started=true
compose up -d --wait db redis
compose run --rm migrate
compose up -d --wait api worker frontend support
compose exec -T api python -c '
import json
import urllib.request
ready = json.load(urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=15))
assert ready["status"] == "ready"
assert all(ready[key] for key in ("db", "redis", "s3", "celery"))
print("API readiness: database, Redis, storage fallback and worker passed")
'
for service in frontend support; do
    compose exec -T "$service" node -e '
      (async () => {
        const origin = "http://127.0.0.1:3000";
        const page = await fetch(origin);
        if (page.status !== 200) throw new Error(`Page HTTP ${page.status}`);
        const html = await page.text();
        const asset = html.match(/\/_next\/static\/[^" ]+\.css/);
        if (!asset) throw new Error("Missing stylesheet reference");
        const css = await fetch(`${origin}${asset[0]}`);
        if (css.status !== 200) throw new Error(`Stylesheet HTTP ${css.status}`);
        console.log("Frontend page and stylesheet: HTTP 200");
      })().catch(error => { console.error(error); process.exit(1); });
    '
done
compose exec -T db sh -ec 'createdb -U "$POSTGRES_USER" suoops_restore_test'
compose exec -T db sh -ec 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
CREATE TABLE suoops_backup_preflight (marker text PRIMARY KEY, amount numeric(12,2) NOT NULL);
INSERT INTO suoops_backup_preflight VALUES ('offserver-restore-ok', 123.45);
SQL
compose exec -T db sh -ec \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' |
    compose exec -T db sh -ec \
        'pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d suoops_restore_test'
compose exec -T db sh -ec '
    source_revision="$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version ORDER BY version_num")"
    restored_revision="$(psql -U "$POSTGRES_USER" -d suoops_restore_test -Atc "SELECT version_num FROM alembic_version ORDER BY version_num")"
    test -n "$source_revision"
    test "$source_revision" = "$restored_revision"
    test "$(psql -U "$POSTGRES_USER" -d suoops_restore_test -Atc "SELECT marker || chr(58) || amount FROM suoops_backup_preflight")" = "offserver-restore-ok:123.45"
'
if [[ -n "${RESTIC_TEST_ENV:-}" ]]; then
    cp "$deploy/backup.sh" "$temp_dir/"
    cp "$RESTIC_TEST_ENV" "$temp_dir/restic.env"
    compose exec -T redis redis-cli SET suoops:backup-preflight offserver-restore-ok >/dev/null
    compose exec -T api sh -ec 'printf "%s\n" offserver-restore-ok > /app/storage/backup-preflight.txt'
    (
        export COMPOSE_PROJECT_NAME=suoops-vps-runtime-test
        export COMPOSE_FILE="$temp_dir/docker-compose.yml:$repo/tests/fixtures/vps-runtime.yml"
        export BACKUP_TAG="suoops-vps-preflight-${temp_dir##*.}"
        bash "$temp_dir/backup.sh"
        set -a
        source "$temp_dir/restic.env"
        set +a
        restic restore latest --tag "$BACKUP_TAG" --target "$temp_dir/restore"
    )
    restored_backups=("$temp_dir"/restore/tmp/suoops-backup.*)
    [[ "${#restored_backups[@]}" == 1 && -d "${restored_backups[0]}" ]]
    restored="${restored_backups[0]}"
    (cd "$restored" && sha256sum -c SHA256SUMS)
    compose exec -T db sh -ec 'createdb -U "$POSTGRES_USER" suoops_offserver_restore_test'
    compose exec -T db sh -ec \
        'pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d suoops_offserver_restore_test' < "$restored/postgres.dump"
    compose exec -T db sh -ec '
        source_revision="$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version ORDER BY version_num")"
        restored_revision="$(psql -U "$POSTGRES_USER" -d suoops_offserver_restore_test -Atc "SELECT version_num FROM alembic_version ORDER BY version_num")"
        test -n "$source_revision"
        test "$source_revision" = "$restored_revision"
        test "$(psql -U "$POSTGRES_USER" -d suoops_offserver_restore_test -Atc "SELECT marker || chr(58) || amount FROM suoops_backup_preflight")" = "offserver-restore-ok:123.45"
    '
    docker run --rm --network none \
        --mount "type=bind,src=$restored,dst=/restore,readonly" \
        redis:8.2.10-bookworm sh -ec '
            mkdir /tmp/redis-restore /tmp/storage-restore
            cp /restore/redis.rdb /tmp/redis-restore/dump.rdb
            redis-check-rdb /tmp/redis-restore/dump.rdb
            redis-server --dir /tmp/redis-restore --port 0 \
                --unixsocket /tmp/redis-restore/socket --daemonize yes --save ""
            trap "redis-cli -s /tmp/redis-restore/socket shutdown nosave" EXIT
            test "$(redis-cli -s /tmp/redis-restore/socket GET suoops:backup-preflight)" = offserver-restore-ok
            redis-cli -s /tmp/redis-restore/socket CONFIG SET appendonly yes
            for attempt in $(seq 1 60); do
                persistence="$(redis-cli -s /tmp/redis-restore/socket INFO persistence)"
                if printf "%s\n" "$persistence" | grep -q "^aof_rewrite_in_progress:0" &&
                   printf "%s\n" "$persistence" | grep -q "^aof_last_bgrewrite_status:ok"; then
                    break
                fi
                if test "$attempt" = 60; then
                    echo "Restored Redis AOF conversion failed or timed out" >&2
                    exit 1
                fi
                sleep 1
            done
            redis-cli -s /tmp/redis-restore/socket shutdown nosave
            redis-server --dir /tmp/redis-restore --port 0 \
                --unixsocket /tmp/redis-restore/socket --daemonize yes --save "" --appendonly yes
            test "$(redis-cli -s /tmp/redis-restore/socket GET suoops:backup-preflight)" = offserver-restore-ok
            tar -C /tmp/storage-restore -xzf /restore/storage.tar.gz
            test "$(cat /tmp/storage-restore/backup-preflight.txt)" = offserver-restore-ok
        '
    echo "Encrypted Spaces backup, checksums, PostgreSQL sample totals, Redis data and storage restore passed."
fi
echo "VPS containers, fresh migrations, full readiness, frontend assets and sample-data backup/restore passed."
