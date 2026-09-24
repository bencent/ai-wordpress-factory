"""Pure domain tests for preview contracts."""
import pytest
from domain.preview import (
    PreviewAssetKind,
    PreviewAssetMediaType,
    PreviewRecord,
    PreviewAsset,
    StoredPreview,
)


class TestPreviewAssetKind:
    def test_exact_values(self):
        assert PreviewAssetKind.DESKTOP_VIEWPORT.value == "desktop_viewport"
        assert PreviewAssetKind.DESKTOP_FULL_PAGE.value == "desktop_full_page"
        assert PreviewAssetKind.MOBILE_VIEWPORT.value == "mobile_viewport"
        assert PreviewAssetKind.MOBILE_FULL_PAGE.value == "mobile_full_page"

    def test_no_extra_members(self):
        members = set(PreviewAssetKind.__members__.values())
        assert len(members) == 4


class TestPreviewAssetMediaType:
    def test_exact_value(self):
        assert PreviewAssetMediaType.PNG.value == "image/png"


class TestPreviewRecordValidConstruction:
    def test_valid_record(self):
        rec = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws-123",
            task_id="task-456",
            run_id="run-789",
            content_version_id="cv-001",
            created_at="2026-09-24T12:00:00Z",
        )
        assert rec.preview_id == "0192f0c1-2345-7abc-8def-0123456789ab"
        assert rec.workspace_id == "ws-123"

    def test_uuid_canonical_formats(self):
        for uuid_str in [
            "0192f0c1-2345-7abc-8def-0123456789ab",
            "aabbccdd-eeff-0011-2233-445566778899",
            "00000000-0000-0000-0000-000000000000",
        ]:
            rec = PreviewRecord(
                preview_id=uuid_str,
                workspace_id="ws",
                task_id="task",
                run_id="run",
                content_version_id="cv",
                created_at="2026-09-24T12:00:00Z",
            )
            assert rec.preview_id == uuid_str


class TestPreviewRecordInvalidUUID:
    @pytest.mark.parametrize("bad_uuid,expected_msg", [
        ("not-a-uuid", "preview_id must be valid UUID"),
        ("0192f0c1-2345-7abc-8def", "preview_id must be valid UUID"),
        ("0192f0c1-2345-7abc-8def-0123456789ab-extra", "preview_id must be valid UUID"),
        ("GGGGGGGG-2345-7abc-8def-0123456789ab", "preview_id must be valid UUID"),
        ("", "preview_id must be non-empty string without whitespace"),
        ("  0192f0c1-2345-7abc-8def-0123456789ab  ", "preview_id must be non-empty string without whitespace"),
        ("0192F0C1-2345-7ABC-8DEF-0123456789AB", "preview_id must be canonical UUID"),
        ("{0192f0c1-2345-7abc-8def-0123456789ab}", "preview_id must be canonical UUID"),
        ("urn:uuid:0192f0c1-2345-7abc-8def-0123456789ab", "preview_id must be canonical UUID"),
        ("0192f0c123457abc8def0123456789ab", "preview_id must be canonical UUID"),
    ])
    def test_invalid_preview_id_raises(self, bad_uuid, expected_msg):
        with pytest.raises(ValueError, match=expected_msg):
            PreviewRecord(
                preview_id=bad_uuid,
                workspace_id="ws",
                task_id="task",
                run_id="run",
                content_version_id="cv",
                created_at="2026-09-24T12:00:00Z",
            )


class TestPreviewRecordEmptyIDs:
    @pytest.mark.parametrize("field", ["workspace_id", "task_id", "run_id", "content_version_id"])
    def test_empty_id_raises(self, field):
        kwargs = {
            "preview_id": "0192f0c1-2345-7abc-8def-0123456789ab",
            "workspace_id": "ws",
            "task_id": "task",
            "run_id": "run",
            "content_version_id": "cv",
            "created_at": "2026-09-24T12:00:00Z",
        }
        kwargs[field] = ""
        with pytest.raises(ValueError, match=f"{field} must be non-empty string without whitespace"):
            PreviewRecord(**kwargs)

    @pytest.mark.parametrize("field", ["workspace_id", "task_id", "run_id", "content_version_id"])
    def test_whitespace_only_id_raises(self, field):
        kwargs = {
            "preview_id": "0192f0c1-2345-7abc-8def-0123456789ab",
            "workspace_id": "ws",
            "task_id": "task",
            "run_id": "run",
            "content_version_id": "cv",
            "created_at": "2026-09-24T12:00:00Z",
        }
        kwargs[field] = "   "
        with pytest.raises(ValueError, match=f"{field} must be non-empty string without whitespace"):
            PreviewRecord(**kwargs)

    @pytest.mark.parametrize("field", ["workspace_id", "task_id", "run_id", "content_version_id"])
    def test_leading_trailing_whitespace_id_raises(self, field):
        kwargs = {
            "preview_id": "0192f0c1-2345-7abc-8def-0123456789ab",
            "workspace_id": "ws",
            "task_id": "task",
            "run_id": "run",
            "content_version_id": "cv",
            "created_at": "2026-09-24T12:00:00Z",
        }
        kwargs[field] = "  ws  "
        with pytest.raises(ValueError, match=f"{field} must be non-empty string without whitespace"):
            PreviewRecord(**kwargs)


