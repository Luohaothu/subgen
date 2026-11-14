"""Descriptor paths for passing open source files to libavformat."""

from __future__ import annotations

import errno
import sys
from pathlib import Path

LINUX = sys.platform.startswith("linux")
DARWIN = sys.platform == "darwin"


def fd_directory() -> Path:
    """Return the directory that exposes open file descriptors as paths."""
    if LINUX:
        return Path("/proc/self/fd")
    if DARWIN:
        return Path("/dev/fd")
    raise OSError(errno.ENOSYS, "file descriptor paths are unavailable")


def fd_path(descriptor: int) -> str:
    """Return a path that names an open descriptor."""
    return str(fd_directory() / str(descriptor))
