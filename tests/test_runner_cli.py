from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from subtitle_pipeline.runner_cli import (
    RunnerCliError,
    build_commands,
    main,
    settings_environment,
)
from subtitle_pipeline.settings import RuntimeSettings, TranslationProvider


def settings(provider: TranslationProvider | None = None) -> RuntimeSettings:
    return RuntimeSettings(
        source_root=Path("/media/videos"),
        work_root=Path("/tmp/subgen"),
        model_root=Path("/tmp/subgen/models"),
        asr_model_path=Path("/tmp/subgen/models/Qwen3-ForcedAligner-0.6B-hf"),
        device="cpu",
        asr_engine="funasr",
        translation=provider,
    )


PROVIDER = TranslationProvider(
    "http://127.0.0.1:8123/v1",
    "vendor/model",
    "2026-11-13",
    review_model="vendor/reviewer",
    review_revision="review-2026-11-13",
)


class RunnerCliTests(unittest.TestCase):
    def test_commands_cover_every_stage_and_use_the_provider(self):
        commands = build_commands(settings(PROVIDER), python="python")
        self.assertEqual(len(commands), 7)
        self.assertEqual(
            commands[0],
            ["python", "-m", "subtitle_pipeline.runtime_cli", "inventory"],
        )
        self.assertIn(
            "/tmp/subgen/models/Qwen3-ForcedAligner-0.6B-hf",
            commands[3],
        )
        primary = commands[4]
        self.assertIn("--pass", primary)
        self.assertIn("primary", primary)
        self.assertIn("vendor/model", primary)
        self.assertIn("2026-11-13", primary)
        review = commands[5]
        self.assertIn("review", review)
        self.assertIn("vendor/reviewer", review)
        self.assertIn("review-2026-11-13", review)
        publish = commands[6]
        self.assertIn("--review-model", publish)
        self.assertIn("vendor/reviewer", publish)

    def test_filters_and_skips_are_applied(self):
        commands = build_commands(
            settings(PROVIDER),
            python="python",
            limit=5,
            video_ids=("a" * 64,),
            skip_inventory=True,
            skip_extract=True,
        )
        self.assertEqual(len(commands), 5)
        for command in commands:
            self.assertIn("--limit", command)
            self.assertIn("5", command)
            self.assertIn("--video-id", command)
            self.assertIn("a" * 64, command)

    def test_environment_carries_the_resolved_settings(self):
        environment = settings_environment(settings(PROVIDER))
        self.assertEqual(environment["SUBGEN_SOURCE_ROOT"], "/media/videos")
        self.assertEqual(environment["SUBGEN_WORK_ROOT"], "/tmp/subgen")
        self.assertEqual(environment["SUBGEN_DEVICE"], "cpu")
        self.assertEqual(
            environment["SUBGEN_TRANSLATION_REVIEW_MODEL"], "vendor/reviewer"
        )

    def test_missing_provider_is_an_actionable_error(self):
        with self.assertRaises(RunnerCliError):
            build_commands(settings())

    def test_dry_run_prints_without_running(self):
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            mock.patch(
                "subtitle_pipeline.runner_cli.load_settings",
                return_value=settings(PROVIDER),
            ),
        ):
            self.assertEqual(main(["--dry-run"]), 0)
        printed = output.getvalue()
        self.assertIn("subtitle_pipeline.corpus_publish_cli", printed)
        self.assertNotIn("failed", printed)


if __name__ == "__main__":
    unittest.main()