class TestPreviewRecordInvalidTimestamp:
    @pytest.mark.parametrize("bad_ts,expected_msg", [
        ("not-a-timestamp", "created_at must include time component"),
        ("2026-13-45T99:99:99Z", "created_at must be valid ISO timestamp"),
        ("", "created_at must be non-empty string without whitespace"),
        ("2026-09-24", "created_at must include time component"),
        ("2026-09-24T12:00:00", "created_at must be timezone-aware"),
        ("2026-09-24T12:00:00-05:00", None),
        ("2026-09-24T00:00:00Z", None),
        ("2026-09-24T00:00:00+00:00", None),
    ])
    def test_invalid_created_at_raises(self, bad_ts, expected_msg):
        if expected_msg is None:
            rec = PreviewRecord(
                preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
                workspace_id="ws",
                task_id="task",
                run_id="run",
                content_version_id="cv",
                created_at=bad_ts,
            )
            assert rec.created_at == bad_ts
        else:
            with pytest.raises(ValueError, match=expected_msg):
                PreviewRecord(
                    preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
                    workspace_id="ws",
                    task_id="task",
                    run_id="run",
                    content_version_id="cv",
                    created_at=bad_ts,
                )


class TestPreviewAssetValidConstruction:
    def test_valid_asset(self):
        asset = PreviewAsset(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            kind=PreviewAssetKind.DESKTOP_VIEWPORT,
            artifact_key="previews/task-123/attempt-1/abc/desktop-viewport.png",
            sha256="a" * 64,
            media_type=PreviewAssetMediaType.PNG,
            width=1440,
            height=900,
            byte_size=123456,
        )
        assert asset.kind == PreviewAssetKind.DESKTOP_VIEWPORT
        assert asset.width == 1440


class TestPreviewAssetInvalidKind:
    def test_raw_string_raises(self):
        with pytest.raises(ValueError, match="kind must be PreviewAssetKind"):
            PreviewAsset(
                preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
                kind="desktop_viewport",
                artifact_key="previews/x.png",
                sha256="a" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            )

    def test_invalid_enum_value_raises(self):
        with pytest.raises(ValueError):
            PreviewAsset(
                preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
                kind="invalid_kind",
                artifact_key="previews/x.png",
                sha256="a" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            )


class TestPreviewAssetInvalidMediaType:
    def test_raw_string_raises(self):
        with pytest.raises(ValueError, match="media_type must be PreviewAssetMediaType"):
            PreviewAsset(
                preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
                kind=PreviewAssetKind.DESKTOP_VIEWPORT,
                artifact_key="previews/x.png",
                sha256="a" * 64,
                media_type="image/png",
                width=100,
                height=100,
                byte_size=100,
            )


class TestPreviewAssetInvalidSHA256:
    @pytest.mark.parametrize("bad_hash", [
        "A" * 64,
        "g" * 64,
        "a" * 63,
        "a" * 65,
        "",
        " " * 64,
    ])
    def test_invalid_sha256_raises(self, bad_hash):
        with pytest.raises(ValueError, match="sha256"):
            PreviewAsset(
                preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
                kind=PreviewAssetKind.DESKTOP_VIEWPORT,
                artifact_key="previews/x.png",
                sha256=bad_hash,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            )


class TestPreviewAssetInvalidNumeric:
    @pytest.mark.parametrize("field,value", [
        ("width", True),
        ("width", False),
        ("width", 0),
        ("width", -1),
        ("width", 1.5),
        ("width", "100"),
        ("height", True),
        ("height", 0),
        ("height", -5),
        ("byte_size", True),
        ("byte_size", 0),
        ("byte_size", -10),
    ])
    def test_invalid_numeric_raises(self, field, value):
        kwargs = {
            "preview_id": "0192f0c1-2345-7abc-8def-0123456789ab",
            "kind": PreviewAssetKind.DESKTOP_VIEWPORT,
            "artifact_key": "previews/x.png",
            "sha256": "a" * 64,
            "media_type": PreviewAssetMediaType.PNG,
            "width": 100,
            "height": 100,
            "byte_size": 100,
        }
        kwargs[field] = value
        with pytest.raises(ValueError, match=f"{field} must be positive int"):
            PreviewAsset(**kwargs)


