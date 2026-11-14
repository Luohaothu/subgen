from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_pipeline.corpus_publish_cli import build_parser, main, run_corpus
from subtitle_pipeline.domain import VideoRecord


class CorpusPublishCliTests(unittest.TestCase):
    @staticmethod
    def _record(video_id: str, *, target_exists: bool = False) -> VideoRecord:
        return VideoRecord(
            video_id=video_id,
            relative_path=f"source/{video_id}.mp4",
            source_bytes=123,
            duration_ms=10_000,
            audio_stream_index=0,
            audio_offset_ms=0,
            target_relative_path=f"source/{video_id}.zh.srt",
            target_exists=target_exists,
            source_mtime_ns=456,
            probe_sha256="f" * 64,
        )

    def test_parser_defaults_to_conservative_nas_writes(self):
        args = build_parser().parse_args(["--review-revision", "r2"])

        self.assertEqual(args.workers, 1)
        self.assertEqual(args.review_model, "Qwen/Qwen3.6-27B-FP8")

    def test_publish_only_verified_local_candidates_and_never_existing_targets(self):
        records = (
            self._record("a" * 64),
            self._record("b" * 64),
            self._record("c" * 64),
            self._record("d" * 64, target_exists=True),
        )
        snapshot = SimpleNamespace(records=records, snapshot_digest="snapshot")
        config = SimpleNamespace(
            work_root=Path("/home/leo/.local/share/abc-subtitles-recovery-6"),
            source_root=Path("/home/leo/nas/Other/abc"),
        )
        published: list[tuple[Path, Path, int, str]] = []

        def candidate_exists(path: Path) -> bool:
            return ("b" * 64) not in str(path)

        def valid(*args, **kwargs) -> bool:
            return ("c" * 64) not in str(args[2])

        def publish(candidate, target, *, expected_bytes, expected_sha256):
            published.append((candidate, target, expected_bytes, expected_sha256))

        with (
            patch(
                "subtitle_pipeline.corpus_publish_cli._runtime_config",
                return_value=config,
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._read_manifest",
                return_value=snapshot,
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._candidate_exists",
                side_effect=candidate_exists,
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._final_valid",
                side_effect=valid,
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._candidate_metadata",
                return_value=(123, "e" * 64),
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._target_path",
                side_effect=lambda root, record: root / record.target_relative_path,
            ),
            patch("subtitle_pipeline.corpus_publish_cli.publish", side_effect=publish),
            patch("subtitle_pipeline.corpus_publish_cli._append_progress"),
            patch("subtitle_pipeline.corpus_publish_cli._atomic_json"),
        ):
            summary = run_corpus(
                review_model="Qwen/Qwen3.6-27B-FP8",
                review_revision="r2",
                workers=1,
                limit=None,
                video_ids=(),
            )

        self.assertEqual(len(published), 1)
        self.assertEqual(
            published[0][1],
            config.source_root / f"source/{'a' * 64}.zh.srt",
        )
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["missing_candidate"], 1)
        self.assertEqual(summary["invalid_candidate"], 1)
        self.assertEqual(summary["target_exists"], 1)
        self.assertEqual(summary["failed"], 0)

    def test_main_returns_nonzero_for_incomplete_corpus(self):
        summary = {
            "failed": 0,
            "missing_candidate": 0,
            "invalid_candidate": 1,
            "target_collision": 0,
        }
        with patch(
            "subtitle_pipeline.corpus_publish_cli.run_corpus",
            return_value=summary,
        ):
            result = main(["--review-revision", "r2"])

        self.assertEqual(result, 1)

    def test_parallel_validation_serializes_atomic_publication(self):
        records = tuple(self._record(str(index) * 64) for index in range(1, 5))
        snapshot = SimpleNamespace(records=records, snapshot_digest="snapshot")
        config = SimpleNamespace(
            work_root=Path("/home/leo/.local/share/abc-subtitles-recovery-6"),
            source_root=Path("/home/leo/nas/Other/abc"),
        )
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def publish(*args, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.01)
            finally:
                with state_lock:
                    active -= 1

        with (
            patch(
                "subtitle_pipeline.corpus_publish_cli._runtime_config",
                return_value=config,
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._read_manifest",
                return_value=snapshot,
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._candidate_exists",
                return_value=True,
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._final_valid",
                return_value=True,
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._candidate_metadata",
                return_value=(123, "e" * 64),
            ),
            patch(
                "subtitle_pipeline.corpus_publish_cli._target_path",
                side_effect=lambda root, record: root / record.target_relative_path,
            ),
            patch("subtitle_pipeline.corpus_publish_cli.publish", side_effect=publish),
            patch("subtitle_pipeline.corpus_publish_cli._append_progress"),
            patch("subtitle_pipeline.corpus_publish_cli._atomic_json"),
        ):
            summary = run_corpus(
                review_model="Qwen/Qwen3.6-27B-FP8",
                review_revision="r2",
                workers=4,
                limit=None,
                video_ids=(),
            )

        self.assertEqual(summary["completed"], 4)
        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
