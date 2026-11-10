#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
umask 077
[[ -f restic.env ]] || { echo "Complete restic.env before initialization." >&2; exit 1; }
chmod 600 restic.env
source ./restic.env
: "${RESTIC_REPOSITORY:?Set RESTIC_REPOSITORY}"
: "${AWS_ACCESS_KEY_ID:?Set AWS_ACCESS_KEY_ID}"
: "${AWS_SECRET_ACCESS_KEY:?Set AWS_SECRET_ACCESS_KEY}"
if [[ -n "${RESTIC_PASSWORD:-}" ]]; then
    echo "Refusing to replace an existing restic password." >&2
    exit 1
fi
printf 'RESTIC_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> restic.env
echo "Generated the restic encryption password in owner-only restic.env. Keep an off-VPS recovery copy."