class TestPreviewAssetInvalidArtifactKey:
    @pytest.mark.parametrize("bad_key,expected_msg", [
        ("/absolute/path.png", "must start with"),
        ("C:\\path\\to\\file.png", "must start with"),
        ("D:/windows/path.png", "must start with"),
        ("//server/share/file.png", "must start with"),
        ("\\\\server\\share\\file.png", "must start with"),
        ("previews/path\x00file.png", "must not contain NUL"),
        ("previews//double.png", "must not have empty segment"),
        ("previews/./file.png", "must not contain"),
        ("previews/../file.png", "must not contain"),
        ("previews/task/file.png?query=1", "must not contain query or fragment"),
        ("previews/task/file.png#frag", "must not contain query or fragment"),
        ("http://example.com/image.png", "must start with"),
        ("https://example.com/image.png", "must start with"),
        ("data:image/png;base64,abc", "must start with"),
        ("previews/task/file.jpg", "extension must be exactly"),
        ("previews/task/file.jpeg", "extension must be exactly"),
        ("previews/task/file.webp", "extension must be exactly"),
        ("notpreviews/task/file.png", "must start with"),
        ("previews/task/file.PNG", "extension must be exactly"),
        ("previews/a:b/file.png", "must not contain colon"),
        ("previews/C:/windows/path.png", "must not contain colon"),
        ("previews/task:sub/file.png", "must not contain colon"),
    ])
    def test_invalid_artifact_key_raises(self, bad_key, expected_msg):
        with pytest.raises(ValueError, match=expected_msg):
            PreviewAsset(
                preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
                kind=PreviewAssetKind.DESKTOP_VIEWPORT,
                artifact_key=bad_key,
                sha256="a" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            )


class TestStoredPreviewValidConstruction:
    def test_valid_stored_preview(self):
        record = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws",
            task_id="task",
            run_id="run",
            content_version_id="cv",
            created_at="2026-09-24T12:00:00Z",
        )
        assets = tuple(
            PreviewAsset(
                preview_id=record.preview_id,
                kind=k,
                artifact_key=f"previews/task/attempt-1/abc/{k.value}.png",
                sha256="a" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            )
            for k in PreviewAssetKind
        )
        sp = StoredPreview(record=record, assets=assets)
        assert len(sp.assets) == 4
        assert sp.assets[0].kind == PreviewAssetKind.DESKTOP_FULL_PAGE
        assert sp.assets[1].kind == PreviewAssetKind.DESKTOP_VIEWPORT
        assert sp.assets[2].kind == PreviewAssetKind.MOBILE_FULL_PAGE
        assert sp.assets[3].kind == PreviewAssetKind.MOBILE_VIEWPORT


class TestStoredPreviewInvalidAssetCount:
    @pytest.mark.parametrize("count", [0, 1, 2, 3, 5, 6])
    def test_invalid_count_raises(self, count):
        record = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws",
            task_id="task",
            run_id="run",
            content_version_id="cv",
            created_at="2026-09-24T12:00:00Z",
        )
        assets = tuple(
            PreviewAsset(
                preview_id=record.preview_id,
                kind=PreviewAssetKind.DESKTOP_VIEWPORT,
                artifact_key=f"previews/x{i}.png",
                sha256="a" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            )
            for i in range(count)
        )
        with pytest.raises(ValueError, match="exactly 4 assets"):
            StoredPreview(record=record, assets=assets)


class TestStoredPreviewDuplicateKinds:
    def test_duplicate_kind_raises(self):
        record = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws",
            task_id="task",
            run_id="run",
            content_version_id="cv",
            created_at="2026-09-24T12:00:00Z",
        )
        assets = (
            PreviewAsset(
                preview_id=record.preview_id,
                kind=PreviewAssetKind.DESKTOP_VIEWPORT,
                artifact_key="previews/x1.png",
                sha256="a" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            ),
            PreviewAsset(
                preview_id=record.preview_id,
                kind=PreviewAssetKind.DESKTOP_VIEWPORT,
                artifact_key="previews/x2.png",
                sha256="b" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            ),
            PreviewAsset(
                preview_id=record.preview_id,
                kind=PreviewAssetKind.MOBILE_VIEWPORT,
                artifact_key="previews/x3.png",
                sha256="c" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            ),
            PreviewAsset(
                preview_id=record.preview_id,
                kind=PreviewAssetKind.MOBILE_FULL_PAGE,
                artifact_key="previews/x4.png",
                sha256="d" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            ),
        )
        with pytest.raises(ValueError, match="must contain exactly one of each"):
            StoredPreview(record=record, assets=assets)


