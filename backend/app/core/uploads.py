"""Safe upload handling shared by CV upload and request attachments."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import UploadFile

from app.config import settings
from app.parsing.safety import check_file, sanitize_filename


async def save_upload(upload: UploadFile, dest_dir: Path) -> tuple[Path, str] | str:
    """Write an upload to dest_dir after the safety checks.

    Returns (final_path, sha256) or a plain-English error message. The file is first written to a
    dot-named scratch file (ignored by the folder poller), checked, then renamed.
    """
    limit = settings.max_upload_mb * 1024 * 1024
    h = hashlib.sha256()
    scratch = dest_dir / f".upload-{os.urandom(6).hex()}"
    size = 0
    try:
        with scratch.open("wb") as out:
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    return f"That file is larger than {settings.max_upload_mb} MB."
                h.update(chunk)
                out.write(chunk)
        check = check_file(scratch)
        if not check.ok:
            return check.reason or "That file could not be accepted."
        sha = h.hexdigest()
        final = dest_dir / f"{sha[:12]}_{sanitize_filename(upload.filename or 'file')}"
        scratch.replace(final)
        return final, sha
    finally:
        scratch.unlink(missing_ok=True)
