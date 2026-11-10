# Single-VPS deployment and migration

Target: one DigitalOcean Ubuntu 24.04 LTS, x86-64 Droplet with 8 GiB RAM in London.
Services: Caddy, main frontend, support/admin frontend, API, Celery worker,
one Celery Beat scheduler, PostgreSQL, and Redis. Only Caddy publishes ports.
This is **not high availability**: losing the Droplet interrupts every service.

The existing development Compose and Railway/Vercel deployments are unchanged.
Keep existing S3/object storage; do not move invoice files or change bucket names
as part of the compute migration.

Run the infrastructure regression checks from the backend repository with
`bash tests/test_vps_deployment.sh` (Docker Compose, jq and OpenSSL required).
The checks use disposable credentials and a network-isolated environment probe,
never the production environment files.
After building immutable tagged images, run
`RELEASE_TAG=YOUR_TAG bash tests/test_vps_runtime.sh` from the backend repository.
It uses a separate disposable project, test-only settings, internal-only
networking and no public ingress. It checks fresh migrations, API dependencies,
both frontends' assets, and a sample-data backup/restore, then removes only its own
test containers/volumes. This does not verify production data.
To also test the configured encrypted off-server repository, run:

```bash
RESTIC_TEST_ENV=/opt/suoops/suoops-backend/deploy/vps/restic.env \
  RELEASE_TAG=YOUR_TAG bash tests/test_vps_runtime.sh
```

Initialize the repository first. The optional test runs the actual backup script
against disposable containers, restores from Spaces, checks every file checksum,
and verifies PostgreSQL sample totals, Redis data and storage contents. It retains
a snapshot tagged `suoops-vps-preflight-...`, separate from production's `suoops-vps`
snapshots. Restored files and disposable credentials are removed locally.

## 1. Provision and protect the VPS

1. In DigitalOcean, choose a **bundled Basic Droplet**, Ubuntu 24.04 LTS, x86-64,
   8 GiB RAM. Compare the displayed monthly price before purchase: Premium CPU
   plans and newer resource-priced plans can cost more. London is selected;
   verify latency from Nigeria and to existing object storage before cutover.
2. Add an SSH public key, not a password. Enable daily Droplet backups.
   The London bundled Regular plan selected in the dashboard on 2026-11-08
   costs $48/month (4 vCPU, 8 GiB RAM, 160 GB SSD); daily backups add
   $14.40/month. Taxes, off-server storage and any overages are additional.
   Optionally supply `cloud-init.yml` as the startup script. It installs Docker
   from the official repository, backup tools, a key-only `suoops` operator,
   security updates and UFW. It copies the provider-injected SSH public keys;
   no private keys or application secrets are embedded.
   Verify `cloud-init status --wait` and `/var/lib/suoops-bootstrap-complete`.
   Root key access remains available until non-root SSH is verified. After
   testing `suoops` SSH and sudo, install `sshd-hardened.conf` as
   `/etc/ssh/sshd_config.d/00-suoops.conf`, validate with `sshd -t`, reload SSH
   and test a second connection.
3. Configure a DigitalOcean Cloud Firewall: TCP 22 only from operator IPs;
   TCP 80/443 and UDP 443 only from Cloudflare's published IPv4/IPv6 networks.
   Temporarily include operator IPs on web ports for direct-origin TLS validation,
   then remove those exceptions after the proxied domains are verified.
   Allow outbound traffic. Do not expose
   3000, 8000, 5432, or 6379. Docker-published ports can bypass UFW rules, so use
   the cloud firewall as the perimeter control.
   With a dynamic operator IP, manage SSH source restrictions in the cloud firewall,
   while UFW permits TCP 22 and `sshd` remains key-only. Duplicating a changing `/32`
   in UFW can lock out both SSH and the network-based Droplet Console. After an IP
   change, update only the cloud rule and verify a new non-root SSH connection.
   Recovery Console access requires a local password; a temporary root-password
   reset is a rebooting recovery action, not a reason to enable root/password SSH.
   Lock the temporary root password again once non-root SSH/sudo are verified.
4. Create a non-root SSH operator with sudo; test its login before disabling
   root/password SSH. Enable security updates and external uptime monitoring.
5. Install Docker Engine and the Compose plugin using Docker's official Ubuntu
   instructions. Require Compose **2.30+** for raw backend environment files.
   Install backup tools with `sudo apt-get install restic`.
6. Place both repositories alongside each other:

   ```text
   /opt/suoops/suoops-backend
   /opt/suoops/suoops-frontend
   ```

   Transfer only reviewed application/deployment source. Do not send local
   `.env` files, virtualenvs, logs, private keys, node_modules, or old dumps with
   the code. Uncommitted changes are not included in a normal clone: explicitly
   review and transfer the intended release.
   For the selected stable migration release, retain preflight directories and
   make these stable paths symlinks to the clean release repositories. Backup
   systemd units use the stable backend path; copy owner-only `restic.env` to the
   selected release before switching. Keep the timer disabled until production
   data is restored and a real backup succeeds.
7. Monitor memory, disk, Redis memory/errors, Postgres connections, worker
   health, scheduler failures and backup failures. Container memory limits are
   ceilings, not reservations. Build on a separate machine/CI for later
   releases rather than consuming production RAM.

## 2. Initialize credentials and preserve active settings

On the VPS:

```bash
cd /opt/suoops/suoops-backend/deploy/vps
bash init-secrets.sh
```

This generates **new** database, Redis, JWT, OAuth-state and telemetry keys into
owner-only ignored files, without printing them. It refuses to overwrite files.
It does not rotate credentials at external providers.

Complete `.env` with a valid `ACME_EMAIL`, source PostgreSQL major version, and
the public frontend build settings currently in Vercel. Database and Redis
passwords must remain hex strings generated by the script (safe in URLs).
PostgreSQL 18 matches the live Railway source verified on 2026-11-08 (18.6).
Query the source again before the final export. The volume mounts at
`/var/lib/postgresql` with major-version-specific `PGDATA`, as required for the
PostgreSQL 18 image layout. Changing `POSTGRES_VERSION` is not a database upgrade:
restore into a fresh volume and test it. Do not reuse an older `/var/lib/postgresql/data`
volume with this layout without an explicit data migration.
The source also preloads `pg_stat_statements`; the target preserves this so its
restored extension views work. Compare application base-table counts and numeric
totals from one exported source snapshot, not volatile monitoring-view statistics.

Redis 8.2.10 matches the live Railway source verified on 2026-11-08. Its replication
snapshot uses RDB format 0012. Recheck the source version before the final export;
use the same version for production and recovery tests. Preserve Redis data:
the source has persistent RedBeat schedules even when all task queues are empty.

Complete `backend.env` from a secure export of **all active production settings**,
not just the example. Keep WhatsApp templates, feature flags, payment plan codes,
mail configuration, storage, OAuth, and provider selection unchanged. Backend
raw env files do not expand `$` values; do not add shell quotes around values.
Blank required provider settings will prevent production startup.

**Preserve the current `ENCRYPTION_KEY` exactly.** A fresh Fernet key cannot
decrypt existing rows. Back it up securely and rotate only through a separately
tested decrypt/re-encrypt migration. Do not rotate it with JWT credentials.

Complete owner-only `caddy.env` with `CF_API_TOKEN` from a new Cloudflare token
restricted to **Zone Read + DNS Edit for suoops.com only**. Do not put this
token in frontend/backend settings or build arguments. It is used only by
Caddy and included in encrypted backups.
If the token is restricted to the VPS source IP, update that restriction or issue
a replacement before DNS-01 certificate recovery on a Droplet with a different IP.

Compose overrides `ENV`, database/Redis URLs, domains, CORS and proxy settings.
The configuration preserves Cloudflare-proxied sibling domains under
`suoops.com`. Caddy obtains certificates through DNS-01 before changing origin
records. Its Cloudflare module is built into the custom image. Caddy trusts
`CF-Connecting-IP` only from Cloudflare's published networks, removes that
header upstream and forwards one normalized client-IP hop. Use the
`X-SuoOps-Origin: vps` response marker when verifying the DNS cutover. Keep
`TRUSTED_PROXY_HOPS=1`; verify genuine and forged-header requests, including
support/admin restrictions. Review the ranges against
https://www.cloudflare.com/ips-v4 and https://www.cloudflare.com/ips-v6
when upgrading. Other CDNs require a separately verified configuration.

