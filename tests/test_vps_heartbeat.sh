#!/usr/bin/env bash
set -euo pipefail
repo="$(cd -- "$(dirname -- "$0")/.." && pwd)"
umask 077
temp_dir="$(mktemp -d)"
cleanup() {
    rm -f -- "$temp_dir/heartbeat.sh" "$temp_dir/monitor.env" "$temp_dir/calls" "$temp_dir/bin/curl"
    rmdir -- "$temp_dir/bin" "$temp_dir"
}
trap cleanup EXIT
mkdir "$temp_dir/bin"
cp "$repo/deploy/vps/heartbeat.sh" "$temp_dir/"
cat > "$temp_dir/bin/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"--config -"* ]]; then
    config="$(cat)"
    [[ "$config" == 'url = "https://hc-ping.com/'* ]]
    echo callback >> "$CALL_LOG"
    [[ "${FAIL_CALLBACK:-false}" != true ]]
else
    [[ "$*" == *https://api.suoops.com/ready* ]]
    echo readiness >> "$CALL_LOG"
    [[ "${FAIL_API:-false}" != true ]]
fi
SH
chmod 755 "$temp_dir/bin/curl"
export PATH="$temp_dir/bin:$PATH" CALL_LOG="$temp_dir/calls"
printf '%s\n' "API_HEALTH_URL='https://api.suoops.com/ready'" \
    "API_HEARTBEAT_URL='https://hc-ping.com/11111111-1111-1111-1111-111111111111'" \
    "BACKUP_HEARTBEAT_URL='https://hc-ping.com/22222222-2222-2222-2222-222222222222'" > "$temp_dir/monitor.env"
bash "$temp_dir/heartbeat.sh" api
[[ "$(cat "$CALL_LOG")" == $'readiness\ncallback' ]]
: > "$CALL_LOG"
bash "$temp_dir/heartbeat.sh" backup
[[ "$(cat "$CALL_LOG")" == callback ]]
: > "$CALL_LOG"
if FAIL_API=true bash "$temp_dir/heartbeat.sh" api; then
    echo "Failed readiness unexpectedly sent a success heartbeat" >&2; exit 1
fi
[[ "$(cat "$CALL_LOG")" == readiness ]]
if FAIL_CALLBACK=true bash "$temp_dir/heartbeat.sh" backup; then
    echo "Failed heartbeat delivery unexpectedly succeeded" >&2; exit 1
fi
chmod 644 "$temp_dir/monitor.env"
: > "$CALL_LOG"
if bash "$temp_dir/heartbeat.sh" backup; then
    echo "Unprotected monitor settings were accepted" >&2; exit 1
fi
[[ ! -s "$CALL_LOG" ]]
chmod 600 "$temp_dir/monitor.env"
printf '%s\n' "BACKUP_HEARTBEAT_URL='http://hc-ping.com/22222222-2222-2222-2222-222222222222'" > "$temp_dir/monitor.env"
if bash "$temp_dir/heartbeat.sh" backup; then
    echo "Insecure callback URL was accepted" >&2; exit 1
fi
[[ ! -s "$CALL_LOG" ]]
echo "API readiness gating, backup callbacks, delivery failures and protected settings passed."
