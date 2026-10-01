# Operations Runbook — Single-User VPS V1 (Phase 8.6-2)

This is the operator manual for the Docker Compose deployment. It covers
bootstrap, daily operation, backup/restore, update, rollback, and the
security posture. Live AI / WordPress validation is a separate closeout
step (Phase 8.6-3) and is not performed here.

Conventions used below:

- All commands run on the VPS host from the repository checkout directory.
- `docker compose` means the Compose file in this repo (`compose.yaml`).
- Never commit `.env` or `deployment/profiles.json` (both gitignored).

## 1. Fresh bootstrap (empty server)

Prerequisites: git, Docker Engine + Compose plugin.

1. Clone the repository and check out the release ref under test.
2. Copy the environment template and fill in secrets (see section 2):
   `cp .env.example .env`
3. Copy the profiles template and edit site/brand to match your submissions:
   `cp deployment/profiles.example.json deployment/profiles.json`
   `workspace_id` and `provider_connection_id` already match the
   migration-seeded default workspace and OPENAI connection; change them
   only if you provisioned different ones. No credentials go in this file.
4. Build and start everything (migration runs first, automatically):
   `docker compose up -d --build`
   The one-shot `migrate` service applies migrations 1–16 to the shared
   `aiwf-data` volume and exits 0; api/worker/publication-worker start only
   after it succeeds (`service_completed_successfully`).
5. If the model differs from the seeded `gpt-4`, set it (section 3).
6. Provision the WordPress publishing target (section 3). Required before
   any publication; content generation works without it.
7. Verify health:
   `curl http://127.0.0.1:8000/api/v1/system/status`
   Expect HTTP 200 with `"api":"OK"`, `"database":"OK"`,
   `"schema_version":16`. (`worker.status` is `UNKNOWN` until a run is
   actively claimed — that is normal, not a failure. See section 4.)

## 2. Environment and secrets

- `.env` is loaded by Compose (`env_file`), never by the application.
- Safe values: `HOST_PORT`, `AIWF_DATABASE`, `AIWF_PROFILES_FILE` (defaults
  in `.env.example` match the Compose volume layout; keep them).
- Secret values (empty placeholders in `.env.example`, real values only in
  `.env`): `OPENAI_API_KEY`, `GROQ_API_KEY` (only if a connection references
  it), `AIWF_WP_APP_PASSWORD`. The database stores only `env:NAME`
  references; raw secrets never enter SQLite, logs, or API responses.
- Protect `.env` as a secret: host-readable only by the operator
  (`chmod 600 .env`), include it in backups separately from any shared or
  off-site artifact copies (section 5).

## 3. Admin commands inside Docker

There is no `admin` Compose service. Run admin through the one-shot
`migrate` service definition, which carries the image, environment, and
database volume (overriding only its command). Admin self-migrates, so
these are safe on a fresh volume:

```sh
# Set the default TEXT model (bumps provider configuration_version by one;
# already-queued runs keep their snapshot):
docker compose run --rm migrate python -m admin provider update \
  --workspace-id 9bf33b12-307b-4e08-a9fb-b84d88e1c162 \
  --provider-connection-id 9ac50c29-9366-4a03-9180-ae223b53e472 \
  --default-model gpt-4.1

# Provision the WordPress publishing target (once; fails if an ACTIVE
# target already exists rather than replacing it):
docker compose run --rm migrate python -m admin publishing-target add \
  --workspace-id 9bf33b12-307b-4e08-a9fb-b84d88e1c162 \
  --base-url https://example.test \
  --username deployer \
  --credential-reference env:AIWF_WP_APP_PASSWORD
```

Use the IDs above only for the migration-seeded default workspace and
OPENAI connection; otherwise substitute your own. Provisioning performs no
external calls — the first real publication validates connectivity. There
are no admin commands for provider creation/listing or target
update/disable/listing in V1; changing a target URL requires a new
provisioning cycle per current application behavior.

## 4. Normal operations

```sh
docker compose up -d --build   # initial deployment or source/image update
docker compose stop            # normal stop (containers kept)
docker compose up -d           # restart after stop (no rebuild)
docker compose ps              # service status
docker compose logs -f api                # API logs
docker compose logs -f worker             # content-worker logs
docker compose logs -f publication-worker # publication-worker logs
curl http://127.0.0.1:8000/api/v1/system/status  # API health
```

Distinguish three independent signals: container running (`ps`/`up`),
API healthy (`system/status` HTTP 200 with `"database":"OK"`), and worker
liveness (`worker.status` in the same payload — `UNKNOWN` with no active
run, `ONLINE`/`OFFLINE` from run heartbeats). Task and publication
progress itself is visible per task through the API task/events endpoints
and the UI; worker idleness with an empty queue is normal, not an error.

