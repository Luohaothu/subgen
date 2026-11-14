import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from subtitle_pipeline.segments import (
    SpeechSegment,
    materialize_wav_batch,
    normalize_vad,
)
from subtitle_pipeline.vad_runner import normalize_vad_payload


class SegmentTests(unittest.TestCase):
    def test_padding_does_not_change_ownership(self):
        spans = normalize_vad(
            "video-a",
            [[1000, 5000], [5200, 9000]],
            duration_ms=10000,
            padding_ms=300,
            max_span_ms=30000,
        )
        self.assertEqual((spans[0].owner_start_ms, spans[0].owner_end_ms), (1000, 5000))
        self.assertEqual(
            (spans[0].decode_start_ms, spans[0].decode_end_ms), (700, 5300)
        )
        self.assertEqual(
            (spans[1].decode_start_ms, spans[1].decode_end_ms), (4900, 9300)
        )
        self.assertEqual(spans[0].segment_id, "video-a-seg-00000000")

    def test_overlong_vad_span_is_split_without_gaps(self):
        spans = normalize_vad(
            "video-a",
            [[0, 65000]],
            duration_ms=65000,
            padding_ms=300,
            max_span_ms=30000,
        )
        self.assertEqual(
            [(s.owner_start_ms, s.owner_end_ms) for s in spans],
            [(0, 30000), (30000, 60000), (60000, 65000)],
        )

    def test_overlapping_vad_spans_merge_and_video_ids_stay_unique(self):
        a = normalize_vad("video-a", [[1000, 5000], [4500, 7000]], 10000, 300, 30000)
        b = normalize_vad("video-b", [[1000, 7000]], 10000, 300, 30000)
        touching = normalize_vad(
            "video-c", [[1000, 5000], [5000, 7000]], 10000, 300, 30000
        )
        self.assertEqual(
            [(s.owner_start_ms, s.owner_end_ms) for s in a], [(1000, 7000)]
        )
        self.assertEqual(
            [(s.owner_start_ms, s.owner_end_ms) for s in touching], [(1000, 7000)]
        )
        self.assertNotEqual(a[0].segment_id, b[0].segment_id)

    def test_invalid_or_empty_spans_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-positive"):
            normalize_vad("video-a", [[1000, 1000]], 2000, 300, 30000)

    def test_materialized_wav_has_exact_padded_frame_count(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            flac = root / "source.flac"
            sf.write(flac, np.zeros(16000, dtype=np.int16), 16000, subtype="PCM_16")
            segment = SpeechSegment("video-a-seg-00000000", 0, 200, 500, 100, 600)
            outputs = materialize_wav_batch(flac, [segment], root / "wav")
            self.assertEqual(sf.info(outputs[segment.segment_id]).frames, 8000)

    def test_vad_payload_accepts_documented_shapes_only(self):
        self.assertEqual(normalize_vad_payload([{"value": [[10, 20]]}]), [[10, 20]])
        self.assertEqual(normalize_vad_payload({"segments": [[30, 40]]}), [[30, 40]])
        with self.assertRaisesRegex(ValueError, "unsupported VAD payload"):
            normalize_vad_payload({"text": "not-vad"})

    def test_invalid_parameters_cannot_break_30_second_bound_or_termination(self):
        for max_span in (30001, 0, -1):
            with self.assertRaisesRegex(ValueError, "max_span_ms"):
                normalize_vad("video-a", [[0, 1000]], 2000, 300, max_span)
        with self.assertRaisesRegex(ValueError, "padding_ms"):
            normalize_vad("video-a", [[0, 1000]], 2000, -1, 30000)
        with self.assertRaisesRegex(ValueError, "duration_ms"):
            normalize_vad("video-a", [[0, 1000]], 0, 300, 30000)

    def test_raw_intervals_are_strict_and_order_checked_before_clipping(self):
        with self.assertRaisesRegex(ValueError, "two-integer list"):
            normalize_vad("video-a", [(0, 1000)], 2000, 300, 30000)
        with self.assertRaisesRegex(ValueError, "two-integer list"):
            normalize_vad("video-a", [[0.0, 1000]], 2000, 300, 30000)
        with self.assertRaisesRegex(ValueError, "not ordered"):
            normalize_vad("video-a", [[1000, 1500], [-1000, -500]], 2000, 300, 30000)

    def test_materialization_rejects_decode_interval_past_audio_end(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            flac = root / "source.flac"
            sf.write(flac, np.zeros(16000, dtype=np.int16), 16000, subtype="PCM_16")
            segment = SpeechSegment("video-a-seg-00000000", 0, 800, 1000, 800, 1200)
            with self.assertRaisesRegex(ValueError, "decode interval exceeds audio"):
                materialize_wav_batch(flac, [segment], root / "wav")

    def test_materialization_clamps_rounded_millisecond_audio_end(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            flac = root / "source.flac"
            # 16009 frames round to 1001 ms, which converts back to 16016 frames.
            sf.write(flac, np.zeros(16009, dtype=np.int16), 16000, subtype="PCM_16")
            segment = SpeechSegment("video-a-seg-00000000", 0, 0, 1001, 0, 1001)
            outputs = materialize_wav_batch(flac, [segment], root / "wav")
            self.assertEqual(sf.info(outputs[segment.segment_id]).frames, 16009)


if __name__ == "__main__":
    unittest.main()
