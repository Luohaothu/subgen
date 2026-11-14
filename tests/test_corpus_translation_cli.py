from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_pipeline.corpus_translation_cli import (
    _draft_valid,
    _final_valid,
    build_parser,
    main,
    run_corpus,
)
from subtitle_pipeline.domain import VideoRecord
from subtitle_pipeline.translation_runtime_cli import TranslationRuntimeError


class CorpusTranslationCliTests(unittest.TestCase):
    @staticmethod
    def _record(
        video_id: str,
        *,
        target_exists: bool = False,
        audio_stream_index: int | None = 0,
    ) -> VideoRecord:
        return VideoRecord(
            video_id=video_id,
            relative_path=f"source/{video_id}.mp4",
            source_bytes=123,
            duration_ms=10_000,
            audio_stream_index=audio_stream_index,
            audio_offset_ms=0 if audio_stream_index is not None else None,
            target_relative_path=f"source/{video_id}.zh.srt",
            target_exists=target_exists,
            source_mtime_ns=456,
            probe_sha256="f" * 64,
        )

    def test_parser_defaults_to_parallel_two_pass_runtime(self):
        args = build_parser().parse_args(
            [
                "--pass",
                "primary",
                "--endpoint",
                "http://127.0.0.1:8008/v1",
                "--revision",
                "r1",
            ]
        )

        self.assertEqual(args.workers, 8)
        self.assertEqual(args.commit_size, 20)
        self.assertEqual(args.max_tokens, 2048)
        self.assertEqual(args.timeout, 600.0)
        self.assertFalse(args.parallel_review_blocks)

    def test_parser_accepts_workers_up_to_service_sequence_limit(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "--pass",
                "review",
                "--endpoint",
                "http://127.0.0.1:8123",
                "--revision",
                "r1",
                "--workers",
                "36",
            ]
        )

        self.assertEqual(args.workers, 36)
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--pass",
                    "review",
                    "--endpoint",
                    "http://127.0.0.1:8123",
                    "--revision",
                    "r1",
                    "--workers",
                    "37",
                ]
            )

    def test_primary_filters_inventory_and_records_resumable_statuses(self):
        records = (
            self._record("a" * 64),
            self._record("b" * 64),
            self._record("c" * 64, target_exists=True),
            self._record("d" * 64, audio_stream_index=None),
        )
        snapshot = SimpleNamespace(records=records, snapshot_digest="snapshot")
        config = SimpleNamespace(
            work_root=Path("/home/leo/.local/share/abc-subtitles-recovery-6")
        )
        calls: list[str] = []

        def alignment_exists(path: Path) -> bool:
            return ("a" * 64) in str(path)

        def primary(_alignment: Path, **kwargs):
            calls.append(kwargs["video_id"])
            return {"cue_count": 12, "block_count": 2}

        with (
            patch(
                "subtitle_pipeline.corpus_translation_cli._runtime_config",
                return_value=config,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._read_manifest",
                return_value=snapshot,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._alignment_exists",
                side_effect=alignment_exists,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._draft_valid",
                return_value=False,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli.run_primary",
                side_effect=primary,
            ),
            patch("subtitle_pipeline.corpus_translation_cli._append_progress"),
            patch("subtitle_pipeline.corpus_translation_cli._atomic_json"),
        ):
            summary = run_corpus(
                pass_name="primary",
                endpoint="http://127.0.0.1:8008/v1",
                model="primary-model",
                revision="r1",
                workers=2,
                commit_size=20,
                max_tokens=2048,
                timeout=600.0,
                api_key="local",
                limit=None,
                video_ids=(),
                client_factory=lambda: object(),
            )

        self.assertEqual(calls, ["a" * 64])
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["missing_alignment"], 1)
        self.assertEqual(summary["target_exists"], 1)
        self.assertEqual(summary["no_audio"], 1)
        self.assertEqual(summary["failed"], 0)

    def test_review_skips_verified_final_and_defers_missing_draft(self):
        records = (self._record("e" * 64), self._record("f" * 64))
        snapshot = SimpleNamespace(records=records, snapshot_digest="snapshot")
        config = SimpleNamespace(
            work_root=Path("/home/leo/.local/share/abc-subtitles-recovery-6")
        )

        def draft_exists(path: Path) -> bool:
            return ("e" * 64) in str(path)

        with (
            patch(
                "subtitle_pipeline.corpus_translation_cli._runtime_config",
                return_value=config,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._read_manifest",
                return_value=snapshot,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._alignment_exists",
                return_value=True,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._draft_exists",
                side_effect=draft_exists,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._final_valid",
                return_value=True,
            ),
            patch("subtitle_pipeline.corpus_translation_cli.run_review") as review,
            patch("subtitle_pipeline.corpus_translation_cli._append_progress"),
            patch("subtitle_pipeline.corpus_translation_cli._atomic_json"),
        ):
            summary = run_corpus(
                pass_name="review",
                endpoint="http://127.0.0.1:8008/v1",
                model="review-model",
                revision="r2",
                workers=2,
                commit_size=20,
                max_tokens=2048,
                timeout=600.0,
                api_key="local",
                limit=None,
                video_ids=(),
                client_factory=lambda: object(),
            )

        review.assert_not_called()
        self.assertEqual(summary["existing"], 1)
        self.assertEqual(summary["missing_draft"], 1)

    def test_review_shares_worker_budget_across_parallel_block_executors(self):
        records = (self._record("7" * 64), self._record("8" * 64))
        snapshot = SimpleNamespace(records=records, snapshot_digest="snapshot")
        config = SimpleNamespace(
            work_root=Path("/home/leo/.local/share/abc-subtitles-recovery-6")
        )

        with (
            patch(
                "subtitle_pipeline.corpus_translation_cli._runtime_config",
                return_value=config,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._read_manifest",
                return_value=snapshot,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._alignment_exists",
                return_value=True,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._draft_exists",
                return_value=True,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._final_valid",
                return_value=False,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli.run_review",
                return_value={"cue_count": 1, "block_count": 1},
            ) as review,
            patch("subtitle_pipeline.corpus_translation_cli._append_progress"),
            patch("subtitle_pipeline.corpus_translation_cli._atomic_json"),
        ):
            summary = run_corpus(
                pass_name="review",
                endpoint="http://127.0.0.1:8123/v1",
                model="review-model",
                revision="r2",
                workers=2,
                commit_size=20,
                max_tokens=2048,
                timeout=600.0,
                api_key="local",
                limit=None,
                video_ids=(),
                client_factory=lambda: object(),
                parallel_review_blocks=True,
            )

        self.assertEqual(summary["completed"], 2)
        self.assertEqual(review.call_count, 2)
        executors = {
            id(call.kwargs["block_executor"]) for call in review.call_args_list
        }
        self.assertEqual(len(executors), 1)
        self.assertNotIn(id(None), executors)

    def test_primary_retries_length_exhaustion_with_smaller_blocks(self):
        record = self._record("1" * 64)
        snapshot = SimpleNamespace(records=(record,), snapshot_digest="snapshot")
        config = SimpleNamespace(
            work_root=Path("/home/leo/.local/share/abc-subtitles-recovery-6")
        )
        commit_sizes: list[int] = []

        def primary(_alignment: Path, **kwargs):
            commit_sizes.append(kwargs["commit_size"])
            if len(commit_sizes) == 1:
                raise TranslationRuntimeError(
                    "primary block blocked: invalid_json; finish_reason=length"
                )
            return {"cue_count": 12, "block_count": 3}

        with (
            patch(
                "subtitle_pipeline.corpus_translation_cli._runtime_config",
                return_value=config,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._read_manifest",
                return_value=snapshot,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._alignment_exists",
                return_value=True,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._draft_valid",
                return_value=False,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli.run_primary",
                side_effect=primary,
            ),
            patch("subtitle_pipeline.corpus_translation_cli._append_progress"),
            patch("subtitle_pipeline.corpus_translation_cli._atomic_json"),
        ):
            summary = run_corpus(
                pass_name="primary",
                endpoint="http://127.0.0.1:8008/v1",
                model="primary-model",
                revision="r1",
                workers=1,
                commit_size=20,
                max_tokens=2048,
                timeout=600.0,
                api_key="local",
                limit=None,
                video_ids=(),
                client_factory=lambda: object(),
            )

        self.assertEqual(commit_sizes, [20, 1])
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["failed"], 0)

    def test_primary_retries_quality_exhaustion_with_progressively_smaller_blocks(self):
        record = self._record("2" * 64)
        snapshot = SimpleNamespace(records=(record,), snapshot_digest="snapshot")
        config = SimpleNamespace(
            work_root=Path("/home/leo/.local/share/abc-subtitles-recovery-6")
        )
        commit_sizes: list[int] = []

        def primary(_alignment: Path, **kwargs):
            commit_sizes.append(kwargs["commit_size"])
            if len(commit_sizes) < 3:
                raise TranslationRuntimeError(
                    "primary block 4 blocked after attempts: quality_gate"
                )
            return {"cue_count": 12, "block_count": 3}

        with (
            patch(
                "subtitle_pipeline.corpus_translation_cli._runtime_config",
                return_value=config,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._read_manifest",
                return_value=snapshot,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._alignment_exists",
                return_value=True,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._draft_valid",
                return_value=False,
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli.run_primary",
                side_effect=primary,
            ),
            patch("subtitle_pipeline.corpus_translation_cli._append_progress"),
            patch("subtitle_pipeline.corpus_translation_cli._atomic_json"),
        ):
            summary = run_corpus(
                pass_name="primary",
                endpoint="http://127.0.0.1:8008/v1",
                model="primary-model",
                revision="r1",
                workers=1,
                commit_size=20,
                max_tokens=2048,
                timeout=600.0,
                api_key="local",
                limit=None,
                video_ids=(),
                client_factory=lambda: object(),
            )

        self.assertEqual(commit_sizes, [20, 10, 5])
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["failed"], 0)

    def test_primary_accepts_verified_length_fallback_draft(self):
        verified = SimpleNamespace(
            primary_model="primary-model",
            primary_revision="r1",
            commit_size=1,
        )
        with (
            patch(
                "subtitle_pipeline.corpus_translation_cli._load_alignment",
                return_value=(),
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._verified_draft",
                return_value=verified,
            ) as verified_draft,
        ):
            self.assertTrue(
                _draft_valid(
                    Path("/work"),
                    Path("alignment.json"),
                    Path("draft.json"),
                    video_id="1" * 64,
                    model="primary-model",
                    revision="r1",
                    commit_size=20,
                )
            )
            self.assertEqual(
                verified_draft.call_args.args[0],
                Path("/work/draft.json"),
            )
            verified.commit_size = 10
            self.assertTrue(
                _draft_valid(
                    Path("/work"),
                    Path("alignment.json"),
                    Path("draft.json"),
                    video_id="1" * 64,
                    model="primary-model",
                    revision="r1",
                    commit_size=20,
                )
            )
            verified.commit_size = 5
            self.assertTrue(
                _draft_valid(
                    Path("/work"),
                    Path("alignment.json"),
                    Path("draft.json"),
                    video_id="1" * 64,
                    model="primary-model",
                    revision="r1",
                    commit_size=20,
                )
            )

    def test_final_validation_resolves_artifacts_under_work_root(self):
        with (
            patch(
                "subtitle_pipeline.corpus_translation_cli._load_alignment",
                return_value=(),
            ),
            patch(
                "subtitle_pipeline.corpus_translation_cli._verified_draft",
                return_value=SimpleNamespace(),
            ) as verified_draft,
            patch(
                "subtitle_pipeline.corpus_translation_cli._strict_json",
                return_value={},
            ) as strict_json,
        ):
            self.assertFalse(
                _final_valid(
                    Path("/work"),
                    Path("alignment.json"),
                    Path("draft.json"),
                    Path("final.json"),
                    Path("final.zh.srt"),
                    video_id="1" * 64,
                    review_model="review-model",
                    review_revision="r2",
                    video_duration_ms=10_000,
                )
            )

        self.assertEqual(verified_draft.call_args.args[0], Path("/work/draft.json"))
        self.assertEqual(strict_json.call_args.args[0], Path("/work/final.json"))

    def test_main_returns_nonzero_for_incomplete_corpus(self):
        summary = {
            "failed": 0,
            "missing_alignment": 1,
            "missing_draft": 0,
        }
        with patch(
            "subtitle_pipeline.corpus_translation_cli.run_corpus",
            return_value=summary,
        ):
            result = main(
                [
                    "--pass",
                    "primary",
                    "--endpoint",
                    "http://127.0.0.1:8123",
                    "--revision",
                    "r1",
                ]
            )

        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
