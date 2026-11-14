import os
import tempfile
import unittest
from pathlib import Path

import soundfile as sf

from subtitle_pipeline import platform_io
from subtitle_pipeline.media import MediaExtractionError, extract_audio
from subtitle_pipeline.runtime_cli import build_parser
from tests.media_fixture import write_sample_media


class RuntimeCliTests(unittest.TestCase):
    def test_extract_produces_trimmed_mono_flac(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "sample.mkv"
            output = Path(td) / "result.flac"
            output.write_bytes(b"")
            write_sample_media(source, seconds=1.0)
            extract_audio(source, 1, 500, output, timeout_seconds=10)
            info = sf.info(output)
            self.assertEqual(info.format, "FLAC")
            self.assertEqual(info.samplerate, 16_000)
            self.assertEqual(info.channels, 1)
            self.assertEqual(info.frames, 8_000)

    def test_extract_reads_an_open_descriptor_path(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "sample.mkv"
            output = Path(td) / "result.flac"
            write_sample_media(source, seconds=0.5)
            descriptor = os.open(source, os.O_RDONLY)
            try:
                extract_audio(
                    Path(platform_io.fd_path(descriptor)),
                    1,
                    250,
                    output,
                    timeout_seconds=10,
                )
            finally:
                os.close(descriptor)
            info = sf.info(output)
            self.assertEqual(info.frames, 4_000)

    def test_extract_rejects_a_missing_stream(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "sample.mkv"
            output = Path(td) / "result.flac"
            write_sample_media(source, seconds=0.5)
            with self.assertRaises(MediaExtractionError):
                extract_audio(source, 9, 500, output, timeout_seconds=10)

    def test_extract_defaults_to_two_workers_and_accepts_filters(self):
        video_id = "a" * 64
        args = build_parser().parse_args(
            ["extract", "--limit", "3", "--video-id", video_id]
        )

        self.assertEqual(args.command, "extract")
        self.assertEqual(args.workers, 2)
        self.assertEqual(args.limit, 3)
        self.assertEqual(args.video_ids, [video_id])


if __name__ == "__main__":
    unittest.main()
