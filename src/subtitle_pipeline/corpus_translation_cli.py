from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .alignment import timestamp_digest
from .domain import VideoRecord
from .runtime_cli import _append_progress, _read_manifest, _runtime_config
from .settings import load_settings
from .smoke_cli import _atomic_json
from .srt import Cue, render_srt, validate_candidate
from .translation import (
    build_translation_blocks,
    glossary_digest,
    load_simplifier,
    source_cue_digest,
)
from .translation_runner import ESCALATION_MODEL_ID, PRIMARY_MODEL_ID
from .translation_runtime_cli import (
    FINAL_SCHEMA,
    OpenAICompatibleClient,
    TranslationRuntimeError,
    _artifact_from_dict,
    _load_alignment,
    _strict_json,
    _verified_draft,
    run_primary,
    run_review,
)

TRANSLATION_RELATIVE_ROOT = Path("translation/corpus")
PRIMARY_SUMMARY = Path("state/corpus_translation_primary_summary.json")
REVIEW_SUMMARY = Path("state/corpus_translation_review_summary.json")
LENGTH_FALLBACK_COMMIT_SIZE = 1
QUALITY_FALLBACK_COMMIT_SIZE = 10
FINAL_QUALITY_FALLBACK_COMMIT_SIZE = 5


class CorpusTranslationError(RuntimeError):
    pass


def _alignment_relative(video_id: str) -> Path:
    return Path("asr/corpus") / video_id / "alignment.json"


def _output_relative(video_id: str) -> Path:
    return TRANSLATION_RELATIVE_ROOT / video_id


def _draft_relative(video_id: str) -> Path:
    return _output_relative(video_id) / f"{video_id}.draft.translation.json"


def _final_json_relative(video_id: str) -> Path:
    return _output_relative(video_id) / f"{video_id}.translation.json"


def _srt_relative(video_id: str) -> Path:
    return _output_relative(video_id) / f"{video_id}.zh.srt"


def _alignment_exists(path: Path) -> bool:
    return path.is_file()


def _draft_exists(path: Path) -> bool:
    return path.is_file()


def _draft_valid(
    work_root: Path,
    alignment: Path,
    draft: Path,
    *,
    video_id: str,
    model: str,
    revision: str,
    commit_size: int,
) -> bool:
    try:
        cues = _load_alignment(alignment, work_root)
        verified = _verified_draft(work_root / draft, cues, video_id=video_id)
        return (
            verified.primary_model == model
            and verified.primary_revision == revision
            and verified.commit_size
            in {
                commit_size,
                min(commit_size, QUALITY_FALLBACK_COMMIT_SIZE),
                min(commit_size, FINAL_QUALITY_FALLBACK_COMMIT_SIZE),
                min(commit_size, LENGTH_FALLBACK_COMMIT_SIZE),
            }
        )
    except (OSError, TypeError, ValueError, TranslationRuntimeError):
        return False


