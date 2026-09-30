# Phase 8.1 HTTP API

FastAPI 0.137.1 / Uvicorn 0.49.0. Install the declared requirements using the project's Python environment.
Initialize the existing database with the existing migration runner before starting the API; requests never migrate schemas.

Set `AIWF_DATABASE` to the existing SQLite database location and `AIWF_PROFILES_FILE` to a deployment-owned JSON array of allowed profiles.
Each entry supplies `workspace_id`, `site_id`, `brand_profile_id`, optional `client_profile_id`, and `provider_connection_id`.
An optional `snapshot` uses the existing credential-free SubmissionProfile contract. Do not put credentials in this file.
Missing or unmatched profiles fail closed. Workspace ownership is verified by the existing scoped submission service.
The client cannot choose a Workspace. The backend resolves the active default Workspace.

The API and Worker run as two independent processes. In Windows PowerShell,
start each process in its own terminal using the same `AIWF_DATABASE` and
`AIWF_PROFILES_FILE` environment settings.

Terminal 1 — API:

```powershell
python -m uvicorn api.app:get_app --factory --host 127.0.0.1 --port 8000
```

`--factory` is required. `api.app` exposes a lazy `get_app()` factory and its
module-level `app` name is `None` until that factory runs, so the `api.app:app`
form starts a process in which every request fails with
`'NoneType' object is not callable`. Pass the factory and `--factory`, and
`get_app()` builds the application on first use.

Terminal 2 — Worker:

```powershell
python -m worker
```

Terminal 3 — publication worker (a separate runtime with a separate composition;
it never constructs the content worker's dependencies):

```powershell
python -m worker --publication
```

For diagnosis or a single queued-task attempt, run:

```powershell
python -m worker --once
```

The polling interval is optional and is expressed in seconds:

```powershell
python -m worker --poll-seconds 1
```

Provider credentials are resolved through the existing `env:` credential
references. Never place credential values on the command line or in the
profiles file. systemd, Docker, Windows Service integration, and automatic
restart configuration are deferred to Phase 8.6. This slice does not document
public-network binding or production authentication. It has no login and no
public deployment configuration.

The Phase 8.1 routes below have since been joined by Human Review and
Publication routes added in 8.3; the table is the original 8.1 contract, kept as
written for that milestone.

## Publishing target provisioning

A workspace publishes only to a configured destination, and nothing in the
application creates one. Record it with the provisioning CLI, which reads the
same `AIWF_DATABASE` as the API and Worker and migrates it on open:

```powershell
python -m admin publishing-target add `
  --workspace-id <workspace_id> `
  --base-url https://example.test `
  --username <wordpress_username> `
  --credential-reference env:AIWF_WP_APP_PASSWORD
```

`--credential-reference` is the **pointer** that gets stored in the database
(`env:AIWF_WP_APP_PASSWORD`). The value it points at lives in the shell
environment of the API and publication worker:

```sh
export AIWF_WP_APP_PASSWORD='...'
```

**The Application Password value is never stored in the database, never passed
as a command-line argument, and never read by the provisioning command.** There
is deliberately no `--password` or `--application-password` flag: a value on the
command line lives in shell history and the process table. Only the reference
row is durable; the secret is resolved at publication time by the existing
`EnvironmentSecretResolver`, inside the worker, exactly as AI provider
credentials are.

Provisioning records configuration; it does not validate the credential.
Reachability and authentication are established by the first real publication.

A workspace may hold exactly one ACTIVE target per provider type — a partial
unique index enforces it. If one already exists the command fails and changes
nothing; it never replaces a destination. Rotation is a configuration change on
the existing target (a new `configuration_version`, with the previous
configuration preserved for publications that already snapshotted it), not a new
target.

## Routes

| Method | Path | Result |
|---|---|---|
| POST | /api/v1/tasks | Idempotency-Key required; 201 new, 200 permanent replay, 409 conflict |
| GET | /api/v1/tasks | tasks + next_cursor; default limit 50, maximum 100 |
| GET | /api/v1/tasks/{task_id} | Public task summary and current Run projection; 404 for missing/foreign IDs |
| GET | /api/v1/tasks/{task_id}/events | PUBLIC events after sequence; last_sequence polling marker |
| POST | /api/v1/tasks/{task_id}/retry | Empty body or {}; 200 queued new Run, 409 when unavailable |
| GET | /api/v1/system/status | API/database and observed Worker heartbeat state |

Request bodies are limited to 32 KiB, including streamed bodies. Unknown query parameters and client Workspace headers are rejected.
Task creation uses the existing POST/PAGE domain validator and forces human review. No synchronous AI or Publisher call occurs.
No wildcard CORS is enabled. OpenAPI/docs endpoints are disabled.

Cursor is a versioned base64url JSON envelope containing the last `(created_at, task_id)` pair.
Clients must treat it as opaque. It contains no authority or Workspace selector; SQL always independently scopes the query.
Unsupported cursor versions and malformed values return 400. Keys use stable descending timestamp/ID ordering, not offset.

Retry is an explicit transaction: expected status and current Run are checked, then a new attempt and event are inserted and the Task pointer changes.
Only FAILED/WORKER_LOST qualify; concurrent duplicate requests produce one success and a conflict.
The old request, Run, events and invocation audit remain unchanged. Provider selection is copied from the old Run; it is never silently switched.

Errors use `{ "error": { "code": "...", "message": "...", "request_id": "UUID" } }`.
Transport/domain validation is 400, unavailable resources 400, missing Task 404, conflicts 409, persistence failure 503, unexpected failures 500.
Raw exceptions and snapshots are never returned. Unexpected errors log only a fixed message and server-generated request ID.
Event summary/metadata are projected from allowlists; internal-only events are not returned.

Worker health observes current active Run heartbeat timestamps. Fresh (<60s) means ONLINE; stale means OFFLINE.
No active heartbeat means UNKNOWN, because the existing Worker has no idle-process heartbeat.
This is an observation, not a process-liveness guarantee. Worker offline/unknown returns HTTP 200; database unavailable also returns a safe status payload.

## Test environment difference

The local Starlette TestClient prefers the already-installed httpx2 package.
The project directly declares httpx only; httpx2 is not a Phase 8.1 project dependency.
Without httpx2, this Starlette version falls back to httpx. Clean deployment validation
and a complete transitive dependency lock are deferred to Phase 8.6.
The Uvicorn smoke test only loads the application through Config, using loopback/port 0
configuration without starting a server, binding a socket or resolving credentials.
