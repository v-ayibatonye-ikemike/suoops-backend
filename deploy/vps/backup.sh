#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
umask 077
command -v restic >/dev/null
command -v flock >/dev/null
[[ -f restic.env ]] || { echo "Missing restic.env" >&2; exit 1; }

exec 9>./.backup.lock
flock -n 9 || { echo "Another SuoOps backup is running" >&2; exit 1; }
set -a
source ./restic.env
set +a
: "${RESTIC_REPOSITORY:?Set RESTIC_REPOSITORY}"
: "${RESTIC_PASSWORD:?Set RESTIC_PASSWORD}"
: "${AWS_ACCESS_KEY_ID:?Set AWS_ACCESS_KEY_ID}"
: "${AWS_SECRET_ACCESS_KEY:?Set AWS_SECRET_ACCESS_KEY}"

temp_dir="$(mktemp -d /tmp/suoops-backup.XXXXXXXX)"
cleanup() {
    rm -f -- "$temp_dir/postgres.dump" "$temp_dir/redis.rdb" \
        "$temp_dir/storage.tar.gz" "$temp_dir/.env" "$temp_dir/backend.env" "$temp_dir/caddy.env" "$temp_dir/monitor.env" \
        "$temp_dir/SHA256SUMS"
    rmdir -- "$temp_dir"
}
trap cleanup EXIT

docker compose exec -T db sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' > "$temp_dir/postgres.dump"
docker compose exec -T redis redis-cli --rdb /data/backup.rdb
docker compose cp redis:/data/backup.rdb "$temp_dir/redis.rdb"
docker compose exec -T redis rm -f /data/backup.rdb
docker compose exec -T api tar -C /app/storage -czf - . > "$temp_dir/storage.tar.gz"
cp .env backend.env caddy.env monitor.env "$temp_dir/"
(
    cd "$temp_dir"
    sha256sum postgres.dump redis.rdb storage.tar.gz .env backend.env caddy.env monitor.env > SHA256SUMS
)
restic backup "$temp_dir" --tag "${BACKUP_TAG:-suoops-vps}"
restic check
if [[ "${BACKUP_TAG:-suoops-vps}" == suoops-vps ]]; then
    bash ./heartbeat.sh backup
fi
echo "Encrypted off-server backup and repository check completed."