class TestStoredPreviewMissingKind:
    def test_missing_kind_raises(self):
        record = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws",
            task_id="task",
            run_id="run",
            content_version_id="cv",
            created_at="2026-09-24T12:00:00Z",
        )
        assets = tuple(
            PreviewAsset(
                preview_id=record.preview_id,
                kind=k,
                artifact_key=f"previews/x.png",
                sha256="a" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            )
            for k in [
                PreviewAssetKind.DESKTOP_VIEWPORT,
                PreviewAssetKind.DESKTOP_FULL_PAGE,
                PreviewAssetKind.MOBILE_VIEWPORT,
                PreviewAssetKind.MOBILE_VIEWPORT,
            ]
        )
        with pytest.raises(ValueError, match="must contain exactly one of each"):
            StoredPreview(record=record, assets=assets)


class TestStoredPreviewMismatchedPreviewID:
    def test_mismatched_preview_id_raises(self):
        record = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws",
            task_id="task",
            run_id="run",
            content_version_id="cv",
            created_at="2026-09-24T12:00:00Z",
        )
        assets = tuple(
            PreviewAsset(
                preview_id="aabbccdd-eeff-0011-2233-445566778899",
                kind=k,
                artifact_key=f"previews/x.png",
                sha256="a" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            )
            for k in PreviewAssetKind
        )
        with pytest.raises(ValueError, match="asset preview_id must match record preview_id"):
            StoredPreview(record=record, assets=assets)


class TestStoredPreviewDeterministicOrdering:
    def test_assets_sorted_by_kind_value(self):
        record = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws",
            task_id="task",
            run_id="run",
            content_version_id="cv",
            created_at="2026-09-24T12:00:00Z",
        )
        kinds_random = [
            PreviewAssetKind.MOBILE_VIEWPORT,
            PreviewAssetKind.DESKTOP_VIEWPORT,
            PreviewAssetKind.MOBILE_FULL_PAGE,
            PreviewAssetKind.DESKTOP_FULL_PAGE,
        ]
        assets = tuple(
            PreviewAsset(
                preview_id=record.preview_id,
                kind=k,
                artifact_key=f"previews/{k.value}.png",
                sha256="a" * 64,
                media_type=PreviewAssetMediaType.PNG,
                width=100,
                height=100,
                byte_size=100,
            )
            for k in kinds_random
        )
        sp = StoredPreview(record=record, assets=assets)
        assert [a.kind for a in sp.assets] == [
            PreviewAssetKind.DESKTOP_FULL_PAGE,
            PreviewAssetKind.DESKTOP_VIEWPORT,
            PreviewAssetKind.MOBILE_FULL_PAGE,
            PreviewAssetKind.MOBILE_VIEWPORT,
        ]


class TestStoredPreviewInvalidAssetType:
    def test_non_preview_asset_raises_value_error_not_attribute_error(self):
        record = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws",
            task_id="task",
            run_id="run",
            content_version_id="cv",
            created_at="2026-09-24T12:00:00Z",
        )
        class FakeAsset:
            kind = PreviewAssetKind.DESKTOP_VIEWPORT
            preview_id = record.preview_id
        assets = (FakeAsset(),) * 4
        with pytest.raises(ValueError, match="each asset must be PreviewAsset"):
            StoredPreview(record=record, assets=assets)

    def test_none_in_assets_raises_value_error(self):
        record = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws",
            task_id="task",
            run_id="run",
            content_version_id="cv",
            created_at="2026-09-24T12:00:00Z",
        )
        assets = (None,) * 4
        with pytest.raises(ValueError, match="each asset must be PreviewAsset"):
            StoredPreview(record=record, assets=assets)

    def test_string_in_assets_raises_value_error(self):
        record = PreviewRecord(
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            workspace_id="ws",
            task_id="task",
            run_id="run",
            content_version_id="cv",
            created_at="2026-09-24T12:00:00Z",
        )
        assets = ("not an asset",) * 4
        with pytest.raises(ValueError, match="each asset must be PreviewAsset"):
            StoredPreview(record=record, assets=assets)