Before starting a restored database with a new JWT secret, rehearse the
OAuth token migration against an isolated copy of the final source dump.
With only the private target database running, use owner-only exports:

```bash
docker compose --profile tools run --rm --no-deps --user root \
  -v /opt/suoops/migration-source/railway-source.json:/run/source-settings.json:ro \
  -v "$PWD/backend.env":/run/target-settings.env:ro \
  --entrypoint python migrate -m deploy.vps.reencrypt_oauth_tokens \
  --source-settings /run/source-settings.json \
  --target-settings /run/target-settings.env \
  --expected-count "${OAUTH_ROW_COUNT:?Set from the final source snapshot}"
```

This is a dry-run. Repeat with `--apply` to commit, then dry-run again to
verify the ciphertexts are already using the target key. The tool checks
permissions, the private `db` host, active JWT equality, row count and
decrypt/re-encrypt equality, and rolls back the entire transaction on failure.
Root is used only for this one-off process to read protected mounts; normal
application containers remain non-root. Preserve the old recovery keys in
encrypted backup; never print token contents.

Before startup:

```bash
docker compose config --quiet
docker compose build api frontend support caddy
```

Never print `docker compose config` without `--quiet`: it includes secrets.
Public frontend settings are compiled into the image; changing them requires a
rebuild. No provider secrets or Sentry auth token belong in build arguments.
The main frontend mounts its existing `.npmrc` as a build secret to preserve
the repository's npm installation settings. The support app uses npm defaults.
For direct main-frontend builds outside Compose, pass
`--secret id=npmrc,src=/path/to/suoops-frontend/.npmrc`.
If source-map uploads are required, wire a scoped Sentry token through a
build-secret/CI integration separately; it is not included in this recipe.

## 3. Rehearse before cutover

Inventory the **live** Railway database/Redis versions, dataset size, active
queue names and scheduled tasks. Repository Render documentation and any old
dump are not proof of the current production source.

Export a fresh PostgreSQL custom-format dump using a compatible `pg_dump`
(same or newer major than the source). Store connection credentials in an
owner-only `.pgpass`/service file, not in commands, chat, or shell history.
Use the provider's TLS-protected endpoint.

```bash
pg_dump --dbname=service=railway-source --format=custom --no-owner --no-acl \
  --file=source.dump
pg_restore --list source.dump > source-manifest.txt
sha256sum source.dump
```

Securely transfer the dump and verify its checksum on the VPS. Start only the
data services, then restore into the **empty new target**:

```bash
docker compose up -d --wait db redis
docker compose exec -T db sh -ec \
  'pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < /secure/path/source.dump
docker compose run --rm migrate
```

Do not use `--clean` against an unverified target. If rehearsal restores are
repeated, use a separate empty database/project; never erase the live volumes.
Check schema migration revision, row counts, key business totals (wallets,
payments, invoices, orders, payouts), foreign keys, and sample encrypted fields.

Keep Beat **off** during rehearsal. A worker may process queued tasks and contact
customers/payment providers; use a disposable rehearsal Redis with no production
tasks and safely disabled outbound integrations. Do not run live-money tests.
Rehearse login/CSRF/OAuth, invoices/PDFs, storefront and support/admin access.

## 4. Backups and restore test (cutover prerequisite)

Droplet snapshots are not a substitute for database-aware off-server backups.
Use a dedicated private Spaces/S3 bucket with a restricted backup access key.
Keep the restic password and recovery credentials in a password manager outside
the VPS. Existing invoice storage requires its own versioning/backup policy.

```bash
cp restic.env.example restic.env
chmod 600 restic.env
# Edit restic.env securely; use shell-quoted values.
# If RESTIC_PASSWORD is blank, generate it without printing it:
bash init-backup-secrets.sh
set -a
source ./restic.env
set +a
restic init
bash backup.sh
```

Run the initial backup as the deployment owner before enabling the root timer.
The shared `.backup.lock` stays inside the protected deployment directory,
avoiding cross-user lock-file restrictions in the world-writable `/tmp` directory.