def _final_valid(
    work_root: Path,
    alignment: Path,
    draft_path: Path,
    final_path: Path,
    srt_path: Path,
    *,
    video_id: str,
    review_model: str,
    review_revision: str,
    video_duration_ms: int,
) -> bool:
    try:
        cues = _load_alignment(alignment, work_root)
        draft = _verified_draft(work_root / draft_path, cues, video_id=video_id)
        value = _strict_json(work_root / final_path)
        required = {
            "schema_version",
            "video_id",
            "cue_count",
            "timestamp_digest",
            "source_digest",
            "glossary_digest",
            "draft_json",
            "primary",
            "review",
            "blocks",
            "cues",
        }
        if not isinstance(value, dict) or set(value) != required:
            return False
        expected_timestamp = timestamp_digest(cues)
        expected_source = source_cue_digest(cues)
        expected_glossary = glossary_digest(draft.glossary.glossary)
        if (
            value["schema_version"] != FINAL_SCHEMA
            or value["video_id"] != video_id
            or value["cue_count"] != len(cues)
            or value["timestamp_digest"] != expected_timestamp
            or value["source_digest"] != expected_source
            or value["glossary_digest"] != expected_glossary
            or value["primary"]
            != {"model": draft.primary_model, "revision": draft.primary_revision}
            or value["review"] != {"model": review_model, "revision": review_revision}
        ):
            return False
        blocks = build_translation_blocks(cues, commit_size=draft.commit_size)
        raw_blocks = value["blocks"]
        raw_cues = value["cues"]
        if (
            type(raw_blocks) is not list
            or len(raw_blocks) != len(blocks)
            or type(raw_cues) is not list
            or len(raw_cues) != len(cues)
        ):
            return False
        final_texts: dict[str, str] = {}
        for block, primary, raw_block in zip(
            blocks, draft.artifacts, raw_blocks, strict=True
        ):
            if not isinstance(raw_block, dict) or set(raw_block) != {
                "commit_ids",
                "primary_artifact",
                "review_artifact",
            }:
                return False
            if raw_block["commit_ids"] != list(block.commit_ids):
                return False
            stored_primary = _artifact_from_dict(raw_block["primary_artifact"])
            if stored_primary != primary:
                return False
            review = _artifact_from_dict(raw_block["review_artifact"])
            if (
                review.model_id != review_model
                or review.revision != review_revision
                or review.pass_name not in {"review", "escalation"}
                or review.status not in {"review_ok", "escalation_ok"}
                or review.timestamp_digest != timestamp_digest(block.commit_cues)
                or review.source_digest != expected_source
                or review.glossary_digest != expected_glossary
                or tuple(cue_id for cue_id, _ in review.texts) != block.commit_ids
            ):
                return False
            final_texts.update(review.texts)
        rendered_cues: list[Cue] = []
        for source, row in zip(cues, raw_cues, strict=True):
            expected = {
                "id": source.cue_id,
                "start_ms": source.start_ms,
                "end_ms": source.end_ms,
                "ja": source.text,
                "draft_zh": draft.texts[source.cue_id],
                "zh": final_texts.get(source.cue_id),
            }
            if not isinstance(row, dict) or row != expected:
                return False
            rendered_cues.append(
                Cue(source.cue_id, source.start_ms, source.end_ms, row["zh"])
            )
        expected_srt = render_srt(tuple(rendered_cues))
        if (work_root / srt_path).read_text(encoding="utf-8") != expected_srt:
            return False
        return not validate_candidate(
            expected_srt,
            cues,
            video_duration_ms,
            simplifier=load_simplifier(),
        )
    except (OSError, TypeError, ValueError, KeyError, TranslationRuntimeError):
        return False


def _select_records(
    records: Sequence[VideoRecord],
    *,
    limit: int | None,
    video_ids: Sequence[str],
) -> tuple[VideoRecord, ...]:
    requested = tuple(video_ids)
    if len(requested) != len(set(requested)):
        raise CorpusTranslationError("--video-id values must be unique")
    by_id = {record.video_id: record for record in records}
    unknown = tuple(video_id for video_id in requested if video_id not in by_id)
    if unknown:
        raise CorpusTranslationError(f"unknown --video-id values: {unknown!r}")
    requested_set = set(requested)
    selected = tuple(
        record
        for record in records
        if not requested_set or record.video_id in requested_set
    )
    return selected if limit is None else selected[:limit]


