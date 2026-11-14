from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import soundfile as sf

from .asr_runner import create_model, generate_with_retries
from .domain import VideoRecord
from .runtime_cli import (
    RuntimeCliError,
    _append_progress,
    _expected_duration_ms,
    _read_manifest,
    _runtime_config,
    _strict_json_load,
    _verify_audio,
)
from .segments import SpeechSegment, materialize_wav_batch, normalize_vad
from .settings import resolve_device
from .smoke_cli import _append_trace, _atomic_json, _read_trace
from .vad_runner import normalize_vad_payload

DEFAULT_VAD_MODEL = Path("models/fsmn-vad")
ASR_RELATIVE_ROOT = Path("asr/corpus")
SUMMARY_RELATIVE_PATH = Path("state/corpus_asr_summary.json")
_MISSING = object()


class CorpusAsrError(RuntimeError):
    pass


class ModelPool:
    """Lazily construct at most one VAD model and one FunASR-vLLM model."""

    def __init__(
        self,
        create_vad: Callable[[], Any],
        create_asr: Callable[[], Any],
    ) -> None:
        self._create_vad = create_vad
        self._create_asr = create_asr
        self._vad: Any = _MISSING
        self._asr: Any = _MISSING

    def vad(self) -> Any:
        if self._vad is _MISSING:
            self._vad = self._create_vad()
        return self._vad

    def asr(self) -> Any:
        if self._asr is _MISSING:
            self._asr = self._create_asr()
        return self._asr


def _empty_omission(row: object) -> bool:
    return (
        isinstance(row, dict)
        and row.get("error") == {"type": "quality_gate", "flags": ["empty"]}
        and row.get("text") == ""
    )


def _quality_omission(row: object) -> bool:
    return (
        isinstance(row, dict)
        and row.get("error")
        in (
            {"type": "quality_gate", "flags": ["abnormal_repetition"]},
            {"type": "quality_gate", "flags": ["unexpected_language"]},
        )
        and isinstance(row.get("text"), str)
        and bool(row["text"].strip())
    )


def _accepted_result(row: object) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("segment_id"), str)
        and isinstance(row.get("text"), str)
        and isinstance(row.get("raw"), dict)
        and (row.get("error") is None or _empty_omission(row) or _quality_omission(row))
    )


def verified_asr(
    path: Path,
    video_id: str,
    expected_duration_ms: int,
    *,
    tolerance_ms: int = 0,
) -> bool:
    """Return true only for a complete, successful, ordered per-video result."""

    try:
        value = _strict_json_load(path)
        if not isinstance(value, dict):
            return False
        duration_ms = value.get("duration_ms")
        segment_count = value.get("segment_count")
        segments = value.get("segments")
        results = value.get("results")
        if (
            value.get("video_id") != video_id
            or type(duration_ms) is not int
            or abs(duration_ms - expected_duration_ms) > tolerance_ms
            or type(segment_count) is not int
            or segment_count < 0
            or value.get("failure_count") != 0
            or type(segments) is not list
            or type(results) is not list
            or len(segments) != segment_count
            or len(results) != segment_count
        ):
            return False
        segment_ids = [
            segment.get("segment_id") if isinstance(segment, dict) else None
            for segment in segments
        ]
        result_ids = [
            result.get("segment_id") if isinstance(result, dict) else None
            for result in results
        ]
        if (
            segment_ids != result_ids
            or len(segment_ids) != len(set(segment_ids))
            or any(
                not isinstance(segment_id, str)
                or not segment_id.startswith(f"{video_id}-seg-")
                for segment_id in segment_ids
            )
            or any(not _accepted_result(result) for result in results)
            or any(
                not isinstance(result.get("text"), str)
                or not isinstance(result.get("raw"), dict)
                for result in results
            )
        ):
            return False
        return value.get("empty_omission_count") == sum(
            _empty_omission(result) for result in results
        ) and value.get("quality_omission_count") == sum(
            _quality_omission(result) for result in results
        )
    except (OSError, RuntimeCliError, TypeError, ValueError):
        return False