Cloud-init enables Redis-compatible memory overcommit for reliable RDB saves
and AOF rewrites. On an existing VPS, apply the same persistent setting:

```bash
sudo install -m 644 90-suoops-redis.conf /etc/sysctl.d/90-suoops-redis.conf
sudo sysctl -p /etc/sysctl.d/90-suoops-redis.conf
```

The script uses `pg_dump`, a Redis replication snapshot, a storage archive and
the deployment environment files, then encrypts/uploads them through restic.
It fails if any step fails. PostgreSQL is transaction-consistent, but this
running-service backup is **not atomic across PostgreSQL, Redis and files**.
Do not blindly replay restored payment tasks; reconcile them first.

On the VPS, after a successful manual backup:

```bash
sudo install -m 644 suoops-backup.service suoops-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now suoops-backup.timer
sudo systemctl start suoops-backup.service
sudo systemctl status suoops-backup.service --no-pager
```

Configure external alerts for failed jobs **and absence of a successful backup**.
Hourly snapshots target roughly one hour of data loss, not a guarantee. For
stricter recovery objectives, add PostgreSQL WAL archiving/PITR.

### Independent heartbeat alerts

Create two free Healthchecks checks with email notifications enabled: API readiness
with a 60-second period and 300-second grace, and hourly backup with a 3600-second
period and 900-second grace. Populate owner-only `monitor.env` from its example;
callback URLs are private credentials. Confirm a failure and recovery email for
each check before relying on them.

The API probe runs on the VPS and only pings after the public `/ready` endpoint
succeeds. Missing pings also detect host/probe failure. Production backup pings
only after upload and `restic check` succeed. Preflight backup tags never refresh
the production backup check. Heartbeat delivery failures are explicit job failures.
The monitor settings are included in encrypted recovery backups.

After public cutover and readiness verification:

```bash
sudo install -m 644 suoops-api-heartbeat.service suoops-api-heartbeat.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start suoops-api-heartbeat.service
sudo systemctl enable --now suoops-api-heartbeat.timer
```

Restore a snapshot onto a **separate test host** before cutover and monthly:

```bash
restic snapshots --tag suoops-vps
restic restore SNAPSHOT_ID --target /secure/restore-test
```

Locate its `/tmp/suoops-backup.*` directory under the restore target; run
`sha256sum -c SHA256SUMS` there. Initialize an empty matching-version PostgreSQL
target and restore `postgres.dump` using the command above. Verify counts,
financial totals and encrypted fields; inspect `storage.tar.gz` before extracting
into its storage volume with the app user's ownership.

For Redis recovery, use an **empty** Redis volume with consumers stopped, put
`redis.rdb` at `/data/dump.rdb` owned by the image's redis user. Do **not** start
the normal service yet: `appendonly yes` can initialize empty AOF files instead
of loading the RDB, even without a pre-existing AOF. Load with AOF disabled,
convert the loaded dataset to AOF, then start the normal service:

```bash
docker compose run --rm --no-deps redis sh -ec '
  test -f /data/dump.rdb
  test ! -e /data/appendonlydir
  test ! -e /data/appendonly.aof
  umask 077
  printf "requirepass %s\nappendonly no\nsave \"\"\n" "$REDIS_PASSWORD" > /tmp/restore.conf
  redis-server /tmp/restore.conf --daemonize yes --port 0 \
    --unixsocket /tmp/restore.sock --logfile /tmp/restore.log
  trap "redis-cli -s /tmp/restore.sock shutdown nosave" EXIT
  for attempt in $(seq 1 60); do
    if test "$(redis-cli -s /tmp/restore.sock PING)" = PONG; then break; fi
    if test "$attempt" = 60; then echo "Redis restore readiness timed out" >&2; exit 1; fi
    sleep 1
  done
  redis-cli -s /tmp/restore.sock DBSIZE
  redis-cli -s /tmp/restore.sock CONFIG SET appendonly yes
  for attempt in $(seq 1 60); do
    persistence="$(redis-cli -s /tmp/restore.sock INFO persistence)"
    if printf "%s\n" "$persistence" | grep -q "^aof_rewrite_in_progress:0" &&
       printf "%s\n" "$persistence" | grep -q "^aof_last_bgrewrite_status:ok"; then break; fi
    if test "$attempt" = 60; then echo "Redis AOF conversion failed or timed out" >&2; exit 1; fi
    sleep 1
  done
'
docker compose up -d --wait redis
```

