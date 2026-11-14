import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from subtitle_pipeline.corpus_asr_cli import (
    ModelPool,
    _load_or_run_vad,
    build_parser,
    verified_asr,
)


class CorpusAsrCliTests(unittest.TestCase):
    def test_model_pool_creates_each_model_only_once(self):
        calls = {"vad": 0, "asr": 0}

        def create_vad():
            calls["vad"] += 1
            return object()

        def create_asr():
            calls["asr"] += 1
            return object()

        pool = ModelPool(create_vad, create_asr)

        self.assertIs(pool.vad(), pool.vad())
        self.assertIs(pool.asr(), pool.asr())
        self.assertEqual(calls, {"vad": 1, "asr": 1})

    def test_verified_asr_requires_complete_success_in_segment_order(self):
        video_id = "a" * 64
        payload = {
            "video_id": video_id,
            "duration_ms": 1234,
            "segment_count": 2,
            "failure_count": 0,
            "empty_omission_count": 1,
            "quality_omission_count": 0,
            "segments": [
                {"segment_id": f"{video_id}-seg-00000000"},
                {"segment_id": f"{video_id}-seg-00000001"},
            ],
            "results": [
                {
                    "segment_id": f"{video_id}-seg-00000000",
                    "text": "こんにちは",
                    "raw": {"text": "こんにちは"},
                    "error": None,
                },
                {
                    "segment_id": f"{video_id}-seg-00000001",
                    "text": "",
                    "raw": {"text": ""},
                    "error": {"type": "quality_gate", "flags": ["empty"]},
                },
            ],
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "asr.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(verified_asr(path, video_id, 1234))

            payload["results"].reverse()
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(verified_asr(path, video_id, 1234))

    def test_verified_asr_accepts_audited_post_retry_quality_omission(self):
        video_id = "b" * 64
        segment_id = f"{video_id}-seg-00000000"
        payload = {
            "video_id": video_id,
            "duration_ms": 1234,
            "segment_count": 1,
            "failure_count": 0,
            "empty_omission_count": 0,
            "quality_omission_count": 1,
            "segments": [{"segment_id": segment_id}],
            "results": [
                {
                    "segment_id": segment_id,
                    "text": "はぁ" * 40,
                    "raw": {"text": "はぁ" * 40},
                    "error": {
                        "type": "quality_gate",
                        "flags": ["abnormal_repetition"],
                    },
                }
            ],
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "asr.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(verified_asr(path, video_id, 1234))

            payload["quality_omission_count"] = 0
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(verified_asr(path, video_id, 1234))

            path.write_text('{"video_id":', encoding="utf-8")
            self.assertFalse(verified_asr(path, video_id, 1234))

    def test_parser_preserves_bf16_vllm_batching_defaults(self):
        args = build_parser().parse_args([])

        self.assertEqual(args.batch_size, 32)
        self.assertEqual(args.vad_model, Path("models/fsmn-vad"))
        self.assertIsNone(args.limit)

    def test_vad_artifact_resumes_without_reloading_model(self):
        class FakeVad:
            def generate(self, *, input):
                return [{"value": [[10, 20], [30, 40]]}]

        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            first_pool = ModelPool(lambda: FakeVad(), lambda: object())
            first = _load_or_run_vad(
                Path("audio.flac"),
                output_dir,
                duration_ms=100,
                model_pool=first_pool,
            )
            second_pool = ModelPool(
                lambda: self.fail("VAD model must not reload for valid artifact"),
                lambda: object(),
            )
            second = _load_or_run_vad(
                Path("audio.flac"),
                output_dir,
                duration_ms=100,
                model_pool=second_pool,
            )

        self.assertEqual(first, [[10, 20], [30, 40]])
        self.assertEqual(second, first)


if __name__ == "__main__":
    unittest.main()