def _process_record(
    record: VideoRecord,
    *,
    pass_name: str,
    work_root: Path,
    model: str,
    revision: str,
    commit_size: int,
    max_tokens: int,
    client_factory: Callable[[], Any],
    review_block_executor: Executor | None = None,
) -> tuple[str, dict[str, object] | None]:
    alignment = _alignment_relative(record.video_id)
    absolute_alignment = work_root / alignment
    if not _alignment_exists(absolute_alignment):
        return "missing_alignment", None
    output = _output_relative(record.video_id)
    draft = _draft_relative(record.video_id)
    absolute_draft = work_root / draft
    if pass_name == "primary":
        if _draft_valid(
            work_root,
            alignment,
            draft,
            video_id=record.video_id,
            model=model,
            revision=revision,
            commit_size=commit_size,
        ):
            return "existing", None
        primary_kwargs = {
            "video_id": record.video_id,
            "output_dir": output,
            "model": model,
            "revision": revision,
            "max_tokens": max_tokens,
            "work_root": work_root,
        }
        active_commit_size = commit_size
        while True:
            try:
                result = run_primary(
                    alignment,
                    client=client_factory(),
                    commit_size=active_commit_size,
                    **primary_kwargs,
                )
                break
            except TranslationRuntimeError as exc:
                diagnostic = str(exc)
                if "primary block" not in diagnostic or "blocked" not in diagnostic:
                    raise
                if (
                    "invalid_json" in diagnostic
                    and "finish_reason=length" in diagnostic
                ):
                    fallback_commit_size = min(
                        active_commit_size, LENGTH_FALLBACK_COMMIT_SIZE
                    )
                    fallback_reason = "length"
                elif active_commit_size > QUALITY_FALLBACK_COMMIT_SIZE:
                    fallback_commit_size = QUALITY_FALLBACK_COMMIT_SIZE
                    fallback_reason = "quality"
                elif active_commit_size > FINAL_QUALITY_FALLBACK_COMMIT_SIZE:
                    fallback_commit_size = FINAL_QUALITY_FALLBACK_COMMIT_SIZE
                    fallback_reason = "quality"
                else:
                    # A commit-size change invalidates the checkpoint layout.
                    # Keep a quality failure at five cues resumable for a
                    # targeted correction instead of silently rebuilding the
                    # entire video one cue at a time.  Only an actual length
                    # truncation is allowed to cross that boundary above.
                    raise
                if fallback_commit_size == active_commit_size:
                    raise
                print(
                    f"PRIMARY {fallback_reason} fallback "
                    f"video={record.video_id} commit_size={fallback_commit_size}",
                    flush=True,
                )
                active_commit_size = fallback_commit_size
        return "completed", result
    if not _draft_exists(absolute_draft):
        return "missing_draft", None
    final_path = _final_json_relative(record.video_id)
    srt_path = _srt_relative(record.video_id)
    if _final_valid(
        work_root,
        alignment,
        draft,
        final_path,
        srt_path,
        video_id=record.video_id,
        review_model=model,
        review_revision=revision,
        video_duration_ms=record.duration_ms,
    ):
        return "existing", None
    result = run_review(
        alignment,
        draft,
        video_id=record.video_id,
        output_dir=output,
        client=client_factory(),
        model=model,
        revision=revision,
        max_tokens=max_tokens,
        video_duration_ms=record.duration_ms,
        work_root=work_root,
        block_executor=review_block_executor,
    )
    return "completed", result


