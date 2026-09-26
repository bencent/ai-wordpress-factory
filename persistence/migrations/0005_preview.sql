-- Migration 0005: Preview persistence schema
-- Creates preview_records and preview_assets with full ownership enforcement
-- No ON DELETE CASCADE; immutable append-only design

-- preview_records: one per content_version, immutable after insert
CREATE TABLE preview_records (
    preview_id TEXT PRIMARY KEY NOT NULL,
    workspace_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    content_version_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL CHECK (created_at != ''),
    FOREIGN KEY (workspace_id, task_id) REFERENCES tasks (workspace_id, task_id),
    FOREIGN KEY (task_id, run_id) REFERENCES task_runs (task_id, run_id),
    FOREIGN KEY (task_id, content_version_id) REFERENCES content_versions (task_id, content_version_id)
) STRICT;

-- preview_assets: exactly 4 rows per preview (one per PreviewAssetKind)
CREATE TABLE preview_assets (
    preview_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'desktop_viewport',
        'desktop_full_page',
        'mobile_viewport',
        'mobile_full_page'
    )),
    artifact_key TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    media_type TEXT NOT NULL CHECK (media_type = 'image/png'),
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    PRIMARY KEY (preview_id, kind),
    FOREIGN KEY (preview_id) REFERENCES preview_records (preview_id)
) STRICT;

-- Indexes for common query patterns
CREATE INDEX preview_records_workspace_task ON preview_records (workspace_id, task_id, created_at);
CREATE INDEX preview_records_run ON preview_records (run_id, created_at);

-- Append-only: reject UPDATE on preview_records
CREATE TRIGGER preview_records_no_update
BEFORE UPDATE ON preview_records
BEGIN
    SELECT RAISE(ABORT, 'Preview records are immutable');
END;

-- Append-only: reject DELETE on preview_records
CREATE TRIGGER preview_records_no_delete
BEFORE DELETE ON preview_records
BEGIN
    SELECT RAISE(ABORT, 'Preview records are immutable');
END;

-- Append-only: reject UPDATE on preview_assets
CREATE TRIGGER preview_assets_no_update
BEFORE UPDATE ON preview_assets
BEGIN
    SELECT RAISE(ABORT, 'Preview assets are immutable');
END;

-- Append-only: reject DELETE on preview_assets
CREATE TRIGGER preview_assets_no_delete
BEFORE DELETE ON preview_assets
BEGIN
    SELECT RAISE(ABORT, 'Preview assets are immutable');
END;

-- Ownership chain enforcement at INSERT time
-- Closes the cross-run ownership gap:
-- ensures the selected content_version matches (task_id, run_id) of the preview record
CREATE TRIGGER preview_records_ownership_chain
BEFORE INSERT ON preview_records
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'Content version not in task/run')
    WHERE NOT EXISTS (
        SELECT 1 FROM content_versions
        WHERE content_version_id = NEW.content_version_id
          AND task_id = NEW.task_id
          AND run_id = NEW.run_id
    );
END;