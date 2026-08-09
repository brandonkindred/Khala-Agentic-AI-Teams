"""Team asset (file system) domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers (status codes, bodies,
    filesystem side effects). Collaborators are read from ``api.main`` at call
    time so tests can ``monkeypatch.setattr(main, …)`` — in particular
    ``upload_team_asset`` reads ``_main._ASSET_UPLOAD_CHUNK_BYTES`` (not a
    module-local constant) so a test that patches it on ``main`` is honored.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse

from agent_team_studio.agentic_team_provisioning.models import AssetInfo
from shared.env_config import env_int

_DEFAULT_MAX_ASSET_BYTES = 10 * 1024 * 1024  # 10 MiB
_ASSET_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB, read granularity while enforcing the limit


def _max_asset_upload_bytes() -> int:
    """Configured per-asset upload ceiling (``AGENTIC_TEAM_MAX_ASSET_BYTES``).

    Postconditions: returns a positive int — the parsed env var when set and
    valid, else ``_DEFAULT_MAX_ASSET_BYTES`` (per ``shared.env_config.env_int``:
    garbage or unset falls back to the default, never raises).
    """
    return env_int("AGENTIC_TEAM_MAX_ASSET_BYTES", _DEFAULT_MAX_ASSET_BYTES, floor=1)


def _safe_asset_name(name: str) -> str:
    """Sanitize asset name to prevent path traversal."""
    sanitized = Path(name).name
    if not sanitized or sanitized in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid asset name")
    return sanitized


def list_team_assets(team_id: str):
    """List files in the team's asset directory.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with an ``AssetInfo`` per regular file directly in
        ``infra.assets_dir`` (subdirectories are not walked), sorted by name;
        empty list if the directory doesn't exist yet or has no files; ``404``
        if the team is not found.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    assets: List[AssetInfo] = []
    if infra.assets_dir.is_dir():
        for p in sorted(infra.assets_dir.iterdir()):
            if p.is_file():
                stat = p.stat()
                assets.append(
                    AssetInfo(
                        name=p.name,
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    )
                )
    return assets


def download_team_asset(team_id: str, name: str):
    """Download a specific asset file.

    Preconditions: none beyond ``team_id``/``name`` being valid path segments.
    Postconditions: ``200`` streaming the file's bytes with an RFC 5987-encoded
        ``Content-Disposition`` header (safe for names containing quotes or
        non-ASCII characters, which would otherwise malform a raw ``filename=``
        header); ``404`` if ``name`` sanitizes to an invalid asset name, the
        resolved path escapes ``assets_dir`` (e.g. a symlink inside the
        directory pointing elsewhere on the host), or no such file exists.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    safe_name = _safe_asset_name(name)
    assets_root = infra.assets_dir.resolve()
    path = (infra.assets_dir / safe_name).resolve()
    if not path.is_relative_to(assets_root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    encoded_name = quote(safe_name)
    return FileResponse(
        str(path),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


async def upload_team_asset(team_id: str, file: UploadFile):
    """Upload a file to the team's asset directory.

    Preconditions: none beyond a valid multipart upload.
    Postconditions: ``200`` with the stored asset's metadata; ``400`` if the
        filename sanitizes to nothing usable; ``409`` if an asset with the same
        sanitized name already exists (uploads never silently overwrite one
        another); ``413`` if the upload exceeds
        ``AGENTIC_TEAM_MAX_ASSET_BYTES`` (default 10 MiB). Each chunk is
        written to disk as it's read rather than buffered in memory, so
        neither the in-flight memory footprint nor the on-disk footprint
        ever exceeds the configured limit; on any failure (the ``413``, or
        otherwise) the partial file is removed, so a failed upload never
        leaves a truncated asset behind. The filesystem open/write/close/stat
        calls all run off the event loop (``asyncio.to_thread``) so a large
        upload can't stall concurrent requests.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    safe_name = _safe_asset_name(file.filename or "upload")
    dest = infra.assets_dir / safe_name
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"Asset already exists: {safe_name}")

    max_bytes = _max_asset_upload_bytes()
    total = 0
    try:
        handle = await asyncio.to_thread(dest.open, "wb")
        try:
            while True:
                chunk = await file.read(_main._ASSET_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail="Asset exceeds maximum upload size")
                await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)
    except BaseException:
        await asyncio.to_thread(dest.unlink, missing_ok=True)
        raise

    stat = await asyncio.to_thread(dest.stat)
    return AssetInfo(
        name=safe_name,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )
