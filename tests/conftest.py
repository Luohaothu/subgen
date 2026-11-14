"""Test configuration.

The pipeline validates that every path component is a real directory and not a
symlink. On macOS the default temporary directory sits behind `/var`, which is a
symlink to `/private/var`, so the validator rejects every temporary directory the
tests create. Point the tests at the canonical path instead.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if sys.platform == "darwin":
    candidate = Path("/private/tmp")
    if candidate.is_dir():
        tempfile.tempdir = str(candidate)
