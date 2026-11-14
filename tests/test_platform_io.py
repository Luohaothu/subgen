from __future__ import annotations

import os
import tempfile
import unittest

from subtitle_pipeline import platform_io


class PlatformIoTests(unittest.TestCase):
    def test_fd_path_matches_the_platform_directory(self):
        descriptor = os.open(tempfile.gettempdir(), os.O_RDONLY)
        try:
            self.assertEqual(
                platform_io.fd_path(descriptor),
                str(platform_io.fd_directory() / str(descriptor)),
            )
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
