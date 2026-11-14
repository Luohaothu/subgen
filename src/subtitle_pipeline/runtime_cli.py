from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha1
from pathlib import Path

import soundfile as sf

from . import platform_io
from .config import ModelRevisions, PipelineConfig
from .domain import VideoRecord
from .media import (
    MediaExtractionError,
    extract_audio,
    probe_media,
    probe_sha256,
)
from .settings import load_settings, resolve_device
from .state import (
    InventorySnapshot,
    _open_source_descriptor,
    discover_video_paths,
    observe_video,
    validate_initial_inventory,
)

MANIFEST_RELATIVE_PATH = Path("state/runtime_inventory.json")
PROGRESS_RELATIVE_PATH = Path("state/runtime_progress.jsonl")
MODEL_REVISIONS_RELATIVE_PATH = Path("state/model_revisions.json")

_MODEL_DIRECTORIES = {
    "vad": "fsmn-vad",
    "aligner": "Qwen3-ForcedAligner-0.6B-hf",
    "primary_translation": "Qwen3.5-35B-A3B-FP8",
    "fallback_translation": "Qwen3-30B-A3B-Instruct-2507-FP8",
    "reviewer_translation": "Qwen3.6-27B-FP8",
}


class RuntimeCliError(RuntimeError):
    pass


