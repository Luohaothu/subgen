from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_pipeline.corpus_audit_cli import (
    CorpusAuditError,
    audit_post_cleanup,
    audit_pre_cleanup,
    snapshot_protected,
)
from subtitle_pipeline.domain import VideoRecord


class CorpusAuditCliTests(unittest.TestCase):
    @staticmethod
    def _record(
        video_id: str,
        *,
        target_exists: bool = False,
        audio: bool = True,
    ) -> VideoRecord:
        return VideoRecord(
            video_id=video_id,
            relative_path=f"source/{video_id}.mp4",
            source_bytes=123,
            duration_ms=10_000,
            audio_stream_index=0 if audio else None,
            audio_offset_ms=0 if audio else None,
            target_relative_path=f"source/{video_id}.zh.srt",
            target_exists=target_exists,
            source_mtime_ns=456,
            probe_sha256="f" * 64,
        )

    def _fixture(self, directory: str):
        root = Path(directory)
        source_root = root / "source-root"
        work_root = root / "work-root"
        (source_root / "source").mkdir(parents=True)
        (work_root / "state").mkdir(parents=True)
        generated = self._record("a" * 64)
        protected = self._record("b" * 64, target_exists=True)
        no_audio = self._record("c" * 64, audio=False)
        snapshot = SimpleNamespace(
            records=(generated, protected, no_audio),
            snapshot_digest="snapshot",
        )
        config = SimpleNamespace(source_root=source_root, work_root=work_root)
        protected_target = source_root / protected.target_relative_path
        protected_target.write_text("protected\n", encoding="utf-8")
        snapshot_protected(config, snapshot)
        candidate = (
            work_root
            / "translation/corpus"
            / generated.video_id
            / f"{generated.video_id}.zh.srt"
        )
        candidate.parent.mkdir(parents=True)
        candidate.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n你好。\n\n", encoding="utf-8"
        )
        generated_target = source_root / generated.target_relative_path
        generated_target.write_bytes(candidate.read_bytes())
        summary = {
            "run_id": "publish-run",
            "selected": 3,
            "completed": 1,
            "target_exists": 1,
            "no_audio": 1,
            "target_collision": 0,
            "missing_candidate": 0,
            "invalid_candidate": 0,
            "failed": 0,
        }
        (work_root / "state/corpus_publish_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
        return config, snapshot, generated_target, protected_target

    def test_pre_and_post_cleanup_audits_bind_every_target_hash(self):
        with TemporaryDirectory() as directory:
            config, snapshot, generated_target, _ = self._fixture(directory)
            with patch(
                "subtitle_pipeline.corpus_audit_cli._final_valid", return_value=True
            ):
                audit = audit_pre_cleanup(
                    config,
                    snapshot,
                    review_model="Qwen/Qwen3.6-27B-FP8",
                    review_revision="review-revision",
                )

            self.assertEqual(audit["status"], "verified")
            self.assertEqual(audit["target_count"], 2)
            self.assertEqual(audit["protected_count"], 1)
            shutil.rmtree(config.work_root / "translation")
            final = audit_post_cleanup(config, snapshot)
            self.assertEqual(final["status"], "verified_after_cleanup")
            self.assertEqual(final["published_targets"], 2)
            self.assertEqual(final["protected_targets"], 1)

            generated_target.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(CorpusAuditError, "changed after audit"):
                audit_post_cleanup(config, snapshot)

    def test_protected_snapshot_refuses_changed_existing_subtitle(self):
        with TemporaryDirectory() as directory:
            config, snapshot, _, protected_target = self._fixture(directory)
            protected_target.write_text("changed\n", encoding="utf-8")

            with self.assertRaisesRegex(CorpusAuditError, "protected target"):
                snapshot_protected(config, snapshot)

    def test_pre_cleanup_refuses_target_that_differs_from_candidate(self):
        with TemporaryDirectory() as directory:
            config, snapshot, generated_target, _ = self._fixture(directory)
            generated_target.write_text("changed\n", encoding="utf-8")

            with (
                patch(
                    "subtitle_pipeline.corpus_audit_cli._final_valid",
                    return_value=True,
                ),
                self.assertRaisesRegex(CorpusAuditError, "differs from candidate"),
            ):
                audit_pre_cleanup(
                    config,
                    snapshot,
                    review_model="Qwen/Qwen3.6-27B-FP8",
                    review_revision="review-revision",
                )


if __name__ == "__main__":
    unittest.main()
