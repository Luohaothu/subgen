import tempfile
import unittest
from pathlib import Path

from subtitle_pipeline.config import AudioSelectionPolicy
from subtitle_pipeline.media import (
    MediaProbeError,
    MediaValidationError,
    audio_offset_ms,
    parse_probe,
    presentation_origin_ms,
    probe_media,
    probe_sha256,
    stash_target_path,
)
from tests.media_fixture import write_sample_media


class MediaTests(unittest.TestCase):
    def test_stash_target_uses_video_stem_and_two_letter_language(self):
        self.assertEqual(
            stash_target_path(Path("dir/Movie.mp4")), Path("dir/Movie.zh.srt")
        )
        self.assertEqual(stash_target_path(Path("x/a.b.mkv")), Path("x/a.b.zh.srt"))

    def test_audio_offset_is_relative_to_presentation_origin(self):
        self.assertEqual(presentation_origin_ms(-0.500, -0.250), -500)
        self.assertEqual(presentation_origin_ms(None, 1.000), 1000)
        self.assertEqual(presentation_origin_ms(None, None), 0)
        self.assertEqual(audio_offset_ms(0.500, 0.000, 0.000), 500)
        self.assertEqual(audio_offset_ms(1.250, None, 1.000), 250)
        self.assertEqual(audio_offset_ms(-0.250, -0.500, -0.500), 250)

    def test_probe_without_audio_is_explicit(self):
        probe = {
            "format": {"duration": "10.0", "start_time": "0"},
            "streams": [{"index": 0, "codec_type": "video", "start_time": "0"}],
        }
        record = parse_probe("missing.mp4", 100, probe, False)
        self.assertIsNone(record.audio_stream_index)
        self.assertIsNone(record.audio_offset_ms)
        self.assertIsNone(record.audio_codec)
        self.assertIsNone(record.audio_channels)
        self.assertIsNone(record.audio_sample_rate_hz)
        self.assertIsNone(record.audio_duration_ms)
        self.assertIsNone(record.audio_start_ms)
        self.assertIsNone(record.audio_language)
        self.assertEqual(record.format_start_ms, 0)
        self.assertEqual(record.video_start_ms, 0)
        self.assertEqual(record.presentation_origin_ms, 0)
        self.assertEqual(record.target_relative_path, "missing.zh.srt")

    def test_probe_hash_is_canonical_and_audio_selection_is_deterministic(self):
        probe = {
            "format": {"duration": "10.0", "start_time": "-0.500"},
            "streams": [
                {
                    "index": 4,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": "48000",
                    "start_time": "0.250",
                    "duration": "9.0",
                    "tags": {"language": "eng"},
                },
                {
                    "index": 3,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": "48000",
                    "start_time": "0.000",
                    "duration": "9.5",
                    "tags": {"language": "jpn"},
                },
                {"index": 0, "codec_type": "video", "start_time": "-0.250"},
            ],
        }
        reordered = {"streams": probe["streams"], "format": probe["format"]}
        self.assertEqual(probe_sha256(probe), probe_sha256(reordered))
        record = parse_probe(
            "dir/movie.mp4",
            123,
            probe,
            False,
            source_mtime_ns=456,
            source_device=7,
            source_inode=8,
            inventory_schema_version="inventory-v1",
        )
        self.assertEqual(record.audio_stream_index, 3)
        self.assertEqual(record.audio_codec, "aac")
        self.assertEqual(record.audio_channels, 2)
        self.assertEqual(record.audio_sample_rate_hz, 48_000)
        self.assertEqual(record.audio_duration_ms, 9_500)
        self.assertEqual(record.audio_start_ms, 0)
        self.assertEqual(record.audio_language, "jpn")
        self.assertEqual(record.format_start_ms, -500)
        self.assertEqual(record.video_start_ms, -250)
        self.assertEqual(record.presentation_origin_ms, -500)
        self.assertEqual(record.audio_offset_ms, 500)
        self.assertEqual(record.probe_sha256, probe_sha256(probe))

    def test_selection_falls_back_to_untagged_then_lowest_index(self):
        base = {"format": {"duration": "2.0"}}
        streams = [
            {
                "index": 5,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "sample_rate": "48000",
                "tags": {"language": "eng"},
            },
            {
                "index": 7,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "sample_rate": "48000",
                "tags": {"language": "und"},
            },
        ]
        untagged = parse_probe("a.mp4", 1, {**base, "streams": streams}, False)
        self.assertEqual(untagged.audio_stream_index, 7)
        lowest = parse_probe(
            "a.mp4",
            1,
            {**base, "streams": streams},
            False,
            audio_selection_policy=AudioSelectionPolicy(
                prefer_default_disposition=False,
                prefer_untagged=False,
            ),
        )
        self.assertEqual(lowest.audio_stream_index, 5)

    def test_selection_order_is_language_then_default_then_untagged(self):
        def audio(index, language, default):
            return {
                "index": index,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "sample_rate": "48000",
                "tags": {"language": language},
                "disposition": {"default": default},
            }

        probe = {
            "format": {"duration": "2.0"},
            "streams": [
                audio(9, "und", 0),
                audio(7, "eng", 1),
                audio(5, "jpn", 0),
            ],
        }
        preferred = parse_probe("a.mp4", 1, probe, False)
        self.assertEqual(preferred.audio_stream_index, 5)

        same_language = {
            "format": {"duration": "2.0"},
            "streams": [
                audio(3, "jpn", 0),
                audio(6, "jpn", 1),
            ],
        }
        self.assertEqual(
            parse_probe("a.mp4", 1, same_language, False).audio_stream_index,
            6,
        )

        without_japanese = {**probe, "streams": probe["streams"][:2]}
        default = parse_probe("a.mp4", 1, without_japanese, False)
        self.assertEqual(default.audio_stream_index, 7)

    def test_unselected_audio_does_not_require_selected_stream_evidence(self):
        probe = {
            "format": {"duration": "2.0"},
            "streams": [
                {
                    "index": 8,
                    "codec_type": "audio",
                    "tags": {"language": "eng"},
                },
                {
                    "index": 2,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": "48000",
                    "tags": {"language": "jpn"},
                },
                {"index": 0, "codec_type": "video"},
            ],
        }
        self.assertEqual(
            parse_probe("a.mp4", 1, probe, False).audio_stream_index,
            2,
        )

    def test_probe_validation_rejects_nonfinite_boolean_and_duplicate_streams(self):
        invalid = (
            {"format": {"duration": "nan"}, "streams": []},
            {"format": {"duration": "0"}, "streams": []},
            {"format": {"duration": True}, "streams": []},
            {
                "format": {"duration": "1"},
                "streams": [
                    {"index": 1, "codec_type": "audio"},
                    {"index": 1, "codec_type": "video"},
                ],
            },
        )
        for probe in invalid:
            with self.subTest(probe=probe):
                with self.assertRaises(MediaValidationError):
                    parse_probe("a.mp4", 1, probe, False)

    def test_probe_media_reads_a_real_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sample.mkv"
            write_sample_media(path, seconds=0.5)
            result = probe_media(path, timeout_seconds=10)
            self.assertEqual(result["format"]["duration"], "0.500000")
            self.assertEqual(
                [stream["codec_type"] for stream in result["streams"]],
                ["video", "audio"],
            )
            audio = result["streams"][1]
            self.assertEqual(audio["sample_rate"], "44100")
            self.assertEqual(audio["channels"], 1)
            record = parse_probe("a.mkv", path.stat().st_size, result, False)
            self.assertEqual(record.audio_stream_index, 1)

    def test_probe_media_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(MediaProbeError):
                probe_media(Path(td) / "missing.mkv")

    def test_probe_media_rejects_invalid_timeout(self):
        with self.assertRaises(MediaProbeError):
            probe_media(Path("missing.mkv"), timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
