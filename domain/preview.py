"""Preview domain contracts."""
from dataclasses import dataclass
from enum import Enum
import re
from uuid import UUID


class PreviewAssetKind(str, Enum):
    DESKTOP_VIEWPORT = "desktop_viewport"
    DESKTOP_FULL_PAGE = "desktop_full_page"
    MOBILE_VIEWPORT = "mobile_viewport"
    MOBILE_FULL_PAGE = "mobile_full_page"


class PreviewAssetMediaType(str, Enum):
    PNG = "image/png"


_ALL_KINDS = frozenset(PreviewAssetKind)


def _validate_uuid(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be string")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty string without whitespace")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError(f"{field} must be valid UUID")
    if str(parsed) != value:
        raise ValueError(f"{field} must be canonical UUID (lowercase, hyphenated)")
    return value


def _validate_non_empty_str(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be string")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty string without whitespace")
    return value


def _validate_timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("created_at must be string")
    if not value or value != value.strip():
        raise ValueError("created_at must be non-empty string without whitespace")
    if "T" not in value and " " not in value:
        raise ValueError("created_at must include time component")
    try:
        from datetime import datetime
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("created_at must be valid ISO timestamp")
    if dt.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    return value


def _validate_sha256(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("sha256 must be 64-character hex string")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("sha256 must be lowercase hex")
    return value


def _validate_positive_int(value: int, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be positive int")
    return value


def _validate_artifact_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("artifact_key must be string")
    if not value or value != value.strip():
        raise ValueError("artifact_key must be non-empty string without whitespace")
    if not value.startswith("previews/"):
        raise ValueError("artifact_key must start with 'previews/'")
    if "\\" in value:
        raise ValueError("artifact_key must not contain backslash")
    if ":" in value:
        raise ValueError("artifact_key must not contain colon")
    if value.startswith("/"):
        raise ValueError("artifact_key must not be absolute path")
    if re.search(r"^[A-Za-z]:", value):
        raise ValueError("artifact_key must not contain Windows drive")
    if value.startswith("//") or value.startswith("\\\\"):
        raise ValueError("artifact_key must not be UNC path")
    if "\x00" in value:
        raise ValueError("artifact_key must not contain NUL")
    if "//" in value or value.startswith("/") or value.endswith("/"):
        raise ValueError("artifact_key must not have empty segment")
    parts = value.split("/")
    for part in parts:
        if part == "." or part == "..":
            raise ValueError("artifact_key must not contain '.' or '..'")
    if "?" in value or "#" in value:
        raise ValueError("artifact_key must not contain query or fragment")
    if value.lower().startswith("http:") or value.lower().startswith("https:") or value.lower().startswith("data:"):
        raise ValueError("artifact_key must not be URL or data URL")
    if not value.endswith(".png"):
        raise ValueError("artifact_key extension must be exactly '.png'")
    return value


@dataclass(frozen=True, kw_only=True)
class PreviewRecord:
    preview_id: str
    workspace_id: str
    task_id: str
    run_id: str
    content_version_id: str
    created_at: str

    def __post_init__(self):
        _validate_uuid(self.preview_id, "preview_id")
        _validate_non_empty_str(self.workspace_id, "workspace_id")
        _validate_non_empty_str(self.task_id, "task_id")
        _validate_non_empty_str(self.run_id, "run_id")
        _validate_non_empty_str(self.content_version_id, "content_version_id")
        _validate_timestamp(self.created_at)


@dataclass(frozen=True, kw_only=True)
class PreviewAsset:
    preview_id: str
    kind: PreviewAssetKind
    artifact_key: str
    sha256: str
    media_type: PreviewAssetMediaType
    width: int
    height: int
    byte_size: int

    def __post_init__(self):
        _validate_uuid(self.preview_id, "preview_id")
        if not isinstance(self.kind, PreviewAssetKind):
            raise ValueError("kind must be PreviewAssetKind")
        _validate_artifact_key(self.artifact_key)
        _validate_sha256(self.sha256)
        if not isinstance(self.media_type, PreviewAssetMediaType):
            raise ValueError("media_type must be PreviewAssetMediaType")
        _validate_positive_int(self.width, "width")
        _validate_positive_int(self.height, "height")
        _validate_positive_int(self.byte_size, "byte_size")


@dataclass(frozen=True, kw_only=True)
class StoredPreview:
    record: PreviewRecord
    assets: tuple[PreviewAsset, ...]

    def __post_init__(self):
        if not isinstance(self.record, PreviewRecord):
            raise ValueError("record must be PreviewRecord")
        if not isinstance(self.assets, tuple):
            raise ValueError("assets must be tuple")
        if len(self.assets) != 4:
            raise ValueError("StoredPreview requires exactly 4 assets")
        for a in self.assets:
            if not isinstance(a, PreviewAsset):
                raise ValueError("each asset must be PreviewAsset")
        kinds = {a.kind for a in self.assets}
        if kinds != _ALL_KINDS:
            raise ValueError("assets must contain exactly one of each PreviewAssetKind")
        for a in self.assets:
            if a.preview_id != self.record.preview_id:
                raise ValueError("asset preview_id must match record preview_id")
        object.__setattr__(self, "assets", tuple(
            sorted(self.assets, key=lambda a: a.kind.value)
        ))