Verify restored keys, durable schedules and queued/delayed tasks after the normal
service starts. Expired TTL keys may legitimately disappear. Existing AOF files
take precedence over RDB: never copy an RDB over a running production Redis and
expect it to restore. Reconcile stale locks before starting the worker/scheduler.

Keep 24 hourly, 7 daily, 4 weekly and 6 monthly snapshots, for example:

```bash
restic forget --tag suoops-vps --group-by tags \
  --keep-hourly 24 --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
```

Run pruning separately from the hourly backup; review retention and test recovery
first. `restic check` checks repository integrity, not application recoverability.

## 5. Production cutover

1. Lower relevant DNS TTLs ahead of the migration. Inventory A/AAAA/CNAME and
   CAA records for root, www, API and support; remove stale IPv6 records when
   the new VPS does not serve IPv6.
2. Announce a maintenance window. Stop old Beat first, block old API writes
   including webhooks, then drain old workers and stop them. Coordinate webhook
   delivery/retries with Meta and payment providers. Do not run old and new
   schedulers concurrently.
3. Take a **final fresh database dump after writes stop**, and restore it into
   the new clean target. Reconcile payment events during the window. Export
   source Redis through its supported provider export, TLS, or authenticated,
   host-key-pinned SSH into the private source container. Run `redis-cli --rdb`
   there with `REDISCLI_AUTH` supplied securely (never a password in CLI flags).
4. Do not flush old Redis. Inventory queues, in-flight/delayed tasks, OTPs,
   results, locks and RedBeat schedule state. A drained empty new Redis is
   acceptable **only after confirming no pending/delayed tasks or durable
   scheduler state are being discarded**. Otherwise import the source RDB
   into the empty target volume as above; check Redis version compatibility
   and reconcile stale locks before consumers start.
5. Run migrations once. Allow-list the Droplet's static outbound IPv4 with
   Paystack/Flutterwave and other restricted providers. Preserve existing
   outbound proxies until direct calls have been verified.
6. Start the app services, leaving Beat stopped:

   ```bash
   docker compose up -d --wait frontend support api worker
   docker compose exec -T api python -c \
     "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=15).read().decode())"
   ```

7. Start Caddy before changing production DNS, obtain DNS-01 certificates,
   and verify TLS directly using `curl --resolve` without disabling checks.
   Save the existing origin records, proxy flags and TTLs for rollback.
   Then move root/www/API/support origin records to the Droplet, retaining
   their Cloudflare proxy flags. Ensure Cloudflare uses Full (strict) TLS.
   If other proxied zone consumers are unverified, use a Configuration Rule
   matching only the four migrated hostnames and leave the zone-wide mode
   unchanged:

   ```text
   http.host in {"suoops.com" "www.suoops.com" "api.suoops.com" "support.suoops.com"}
   ```

   Set the rule's SSL option to `strict`. Manage this through the authenticated
   dashboard or a separate Config Rules token; do not broaden Caddy's DNS token.
   After direct-origin TLS and public routing pass, remove operator web-port
   exceptions and verify direct non-Cloudflare HTTPS is blocked while all four
   proxied sites and operator SSH remain reachable.

   ```bash
   docker compose up -d caddy
   curl --fail --show-error https://api.suoops.com/ready
   curl --fail --show-error --output /dev/null https://suoops.com/
   curl --fail --show-error --output /dev/null https://support.suoops.com/
   ```

   DNS-01 needs the scoped token and public TXT-record propagation, not an
   early production A-record change. Until certificate issuance and verified
   direct-origin TLS succeed, do not change production origin records.
8. Verify login, CSRF cookies, Google OAuth, support/admin IP restrictions,
   invoice and receipt PDFs, uploads, provider connectivity, payment webhook
   signatures/idempotency, and a controlled real transaction if explicitly
   approved. Confirm balances/totals and worker health again.
