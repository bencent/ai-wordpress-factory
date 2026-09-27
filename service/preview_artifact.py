"""Preview artifact delivery: filesystem resolution and integrity validation."""
from hashlib import sha256
from pathlib import Path
import struct
from domain.preview import PreviewAsset, PreviewAssetKind
from domain.submission import PreviewAssetUnavailable


def validate_png_metadata(data: bytes) -> tuple[int, int]:
    """Validate PNG signature and IHDR chunk, return (width, height).

    Raises:
        ValueError: if PNG signature or IHDR chunk is invalid
    """
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError("Invalid PNG signature")
    if len(data) < 24:
        raise ValueError("PNG too small for IHDR")
    length = struct.unpack('>I', data[8:12])[0]
    if length != 13:
        raise ValueError(f"IHDR chunk length {length} != 13")
    if data[12:16] != b'IHDR':
        raise ValueError("First chunk is not IHDR")
    width = struct.unpack('>I', data[16:20])[0]
    height = struct.unpack('>I', data[20:24])[0]
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    return width, height


def resolve_artifact_path(artifact_key: str, preview_base_dir: str) -> Path:
    """Resolve artifact_key to physical path beneath trusted preview root.

    Args:
        artifact_key: Persisted artifact key (e.g., "previews/abc/def.png")
        preview_base_dir: Configured preview base directory (trusted root)

    Returns:
        Resolved absolute Path

    Raises:
        PreviewAssetUnavailable: if path escapes trusted root, is a symlink, or prefix invalid
    """
    if not artifact_key.startswith("previews/"):
        raise PreviewAssetUnavailable("Invalid artifact key prefix")

    trusted_root = Path(preview_base_dir).resolve()
    relative_path = artifact_key[len("previews/"):]
    if not relative_path:
        raise PreviewAssetUnavailable("Invalid artifact key: empty relative path")

    candidate_unresolved = trusted_root / relative_path

    try:
        if candidate_unresolved.is_symlink():
            raise PreviewAssetUnavailable("Artifact is a symlink")
    except OSError:
        raise PreviewAssetUnavailable("Artifact path inspection failed")

    try:
        candidate = candidate_unresolved.resolve()
    except OSError:
        raise PreviewAssetUnavailable("Artifact path resolution failed")

    if not candidate.is_relative_to(trusted_root):
        raise PreviewAssetUnavailable("Artifact path escapes trusted root")

    return candidate


def read_and_validate_asset(asset: PreviewAsset, preview_base_dir: str) -> bytes:
    """Read and validate preview asset from filesystem.

    Performs all integrity checks:
    - File exists and is regular file (not symlink)
    - PNG signature valid
    - IHDR valid
    - Width matches persisted
    - Height matches persisted
    - byte_size matches persisted
    - SHA256 matches persisted

    Args:
        asset: Persisted PreviewAsset domain object
        preview_base_dir: Configured preview base directory (trusted root)

    Returns:
        Validated PNG bytes (exact bytes that passed all checks)

    Raises:
        PreviewAssetUnavailable: if any check fails
    """
    try:
        path = resolve_artifact_path(asset.artifact_key, preview_base_dir)
    except PreviewAssetUnavailable:
        raise
    except OSError:
        raise PreviewAssetUnavailable("Artifact path resolution failed")

    try:
        if not path.exists():
            raise PreviewAssetUnavailable("Artifact file not found")
        if not path.is_file():
            raise PreviewAssetUnavailable("Artifact is not a regular file")
    except OSError:
        raise PreviewAssetUnavailable("Artifact file inspection failed")

    try:
        data = path.read_bytes()
    except OSError:
        raise PreviewAssetUnavailable("Artifact read failed")

    if len(data) != asset.byte_size:
        raise PreviewAssetUnavailable("Byte size mismatch")

    actual_sha256 = sha256(data).hexdigest()
    if actual_sha256 != asset.sha256:
        raise PreviewAssetUnavailable("SHA256 mismatch")

    actual_width, actual_height = validate_png_metadata(data)
    if actual_width != asset.width:
        raise PreviewAssetUnavailable("Width mismatch")
    if actual_height != asset.height:
        raise PreviewAssetUnavailable("Height mismatch")

    return data


def get_asset_by_kind(stored_preview, kind: PreviewAssetKind) -> PreviewAsset:
    """Find asset by kind in stored preview.

    Args:
        stored_preview: StoredPreview domain object
        kind: PreviewAssetKind to find

    Returns:
        Matching PreviewAsset

    Raises:
        PreviewAssetUnavailable: if kind not found
    """
    for asset in stored_preview.assets:
        if asset.kind == kind:
            return asset
    raise PreviewAssetUnavailable("Asset kind not found in preview")


def deliver_preview_asset(stored_preview, kind: PreviewAssetKind, preview_base_dir: str) -> bytes:
    """Complete delivery flow: find asset, validate, return bytes.

    Args:
        stored_preview: StoredPreview domain object
        kind: PreviewAssetKind to deliver
        preview_base_dir: Configured preview base directory (trusted root)

    Returns:
        Validated PNG bytes

    Raises:
        PreviewAssetUnavailable: if any step fails
    """
    asset = get_asset_by_kind(stored_preview, kind)
    return read_and_validate_asset(asset, preview_base_dir)