def _create_vad_model(model_path: Path, device: str) -> Any:
    local_model = model_path.expanduser().resolve(strict=True)
    if not local_model.is_dir():
        raise CorpusAsrError(f"VAD model is not a directory: {local_model}")
    from funasr import AutoModel

    resolved = resolve_device(device)
    return AutoModel(
        model=str(local_model),
        device="cpu" if resolved == "mps" else resolved,
    )


def _audio_duration_ms(flac: Path) -> int:
    with sf.SoundFile(flac) as audio:
        if audio.samplerate != 16_000 or audio.channels != 1:
            raise CorpusAsrError(
                f"audio is not 16 kHz mono: {flac}: "
                f"{audio.samplerate} Hz/{audio.channels} ch"
            )
        if audio.frames <= 0:
            raise CorpusAsrError(f"audio is empty: {flac}")
        return round(audio.frames * 1000 / audio.samplerate)


def _load_or_run_vad(
    flac: Path,
    output_dir: Path,
    *,
    duration_ms: int,
    model_pool: ModelPool,
) -> list[list[int]]:
    path = output_dir / "vad.json"
    if path.exists():
        try:
            value = _strict_json_load(path)
            if (
                isinstance(value, dict)
                and value.get("duration_ms") == duration_ms
                and "raw" in value
            ):
                return normalize_vad_payload(value["raw"])
        except (OSError, TypeError, ValueError):
            pass

    raw = model_pool.vad().generate(input=str(flac))
    intervals = normalize_vad_payload(raw)
    _atomic_json(
        path,
        {"duration_ms": duration_ms, "raw": {"value": intervals}},
    )
    return intervals


def _write_segments(
    path: Path,
    duration_ms: int,
    segments: list[SpeechSegment],
) -> None:
    _atomic_json(
        path,
        {
            "duration_ms": duration_ms,
            "segment_count": len(segments),
            "segments": [asdict(segment) for segment in segments],
        },
    )


