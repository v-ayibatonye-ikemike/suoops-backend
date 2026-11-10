#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
[[ -f monitor.env && ! -L monitor.env ]] ||
    { echo "Missing regular monitor.env" >&2; exit 1; }
mode="$(stat -c '%a' monitor.env 2>/dev/null || stat -f '%Lp' monitor.env)"
[[ "$mode" == 600 ]] || { echo "monitor.env must have mode 600" >&2; exit 1; }
source ./monitor.env

case "${1:-}" in
    api) url="${API_HEARTBEAT_URL:-}" ;;
    backup) url="${BACKUP_HEARTBEAT_URL:-}" ;;
    *) echo "Usage: heartbeat.sh api|backup" >&2; exit 1 ;;
esac
[[ "$url" =~ ^https://hc-ping\.com/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/?$ ]] ||
    { echo "Configure a valid Healthchecks HTTPS callback in monitor.env" >&2; exit 1; }

if [[ "$1" == api ]]; then
    [[ "${API_HEALTH_URL:-}" =~ ^https://[a-zA-Z0-9.-]+/ready$ ]] ||
        { echo "Configure an HTTPS API /ready URL in monitor.env" >&2; exit 1; }
    if ! curl --fail --silent --show-error --output /dev/null \
        --connect-timeout 5 --max-time 20 "$API_HEALTH_URL"; then
        echo "API readiness probe failed; heartbeat not sent" >&2
        exit 1
    fi
fi

if ! printf 'url = "%s"\n' "$url" |
    curl --config - --fail --silent --show-error --output /dev/null \
        --connect-timeout 5 --max-time 15 --retry 2 --data ""; then
    echo "External $1 heartbeat delivery failed" >&2
    exit 1
fi
echo "External $1 heartbeat completed."