9. Start **one** scheduler: `docker compose up -d beat`. Check its logs and
   task execution, then verify an off-server backup and restore.

Keep old services disabled but available for the agreed rollback window.
Do not cancel Railway/Vercel or destroy their data during DNS propagation.

## 6. Credential rotation checklist

Track each credential's owner, consumers, replacement, successful verification,
and revocation; record no values in source or reports.

| Credential | Action and verification |
| --- | --- |
| Database / Redis | New local credentials are generated; retire old provider credentials after rollback window. Changing env files does not change credentials in an existing PostgreSQL volume: use an explicit SQL role-password change and coordinated restart for later rotations. |
| JWT / OAuth-state | New keys invalidate sessions and in-flight OAuth. Announce re-login. Stored OAuth access/refresh tokens are also encrypted with a key derived from `JWT_SECRET`: before starting a restored target with the new JWT key, strictly decrypt those tokens with the old key and re-encrypt them with the new key in a tested, target-only transaction. Verify decrypted contents remain identical without logging them. Preserve an encrypted source backup and the old recovery key; final export must repeat this step for any new rows. Remove duplicated old values from copied settings only after this succeeds. |
| Encryption key | Preserve; rotate only with tested re-encryption and recoverable backups. |
| Telemetry ingestion | New key is generated; update all ingestion clients and verify authorized events. |
| Meta WhatsApp / Facebook / Instagram | Issue replacement tokens and test sending/signatures. Rotating an app secret affects all app consumers; coordinate them. Change webhook verify token with Meta's verification setup, not independently. |
| Paystack / Flutterwave / courier / other APIs | Check each provider's rotation mechanism and whether old/new keys can coexist. Coordinate webhook secrets, IP allow-lists and all consumers; verify without duplicate payments/payouts. |
| S3 / mail / Google OAuth / OpenAI / HioBuy | Issue scoped replacements for active integrations, stage provider authentication before replacing live settings, recreate API/worker/Beat together, verify each running consumer, then revoke old keys. Preserve provider IP allow-lists and encrypt the updated configuration off-server. Set `EMAIL_TRANSPORT=brevo_api` with a REST `BREVO_API_KEY` on DigitalOcean, which blocks SMTP ports 25/465/587. This transports OTP, support/team mail, worker mail and invoice/receipt PDFs over HTTPS; the application default `smtp` preserves legacy SMTP behavior. Verify actual owner OTP delivery, not only API authentication; verify SMTP keys separately over TLS without sending mail. Preserve disabled contact-sync and inactive mail-transport flags when rotating their configured keys. Do not enable inactive integrations during migration. |
| Mapbox | The frontend static location image needs only a public `pk.` token with `styles:tiles`, restricted to `https://suoops.com` (Mapbox also permits its HTTPS subdomains, including `www`). Verify image responses for allowed origins and HTTP 403 for an unrelated origin. Keep the backend geocoding token separate: do not give it the browser token's URL restrictions. Rebuild the frontend with `NEXT_PUBLIC_MAPBOX_TOKEN`, test, and retire the old frontend token only when no other consumer needs it. |
| Sentry / CI / GitHub / deployment / backup keys | Replace scoped tokens through their provider and secure secret stores. Keep backup recovery credentials outside the VPS. |
| Railway / Vercel access tokens | Revoke obsolete deployment access only after rollback is no longer required and no other project depends on it. |

For providers allowing only one active key, rotation can immediately break the
old platform. Schedule it explicitly rather than assuming overlapping validity.
Suspected-compromised credentials require urgent revocation independent of this
normal migration sequence.

## 7. Rollback and later releases

Before new writes, stop new Beat/workers/API and restore original DNS; verify
the old environment's credentials before restarting old services.
**After new writes, DNS-only rollback is unsafe**: reconcile/transfer the new
database and queue state and verify schema compatibility first. Never operate
two writable production databases. Keep the preserved encryption key available.

Use immutable `RELEASE_TAG` image tags for future deployments. Back up and
review migrations before release. Start data services, run the migration job
once, update API/frontend/worker and verify `/ready` before starting Beat.
Rolling back an image does not undo database migrations. This Compose setup
does not provide zero-downtime releases or automatic failover.