def run_corpus(
    *,
    pass_name: str,
    endpoint: str,
    model: str,
    revision: str,
    workers: int,
    commit_size: int,
    max_tokens: int,
    timeout: float,
    api_key: str,
    limit: int | None,
    video_ids: Sequence[str],
    client_factory: Callable[[], Any] | None = None,
    parallel_review_blocks: bool = False,
) -> dict[str, object]:
    if pass_name not in {"primary", "review"}:
        raise CorpusTranslationError("pass_name must be primary or review")
    if type(workers) is not int or not 1 <= workers <= 36:
        raise CorpusTranslationError("workers must be between 1 and 36")
    if type(parallel_review_blocks) is not bool:
        raise CorpusTranslationError("parallel_review_blocks must be a boolean")
    if parallel_review_blocks and pass_name != "review":
        raise CorpusTranslationError(
            "parallel review blocks are only valid for the review pass"
        )
    config = _runtime_config()
    snapshot = _read_manifest(config)
    selected = _select_records(
        snapshot.records,
        limit=limit,
        video_ids=video_ids,
    )
    run_id = uuid.uuid4().hex
    stage = f"corpus_translation_{pass_name}"
    factory = client_factory or (
        lambda: OpenAICompatibleClient(endpoint, api_key=api_key, timeout=timeout)
    )
    counts = {
        "completed": 0,
        "existing": 0,
        "no_audio": 0,
        "target_exists": 0,
        "missing_alignment": 0,
        "missing_draft": 0,
        "failed": 0,
    }
    processable: list[VideoRecord] = []
    for record in selected:
        if record.target_exists:
            counts["target_exists"] += 1
            continue
        if record.audio_stream_index is None:
            counts["no_audio"] += 1
        else:
            processable.append(record)
    _append_progress(
        config,
        {
            "stage": stage,
            "event": "batch_started",
            "run_id": run_id,
            "selected": len(selected),
            "processable": len(processable),
            "workers": workers,
            "parallel_review_blocks": parallel_review_blocks,
            "model": model,
            "revision": revision,
            "snapshot_digest": snapshot.snapshot_digest,
        },
    )
    with ExitStack() as stack:
        review_block_executor = (
            stack.enter_context(ThreadPoolExecutor(max_workers=workers))
            if parallel_review_blocks
            else None
        )
        executor = stack.enter_context(ThreadPoolExecutor(max_workers=workers))
        futures = {
            executor.submit(
                _process_record,
                record,
                pass_name=pass_name,
                work_root=config.work_root,
                model=model,
                revision=revision,
                commit_size=commit_size,
                max_tokens=max_tokens,
                client_factory=factory,
                review_block_executor=review_block_executor,
            ): record
            for record in processable
        }
        for completed_index, future in enumerate(as_completed(futures), 1):
            record = futures[future]
            try:
                status, result = future.result()
            except KeyboardInterrupt:
                raise
            except BaseException as exc:
                counts["failed"] += 1
                _append_progress(
                    config,
                    {
                        "stage": stage,
                        "event": "video_failed_exception",
                        "run_id": run_id,
                        "video_id": record.video_id,
                        "relative_path": record.relative_path,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                print(
                    f"corpus translation {pass_name} FAILED {record.video_id}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            counts[status] += 1
            event = (
                f"video_{status}"
                if status in {"completed", "existing"}
                else f"deferred_{status}"
            )
            _append_progress(
                config,
                {
                    "stage": stage,
                    "event": event,
                    "run_id": run_id,
                    "video_id": record.video_id,
                    "relative_path": record.relative_path,
                    **(result or {}),
                },
            )
            print(
                f"corpus translation {pass_name} {status} "
                f"{completed_index}/{len(processable)} {record.video_id}",
                flush=True,
            )
    incomplete = counts["missing_alignment"] + counts["missing_draft"]
    summary: dict[str, object] = {
        "stage": stage,
        "run_id": run_id,
        "selected": len(selected),
        **counts,
        "artifact_root": str(config.work_root / TRANSLATION_RELATIVE_ROOT),
        "progress": str(config.work_root / "state/runtime_progress.jsonl"),
    }
    summary_path = PRIMARY_SUMMARY if pass_name == "primary" else REVIEW_SUMMARY
    _atomic_json(config.work_root / summary_path, summary)
    _append_progress(
        config,
        {
            **summary,
            "event": (
                "batch_failed"
                if counts["failed"]
                else "batch_incomplete"
                if incomplete
                else "batch_completed"
            ),
        },
    )
    return summary


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _workers(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 36:
        raise argparse.ArgumentTypeError("must be at most 36")
    return parsed


def _commit_size(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 120:
        raise argparse.ArgumentTypeError("must be at most 120")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 0 < parsed < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _video_id(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("must be a lowercase SHA-256 video ID")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m subtitle_pipeline.corpus_translation_cli",
        description="Parallel resumable full-corpus two-pass subtitle translation",
    )
    parser.add_argument(
        "--pass", dest="pass_name", choices=("primary", "review"), required=True
    )
    parser.add_argument("--endpoint")
    parser.add_argument("--model")
    parser.add_argument("--revision")
    parser.add_argument("--workers", type=_workers, default=8)
    parser.add_argument(
        "--parallel-review-blocks",
        action="store_true",
        help=(
            "share the worker budget across independent blocks so review "
            "concurrency remains full during the final-video tail"
        ),
    )
    parser.add_argument("--commit-size", type=_commit_size, default=20)
    parser.add_argument("--max-tokens", type=_positive_int, default=2048)
    parser.add_argument("--timeout", type=_positive_float, default=600.0)
    parser.add_argument("--api-key-env")
    parser.add_argument("--limit", type=_nonnegative_int)
    parser.add_argument(
        "--video-id",
        dest="video_ids",
        action="append",
        type=_video_id,
        default=[],
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        provider = load_settings().translation
        endpoint = args.endpoint or (provider.endpoint if provider else None)
        revision = args.revision or (provider.revision if provider else None)
        model = args.model or (
            provider.model
            if provider is not None
            else PRIMARY_MODEL_ID
            if args.pass_name == "primary"
            else ESCALATION_MODEL_ID
        )
        api_key_env = args.api_key_env or (
            provider.api_key_env if provider is not None else "OPENAI_API_KEY"
        )
        if not endpoint or not revision:
            raise CorpusTranslationError(
                "translation requires --endpoint and --revision, "
                "or a [translation] section in subgen.toml"
            )
        summary = run_corpus(
            pass_name=args.pass_name,
            endpoint=endpoint,
            model=model,
            revision=revision,
            workers=args.workers,
            commit_size=args.commit_size,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            api_key=os.environ.get(api_key_env, "local"),
            limit=args.limit,
            video_ids=args.video_ids,
            parallel_review_blocks=args.parallel_review_blocks,
        )
    except KeyboardInterrupt:
        print("corpus translation interrupted", file=sys.stderr, flush=True)
        return 130
    except BaseException as exc:
        print(
            f"corpus translation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    incomplete = summary["missing_alignment"] + summary["missing_draft"]
    return 1 if summary["failed"] or incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
