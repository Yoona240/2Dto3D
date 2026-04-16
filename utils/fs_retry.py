"""SeaweedFS IO retry utilities.

SeaweedFS FUSE mounts produce transient IO errors (EIO, ESTALE, timeouts)
under concurrent load.  The helpers here wrap file operations with
exponential-backoff retry so that short-lived glitches are absorbed
without bubbling up to the pipeline.
"""

from __future__ import annotations

import errno
import logging
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient errno values commonly returned by FUSE / NFS mounts.
_TRANSIENT_ERRNOS = {
    errno.EIO,           # 5  — generic I/O error
    errno.EBUSY,         # 16 — device or resource busy
    errno.ESTALE,        # 116 (Linux) — stale file handle
    errno.ETIMEDOUT,     # 110 — connection timed out
    errno.ECONNRESET,    # 104 — connection reset
    errno.ECONNABORTED,  # 103 — connection aborted
}


def _is_transient(exc: OSError) -> bool:
    """Return True if *exc* looks like a transient FUSE / remote-FS error."""
    if isinstance(exc, FileNotFoundError):
        # Real "file does not exist" — not transient.
        # ESTALE sometimes surfaces as FileNotFoundError on some kernels;
        # in that case exc.errno is ESTALE and we should retry.
        return getattr(exc, "errno", None) in _TRANSIENT_ERRNOS
    if isinstance(exc, PermissionError):
        # Transient permission errors happen on FUSE remounts.
        return True
    # All other OSError subclasses: check errno.
    return getattr(exc, "errno", None) in _TRANSIENT_ERRNOS or not isinstance(
        exc, FileNotFoundError
    )


def retry_io(
    func: Callable[[], T],
    *,
    max_retries: int = 5,
    base_delay: float = 1.0,
    description: str = "",
) -> T:
    """Execute *func* with exponential-backoff retry on transient ``OSError``.

    Parameters
    ----------
    func:
        Zero-arg callable that performs the file operation.
    max_retries:
        Maximum number of attempts (including the first).
    base_delay:
        Initial delay in seconds; doubles on each retry.
    description:
        Human-readable label for log messages.
    """
    last_exc: OSError | None = None
    for attempt in range(max_retries):
        try:
            return func()
        except OSError as exc:
            if not _is_transient(exc):
                raise
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "[fs_retry] %s — transient error (attempt %d/%d): %s  "
                    "retrying in %.2fs",
                    description or "io_op",
                    attempt + 1,
                    max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
    # All retries exhausted — re-raise the last error.
    raise last_exc  # type: ignore[misc]


def retry_open_image(image_path: Path | str, mode: str = "RGB"):
    """Open an image with retry, eagerly loading pixels into memory.

    Eagerly calling ``Image.load()`` ensures the file handle is closed
    immediately, avoiding FUSE stale-handle errors on deferred reads.
    """
    from PIL import Image

    image_path = Path(image_path)

    def _open():
        img = Image.open(image_path)
        img.load()  # read pixels into memory now
        return img

    img = retry_io(_open, description=f"open_image {image_path}")
    if mode and img.mode != mode:
        img = img.convert(mode)
    return img