## 5. Backup (quiesced) and restore

V1 backup is QUIESCED BACKUP. Do not copy SQLite files while services are
running: durable state spans the main DB file plus WAL/SHM siblings, and a
hot copy can capture a torn image. There is no hot-backup tooling in V1.

Back up this exact set, in this order:

1. `docker compose stop` (guarantees no writer is mid-transaction).
2. Copy the volume contents:
   - `aiwf-data` (SQLite durable state, including WAL/SHM siblings —
     back up the whole volume contents, never just `aiwf.sqlite3` alone).
   - `aiwf-artifacts` (preview PNGs and local images; the database holds
     only hashes/keys, so files are required for a complete restore).
   - `deployment/profiles.json` (site/brand→connection bindings).
   - `.env` (secrets) — stored separately and protected (see section 2);
     never place it in a shared/off-site bundle unencrypted.
   Use e.g. `docker run --rm -v aiwf-data:/data -v "$PWD/backup:/backup" ...`
   style volume copies, or host volume-path copies; the mechanism matters
   less than the stopped-writers precondition.
3. `docker compose up -d` to resume.

Restore onto a fresh host: install per section 1 (build image, copy `.env`
and `profiles.json` into place), stop services, restore all four items
above, then repair volume ownership, then start. Restoring ONLY the SQLite
DB is explicitly insufficient: artifact files would be missing (previews
fail closed), profiles bindings could mismatch, and secrets would be absent
(credential references would not resolve).

Ownership repair (required): AIWF services run as non-root
(`appuser` UID 997, `app` group GID 997 — the current image contract; if it
ever changes, this step changes with it). Volume contents restored with a
root container arrive owned by root, and root-owned files then fail at
runtime with `attempt to write a readonly database` even though the bytes
are present: SQLite needs directory write access for WAL/SHM/runtime
writes, and the content worker needs write access to artifact storage.
Repair both volumes before starting any service (example with a temporary
container; do NOT use `chmod 777`, and do NOT run the application as root):

```sh
docker run --rm \
  -v aiwf-data:/data \
  -v aiwf-artifacts:/artifacts \
  alpine chown -R 997:997 /data /artifacts
```

Verify the restore, in this order: `docker compose up -d` (the `migrate`
gate must exit successfully), then API health per section 1 step 7
(HTTP 200 with `"database":"OK"`), then confirm a known task's
detail/events read back correctly. Only then is the restore complete.

## 6. Update

Conservative single-user update (no zero-downtime promise in V1):

1. Take a quiesced backup per section 5.
2. `git pull` (or check out the new release ref).
3. `docker compose up -d --build` — plain `up -d` alone does NOT rebuild
   the mutable `aiwf-app:8.6` image; `--build` is required to pick up new
   source. `migrate` runs before the long-running services start.
4. Verify health per section 1 step 7 and confirm `schema_version` advanced
   if the release added migrations.

Rollback / failure: migrations are forward-only — there is no downgrade
path, and an older image may refuse a newer schema (the runner fails closed
on history mismatch rather than corrupting data). If an update fails after
the schema moved, rollback means restoring the pre-update backup from
section 5 onto the previous image, not starting old code against the new
DB. Never hand-edit `schema_migrations`.

## 7. Security posture (operator responsibility)

- The API is intentionally published on host loopback only
  (`127.0.0.1:${HOST_PORT}` in Compose). The application binds `0.0.0.0`
  inside its container; the host side never listens publicly.
- V1 has no application authentication or login. Do NOT publish port 8000
  publicly and do NOT remove the loopback bind.
- Remote access requires an operator-controlled protection layer: SSH
  tunnel (`ssh -L 8000:127.0.0.1:8000 <host>`), an authenticated reverse
  proxy, or a private network such as Tailscale. Providing that layer is
  deployment-environment work and is outside this slice.
- Secrets live only in process environment (`.env` → Compose → container
  env) and are resolved at execution boundaries; they must never be baked
  into images, committed to git, or pasted into logs/chat.

## 8. Logging

- All services log to container stdout (unbuffered Python logging); read
  them with `docker compose logs`. No application log files exist.
- Compose applies bounded json-file rotation to the three long-running
  services (`max-size: 10m`, `max-file: 3`, ~30 MB retained each) so a VPS
  disk cannot fill from logs. The one-shot `migrate` service is excluded
  deliberately (it exits; nothing accumulates). No external log shipper in
  V1; export `docker compose logs` output manually when diagnosing.