def _read_revisions(path: Path) -> ModelRevisions:
    value = _strict_json_load(path)
    fields = {
        "vad",
        "asr",
        "aligner",
        "primary_translation",
        "fallback_translation",
        "reviewer_translation",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeCliError("model revision evidence has invalid fields")
    try:
        return ModelRevisions(**value)
    except (TypeError, ValueError) as exc:
        raise RuntimeCliError(f"model revision evidence is invalid: {exc}") from exc


def _model_revision(path: Path, label: str) -> str:
    for name in ("config.json", "configuration.json"):
        candidate = path / name
        if candidate.is_file():
            return sha1(candidate.read_bytes()).hexdigest()
    raise RuntimeCliError(
        f"{label} model is missing under {path}; "
        "download the checkpoint or set SUBGEN_MODEL_ROOT"
    )


def _translation_revision(settings: object, path: Path, label: str) -> str:
    if path.is_dir():
        return _model_revision(path, label)
    provider = getattr(settings, "translation", None)
    if provider is not None:
        identity = f"{provider.endpoint}|{provider.model}|{label}"
        return sha1(identity.encode("utf-8")).hexdigest()
    raise RuntimeCliError(
        f"{label} model is missing under {path}; "
        "download the checkpoint or configure a translation API"
    )


def _resolve_revisions(settings: object, work_root: Path) -> ModelRevisions:
    revisions_path = work_root / MODEL_REVISIONS_RELATIVE_PATH
    if revisions_path.is_file():
        return _read_revisions(revisions_path)
    model_root = settings.model_root
    return ModelRevisions(
        vad=_model_revision(model_root / _MODEL_DIRECTORIES["vad"], "VAD"),
        asr=_model_revision(settings.asr_model_path, "ASR"),
        aligner=_model_revision(model_root / _MODEL_DIRECTORIES["aligner"], "aligner"),
        primary_translation=_translation_revision(
            settings,
            model_root / _MODEL_DIRECTORIES["primary_translation"],
            "primary translation",
        ),
        fallback_translation=_translation_revision(
            settings,
            model_root / _MODEL_DIRECTORIES["fallback_translation"],
            "fallback translation",
        ),
        reviewer_translation=_translation_revision(
            settings,
            model_root / _MODEL_DIRECTORIES["reviewer_translation"],
            "reviewer translation",
            model=settings.translation.review_model if settings.translation else None,
            revision=settings.translation.review_revision
            if settings.translation
            else None,
        ),
    )


def _runtime_config() -> PipelineConfig:
    settings = load_settings()
    try:
        source_root = settings.source_root.resolve(strict=True)
        work_root = settings.work_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeCliError(f"runtime root is not available: {exc}") from exc
    if source_root != settings.source_root or work_root != settings.work_root:
        raise RuntimeCliError("runtime roots must be canonical absolute paths")
    device = resolve_device(settings.device)
    engine = settings.asr_engine
    if engine == "auto":
        engine = "vllm" if device == "cuda" else "funasr"
    revisions = _resolve_revisions(settings, work_root)
    return PipelineConfig.production(
        revisions=revisions,
        source_root=source_root,
        work_root=work_root,
        model_root=settings.model_root,
        asr_model_path=settings.asr_model_path,
        device=device,
        asr_engine=engine,
    )


def _json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeCliError(f"runtime evidence is not strict JSON: {exc}") from exc
    return (encoded + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            part,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while storing runtime manifest")
            view = view[written:]
        os.close(descriptor)
        descriptor = None
        os.replace(part, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            part.unlink()
        except FileNotFoundError:
            pass


def _append_progress(config: PipelineConfig, value: dict[str, object]) -> None:
    path = config.work_root / PROGRESS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": time.time(), **value}
    payload = _json_bytes(row)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while appending runtime progress")
            view = view[written:]
    finally:
        os.close(descriptor)


def _inventory(config: PipelineConfig) -> dict[str, object]:
    invocation_id = uuid.uuid4().hex
    _append_progress(
        config,
        {"stage": "inventory", "event": "started", "run_id": invocation_id},
    )
    paths = discover_video_paths(config)
    _append_progress(
        config,
        {
            "stage": "inventory",
            "event": "discovered",
            "run_id": invocation_id,
            "count": len(paths),
        },
    )
    records: list[VideoRecord] = []
    for index, relative_path in enumerate(paths, 1):
        try:
            record = observe_video(config, relative_path)
        except BaseException as exc:
            _append_progress(
                config,
                {
                    "stage": "inventory",
                    "event": "failed",
                    "run_id": invocation_id,
                    "index": index,
                    "relative_path": relative_path,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            raise
        records.append(record)
        _append_progress(
            config,
            {
                "stage": "inventory",
                "event": "observed",
                "run_id": invocation_id,
                "index": index,
                "count": len(paths),
                "video_id": record.video_id,
                "relative_path": record.relative_path,
                "audio_stream_index": record.audio_stream_index,
                "audio_duration_ms": record.audio_duration_ms,
                "audio_offset_ms": record.audio_offset_ms,
                "target_exists": record.target_exists,
            },
        )
        print(f"inventory {index}/{len(paths)} {relative_path}", flush=True)

    snapshot = InventorySnapshot.build(
        tuple(records), schema_version=config.schemas.inventory
    )
    validate_initial_inventory(snapshot, config)
    manifest = config.work_root / MANIFEST_RELATIVE_PATH
    _atomic_write(manifest, _json_bytes(snapshot.to_dict()))
    _append_progress(
        config,
        {
            "stage": "inventory",
            "event": "completed",
            "run_id": invocation_id,
            "count": len(snapshot.records),
            "manifest": MANIFEST_RELATIVE_PATH.as_posix(),
            "source_digest": snapshot.source_digest,
            "snapshot_digest": snapshot.snapshot_digest,
        },
    )
    return {
        "stage": "inventory",
        "count": len(snapshot.records),
        "manifest": str(manifest),
        "progress": str(config.work_root / PROGRESS_RELATIVE_PATH),
        "snapshot_digest": snapshot.snapshot_digest,
    }


def _strict_json_load(path: Path) -> object:
    def reject_constant(value: str) -> object:
        raise ValueError(f"nonstandard JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeCliError(f"cannot read runtime inventory {path}: {exc}") from exc


def _read_manifest(config: PipelineConfig) -> InventorySnapshot:
    path = config.work_root / MANIFEST_RELATIVE_PATH
    try:
        snapshot = InventorySnapshot.from_dict(_strict_json_load(path))
        validate_initial_inventory(snapshot, config)
    except RuntimeCliError:
        raise
    except (TypeError, ValueError) as exc:
        raise RuntimeCliError(f"runtime inventory is invalid: {exc}") from exc
    return snapshot


def _expected_duration_ms(record: VideoRecord) -> int:
    if record.audio_duration_ms is not None and record.audio_duration_ms > 0:
        return record.audio_duration_ms
    offset = record.audio_offset_ms
    if offset is None:
        raise RuntimeCliError(f"audio offset is missing for {record.video_id}")
    duration = record.duration_ms - offset
    if duration <= 0:
        raise RuntimeCliError(f"audio duration is invalid for {record.video_id}")
    return duration


def _verify_audio(path: Path, record: VideoRecord, tolerance_ms: int) -> int:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise RuntimeCliError(
            f"audio output cannot be inspected: {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeCliError(f"audio output is not a regular file: {path}")
    try:
        info = sf.info(path)
    except (OSError, RuntimeError) as exc:
        raise RuntimeCliError(f"audio output cannot be read: {path}: {exc}") from exc
    if info.format != "FLAC" or info.channels != 1 or info.samplerate != 16_000:
        raise RuntimeCliError(f"audio output is not 16 kHz mono FLAC: {path}")
    if info.frames <= 0:
        raise RuntimeCliError(f"audio output is empty: {path}")
    duration_ms = round(info.frames / info.samplerate * 1000)
    expected_ms = _expected_duration_ms(record)
    if abs(duration_ms - expected_ms) > tolerance_ms:
        raise RuntimeCliError(
            f"audio duration differs for {record.video_id}: "
            f"{duration_ms} ms != {expected_ms} ms (+/- {tolerance_ms} ms)"
        )
    return duration_ms


def _source_descriptor(config: PipelineConfig, record: VideoRecord) -> int:
    descriptor, generation = _open_source_descriptor(config, record.relative_path)
    expected = (
        record.source_bytes,
        record.source_mtime_ns,
        record.source_device,
        record.source_inode,
    )
    if generation != expected:
        stable_metadata = generation[:3] == expected[:3]
        verified_rebind = False
        if stable_metadata:
            try:
                probe = probe_media(
                    Path(platform_io.fd_path(descriptor)),
                    timeout_seconds=(
                        config.stage_settings.extraction.probe_timeout_seconds
                    ),
                )
                after = os.fstat(descriptor)
                verified_rebind = (
                    (after.st_size, after.st_mtime_ns, after.st_dev) == generation[:3]
                    and record.probe_sha256 is not None
                    and probe_sha256(probe) == record.probe_sha256
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                verified_rebind = False
        if not verified_rebind:
            os.close(descriptor)
            raise RuntimeCliError(
                f"source changed since inventory: {record.relative_path}"
            )
    return descriptor


def _extract_one(
    config: PipelineConfig,
    record: VideoRecord,
    invocation_id: str,
) -> str:
    common: dict[str, object] = {
        "stage": "extract",
        "run_id": invocation_id,
        "video_id": record.video_id,
        "relative_path": record.relative_path,
        "audio_stream_index": record.audio_stream_index,
        "audio_duration_ms": record.audio_duration_ms,
        "audio_offset_ms": record.audio_offset_ms,
    }
    if record.audio_stream_index is None:
        _append_progress(config, {**common, "event": "skipped_no_audio"})
        return "no_audio"

    audio_dir = config.work_root / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    final = audio_dir / f"{record.video_id}.flac"
    if os.path.lexists(final):
        duration_ms = _verify_audio(
            final,
            record,
            config.stage_settings.extraction.duration_tolerance_ms,
        )
        _append_progress(
            config,
            {**common, "event": "skipped_existing", "duration_ms": duration_ms},
        )
        return "existing"

    part = audio_dir / (
        f".{record.video_id}.{os.getpid()}.{uuid.uuid4().hex}.part.flac"
    )
    reservation = os.open(
        part,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(reservation)
    source_fd: int | None = None
    try:
        _append_progress(config, {**common, "event": "started"})
        source_fd = _source_descriptor(config, record)
        expected_ms = _expected_duration_ms(record)
        try:
            extract_audio(
                Path(platform_io.fd_path(source_fd)),
                record.audio_stream_index,
                expected_ms,
                part,
                timeout_seconds=(
                    config.stage_settings.extraction.extraction_timeout_seconds
                ),
            )
        except (OSError, MediaExtractionError) as exc:
            raise RuntimeCliError(
                f"media extraction failed for {record.relative_path}: {exc}"
            ) from exc
        duration_ms = _verify_audio(
            part,
            record,
            config.stage_settings.extraction.duration_tolerance_ms,
        )
        os.replace(part, final)
        _append_progress(
            config,
            {**common, "event": "completed", "duration_ms": duration_ms},
        )
        return "completed"
    except BaseException as exc:
        _append_progress(
            config,
            {
                **common,
                "event": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        try:
            part.unlink()
        except FileNotFoundError:
            pass


def _extract(
    config: PipelineConfig,
    *,
    workers: int,
    limit: int | None,
    video_ids: Sequence[str],
) -> dict[str, object]:
    snapshot = _read_manifest(config)
    requested = tuple(video_ids)
    if len(requested) != len(set(requested)):
        raise RuntimeCliError("--video-id values must be unique")
    records_by_id = {record.video_id: record for record in snapshot.records}
    unknown = tuple(video_id for video_id in requested if video_id not in records_by_id)
    if unknown:
        raise RuntimeCliError(f"unknown --video-id values: {unknown!r}")
    requested_set = set(requested)
    selected = tuple(
        record
        for record in snapshot.records
        if not requested_set or record.video_id in requested_set
    )
    if limit is not None:
        selected = selected[:limit]

    invocation_id = uuid.uuid4().hex
    _append_progress(
        config,
        {
            "stage": "extract",
            "event": "batch_started",
            "run_id": invocation_id,
            "workers": workers,
            "selected": len(selected),
            "limit": limit,
            "video_ids": list(requested),
            "snapshot_digest": snapshot.snapshot_digest,
        },
    )
    counts = {"completed": 0, "existing": 0, "no_audio": 0, "failed": 0}
    failures: list[str] = []
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="runtime-extract"
    ) as pool:
        future_records = {
            pool.submit(_extract_one, config, record, invocation_id): record
            for record in selected
        }
        for future in as_completed(future_records):
            record = future_records[future]
            try:
                result = future.result()
            except BaseException as exc:
                counts["failed"] += 1
                failures.append(f"{record.video_id}: {type(exc).__name__}: {exc}")
                print(
                    f"extract FAILED {record.video_id}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                counts[result] += 1
                print(f"extract {result} {record.video_id}", flush=True)
    _append_progress(
        config,
        {
            "stage": "extract",
            "event": "batch_completed" if not failures else "batch_failed",
            "run_id": invocation_id,
            **counts,
        },
    )
    if failures:
        raise RuntimeCliError(
            f"{len(failures)} extraction(s) failed; first failure: {failures[0]}"
        )
    return {
        "stage": "extract",
        "selected": len(selected),
        **counts,
        "audio_root": str(config.work_root / "audio"),
        "progress": str(config.work_root / PROGRESS_RELATIVE_PATH),
    }


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
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
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise argparse.ArgumentTypeError("must be a lowercase SHA-256 video ID")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m subtitle_pipeline.runtime_cli",
        description="Local resumable inventory and audio extraction runtime",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "inventory",
        help="sequentially inventory and validate all 580 source videos",
    )
    extract = subparsers.add_parser(
        "extract",
        help="extract selected manifest audio to local 16 kHz mono FLAC",
    )
    extract.add_argument("--workers", type=_positive_int, default=2)
    extract.add_argument("--limit", type=_nonnegative_int)
    extract.add_argument(
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
        config = _runtime_config()
        if args.command == "inventory":
            summary = _inventory(config)
        elif args.command == "extract":
            summary = _extract(
                config,
                workers=args.workers,
                limit=args.limit,
                video_ids=args.video_ids,
            )
        else:  # pragma: no cover - argparse enforces the command set.
            raise RuntimeCliError(f"unsupported command: {args.command}")
    except KeyboardInterrupt:
        print("runtime interrupted", file=sys.stderr, flush=True)
        return 130
    except BaseException as exc:
        print(
            f"runtime failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
