from __future__ import annotations

import importlib.util
import json
import math
import sys
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from subtitle_pipeline.align_runner import (
    AlignmentTask,
    align_with_retries,
    alignment_from_funasr_timestamps,
    create_aligner,
)
from subtitle_pipeline.alignment import (
    AlignedToken,
    JapaneseCue,
    build_cues,
    restore_transcript_text,
    timestamp_digest,
    validate_tokens,
)
from subtitle_pipeline.segments import SpeechSegment
from subtitle_pipeline.smoke_cli import _nonlexical_alignment_omission

_HAS_TORCH = (
    importlib.util.find_spec("torch") is not None
    and importlib.util.find_spec("transformers") is not None
)


def segment(
    segment_id: str = "video-seg-00000000",
    *,
    owner_start_ms: int = 0,
    owner_end_ms: int = 1000,
    decode_start_ms: int = 0,
    decode_end_ms: int = 1000,
) -> SpeechSegment:
    return SpeechSegment(
        segment_id,
        0,
        owner_start_ms,
        owner_end_ms,
        decode_start_ms,
        decode_end_ms,
    )


class AlignmentValueTests(unittest.TestCase):
    def test_unreviewed_repetitive_nonlexical_alignment_is_not_omitted(self):
        row = {
            "transcript": "うんうんうんうんうんうん。ああ。",
            "tokens": [],
            "cues": [],
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
            "raw": {"decoded": [{"text": "うん", "start_time": 0.0, "end_time": 8.8}]},
        }

        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_overlong_nonlexical_token_with_lexical_context_is_not_omitted(self):
        row = {
            "transcript": "あ、ああ…はぁ…興味…はぁ…",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
            "raw": {
                "decoded": [
                    {"text": "ああ", "start_time": 0.0, "end_time": 8.8},
                    {"text": "興味", "start_time": 9.0, "end_time": 9.4},
                ]
            },
        }

        self.assertFalse(_nonlexical_alignment_omission(row))
        row["raw"]["decoded"][0]["text"] = "重要"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_overlong_lexical_kana_token_is_never_a_nonlexical_omission(self):
        row = {
            "transcript": "やばい。",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
            "raw": {
                "decoded": [{"text": "やばい", "start_time": 0.0, "end_time": 11.04}]
            },
        }

        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_nonlexical_omission_is_bound_to_id_and_exact_text(self):
        row = {
            "segment_id": (
                "55a3fa823fb111025dbc3b75d0d11a770ffa4f6ad89dfeea335998d4391b3849"
                "-seg-00000824"
            ),
            "transcript": (
                "ああああああああっ、あああああああああっ、"
                "あああああああああっ、あああああああああっ、"
                "あああああああっ、ああああああっ、あ"
            ),
            "error": {
                "type": "alignment_validation",
                "message": "alignment token 18 is outside segment bounds",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "い"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_pure_vocalization_is_an_exact_nonlexical_omission(self):
        row = {
            "segment_id": (
                "29dc848f1ab4388365848a19f5c3191d5a5fd8dfcbd68ee17d0f6bc9dbdbaded"
                "-seg-00001859"
            ),
            "transcript": "あっ、あっ、ああっ、んんっ、んんっ、あっ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_single_vocalization_is_bound_to_exact_segment(self):
        row = {
            "segment_id": (
                "3b81b79381b6109916940c7d82902f1ad3701486a9bbb30a4125eaedadefbf10"
                "-seg-00000620"
            ),
            "transcript": "あ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_kissing_vocalization_is_bound_to_exact_text(self):
        row = {
            "segment_id": (
                "c64f25c2c53af8c26475a90587b306f2f72c6083fb6879fe80a7d6e9feb6961e"
                "-seg-00000633"
            ),
            "transcript": "ん、あん、んちゅっ、ちゅっ、んちゅっ、ちゅっ、ちゅっ、ん",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_repeated_vocalization_is_bound_to_exact_text(self):
        row = {
            "segment_id": (
                "29c8f9c5111cd5896ba7eb0396f0d6ad8eff55c7b5916a03748de01acc8f3248"
                "-seg-00000357"
            ),
            "transcript": "んふぅんっ、" * 11 + "んふ",
            "error": {
                "type": "alignment_validation",
                "message": "alignment token 31 is outside segment bounds: 11040-11040",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_trimmed_vocalization_is_bound_to_exact_segment(self):
        row = {
            "segment_id": (
                "42ec12053bceca0d71b0f0448593d79ad6937b801d9d94b6ca3b57c46fa90e03"
                "-seg-00000498"
            ),
            "transcript": "あん",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_humming_vocalization_is_bound_to_exact_text(self):
        row = {
            "segment_id": (
                "4a9e9f5e17af9ae4eed70c50c68b67a43f51ae79f261e29877ddf1d132d35e95"
                "-seg-00002029"
            ),
            "transcript": "んっ、んっ、んっ、んんっ、んんっ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_sone_009_vocalization_is_bound_to_exact_segment(self):
        row = {
            "segment_id": (
                "a5c6e4418a245405a4d7cc97ef28acf1d6d94827864afc173afe44c5915f944b"
                "-seg-00000305"
            ),
            "transcript": "んっ、んっ、んっ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_sone_692_vocalization_is_bound_to_exact_text(self):
        row = {
            "segment_id": (
                "b63836c08fc72ac0e96421096dc75aea6cb82010b9a12b1a604da3f4cd39a78e"
                "-seg-00001024"
            ),
            "transcript": "んっ、んっ、んんっ、んんっ、んんっ、んんんっ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_sone_822_kissing_is_bound_to_exact_segment(self):
        row = {
            "segment_id": (
                "5a8c2e6076ee16379632b23051ea7c5229b3bf82a5277454c0b9af48ff3dab11"
                "-seg-00000157"
            ),
            "transcript": "んちゅっ、ちゅっ、んちゅっ、んちゅっ、ん",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_ssis_200c_kissing_is_bound_to_exact_text(self):
        row = {
            "segment_id": (
                "e57db2fa9ccecf1a38efbd63706489183e5e71e9100d59625d233f665ff88d47"
                "-seg-00000909"
            ),
            "transcript": "んちゅっ、んちゅっ、ん",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_waaa_428_vocalization_is_bound_to_exact_segment(self):
        row = {
            "segment_id": (
                "cc9ec2599e487178316192e779d87ffafe0f95f40f8fa30e8a05a6be626e22eb"
                "-seg-00001151"
            ),
            "transcript": "あ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_stars770_hallucinated_word_is_exact_omission(self):
        row = {
            "segment_id": (
                "40774ee6aa89911262e62c7b998b78830cacbc1223defc148f5c27a554e58819"
                "-seg-00002022"
            ),
            "transcript": "ふふっ、ああ、そう。ああ、ああ、うんうんうん。あ、あ、あ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_fsdss_819_vocalization_is_bound_to_exact_text(self):
        row = {
            "segment_id": (
                "b2e38d5dc0e50d05833318e903eb46fe15691c71478cd80d108c8e24899d709f"
                "-seg-00000717"
            ),
            "transcript": "んっ、んっ、んんっ、んんっ、んんっ、んんっ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_babd_018_vocalization_is_bound_to_exact_segment(self):
        row = {
            "segment_id": (
                "ff74ebda37e2c690c41d9c4f5830a3e52f82df606ed79cf058a9bc6d87ec51bf"
                "-seg-00000744"
            ),
            "transcript": "あああああああああっ、ああああ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_dass_500_vocalization_is_bound_to_exact_text(self):
        transcript = (
            "んんっ、ああああああっ、ああああああっ、ああああああっ、"
            "ああああああっ、ああああああっ、ああああああっ、"
            "ああああああっ、ああああああっ、あ"
        )
        row = {
            "segment_id": (
                "6d97b3624030401db532f7efb103611a2b71acb93696a9a5e93d8afd852975bf"
                "-seg-00000352"
            ),
            "transcript": transcript,
            "error": {
                "type": "alignment_validation",
                "message": "alignment token 37 is outside segment bounds: 10008-11040",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_ssis_305_short_vocalization_is_exact_omission(self):
        row = {
            "segment_id": (
                "153afabf2909830065a94f5ac05f24521ba8201f03c24e8578adcc022c14bbb5"
                "-seg-00000827"
            ),
            "transcript": "ああああああああああ",
            "error": {
                "type": "alignment_validation",
                "message": "alignment token 2 is outside segment bounds: 1070-2000",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_ssis_371_short_vocalization_is_exact_omission(self):
        row = {
            "segment_id": (
                "1dc08c5da791b244ef15ee4cf275c7b8499c4be123fd207a02f047778bd1839e"
                "-seg-00000596"
            ),
            "transcript": "あああああああああああ",
            "error": {
                "type": "alignment_validation",
                "message": "alignment token 8 is outside segment bounds: 1160-2000",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_ssis_371_long_vocalization_is_exact_omission(self):
        transcript = (
            "んっ、んんっ、んんっ、んんっ、んんっ、んんんっ、"
            "んんんんんんっ、んんんんんんんっ、ああああっ、"
            "んんんんんんんっ、ああああっ、ああああっ、ああああっ、"
            "んんんんんんっ、ああああっ、ああああっ、んんんんんんっ、"
            "ああああっ、んんんんんんっ、ああああっ、ん"
        )
        row = {
            "segment_id": (
                "1dc08c5da791b244ef15ee4cf275c7b8499c4be123fd207a02f047778bd1839e"
                "-seg-00000841"
            ),
            "transcript": transcript,
            "error": {
                "type": "alignment_validation",
                "message": "alignment token 42 is outside segment bounds: 13126-14880",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_npjs_106_vocalization_is_bound_to_exact_text(self):
        row = {
            "segment_id": (
                "5b50354c2806e6d6e4c76c6aaca1aa067d2d5e668d327a1796a4dd79ba9c3df1"
                "-seg-00000905"
            ),
            "transcript": "んっ、んっ、んんっ、んんっ、んんっ、んんんっ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_sone_509_short_vocalization_is_exact_omission(self):
        row = {
            "segment_id": (
                "8a3a1a2fb313d1191ee2c8bfeed698a7cb42e79121c027394dba007bf851bf0d"
                "-seg-00000918"
            ),
            "transcript": "あああああああああああ",
            "error": {
                "type": "alignment_validation",
                "message": "alignment token 5 is outside segment bounds: 1920-2000",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_sone_871_laughter_is_bound_to_exact_text(self):
        row = {
            "segment_id": (
                "daac877a34294e531b22c726015a23ea56498300d94c17d7cceb445fc7be4a82"
                "-seg-00000662"
            ),
            "transcript": "あはははははははははは。",
            "error": {
                "type": "alignment_validation",
                "message": "alignment token 1 is outside segment bounds: 1190-2000",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_sone_952ch_short_vocalization_is_exact_omission(self):
        row = {
            "segment_id": (
                "fc92ac3cf2593527460c6d0f93392527b3c08759cbd63a399f4d6244c1751d0c"
                "-seg-00000706"
            ),
            "transcript": "ああああああああああ",
            "error": {
                "type": "alignment_validation",
                "message": "alignment token 7 is outside segment bounds: 1070-2000",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_ssis_016_vocalization_is_bound_to_exact_text(self):
        row = {
            "segment_id": (
                "e64495fe23fc8052a3ef3bfd58295ec10e9c26caaa5bcc61df6f0454214312f1"
                "-seg-00001529"
            ),
            "transcript": "んっ、んんっ、んんっ、んんっ、んんっ、んんっ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_ipzz_135_kissing_is_bound_to_exact_segment(self):
        row = {
            "segment_id": (
                "6a172e42c0249bdc5f8e19f8aef05abd4d5dd5d1d92a2aedec50794d063dcfab"
                "-seg-00001404"
            ),
            "transcript": "ちゅっ、ちゅっ、ちゅっ、ちゅ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_ssni_959_c_kissing_is_bound_to_exact_segment(self):
        row = {
            "segment_id": (
                "ffb916fccf8122a99c7108f63f157aed7adc3c2480ccd09f7c97c95457ad3903"
                "-seg-00000703"
            ),
            "transcript": "ちゅっ、ちゅっ、ちゅ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["transcript"] += "会話"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_reviewed_juq_138_vocalization_is_bound_to_exact_segment(self):
        row = {
            "segment_id": (
                "1e0c6bc2f3d7073ff49618ff68aaea1cb88807059110470c9d3111be98b4bb8b"
                "-seg-00000554"
            ),
            "transcript": "あ",
            "error": {
                "type": "alignment_validation",
                "message": "individual aligned token exceeds 8,000 ms",
            },
        }

        self.assertTrue(_nonlexical_alignment_omission(row))
        row["segment_id"] = "different-segment"
        self.assertFalse(_nonlexical_alignment_omission(row))

    def test_absolute_times_include_decode_origin_ownership_and_audio_offset(self):
        tokens = [
            AlignedToken("今日は", 0, 900),
            AlignedToken("大丈夫", 1000, 1900),
            AlignedToken("。", 1900, 2000),
        ]
        speech = segment(
            owner_start_ms=5200,
            owner_end_ms=6800,
            decode_start_ms=5000,
            decode_end_ms=7000,
        )

        cues = build_cues(tokens, segment=speech, audio_offset_ms=250)

        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (5450, 7050))
        self.assertEqual(cues[0].cue_id, "video-seg-00000000-cue-0000")
        self.assertEqual(cues[0].text, "今日は大丈夫")

    def test_padding_only_tokens_are_removed_before_cue_grouping(self):
        tokens = [
            AlignedToken("前。", 0, 100),
            AlignedToken("本編。", 300, 900),
            AlignedToken("後。", 1900, 2000),
        ]
        speech = segment(
            owner_start_ms=5200,
            owner_end_ms=6800,
            decode_start_ms=5000,
            decode_end_ms=7000,
        )

        cues = build_cues(tokens, segment=speech, audio_offset_ms=0)

        self.assertEqual([cue.text for cue in cues], ["本編。"])
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (5300, 5900))

    def test_boundary_crossing_tokens_are_intersected_not_discarded(self):
        tokens = [
            AlignedToken("左", 100, 300),
            AlignedToken("中", 300, 1700),
            AlignedToken("右。", 1700, 1900),
        ]
        speech = segment(
            owner_start_ms=5200,
            owner_end_ms=6800,
            decode_start_ms=5000,
            decode_end_ms=7000,
        )

        cues = build_cues(tokens, segment=speech, audio_offset_ms=0)

        self.assertEqual([cue.text for cue in cues], ["左中右。"])
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (5200, 6800))

    def test_negative_presentation_offset_clips_at_zero_and_drops_collapsed_tokens(
        self,
    ):
        tokens = [
            AlignedToken("消", 0, 100),
            AlignedToken("残", 100, 300),
            AlignedToken("る。", 300, 500),
        ]

        cues = build_cues(
            tokens,
            segment=segment(owner_end_ms=500, decode_end_ms=500),
            audio_offset_ms=-250,
        )

        self.assertEqual([cue.text for cue in cues], ["残る。"])
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (0, 250))

    def test_cue_ids_remain_unique_across_segments(self):
        tokens = [AlignedToken("はい。", 0, 500)]
        first = build_cues(
            tokens,
            segment=segment("video-seg-00000000"),
            audio_offset_ms=0,
        )
        second = build_cues(
            tokens,
            segment=segment("video-seg-00000001"),
            audio_offset_ms=0,
        )

        self.assertEqual(first[0].cue_id, "video-seg-00000000-cue-0000")
        self.assertEqual(second[0].cue_id, "video-seg-00000001-cue-0000")
        self.assertNotEqual(first[0].cue_id, second[0].cue_id)

    def test_validate_tokens_requires_alignment_for_nonempty_transcript(self):
        with self.assertRaisesRegex(ValueError, "non-empty transcript"):
            validate_tokens([], 1000, transcript="はい")
        validate_tokens([], 1000, transcript="")

    def test_validate_tokens_rejects_non_integer_and_boolean_times(self):
        for token in (
            AlignedToken("a", 0.0, 100),
            AlignedToken("a", False, 100),
            AlignedToken("a", 0, 100.0),
            AlignedToken("a", 0, True),
        ):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "integer"):
                    validate_tokens([token], 1000)

    def test_validate_tokens_rejects_bounds_and_non_monotonic_rows(self):
        for token in (
            AlignedToken("a", -1, 100),
            AlignedToken("a", 0, 0),
            AlignedToken("a", 0, 1001),
        ):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "bounds"):
                    validate_tokens([token], 1000)
        with self.assertRaisesRegex(ValueError, "non-monotonic"):
            validate_tokens(
                [AlignedToken("a", 100, 200), AlignedToken("b", 150, 250)],
                1000,
            )

    def test_restore_transcript_text_preserves_exact_punctuation_and_spacing(self):
        restored = restore_transcript_text(
            "今日は、 大丈夫！？ ",
            ["今日", "は", "大丈夫"],
        )

        self.assertEqual(restored, ["今日", "は、 ", "大丈夫！？ "])
        self.assertEqual("".join(restored), "今日は、 大丈夫！？ ")

    def test_restore_transcript_text_preserves_repeated_words(self):
        restored = restore_transcript_text(
            "はい、はい、はい。",
            ["はい", "はい", "はい"],
        )

        self.assertEqual(restored, ["はい、", "はい、", "はい。"])
        self.assertEqual("".join(restored), "はい、はい、はい。")

    def test_restore_transcript_text_preserves_punctuation_inside_merged_word(self):
        restored = restore_transcript_text(
            "あっ、ぎゅっ……！",
            ["あっぎゅ", "っ"],
        )

        self.assertEqual(restored, ["あっ、ぎゅ", "っ……！"])
        self.assertEqual("".join(restored), "あっ、ぎゅっ……！")

    def test_restore_transcript_text_accepts_nfkc_equivalent_aligner_words(self):
        restored = restore_transcript_text(
            "３年生、Ｇカップ。",
            ["3", "年生", "G", "カップ"],
        )

        self.assertEqual(restored, ["３", "年生、", "Ｇ", "カップ。"])
        self.assertEqual("".join(restored), "３年生、Ｇカップ。")

    def test_restore_transcript_text_preserves_punctuation_only_aligner_words(self):
        restored = restore_transcript_text(
            "Let's go.",
            ["Let", "'", "s", "go"],
        )

        self.assertEqual(restored, ["Let", "'", "s ", "go."])
        self.assertEqual("".join(restored), "Let's go.")

    def test_restore_transcript_text_rejects_leading_gap(self):
        for transcript in ("、今日は", " 今日は"):
            with self.subTest(transcript=transcript):
                with self.assertRaisesRegex(ValueError, "leading"):
                    restore_transcript_text(transcript, ["今日", "は"])

    def test_restore_transcript_text_rejects_missing_reordered_or_extra_words(self):
        cases = (
            ("今日は", ["今日"]),
            ("今日は", ["は", "今日"]),
            ("今日は", ["今日", "は", "余分"]),
            ("今日は", ["今日", "今日"]),
        )
        for transcript, words in cases:
            with self.subTest(transcript=transcript, words=words):
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    restore_transcript_text(transcript, words)

    def test_restore_transcript_text_rejects_invalid_empty_combinations(self):
        self.assertEqual(restore_transcript_text("", []), [])
        for transcript, words in (("はい", []), ("", ["はい"]), ("はい", [""])):
            with self.subTest(transcript=transcript, words=words):
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    restore_transcript_text(transcript, words)

    def test_long_cues_split_at_sentence_then_phrase_then_aligned_boundaries(self):
        tokens = [
            AlignedToken("一。", 0, 2000),
            AlignedToken("二、", 2000, 5000),
            AlignedToken("三", 5000, 9000),
            AlignedToken("四", 9000, 12000),
        ]

        cues = build_cues(
            tokens,
            segment=segment(owner_end_ms=12000, decode_end_ms=12000),
            audio_offset_ms=0,
        )

        self.assertEqual([cue.text for cue in cues], ["一。", "二、", "三四"])
        self.assertTrue(all(0 < cue.end_ms - cue.start_ms <= 8000 for cue in cues))

    def test_sentence_boundary_is_detected_before_japanese_closing_quote(self):
        cues = build_cues(
            [AlignedToken("一。」 ", 0, 1000), AlignedToken("二", 1000, 2000)],
            segment=segment(owner_end_ms=2000, decode_end_ms=2000),
            audio_offset_ms=0,
        )

        self.assertEqual([cue.text for cue in cues], ["一。」 ", "二"])

    def test_restored_sentence_punctuation_before_whitespace_remains_a_boundary(self):
        cues = build_cues(
            [AlignedToken("一。 ", 0, 1000), AlignedToken("二", 1000, 5000)],
            segment=segment(owner_end_ms=5000, decode_end_ms=5000),
            audio_offset_ms=0,
        )

        self.assertEqual([cue.text for cue in cues], ["一。 ", "二"])

    def test_unpunctuated_cues_split_at_latest_boundary_before_eight_seconds(self):
        tokens = [
            AlignedToken(str(index), index * 1000, (index + 1) * 1000)
            for index in range(20)
        ]

        cues = build_cues(
            tokens,
            segment=segment(owner_end_ms=20000, decode_end_ms=20000),
            audio_offset_ms=0,
        )

        self.assertEqual(
            [(cue.start_ms, cue.end_ms) for cue in cues],
            [(0, 8000), (8000, 16000), (16000, 20000)],
        )
        self.assertEqual(
            "".join(cue.text for cue in cues), "".join(map(str, range(20)))
        )

    def test_a_single_overlong_token_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "token.*8,000"):
            build_cues(
                [AlignedToken("長い", 0, 8001)],
                segment=segment(owner_end_ms=9000, decode_end_ms=9000),
                audio_offset_ms=0,
            )

    def test_large_silence_gap_does_not_create_an_overlong_cue(self):
        cues = build_cues(
            [AlignedToken("前", 0, 100), AlignedToken("後", 9000, 9100)],
            segment=segment(owner_end_ms=10000, decode_end_ms=10000),
            audio_offset_ms=0,
        )

        self.assertEqual(
            [(cue.text, cue.start_ms, cue.end_ms) for cue in cues],
            [("前", 0, 100), ("後", 9000, 9100)],
        )

    def test_timestamp_digest_ignores_text_but_changes_with_time_or_id(self):
        original = [JapaneseCue("cue-1", 100, 200, "はい")]
        translated = [original[0].with_text("好的")]
        moved = [JapaneseCue("cue-1", 101, 200, "はい")]
        renamed = [JapaneseCue("cue-2", 100, 200, "はい")]

        self.assertEqual(timestamp_digest(original), timestamp_digest(translated))
        self.assertNotEqual(timestamp_digest(original), timestamp_digest(moved))
        self.assertNotEqual(timestamp_digest(original), timestamp_digest(renamed))
        self.assertEqual(original[0].text, "はい")


class FakeTensor:
    def __init__(self, name: str, *, floating: bool) -> None:
        self.name = name
        self.floating = floating
        self.to_calls: list[dict[str, object]] = []

    def is_floating_point(self) -> bool:
        return self.floating

    def to(self, **kwargs):
        self.to_calls.append(kwargs)
        return self


class FakeModel:
    def __init__(self) -> None:
        self.device = "cuda:0"
        self.config = types.SimpleNamespace(timestamp_token_id=77)
        self.calls: list[dict[str, object]] = []
        self.logits = object()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(logits=self.logits)


class FakeProcessor:
    def __init__(self, decoded: list[list[dict[str, object]]]) -> None:
        self.decoded = decoded
        self.prepare_calls: list[dict[str, object]] = []
        self.decode_calls: list[dict[str, object]] = []
        self.input_ids = FakeTensor("input_ids", floating=False)
        self.features = FakeTensor("input_features", floating=True)
        self.mask = FakeTensor("attention_mask", floating=False)

    def prepare_forced_aligner_inputs(self, **kwargs):
        self.prepare_calls.append(kwargs)
        word_lists = [
            [str(item["text"]) for item in row]
            for row in self.decoded[: len(kwargs["audio"])]
        ]
        return (
            {
                "input_ids": self.input_ids,
                "input_features": self.features,
                "attention_mask": self.mask,
            },
            word_lists,
        )

    def decode_forced_alignment(self, **kwargs):
        self.decode_calls.append(kwargs)
        return self.decoded[: len(kwargs["word_lists"])]


def fake_torch_module() -> types.ModuleType:
    module = types.ModuleType("torch")
    module.bfloat16 = object()
    module.inference_mode = nullcontext
    return module


class AlignmentRunnerTests(unittest.TestCase):
    def test_funasr_timestamp_fallback_preserves_exact_text_and_absolute_timing(self):
        task = AlignmentTask(
            segment(
                owner_start_ms=5200,
                owner_end_ms=6800,
                decode_start_ms=5000,
                decode_end_ms=7000,
            ),
            Path("fallback.wav"),
            "今日は。",
            250,
        )

        row = alignment_from_funasr_timestamps(
            task,
            [
                {"token": "今日", "start_time": 0.1, "end_time": 0.5},
                {"token": "は", "start_time": 0.5, "end_time": 0.9},
                {"token": "。", "start_time": 0.9, "end_time": 1.0},
            ],
        )

        self.assertIsNone(row.error)
        self.assertEqual("".join(token.text for token in row.tokens), "今日は。")
        self.assertEqual(
            [(token.start_ms, token.end_ms) for token in row.tokens],
            [(100, 500), (500, 900), (900, 1000)],
        )
        self.assertEqual(
            (row.cues[0].start_ms, row.cues[0].end_ms),
            (5450, 6250),
        )
        self.assertEqual(row.raw["phase"], "funasr_timestamp_fallback")

    def test_funasr_timestamp_fallback_selects_unique_sanitized_token_slice(self):
        rows = [
            {"token": "あっ", "start_time": 0.0, "end_time": 0.2},
            {"token": "、", "start_time": 0.2, "end_time": 0.3},
            {"token": "会話", "start_time": 0.3, "end_time": 0.7},
            {"token": "です", "start_time": 0.7, "end_time": 1.0},
            {"token": "。", "start_time": 1.0, "end_time": 1.1},
            {"token": "うん", "start_time": 1.1, "end_time": 1.3},
        ]
        longer_segment = segment(owner_end_ms=1500, decode_end_ms=1500)

        prefix = alignment_from_funasr_timestamps(
            AlignmentTask(longer_segment, "prefix.wav", "あっ、会話", 0),
            rows,
        )
        suffix = alignment_from_funasr_timestamps(
            AlignmentTask(longer_segment, "suffix.wav", "会話です。", 0),
            rows,
        )

        self.assertEqual(
            [token.text for token in prefix.tokens], ["あっ", "、", "会話"]
        )
        self.assertEqual(
            [token.text for token in suffix.tokens], ["会話", "です", "。"]
        )

    def test_funasr_timestamp_fallback_clamps_small_quantized_tail(self):
        task = AlignmentTask(
            segment(owner_end_ms=1190, decode_end_ms=1190),
            "tail.wav",
            "気持ち。",
            0,
        )

        row = alignment_from_funasr_timestamps(
            task,
            [
                {"token": "気持ち", "start_time": 0.6, "end_time": 1.16},
                {"token": "。", "start_time": 1.16, "end_time": 1.22},
            ],
        )

        self.assertIsNone(row.error)
        self.assertEqual(
            [(token.start_ms, token.end_ms) for token in row.tokens],
            [(600, 1160), (1160, 1190)],
        )

    def test_funasr_timestamp_fallback_rejects_ambiguous_or_invalid_evidence(self):
        task = AlignmentTask(segment(), "fallback.wav", "会話", 0)
        cases = (
            (
                "not_substring",
                [{"token": "別文", "start_time": 0.0, "end_time": 0.2}],
            ),
            (
                "ambiguous",
                [
                    {"token": "会話", "start_time": 0.0, "end_time": 0.2},
                    {"token": "会話", "start_time": 0.2, "end_time": 0.4},
                ],
            ),
            (
                "partial_token",
                [{"token": "前会話後", "start_time": 0.0, "end_time": 0.4}],
            ),
            (
                "nonmonotonic",
                [
                    {"token": "会", "start_time": 0.2, "end_time": 0.4},
                    {"token": "話", "start_time": 0.3, "end_time": 0.5},
                ],
            ),
            (
                "large_overrun",
                [{"token": "会話", "start_time": 0.9, "end_time": 1.2}],
            ),
        )

        for label, timestamps in cases:
            with self.subTest(case=label):
                with self.assertRaisesRegex(ValueError, "FunASR timestamp fallback"):
                    alignment_from_funasr_timestamps(task, timestamps)

    def test_native_batch_call_preserves_order_paths_languages_and_decoder_contract(
        self,
    ):
        decoded = [
            [
                {"text": "今日", "start_time": 0.0, "end_time": 0.3},
                {"text": "は", "start_time": 0.3, "end_time": 0.501},
            ],
            [{"text": "はい", "start_time": 0.1, "end_time": 0.6}],
        ]
        processor = FakeProcessor(decoded)
        model = FakeModel()
        tasks = [
            AlignmentTask(segment("a-seg-00000000"), Path("a.wav"), "今日は。", 0),
            AlignmentTask(segment("b-seg-00000000"), Path("b.wav"), "はい！", 25),
        ]
        fake_torch = fake_torch_module()

        with patch.dict(sys.modules, {"torch": fake_torch}):
            rows = align_with_retries(processor, model, tasks)

        self.assertEqual(
            processor.prepare_calls,
            [
                {
                    "audio": ["a.wav", "b.wav"],
                    "transcript": ["今日は。", "はい！"],
                    "language": ["Japanese", "Japanese"],
                }
            ],
        )
        self.assertEqual(len(model.calls), 1)
        self.assertIs(model.calls[0]["input_ids"], processor.input_ids)
        self.assertEqual(
            processor.decode_calls,
            [
                {
                    "logits": model.logits,
                    "input_ids": processor.input_ids,
                    "word_lists": [["今日", "は"], ["はい"]],
                    "timestamp_token_id": 77,
                }
            ],
        )
        self.assertEqual(
            processor.features.to_calls,
            [{"device": "cuda:0", "dtype": fake_torch.bfloat16}],
        )
        self.assertEqual(processor.input_ids.to_calls, [{"device": "cuda:0"}])
        self.assertEqual(processor.mask.to_calls, [{"device": "cuda:0"}])
        self.assertEqual(
            [row.segment_id for row in rows], ["a-seg-00000000", "b-seg-00000000"]
        )
        self.assertEqual([row.transcript for row in rows], ["今日は。", "はい！"])
        self.assertEqual([token.text for token in rows[0].tokens], ["今日", "は。"])
        self.assertEqual(
            (rows[0].tokens[1].start_ms, rows[0].tokens[1].end_ms), (300, 501)
        )
        self.assertEqual((rows[1].cues[0].start_ms, rows[1].cues[0].end_ms), (125, 625))
        self.assertTrue(all(row.error is None for row in rows))
        json.dumps(rows[0].raw, ensure_ascii=False, allow_nan=False)

    def test_native_point_timestamp_is_repaired_to_one_millisecond(self):
        decoded = [
            [
                {"text": "こっち", "start_time": 0.0, "end_time": 0.32},
                {"text": "の", "start_time": 0.32, "end_time": 0.32},
                {"text": "方", "start_time": 0.32, "end_time": 0.64},
            ]
        ]
        processor = FakeProcessor(decoded)
        model = FakeModel()
        task = AlignmentTask(segment(), Path("point.wav"), "こっちの方。", 0)

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(processor, model, [task])[0]

        self.assertIsNone(row.error)
        self.assertEqual([token.text for token in row.tokens], ["こっち", "の", "方。"])
        self.assertEqual(
            [(token.start_ms, token.end_ms) for token in row.tokens],
            [(0, 320), (320, 321), (321, 640)],
        )

    def test_native_consecutive_point_timestamps_get_ordered_one_ms_spans(self):
        decoded = [
            [
                {"text": "前", "start_time": 0.56, "end_time": 0.64},
                {"text": "あ", "start_time": 5.12, "end_time": 5.12},
                {"text": "い", "start_time": 5.12, "end_time": 5.12},
                {"text": "う", "start_time": 5.12, "end_time": 5.12},
                {"text": "え", "start_time": 5.12, "end_time": 5.12},
                {"text": "ん", "start_time": 6.4, "end_time": 6.4},
                {"text": "っ", "start_time": 6.4, "end_time": 6.4},
            ]
        ]
        task = AlignmentTask(
            segment(owner_end_ms=6770, decode_end_ms=6770),
            Path("consecutive-points.wav"),
            "前、あいうえんっ。",
            0,
        )

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(FakeProcessor(decoded), FakeModel(), [task])[0]

        self.assertIsNone(row.error)
        self.assertEqual(
            [(token.start_ms, token.end_ms) for token in row.tokens],
            [
                (560, 640),
                (5120, 5121),
                (5121, 5122),
                (5122, 5123),
                (5123, 5124),
                (6400, 6401),
                (6401, 6402),
            ],
        )

    def test_native_timestamp_quantized_ten_ms_past_segment_end_is_clamped(self):
        decoded = [
            [
                {"text": "すごい", "start_time": 0.16, "end_time": 0.4},
                {"text": "気持ち", "start_time": 0.64, "end_time": 1.2},
            ]
        ]
        task = AlignmentTask(
            segment(owner_end_ms=1190, decode_end_ms=1190),
            Path("quantized-tail.wav"),
            "すごい気持ち。",
            0,
        )

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(FakeProcessor(decoded), FakeModel(), [task])[0]

        self.assertIsNone(row.error)
        self.assertEqual(
            [(token.start_ms, token.end_ms) for token in row.tokens],
            [(160, 400), (640, 1190)],
        )

    def test_native_timestamp_at_padded_320ms_frame_tail_is_clamped(self):
        decoded = [
            [
                {"text": "使っ", "start_time": 5.44, "end_time": 6.4},
                {"text": "て", "start_time": 6.56, "end_time": 6.56},
                {"text": "ください", "start_time": 6.56, "end_time": 6.88},
            ]
        ]
        task = AlignmentTask(
            segment(owner_end_ms=6600, decode_end_ms=6600),
            Path("padded-frame-tail.wav"),
            "使ってください。",
            0,
        )

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(FakeProcessor(decoded), FakeModel(), [task])[0]

        self.assertIsNone(row.error)
        self.assertEqual(
            [(token.start_ms, token.end_ms) for token in row.tokens],
            [(5440, 6400), (6560, 6561), (6561, 6600)],
        )

    def test_native_timestamp_at_padded_640ms_frame_tail_is_clamped(self):
        decoded = [
            [
                {"text": "か", "start_time": 0.56, "end_time": 1.12},
                {"text": "な", "start_time": 1.6, "end_time": 1.6},
            ]
        ]
        task = AlignmentTask(
            segment(owner_end_ms=1130, decode_end_ms=1130),
            Path("padded-double-frame-tail.wav"),
            "かな。",
            0,
        )

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(FakeProcessor(decoded), FakeModel(), [task])[0]

        self.assertIsNone(row.error)
        self.assertEqual(
            [(token.start_ms, token.end_ms) for token in row.tokens],
            [(560, 1120), (1129, 1130)],
        )

    def test_native_marginally_overlong_token_is_bounded_to_one_cue(self):
        decoded = [[["unused"]]]
        decoded = [[{"text": "先生", "start_time": 0.0, "end_time": 8.48}]]
        task = AlignmentTask(
            segment(owner_end_ms=11240, decode_end_ms=11240),
            Path("marginal-overlong.wav"),
            "先生",
            0,
        )

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(FakeProcessor(decoded), FakeModel(), [task])[0]

        self.assertIsNone(row.error)
        self.assertEqual(
            [(token.text, token.start_ms, token.end_ms) for token in row.tokens],
            [("先生", 0, 8000)],
        )
        self.assertEqual([cue.text for cue in row.cues], ["先生"])

    def test_native_point_timestamp_at_quantized_tail_is_repaired_in_bounds(self):
        decoded = [
            [
                {"text": "よかっ", "start_time": 1.12, "end_time": 1.6},
                {"text": "た", "start_time": 1.6, "end_time": 1.6},
            ]
        ]
        task = AlignmentTask(
            segment(owner_end_ms=1590, decode_end_ms=1590),
            Path("quantized-point-tail.wav"),
            "よかった。",
            0,
        )

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(FakeProcessor(decoded), FakeModel(), [task])[0]

        self.assertIsNone(row.error)
        self.assertEqual(
            [(token.start_ms, token.end_ms) for token in row.tokens],
            [(1120, 1589), (1589, 1590)],
        )

    def test_native_tail_point_never_borrows_past_segment_bound(self):
        decoded = [
            [
                {"text": "あ", "start_time": 1.6, "end_time": 1.6},
                {"text": "い", "start_time": 2.4, "end_time": 2.5},
            ]
        ]
        task = AlignmentTask(
            segment(owner_end_ms=1590, decode_end_ms=1590),
            Path("bounded-tail-point.wav"),
            "あい",
            0,
        )

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(FakeProcessor(decoded), FakeModel(), [task])[0]

        self.assertIsNotNone(row.error)
        self.assertIn("alignment token 1", row.error["message"])

    def test_invalid_batch_member_retries_every_task_as_ordered_singletons(self):
        class RetryProcessor(FakeProcessor):
            def prepare_forced_aligner_inputs(self, **kwargs):
                self.prepare_calls.append(kwargs)
                count = len(kwargs["audio"])
                inputs = {
                    "input_ids": self.input_ids,
                    "input_features": self.features,
                }
                if count == 2:
                    return inputs, [["一"], ["二"]]
                return inputs, [["一" if kwargs["audio"][0] == "a.wav" else "二"]]

            def decode_forced_alignment(self, **kwargs):
                self.decode_calls.append(kwargs)
                if len(kwargs["word_lists"]) == 2:
                    return [
                        [{"text": "一", "start_time": 0.0, "end_time": 0.2}],
                        [{"text": "誤", "start_time": 0.0, "end_time": 0.2}],
                    ]
                word = kwargs["word_lists"][0][0]
                return [[{"text": word, "start_time": 0.0, "end_time": 0.2}]]

        processor = RetryProcessor([])
        tasks = [
            AlignmentTask(segment("a"), Path("a.wav"), "一", 0),
            AlignmentTask(segment("b"), Path("b.wav"), "二", 0),
        ]

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            rows = align_with_retries(processor, FakeModel(), tasks)

        self.assertEqual(
            [call["audio"] for call in processor.prepare_calls],
            [["a.wav", "b.wav"], ["a.wav"], ["b.wav"]],
        )
        self.assertEqual([row.segment_id for row in rows], ["a", "b"])
        self.assertTrue(all(row.error is None for row in rows))

    def test_cardinality_mismatch_retries_complete_batch_in_order(self):
        class CardinalityProcessor(FakeProcessor):
            def prepare_forced_aligner_inputs(self, **kwargs):
                self.prepare_calls.append(kwargs)
                return (
                    {
                        "input_ids": self.input_ids,
                        "input_features": self.features,
                    },
                    [[transcript] for transcript in kwargs["transcript"]],
                )

            def decode_forced_alignment(self, **kwargs):
                self.decode_calls.append(kwargs)
                words = kwargs["word_lists"]
                if len(words) > 1:
                    words = words[:1]
                return [
                    [{"text": word, "start_time": 0.0, "end_time": 0.2} for word in row]
                    for row in words
                ]

        processor = CardinalityProcessor(
            [
                [{"text": "一", "start_time": 0.0, "end_time": 0.2}],
                [{"text": "二", "start_time": 0.0, "end_time": 0.2}],
            ]
        )
        tasks = [
            AlignmentTask(segment("a"), "a.wav", "一", 0),
            AlignmentTask(segment("b"), "b.wav", "二", 0),
        ]

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            rows = align_with_retries(processor, FakeModel(), tasks)

        self.assertEqual(
            [call["audio"] for call in processor.prepare_calls],
            [["a.wav", "b.wav"], ["a.wav"], ["b.wav"]],
        )
        self.assertTrue(all(row.error is None for row in rows))

    def test_persistent_singleton_failure_is_typed_and_keeps_exact_funasr_text(self):
        class FailingProcessor(FakeProcessor):
            def prepare_forced_aligner_inputs(self, **kwargs):
                self.prepare_calls.append(kwargs)
                if len(kwargs["audio"]) > 1 or kwargs["audio"][0] == "b.wav":
                    raise RuntimeError("alignment failed")
                return super().prepare_forced_aligner_inputs(**kwargs)

        processor = FailingProcessor(
            [[{"text": "無事", "start_time": 0.0, "end_time": 0.2}]]
        )
        tasks = [
            AlignmentTask(segment("a"), "a.wav", "無事。", 0),
            AlignmentTask(segment("b"), "b.wav", " 元の文！？ ", 0),
        ]

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            rows = align_with_retries(processor, FakeModel(), tasks)

        self.assertIsNone(rows[0].error)
        self.assertEqual(rows[1].transcript, " 元の文！？ ")
        self.assertEqual(rows[1].tokens, ())
        self.assertEqual(rows[1].cues, ())
        self.assertEqual(rows[1].error["type"], "alignment_exception")
        self.assertIn("alignment failed", rows[1].error["message"])
        self.assertEqual(rows[1].raw["phase"], "prepare")
        json.dumps(rows[1].raw, ensure_ascii=False, allow_nan=False)

    def test_persistent_validation_failure_retains_decoded_raw_evidence(self):
        for label, bad_value, expected_message in (
            ("nan", float("nan"), "finite"),
            ("huge_float", 1e308, "scaled"),
            ("huge_integer", 1 << 50000, "represented"),
            ("unknown_object", object(), "numeric"),
        ):
            with self.subTest(case=label):
                decoded = [[{"text": "正", "start_time": bad_value, "end_time": 0.2}]]
                row = None
                with patch.dict(sys.modules, {"torch": fake_torch_module()}):
                    row = align_with_retries(
                        FakeProcessor(decoded),
                        FakeModel(),
                        [AlignmentTask(segment("a"), "a.wav", "正", 0)],
                    )[0]

                self.assertEqual(row.error["type"], "alignment_validation")
                self.assertIn(expected_message, row.error["message"])
                self.assertEqual(row.raw["phase"], "normalize")
                self.assertEqual(row.raw["expected_words"], ["正"])
                json.dumps(row.raw, ensure_ascii=False, allow_nan=False)

                retained = row.raw["decoded"][0]["start_time"]
                if isinstance(bad_value, float) and not math.isfinite(bad_value):
                    self.assertEqual(
                        retained,
                        {"$type": "nonfinite_float", "value": "nan"},
                    )
                elif type(bad_value) is object:
                    self.assertEqual(retained["$type"], "builtins.object")
                    self.assertIn("object at", retained["repr"])
                elif isinstance(bad_value, int) and bad_value.bit_length() > 63:
                    self.assertEqual(retained["$type"], "large_integer")
                    self.assertEqual(int(retained["hex"], 16), bad_value)
                else:
                    self.assertEqual(retained, bad_value)

    def test_word_mismatch_retains_decoded_member_and_expected_words(self):
        class MismatchProcessor(FakeProcessor):
            def prepare_forced_aligner_inputs(self, **kwargs):
                inputs, _ = super().prepare_forced_aligner_inputs(**kwargs)
                return inputs, [["正"]]

        decoded = [[{"text": "誤", "start_time": 0.0, "end_time": 0.2}]]
        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(
                MismatchProcessor(decoded),
                FakeModel(),
                [AlignmentTask(segment("a"), "a.wav", "正", 0)],
            )[0]

        self.assertEqual(row.error["type"], "alignment_validation")
        self.assertEqual(row.raw["phase"], "normalize")
        self.assertEqual(row.raw["expected_words"], ["正"])
        self.assertEqual(row.raw["decoded"], decoded[0])
        json.dumps(row.raw, ensure_ascii=False, allow_nan=False)

    @unittest.skipUnless(_HAS_TORCH, "torch and transformers are not installed")
    def test_real_batch_feature_moves_float_and_integer_tensors_correctly(self):
        import torch
        from transformers.feature_extraction_utils import BatchFeature

        class BatchFeatureProcessor(FakeProcessor):
            def prepare_forced_aligner_inputs(self, **kwargs):
                self.prepare_calls.append(kwargs)
                return (
                    BatchFeature(
                        {
                            "input_ids": torch.tensor([[1, 2]], dtype=torch.long),
                            "input_features": torch.tensor(
                                [[[0.25, 0.5]]], dtype=torch.float32
                            ),
                            "attention_mask": torch.tensor([[1, 1]], dtype=torch.long),
                        }
                    ),
                    [["正"]],
                )

        processor = BatchFeatureProcessor(
            [[{"text": "正", "start_time": 0.0, "end_time": 0.2}]]
        )
        model = FakeModel()
        model.device = "cpu"
        row = align_with_retries(
            processor,
            model,
            [AlignmentTask(segment("a"), "a.wav", "正", 0)],
        )[0]

        self.assertIsNone(row.error)
        self.assertEqual(model.calls[0]["input_ids"].dtype, torch.long)
        self.assertEqual(model.calls[0]["input_features"].dtype, torch.bfloat16)

    def test_missing_nagisa_is_an_actionable_typed_dependency_error(self):
        class MissingNagisaProcessor:
            def prepare_forced_aligner_inputs(self, **kwargs):
                raise ImportError(
                    "Japanese forced alignment requires the `nagisa` package"
                )

        task = AlignmentTask(segment("a"), "a.wav", "はい。", 0)

        with patch.dict(sys.modules, {"torch": fake_torch_module()}):
            row = align_with_retries(MissingNagisaProcessor(), FakeModel(), [task])[0]

        self.assertEqual(row.transcript, "はい。")
        self.assertEqual(row.error["type"], "missing_dependency")
        self.assertIn("pip install nagisa", row.error["message"])
        json.dumps(row.raw, ensure_ascii=False, allow_nan=False)

    def test_factory_loads_only_explicit_local_bf16_model_on_cuda_zero(self):
        calls: list[tuple[str, str, dict[str, object]]] = []
        processor_sentinel = object()

        class Loader:
            @classmethod
            def from_pretrained(cls, path, **kwargs):
                calls.append((cls.__name__, path, kwargs))
                return processor_sentinel

        class ModelLoader:
            @classmethod
            def from_pretrained(cls, path, **kwargs):
                calls.append((cls.__name__, path, kwargs))
                return loaded_model

        class LoadedModel:
            def __init__(self):
                self.to_calls = []
                self.eval_calls = 0

            def to(self, device):
                self.to_calls.append(device)
                return self

            def eval(self):
                self.eval_calls += 1
                return self

        loaded_model = LoadedModel()
        fake_transformers = types.ModuleType("transformers")
        fake_transformers.AutoProcessor = Loader
        fake_transformers.AutoModelForTokenClassification = ModelLoader
        fake_torch = fake_torch_module()

        with patch.dict(
            sys.modules,
            {"torch": fake_torch, "transformers": fake_transformers},
        ):
            processor, model = create_aligner(
                Path("/models/qwen-aligner"), device="cuda"
            )

        self.assertIs(processor, processor_sentinel)
        self.assertIs(model, loaded_model)
        self.assertEqual(
            calls,
            [
                ("Loader", "/models/qwen-aligner", {"local_files_only": True}),
                (
                    "ModelLoader",
                    "/models/qwen-aligner",
                    {"dtype": fake_torch.bfloat16, "local_files_only": True},
                ),
            ],
        )
        self.assertEqual(loaded_model.to_calls, ["cuda:0"])
        self.assertEqual(loaded_model.eval_calls, 1)


if __name__ == "__main__":
    unittest.main()
