# Deployment (Phase 8.6-1)

Single-host Docker Compose packaging. Full operator runbook belongs to 8.6-2;
this stub covers only the files Compose needs from this directory.

## First boot (on the deployment host, not in this repo)

1. Copy `.env.example` to `.env` and fill in real secrets there.
   `.env` is gitignored and is loaded by Compose, not by the app.
2. Copy `deployment/profiles.example.json` to `deployment/profiles.json`
   and set `site_id` / `brand_profile_id` to match your submissions.
   `workspace_id` and `provider_connection_id` already match the
   migration-seeded default workspace and OPENAI connection; change them
   only if you provisioned different ones. No credentials go in this file.
3. `docker compose up -d --build`
4. Confirm: `docker compose ps`, API `GET http://127.0.0.1:8000/api/v1/system/status`.

The `migrate` service initializes the shared SQLite volume on every start
and exits 0 when the schema is current; api/worker/publication-worker wait
for it. The API is published on host loopback only — remote access via SSH
tunnel or a protected reverse proxy.
