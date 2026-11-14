"""Cross-platform pipeline runner.

The Linux supervisor remains the deployment tool for the long-lived host. This
module is the portable entry point. It runs the same stage commands in order on
Linux and macOS and reads the provider settings from ``subgen.toml``.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .settings import (
    RuntimeSettings,
    SettingsError,
    TranslationProvider,
    load_settings,
)

ALIGNER_DIRECTORY = "Qwen3-ForcedAligner-0.6B-hf"


class RunnerCliError(RuntimeError):
    pass


def build_commands(
    settings: RuntimeSettings,
    *,
    python: str = sys.executable,
    limit: int | None = None,
    video_ids: Sequence[str] = (),
    skip_inventory: bool = False,
    skip_extract: bool = False,
    asr_batch_size: int = 32,
    alignment_batch_size: int = 16,
) -> list[list[str]]:
    """Return the ordered stage commands for one corpus run."""
    commands: list[list[str]] = []
    filters: list[str] = []
    if limit is not None:
        filters.extend(["--limit", str(limit)])
    for video_id in video_ids:
        filters.extend(["--video-id", video_id])

    if not skip_inventory:
        commands.append([python, "-m", "subtitle_pipeline.runtime_cli", "inventory"])
    if not skip_extract:
        commands.append(
            [
                python,
                "-m",
                "subtitle_pipeline.runtime_cli",
                "extract",
                "--workers",
                "4",
                *filters,
            ]
        )
    commands.append(
        [
            python,
            "-m",
            "subtitle_pipeline.corpus_asr_cli",
            "--batch-size",
            str(asr_batch_size),
            *filters,
        ]
    )
    commands.append(
        [
            python,
            "-m",
            "subtitle_pipeline.corpus_alignment_cli",
            "--batch-size",
            str(alignment_batch_size),
            "--model-dir",
            str(settings.model_root / ALIGNER_DIRECTORY),
            *filters,
        ]
    )

    provider = settings.translation
    if provider is None:
        raise RunnerCliError(
            "the portable runner needs a [translation] provider in subgen.toml "
            "or the SUBGEN_TRANSLATION_* variables"
        )
    for pass_name, model, revision in (
        ("primary", provider.model, provider.revision),
        ("review", provider.review_model, provider.review_revision),
    ):
        commands.append(
            [
                python,
                "-m",
                "subtitle_pipeline.corpus_translation_cli",
                "--pass",
                pass_name,
                "--endpoint",
                provider.endpoint,
                "--model",
                model,
                "--revision",
                revision,
                "--api-key-env",
                provider.api_key_env,
                *filters,
            ]
        )
    commands.append(
        [
            python,
            "-m",
            "subtitle_pipeline.corpus_publish_cli",
            "--review-model",
            provider.review_model,
            "--review-revision",
            provider.review_revision,
            *filters,
        ]
    )
    return commands


def settings_environment(settings: RuntimeSettings) -> dict[str, str]:
    """Pass the resolved settings to the stage commands."""
    environment = os.environ.copy()
    environment.update(
        {
            "SUBGEN_SOURCE_ROOT": str(settings.source_root),
            "SUBGEN_WORK_ROOT": str(settings.work_root),
            "SUBGEN_MODEL_ROOT": str(settings.model_root),
            "SUBGEN_ASR_MODEL": str(settings.asr_model_path),
            "SUBGEN_DEVICE": settings.device,
            "SUBGEN_ASR_ENGINE": settings.asr_engine,
        }
    )
    provider = settings.translation
    if provider is not None:
        environment.update(
            {
                "SUBGEN_TRANSLATION_ENDPOINT": provider.endpoint,
                "SUBGEN_TRANSLATION_MODEL": provider.model,
                "SUBGEN_TRANSLATION_REVISION": provider.revision,
                "SUBGEN_TRANSLATION_REVIEW_MODEL": provider.review_model,
                "SUBGEN_TRANSLATION_REVIEW_REVISION": provider.review_revision,
                "SUBGEN_API_KEY_ENV": provider.api_key_env,
            }
        )
    return environment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subgen",
        description="Run the subtitle pipeline on Linux or macOS",
        epilog="Use `subgen serve --help` to start the translation server.",
    )
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--model-root", type=Path)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"))
    parser.add_argument("--asr-engine", choices=("auto", "vllm", "funasr"))
    parser.add_argument("--endpoint")
    parser.add_argument("--translation-model")
    parser.add_argument("--translation-revision")
    parser.add_argument("--review-model")
    parser.add_argument("--review-revision")
    parser.add_argument("--api-key-env")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--video-id", dest="video_ids", action="append", default=[])
    parser.add_argument("--asr-batch-size", type=int, default=32)
    parser.add_argument("--alignment-batch-size", type=int, default=16)
    parser.add_argument("--skip-inventory", action="store_true")
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _settings_with_overrides(args: argparse.Namespace) -> RuntimeSettings:
    settings = load_settings()
    changes: dict[str, object] = {}
    for name, value in (
        ("source_root", args.source_root),
        ("work_root", args.work_root),
        ("model_root", args.model_root),
        ("device", args.device),
        ("asr_engine", args.asr_engine),
    ):
        if value is not None:
            changes[name] = value
    if args.endpoint or args.translation_model or args.translation_revision:
        current = settings.translation
        endpoint = args.endpoint or (current.endpoint if current else None)
        model = args.translation_model or (current.model if current else None)
        revision = args.translation_revision or (current.revision if current else None)
        api_key_env = args.api_key_env or (
            current.api_key_env if current else "OPENAI_API_KEY"
        )
        if not (endpoint and model and revision):
            raise SettingsError(
                "translation endpoint, model, and revision must be set together"
            )
        changes["translation"] = TranslationProvider(
            endpoint,
            model,
            revision,
            api_key_env,
            args.review_model or (current.review_model if current else ""),
            args.review_revision or (current.review_revision if current else ""),
        )
    return dataclasses.replace(settings, **changes)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = _settings_with_overrides(args)
        commands = build_commands(
            settings,
            python=args.python,
            limit=args.limit,
            video_ids=args.video_ids,
            skip_inventory=args.skip_inventory,
            skip_extract=args.skip_extract,
            asr_batch_size=args.asr_batch_size,
            alignment_batch_size=args.alignment_batch_size,
        )
    except (SettingsError, RunnerCliError) as exc:
        print(f"subgen run failed: {exc}", file=sys.stderr, flush=True)
        return 2
    environment = settings_environment(settings)
    for command in commands:
        print(" ".join(command), flush=True)
        if not args.dry_run:
            try:
                subprocess.run(command, check=True, env=environment)
            except KeyboardInterrupt:
                print("subgen run interrupted", file=sys.stderr, flush=True)
                return 130
            except subprocess.CalledProcessError as exc:
                print(
                    f"subgen run failed at {command[2]}: exit {exc.returncode}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
