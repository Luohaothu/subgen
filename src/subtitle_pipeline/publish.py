"""Publish a verified subtitle beside its video.

The caller skips videos whose target already exists. This module repeats the
check, then writes one `.part` file and installs it with ``os.replace``. It does
not lock the directory, fsync, or require a no-replace rename; the scheduler
owns mutual exclusion and the pipeline records progress per video.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class PublicationError(RuntimeError):
    pass


class ExistingTargetError(PublicationError):
    pass


class PublicationIntegrityError(PublicationError):
    pass


def _read_verified(
    path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    label: str,
) -> bytes:
    data = path.read_bytes()
    if len(data) != expected_bytes:
        raise PublicationIntegrityError(
            f"{label} byte count differs: {len(data)} != {expected_bytes}"
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise PublicationIntegrityError(f"{label} SHA-256 differs")
    return data


def publish(
    candidate: Path,
    target: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    """Install the candidate at the target, or raise if the target exists."""
    if type(expected_bytes) is not int or expected_bytes < 0:
        raise ValueError("expected_bytes must be a nonnegative integer")
    if (
        type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha256)
    ):
        raise ValueError("expected_sha256 must be lowercase SHA-256")

    source = Path(candidate)
    destination = Path(target)
    if os.path.lexists(destination):
        raise ExistingTargetError(str(destination))

    data = _read_verified(
        source,
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha256,
        label="candidate",
    )
    part = destination.with_name(destination.name + ".part")
    try:
        part.write_bytes(data)
        _read_verified(
            part,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            label="part",
        )
        if os.path.lexists(destination):
            raise ExistingTargetError(str(destination))
        os.replace(part, destination)
    except BaseException:
        try:
            part.unlink()
        except FileNotFoundError:
            pass
        raise
