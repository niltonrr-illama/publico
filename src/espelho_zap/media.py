"""Private, content-addressed media spool for reliable retries.

Host caches are never used as the durable retry source.  Capture copies each
attachment into a product-owned directory, hashing the bytes during the copy.
Only files explicitly marked ``managed_temp`` may later be removed.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
import shutil
import stat
import tempfile
import time
from typing import Iterable

from .models import InboundEvent, MediaAttachment


class MediaSpoolError(RuntimeError):
    def __init__(self, code: str = "media_spool_error"):
        self.code = code
        super().__init__(code)


def _contained(candidate: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        resolved_root = root.resolve(strict=True)
        if candidate == resolved_root or candidate.is_relative_to(resolved_root):
            return True
    return False


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    if len(suffix) > 16 or any(not (char.isalnum() or char == ".") for char in suffix):
        return ".bin"
    return suffix or ".bin"


def _spool_usage_bytes(root: Path) -> int:
    total = 0
    try:
        candidates = tuple(root.iterdir())
    except OSError:
        raise MediaSpoolError("media_spool_io_error") from None
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                raise MediaSpoolError("media_spool_symlink_rejected")
            if candidate.is_file():
                total += int(candidate.stat().st_size)
        except MediaSpoolError:
            raise
        except OSError:
            raise MediaSpoolError("media_spool_io_error") from None
    return total


def _stage_one(
    event_id: str,
    index: int,
    media: MediaAttachment,
    *,
    spool_root: Path,
    source_roots: tuple[Path, ...],
    minimum_free_bytes: int,
    maximum_spool_bytes: int,
) -> tuple[MediaAttachment, bool]:
    source = Path(media.path)
    try:
        if source.is_symlink():
            raise MediaSpoolError("media_symlink_rejected")
        resolved = source.resolve(strict=True)
        roots = source_roots + (spool_root,)
        if not _contained(resolved, roots):
            raise MediaSpoolError("media_path_outside_root")
        before = resolved.stat()
        if not stat.S_ISREG(before.st_mode):
            raise MediaSpoolError("media_not_file")
        if media.size_bytes and before.st_size != media.size_bytes:
            raise MediaSpoolError("media_size_mismatch")
        free = shutil.disk_usage(spool_root).free
        if free - before.st_size < int(minimum_free_bytes):
            raise MediaSpoolError("media_spool_disk_floor")

        stable_key = hashlib.sha256(
            f"{event_id}\x1f{media.media_id}\x1f{index}".encode("utf-8")
        ).hexdigest()
        target = spool_root / f"{stable_key}{_safe_suffix(resolved)}"
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise MediaSpoolError("media_spool_conflict")
            digest, size = _digest_file(target)
            source_digest, source_size = _digest_file(resolved)
            after = resolved.stat()
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ):
                raise MediaSpoolError("media_source_changed")
            if digest != source_digest or size != source_size:
                raise MediaSpoolError("media_spool_conflict")
            if media.sha256 and digest != media.sha256:
                raise MediaSpoolError("media_hash_mismatch")
            if media.size_bytes and size != media.size_bytes:
                raise MediaSpoolError("media_size_mismatch")
            return replace(
                media,
                path=str(target),
                sha256=digest,
                size_bytes=size,
                managed_temp=True,
            ), False

        if _spool_usage_bytes(spool_root) + int(before.st_size) > maximum_spool_bytes:
            raise MediaSpoolError("media_spool_hard_cap")

        descriptor, temporary_name = tempfile.mkstemp(prefix=".capture-", dir=spool_root)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(resolved, flags)
            try:
                opened = os.fstat(source_fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise MediaSpoolError("media_not_file")
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise MediaSpoolError("media_source_changed")
                with os.fdopen(source_fd, "rb", closefd=False) as reader, os.fdopen(
                    descriptor, "wb", closefd=False
                ) as writer:
                    while block := reader.read(1024 * 1024):
                        writer.write(block)
                        digest.update(block)
                        size += len(block)
                    writer.flush()
                    os.fsync(writer.fileno())
            finally:
                os.close(source_fd)
            after = resolved.stat()
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ):
                raise MediaSpoolError("media_source_changed")
            computed = digest.hexdigest()
            if media.sha256 and computed != media.sha256:
                raise MediaSpoolError("media_hash_mismatch")
            if media.size_bytes and size != media.size_bytes:
                raise MediaSpoolError("media_size_mismatch")
            published = True
            try:
                os.link(temporary, target)
            except FileExistsError:
                published = False
                existing_digest, existing_size = _digest_file(target)
                if existing_digest != computed or existing_size != size:
                    raise MediaSpoolError("media_spool_conflict") from None
            if os.name == "posix":
                target.chmod(0o600)
            _fsync_directory(spool_root)
            return replace(
                media,
                path=str(target),
                sha256=computed,
                size_bytes=size,
                managed_temp=True,
            ), published
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
    except MediaSpoolError:
        raise
    except OSError:
        raise MediaSpoolError("media_spool_io_error") from None


def stage_event_media(
    event: InboundEvent,
    *,
    spool_root: str | Path | None,
    source_roots: Iterable[str | Path],
    minimum_free_bytes: int,
    maximum_spool_bytes: int = 1_073_741_824,
) -> tuple[InboundEvent, tuple[Path, ...]]:
    """Return an event whose media paths are durable product-owned files."""

    if not event.media:
        return event, ()
    if spool_root is None:
        raise MediaSpoolError("media_spool_required")
    root = Path(spool_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise MediaSpoolError("media_spool_symlink_rejected")
    root = root.resolve(strict=True)
    if os.name == "posix":
        root.chmod(0o700)
    roots = tuple(Path(item).resolve(strict=True) for item in source_roots)
    if not roots:
        raise MediaSpoolError("source_media_roots_required")
    if (
        isinstance(maximum_spool_bytes, bool)
        or not isinstance(maximum_spool_bytes, int)
        or maximum_spool_bytes <= 0
    ):
        raise MediaSpoolError("media_spool_limit_invalid")
    staged: list[MediaAttachment] = []
    created: list[Path] = []
    try:
        for index, media in enumerate(event.media):
            item, was_created = _stage_one(
                event.event_id,
                index,
                media,
                spool_root=root,
                source_roots=roots,
                minimum_free_bytes=minimum_free_bytes,
                maximum_spool_bytes=maximum_spool_bytes,
            )
            staged.append(item)
            if was_created:
                created.append(Path(item.path))
        return replace(event, media=tuple(staged)), tuple(created)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def remove_orphaned_spool_files(
    spool_root: str | Path | None,
    referenced_paths: Iterable[str],
    *,
    grace_seconds: int = 3600,
    limit: int = 100,
    now: float | int | None = None,
) -> int:
    """Remove bounded old regular files absent from the ledger inventory."""

    if spool_root is None or limit <= 0:
        return 0
    root = Path(spool_root)
    try:
        if root.is_symlink():
            return 0
        root = root.resolve(strict=True)
    except OSError:
        return 0
    referenced = {str(Path(item).resolve()) for item in referenced_paths}
    cutoff = int(time.time() if now is None else now) - max(0, int(grace_seconds))
    removed = 0
    try:
        candidates = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return 0
    for candidate in candidates:
        if removed >= limit:
            break
        try:
            if candidate.name.startswith(".capture-"):
                pass
            elif candidate.suffix == "" or len(candidate.stem) != 64:
                continue
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if str(candidate.resolve()) in referenced or int(candidate.stat().st_mtime) > cutoff:
                continue
            candidate.unlink()
            removed += 1
        except OSError:
            continue
    return removed