def _generate_pending(
    flac: Path,
    output_dir: Path,
    segments: list[SpeechSegment],
    *,
    batch_size: int,
    model_pool: ModelPool,
    config: Any,
    run_id: str,
    record: VideoRecord,
) -> list[dict[str, Any]]:
    trace_path = output_dir / "asr-trace.jsonl"
    completed = _read_trace(trace_path)
    pending = [
        segment
        for segment in segments
        if not _accepted_result(completed.get(segment.segment_id))
    ]
    pending.sort(
        key=lambda segment: (
            segment.decode_end_ms - segment.decode_start_ms,
            segment.index,
        )
    )
    scratch = output_dir / "scratch-wav"
    try:
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset : offset + batch_size]
            wavs = materialize_wav_batch(flac, batch, scratch)
            try:
                rows = generate_with_retries(
                    model_pool.asr(),
                    [
                        (segment.segment_id, wavs[segment.segment_id])
                        for segment in batch
                    ],
                )
                encoded = [asdict(row) for row in rows]
                _append_trace(trace_path, encoded)
                for row in encoded:
                    completed[row["segment_id"]] = row
            finally:
                for wav in wavs.values():
                    try:
                        wav.unlink()
                    except FileNotFoundError:
                        pass
            done = min(offset + len(batch), len(pending))
            _append_progress(
                config,
                {
                    "stage": "corpus_asr",
                    "event": "asr_batch_completed",
                    "run_id": run_id,
                    "video_id": record.video_id,
                    "completed": done,
                    "pending": len(pending),
                    "batch_size": len(batch),
                },
            )
            print(
                f"ASR {record.video_id} {done}/{len(pending)}",
                flush=True,
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return [completed.get(segment.segment_id, {}) for segment in segments]


def _process_record(
    config: Any,
    record: VideoRecord,
    *,
    batch_size: int,
    model_pool: ModelPool,
    run_id: str,
) -> str:
    expected_duration_ms = _expected_duration_ms(record)
    output_dir = config.work_root / ASR_RELATIVE_ROOT / record.video_id
    final_path = output_dir / "asr.json"
    tolerance_ms = config.stage_settings.extraction.duration_tolerance_ms
    if verified_asr(
        final_path,
        record.video_id,
        expected_duration_ms,
        tolerance_ms=tolerance_ms,
    ):
        _append_progress(
            config,
            {
                "stage": "corpus_asr",
                "event": "skipped_verified",
                "run_id": run_id,
                "video_id": record.video_id,
                "relative_path": record.relative_path,
            },
        )
        return "existing"

    flac = config.work_root / "audio" / f"{record.video_id}.flac"
    if not os.path.lexists(flac):
        return "missing_audio"
    _verify_audio(flac, record, tolerance_ms)
    duration_ms = _audio_duration_ms(flac)
    output_dir.mkdir(parents=True, exist_ok=True)
    _append_progress(
        config,
        {
            "stage": "corpus_asr",
            "event": "video_started",
            "run_id": run_id,
            "video_id": record.video_id,
            "relative_path": record.relative_path,
            "duration_ms": duration_ms,
        },
    )

    intervals = _load_or_run_vad(
        flac,
        output_dir,
        duration_ms=duration_ms,
        model_pool=model_pool,
    )
    segments = normalize_vad(record.video_id, intervals, duration_ms)
    _write_segments(output_dir / "segments.json", duration_ms, segments)
    rows = _generate_pending(
        flac,
        output_dir,
        segments,
        batch_size=batch_size,
        model_pool=model_pool,
        config=config,
        run_id=run_id,
        record=record,
    )
    empty_omissions = [row for row in rows if _empty_omission(row)]
    quality_omissions = [row for row in rows if _quality_omission(row)]
    failures = [row for row in rows if not _accepted_result(row)]
    _atomic_json(
        final_path,
        {
            "video_id": record.video_id,
            "relative_path": record.relative_path,
            "duration_ms": duration_ms,
            "audio_offset_ms": record.audio_offset_ms,
            "segment_count": len(segments),
            "failure_count": len(failures),
            "empty_omission_count": len(empty_omissions),
            "quality_omission_count": len(quality_omissions),
            "segments": [asdict(segment) for segment in segments],
            "results": rows,
        },
    )
    event = "video_completed" if not failures else "video_failed_quality"
    _append_progress(
        config,
        {
            "stage": "corpus_asr",
            "event": event,
            "run_id": run_id,
            "video_id": record.video_id,
            "relative_path": record.relative_path,
            "segment_count": len(segments),
            "failure_count": len(failures),
            "empty_omission_count": len(empty_omissions),
            "quality_omission_count": len(quality_omissions),
            "artifact": final_path.relative_to(config.work_root).as_posix(),
        },
    )
    return "completed" if not failures else "failed"


def _select_records(
    records: Sequence[VideoRecord],
    *,
    limit: int | None,
    video_ids: Sequence[str],
) -> tuple[VideoRecord, ...]:
    requested = tuple(video_ids)
    if len(requested) != len(set(requested)):
        raise CorpusAsrError("--video-id values must be unique")
    by_id = {record.video_id: record for record in records}
    unknown = tuple(video_id for video_id in requested if video_id not in by_id)
    if unknown:
        raise CorpusAsrError(f"unknown --video-id values: {unknown!r}")
    requested_set = set(requested)
    selected = tuple(
        record
        for record in records
        if not requested_set or record.video_id in requested_set
    )
    return selected if limit is None else selected[:limit]


def run_corpus(
    *,
    batch_size: int,
    vad_model: Path,
    limit: int | None,
    video_ids: Sequence[str],
    wait_for_audio_seconds: float,
    poll_seconds: float,
) -> dict[str, object]:
    config = _runtime_config()
    snapshot = _read_manifest(config)
    selected = _select_records(
        snapshot.records,
        limit=limit,
        video_ids=video_ids,
    )
    run_id = uuid.uuid4().hex
    model_path = vad_model if vad_model.is_absolute() else config.work_root / vad_model
    model_pool = ModelPool(
        lambda: _create_vad_model(model_path, config.stage_settings.vad.device),
        lambda: create_model(
            model_path=config.asr_model.local_path,
            device=config.stage_settings.asr.device,
            engine=config.stage_settings.asr.engine,
        ),
    )
    counts = {
        "completed": 0,
        "existing": 0,
        "no_audio": 0,
        "target_exists": 0,
        "missing_audio": 0,
        "failed": 0,
    }
    pending: list[VideoRecord] = []
    for record in selected:
        if record.target_exists:
            counts["target_exists"] += 1
            continue
        if record.audio_stream_index is None:
            counts["no_audio"] += 1
            continue
        pending.append(record)
    _append_progress(
        config,
        {
            "stage": "corpus_asr",
            "event": "batch_started",
            "run_id": run_id,
            "selected": len(selected),
            "processable": len(pending),
            "batch_size": batch_size,
            "snapshot_digest": snapshot.snapshot_digest,
            "wait_for_audio_seconds": wait_for_audio_seconds,
        },
    )

    deadline = time.monotonic() + wait_for_audio_seconds
    while pending:
        deferred: list[VideoRecord] = []
        made_progress = False
        for index, record in enumerate(pending, 1):
            try:
                status = _process_record(
                    config,
                    record,
                    batch_size=batch_size,
                    model_pool=model_pool,
                    run_id=run_id,
                )
            except KeyboardInterrupt:
                raise
            except BaseException as exc:
                counts["failed"] += 1
                made_progress = True
                _append_progress(
                    config,
                    {
                        "stage": "corpus_asr",
                        "event": "video_failed_exception",
                        "run_id": run_id,
                        "video_id": record.video_id,
                        "relative_path": record.relative_path,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                print(
                    f"corpus ASR FAILED {record.video_id}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if status == "missing_audio":
                deferred.append(record)
                continue
            counts[status] += 1
            made_progress = True
            print(
                f"corpus ASR {status} {index}/{len(pending)} {record.video_id}",
                flush=True,
            )

        pending = deferred
        if not pending or time.monotonic() >= deadline:
            break
        if not made_progress:
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(poll_seconds, remaining))

    counts["missing_audio"] = len(pending)
    for record in pending:
        _append_progress(
            config,
            {
                "stage": "corpus_asr",
                "event": "deferred_missing_audio",
                "run_id": run_id,
                "video_id": record.video_id,
                "relative_path": record.relative_path,
            },
        )
    summary: dict[str, object] = {
        "stage": "corpus_asr",
        "run_id": run_id,
        "selected": len(selected),
        **counts,
        "artifact_root": str(config.work_root / ASR_RELATIVE_ROOT),
        "progress": str(config.work_root / "state/runtime_progress.jsonl"),
    }
    _atomic_json(config.work_root / SUMMARY_RELATIVE_PATH, summary)
    _append_progress(
        config,
        {
            "stage": "corpus_asr",
            "event": (
                "batch_failed"
                if counts["failed"]
                else "batch_deferred"
                if counts["missing_audio"]
                else "batch_completed"
            ),
            **summary,
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


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 0 <= parsed < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return parsed


def _positive_float(value: str) -> float:
    parsed = _nonnegative_float(value)
    if parsed == 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _video_id(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("must be a lowercase SHA-256 video ID")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m subtitle_pipeline.corpus_asr_cli",
        description=(
            "Resumable local corpus VAD and Japanese FunASR-vLLM batch runtime"
        ),
    )
    parser.add_argument("--batch-size", type=_positive_int, default=32)
    parser.add_argument("--vad-model", type=Path, default=DEFAULT_VAD_MODEL)
    parser.add_argument("--limit", type=_nonnegative_int)
    parser.add_argument(
        "--video-id",
        dest="video_ids",
        action="append",
        type=_video_id,
        default=[],
    )
    parser.add_argument(
        "--wait-for-audio-seconds",
        type=_nonnegative_float,
        default=0.0,
        help="keep one model pair loaded while waiting for concurrent extraction",
    )
    parser.add_argument("--poll-seconds", type=_positive_float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_corpus(
            batch_size=args.batch_size,
            vad_model=args.vad_model,
            limit=args.limit,
            video_ids=args.video_ids,
            wait_for_audio_seconds=args.wait_for_audio_seconds,
            poll_seconds=args.poll_seconds,
        )
    except KeyboardInterrupt:
        print("corpus ASR interrupted", file=sys.stderr, flush=True)
        return 130
    except BaseException as exc:
        print(
            f"corpus ASR failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
