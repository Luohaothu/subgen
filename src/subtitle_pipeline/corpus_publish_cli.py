from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

from .corpus_translation_cli import (
    _alignment_relative,
    _draft_relative,
    _final_json_relative,
    _final_valid,
    _select_records,
    _srt_relative,
)
from .domain import VideoRecord
from .media import stash_target_path
from .publish import ExistingTargetError, PublicationError, publish
from .runtime_cli import _append_progress, _read_manifest, _runtime_config
from .smoke_cli import _atomic_json
from .translation_runner import ESCALATION_MODEL_ID

SUMMARY_RELATIVE_PATH = Path("state/corpus_publish_summary.json")


class CorpusPublishError(RuntimeError):
    pass


def _candidate_exists(path: Path) -> bool:
    return path.is_file()


def _candidate_metadata(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CorpusPublishError(
            f"cannot open local SRT candidate {path}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CorpusPublishError(f"local SRT candidate is not regular: {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        if total != metadata.st_size:
            raise CorpusPublishError(
                f"local SRT candidate changed while hashing: {path}"
            )
        return total, digest.hexdigest()
    finally:
        os.close(descriptor)


def _target_path(source_root: Path, record: VideoRecord) -> Path:
    expected = stash_target_path(Path(record.relative_path)).as_posix()
    if record.target_relative_path != expected:
        raise CorpusPublishError(
            f"inventory target is not the Stash zh sidecar for {record.relative_path}"
        )
    root = source_root.resolve(strict=True)
    candidate = root / record.target_relative_path
    try:
        parent = candidate.parent.resolve(strict=True)
        parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CorpusPublishError(
            f"target directory escapes or is unavailable: {candidate.parent}"
        ) from exc
    return parent / candidate.name


def _process_record(
    config,
    record: VideoRecord,
    *,
    review_model: str,
    review_revision: str,
    publication_lock: Lock,
) -> str:
    alignment = _alignment_relative(record.video_id)
    draft = _draft_relative(record.video_id)
    final_json = _final_json_relative(record.video_id)
    srt = _srt_relative(record.video_id)
    absolute_srt = config.work_root / srt
    if not _candidate_exists(absolute_srt):
        return "missing_candidate"
    if not _final_valid(
        config.work_root,
        alignment,
        draft,
        final_json,
        srt,
        video_id=record.video_id,
        review_model=review_model,
        review_revision=review_revision,
        video_duration_ms=record.duration_ms,
    ):
        return "invalid_candidate"
    target = _target_path(config.source_root, record)
    expected_bytes, expected_sha256 = _candidate_metadata(absolute_srt)
    try:
        with publication_lock:
            publish(
                absolute_srt,
                target,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
            )
    except ExistingTargetError:
        return "target_collision"
    return "completed"


def run_corpus(
    *,
    review_model: str,
    review_revision: str,
    workers: int,
    limit: int | None,
    video_ids: Sequence[str],
) -> dict[str, object]:
    if type(workers) is not int or not 1 <= workers <= 4:
        raise CorpusPublishError("workers must be between 1 and 4")
    config = _runtime_config()
    snapshot = _read_manifest(config)
    selected = _select_records(
        snapshot.records,
        limit=limit,
        video_ids=video_ids,
    )
    run_id = uuid.uuid4().hex
    counts = {
        "completed": 0,
        "no_audio": 0,
        "target_exists": 0,
        "target_collision": 0,
        "missing_candidate": 0,
        "invalid_candidate": 0,
        "failed": 0,
    }
    processable: list[VideoRecord] = []
    publication_lock = Lock()
    for record in selected:
        if record.target_exists:
            counts["target_exists"] += 1
        elif record.audio_stream_index is None:
            counts["no_audio"] += 1
        else:
            processable.append(record)
    _append_progress(
        config,
        {
            "stage": "corpus_publish",
            "event": "batch_started",
            "run_id": run_id,
            "selected": len(selected),
            "processable": len(processable),
            "workers": workers,
            "review_model": review_model,
            "review_revision": review_revision,
            "snapshot_digest": snapshot.snapshot_digest,
        },
    )
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _process_record,
                config,
                record,
                review_model=review_model,
                review_revision=review_revision,
                publication_lock=publication_lock,
            ): record
            for record in processable
        }
        for completed_index, future in enumerate(as_completed(futures), 1):
            record = futures[future]
            try:
                status = future.result()
            except KeyboardInterrupt:
                raise
            except BaseException as exc:
                counts["failed"] += 1
                _append_progress(
                    config,
                    {
                        "stage": "corpus_publish",
                        "event": "video_failed_exception",
                        "run_id": run_id,
                        "video_id": record.video_id,
                        "relative_path": record.relative_path,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "finalized": (
                            exc.finalized
                            if isinstance(exc, PublicationError)
                            else False
                        ),
                    },
                )
                print(
                    f"corpus publish FAILED {record.video_id}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            counts[status] += 1
            _append_progress(
                config,
                {
                    "stage": "corpus_publish",
                    "event": (
                        "video_completed"
                        if status == "completed"
                        else f"video_{status}"
                    ),
                    "run_id": run_id,
                    "video_id": record.video_id,
                    "relative_path": record.relative_path,
                    "target_relative_path": record.target_relative_path,
                },
            )
            print(
                f"corpus publish {status} {completed_index}/{len(processable)} "
                f"{record.video_id}",
                flush=True,
            )
    incomplete = (
        counts["missing_candidate"]
        + counts["invalid_candidate"]
        + counts["target_collision"]
    )
    summary: dict[str, object] = {
        "stage": "corpus_publish",
        "run_id": run_id,
        "selected": len(selected),
        **counts,
        "source_root": str(config.source_root),
        "progress": str(config.work_root / "state/runtime_progress.jsonl"),
    }
    _atomic_json(config.work_root / SUMMARY_RELATIVE_PATH, summary)
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


def _workers(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= parsed <= 4:
        raise argparse.ArgumentTypeError("must be between 1 and 4")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def _video_id(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("must be a lowercase SHA-256 video ID")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m subtitle_pipeline.corpus_publish_cli",
        description="Verify and atomically publish Stash-compatible zh SRT sidecars",
    )
    parser.add_argument("--review-model", default=ESCALATION_MODEL_ID)
    parser.add_argument("--review-revision", required=True)
    parser.add_argument("--workers", type=_workers, default=1)
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
        summary = run_corpus(
            review_model=args.review_model,
            review_revision=args.review_revision,
            workers=args.workers,
            limit=args.limit,
            video_ids=args.video_ids,
        )
    except KeyboardInterrupt:
        print("corpus publish interrupted", file=sys.stderr, flush=True)
        return 130
    except BaseException as exc:
        print(
            f"corpus publish failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    incomplete = (
        summary["missing_candidate"]
        + summary["invalid_candidate"]
        + summary["target_collision"]
    )
    return 1 if summary["failed"] or incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
