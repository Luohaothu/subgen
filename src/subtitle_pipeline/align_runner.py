from __future__ import annotations

import math
import os
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from subtitle_pipeline.alignment import (
    MAX_CUE_MS,
    AlignedToken,
    JapaneseCue,
    build_cues,
    restore_transcript_text,
    validate_tokens,
)
from subtitle_pipeline.segments import SpeechSegment

WavPath = str | os.PathLike[str]


@dataclass(frozen=True)
class AlignmentTask:
    segment: SpeechSegment
    wav_path: WavPath
    transcript: str
    audio_offset_ms: int


@dataclass(frozen=True)
class _OfficialAlignerBackend:
    aligner: Any


@dataclass(frozen=True)
class AlignmentResult:
    segment_id: str
    transcript: str
    tokens: tuple[AlignedToken, ...]
    cues: tuple[JapaneseCue, ...]
    raw: object = None
    error: dict[str, Any] | None = None


class AlignmentValidationError(ValueError):
    def __init__(self, message: str, raw: object = None) -> None:
        super().__init__(message)
        self.raw = raw


class MissingAlignmentDependency(ImportError):
    def __init__(self, message: str, raw: object = None) -> None:
        super().__init__(message)
        self.raw = raw


class AlignmentExecutionError(RuntimeError):
    def __init__(self, message: str, raw: object = None) -> None:
        super().__init__(message)
        self.raw = raw


def _funasr_fallback_error(message: str, evidence: object) -> AlignmentValidationError:
    return AlignmentValidationError(f"FunASR timestamp fallback: {message}", evidence)


def _safe_repr(value: object) -> str:
    try:
        return repr(value)
    except Exception as exc:
        return f"<unrepresentable {type(value).__name__}: {type(exc).__name__}>"


def _json_snapshot(value: object, seen: set[int] | None = None) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if -(2**63) <= value < 2**63:
            return value
        return {"$type": "large_integer", "hex": hex(value)}
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            label = "nan"
        elif value > 0:
            label = "inf"
        else:
            label = "-inf"
        return {"$type": "nonfinite_float", "value": label}
    if isinstance(value, bytes):
        return {"$type": "bytes", "hex": value.hex()}

    if seen is None:
        seen = set()
    if isinstance(value, (Mapping, list, tuple)):
        object_id = id(value)
        if object_id in seen:
            return {"$type": "cycle", "repr": _safe_repr(value)}
        seen.add(object_id)
        try:
            if isinstance(value, Mapping):
                result: dict[str, object] = {}
                for key, item in value.items():
                    text_key = key if isinstance(key, str) else _safe_repr(key)
                    unique_key = text_key
                    suffix = 2
                    while unique_key in result:
                        unique_key = f"{text_key}#{suffix}"
                        suffix += 1
                    result[unique_key] = _json_snapshot(item, seen)
                return result
            return [_json_snapshot(item, seen) for item in value]
        finally:
            seen.remove(object_id)

    value_type = type(value)
    return {
        "$type": f"{value_type.__module__}.{value_type.__qualname__}",
        "repr": _safe_repr(value),
    }


def _load_transformers_aligner(
    model_path: str,
    torch: Any,
    dtype: Any,
    device: str,
) -> tuple[Any, Any]:
    from transformers import AutoModelForTokenClassification, AutoProcessor

    processor = AutoProcessor.from_pretrained(model_path, local_files_only=True)
    model = AutoModelForTokenClassification.from_pretrained(
        model_path,
        dtype=dtype,
        local_files_only=True,
    )
    model = model.to(device)
    model.eval()
    return processor, model


def create_aligner(
    model_dir: WavPath,
    *,
    device: str = "auto",
    dtype: Any | None = None,
) -> tuple[Any, Any]:
    import torch

    from .settings import resolve_device

    resolved = resolve_device(device)
    target = "cuda:0" if resolved == "cuda" else resolved
    if dtype is None:
        if resolved == "cuda":
            dtype = torch.bfloat16
        elif resolved == "mps":
            dtype = torch.float16
        else:
            dtype = torch.float32
    model_path = os.fspath(model_dir)
    if resolved == "cuda":
        try:
            from qwen_asr import Qwen3ForcedAligner
        except ImportError:
            return _load_transformers_aligner(model_path, torch, dtype, target)
        aligner = Qwen3ForcedAligner.from_pretrained(
            model_path,
            dtype=dtype,
            device_map=target,
            local_files_only=True,
        )
        model = getattr(aligner, "model", None)
        if model is not None and callable(getattr(model, "eval", None)):
            model.eval()
        return _OfficialAlignerBackend(aligner), None
    return _load_transformers_aligner(model_path, torch, dtype, target)


def _prepare(
    processor: Any,
    tasks: list[AlignmentTask],
) -> tuple[Any, list[list[str]]]:
    try:
        return processor.prepare_forced_aligner_inputs(
            audio=[os.fspath(task.wav_path) for task in tasks],
            transcript=[task.transcript for task in tasks],
            language=["Japanese"] * len(tasks),
        )
    except ImportError as exc:
        if "nagisa" in str(exc).lower():
            raise MissingAlignmentDependency(
                "Japanese forced alignment requires nagisa; "
                "install it with: pip install nagisa"
            ) from exc
        raise


def _move_inputs(inputs: Any, model: Any, torch: Any, dtype: Any) -> dict[str, Any]:
    if not hasattr(inputs, "items"):
        raise AlignmentValidationError("processor inputs must be a mapping")
    device = getattr(model, "device", "cuda:0")
    moved: dict[str, Any] = {}
    for name, value in inputs.items():
        if hasattr(value, "to"):
            kwargs: dict[str, Any] = {"device": device}
            is_floating = getattr(value, "is_floating_point", None)
            if callable(is_floating) and is_floating():
                kwargs["dtype"] = dtype
            value = value.to(**kwargs)
        moved[name] = value
    if "input_ids" not in moved:
        raise AlignmentValidationError("processor inputs are missing input_ids")
    return moved


def _milliseconds(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AlignmentValidationError(f"decoded {field} must be numeric seconds")
    try:
        seconds = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise AlignmentValidationError(
            f"decoded {field} cannot be represented as numeric seconds"
        ) from exc
    if not math.isfinite(seconds):
        raise AlignmentValidationError(f"decoded {field} must be finite")
    scaled = seconds * 1000
    if not math.isfinite(scaled):
        raise AlignmentValidationError(
            f"decoded {field} scaled milliseconds must be finite"
        )
    return round(scaled)


_NATIVE_QUANTIZATION_TOLERANCE_MS = 640
_MAX_REPLACEMENT_MATCH_CHARS = 8


@dataclass(frozen=True)
class _TimestampPatternAtom:
    text: str | None
    start_row: int
    end_row: int


def _timestamp_pattern_atoms(token_texts: list[str]) -> list[_TimestampPatternAtom]:
    atoms: list[_TimestampPatternAtom] = []
    for row_index, token in enumerate(token_texts):
        for character in token:
            if character == "\ufffd":
                if (
                    atoms
                    and atoms[-1].text is None
                    and atoms[-1].end_row >= row_index - 1
                ):
                    previous = atoms[-1]
                    atoms[-1] = _TimestampPatternAtom(
                        None,
                        previous.start_row,
                        row_index,
                    )
                else:
                    atoms.append(_TimestampPatternAtom(None, row_index, row_index))
                continue
            normalized = unicodedata.normalize("NFKC", character)
            if not normalized:
                raise AlignmentValidationError(
                    "timestamp token normalization produced empty text"
                )
            atoms.extend(
                _TimestampPatternAtom(value, row_index, row_index)
                for value in normalized
            )
    return atoms


def _normalized_transcript_units(transcript: str) -> list[tuple[str, int]]:
    units: list[tuple[str, int]] = []
    for original_index, character in enumerate(transcript):
        normalized = unicodedata.normalize("NFKC", character)
        if not normalized:
            raise AlignmentValidationError(
                "transcript normalization produced empty text"
            )
        units.extend((value, original_index) for value in normalized)
    return units


def _replacement_timestamp_rows(
    transcript: str,
    timestamps: list[object],
    token_texts: list[str],
) -> tuple[list[object], list[tuple[str, int, int]], dict[str, object]]:
    """Repair only uniquely matched U+FFFD spans in FunASR token timestamps."""

    atoms = _timestamp_pattern_atoms(token_texts)
    target_units = _normalized_transcript_units(transcript)
    matches: list[tuple[int, int, list[tuple[int, int, int]]]] = []
    best_score: tuple[int, int] | None = None

    def is_start_boundary(index: int) -> bool:
        return index == 0 or atoms[index - 1].end_row < atoms[index].start_row

    def is_end_boundary(index: int) -> bool:
        return index == len(atoms) or atoms[index - 1].end_row < atoms[index].start_row

    def visit(
        atom_index: int,
        target_index: int,
        mapping: list[tuple[int, int, int]],
        start_atom: int,
    ) -> None:
        nonlocal best_score
        if target_index == len(target_units):
            if atom_index > start_atom and is_end_boundary(atom_index):
                literal_count = sum(
                    atoms[index].text is not None for index, _, _ in mapping
                )
                replacement_count = sum(
                    target_end - target_start
                    for index, target_start, target_end in mapping
                    if atoms[index].text is None
                )
                score = (literal_count, -replacement_count)
                match = (
                    atoms[start_atom].start_row,
                    atoms[atom_index - 1].end_row,
                    list(mapping),
                )
                if best_score is None or score > best_score:
                    best_score = score
                    matches.clear()
                    matches.append(match)
                elif score == best_score and len(matches) < 2:
                    matches.append(match)
            return
        if atom_index == len(atoms):
            return

        atom = atoms[atom_index]
        if atom.text is not None:
            if atom.text == target_units[target_index][0]:
                mapping.append((atom_index, target_index, target_index + 1))
                visit(atom_index + 1, target_index + 1, mapping, start_atom)
                mapping.pop()
            return

        maximum = min(
            _MAX_REPLACEMENT_MATCH_CHARS,
            len(target_units) - target_index,
        )
        for consumed in range(1, maximum + 1):
            mapping.append((atom_index, target_index, target_index + consumed))
            visit(atom_index + 1, target_index + consumed, mapping, start_atom)
            mapping.pop()

    for start_atom in range(len(atoms)):
        if not is_start_boundary(start_atom):
            continue
        visit(start_atom, 0, [], start_atom)

    if not matches:
        raise AlignmentValidationError(
            "replacement-token repair did not match the transcript"
        )
    if len(matches) != 1:
        raise AlignmentValidationError("replacement-token repair is ambiguous")

    start_row, end_row, mapping = matches[0]
    character_spans: list[list[int] | None] = [None] * len(transcript)
    for atom_index, target_start, target_end in mapping:
        atom = atoms[atom_index]
        for target_index in range(target_start, target_end):
            original_index = target_units[target_index][1]
            span = character_spans[original_index]
            if span is None:
                character_spans[original_index] = [atom.start_row, atom.end_row]
            else:
                span[0] = min(span[0], atom.start_row)
                span[1] = max(span[1], atom.end_row)
    if any(span is None for span in character_spans):
        raise AlignmentValidationError(
            "replacement-token repair did not cover the complete transcript"
        )

    grouped: list[list[object]] = []
    for index, span in enumerate(character_spans):
        assert span is not None
        if grouped and span[0] <= grouped[-1][2]:
            grouped[-1][0] = str(grouped[-1][0]) + transcript[index]
            grouped[-1][2] = max(int(grouped[-1][2]), span[1])
        else:
            grouped.append([transcript[index], span[0], span[1]])

    rows: list[tuple[str, int, int]] = []
    for text, first_row, last_row in grouped:
        first = timestamps[int(first_row)]
        last = timestamps[int(last_row)]
        if not isinstance(first, Mapping) or not isinstance(last, Mapping):
            raise AlignmentValidationError("timestamp rows must be mappings")
        rows.append(
            (
                str(text),
                _milliseconds(first.get("start_time"), "start_time"),
                _milliseconds(last.get("end_time"), "end_time"),
            )
        )

    return (
        timestamps[start_row : end_row + 1],
        rows,
        {
            "kind": "unique_replacement_character_match",
            "selected_start_row": start_row,
            "selected_end_row": end_row,
        },
    )


def _repair_native_tokens(
    tokens: tuple[AlignedToken, ...],
    duration_ms: int,
) -> tuple[AlignedToken, ...]:
    """Repair only bounded Qwen timestamp-grid artifacts before strict validation."""

    rows = [[token.text, token.start_ms, token.end_ms] for token in tokens]
    for row in rows:
        start_ms = row[1]
        end_ms = row[2]
        if duration_ms < start_ms <= duration_ms + _NATIVE_QUANTIZATION_TOLERANCE_MS:
            row[1] = duration_ms
        if duration_ms < end_ms <= duration_ms + _NATIVE_QUANTIZATION_TOLERANCE_MS:
            row[2] = duration_ms
        span_ms = row[2] - row[1]
        if MAX_CUE_MS < span_ms <= MAX_CUE_MS + _NATIVE_QUANTIZATION_TOLERANCE_MS:
            row[2] = row[1] + MAX_CUE_MS

    index = 0
    while index < len(rows):
        anchor_ms = rows[index][1]
        if rows[index][2] != anchor_ms or not 0 <= anchor_ms <= duration_ms:
            index += 1
            continue

        run_end = index + 1
        while (
            run_end < len(rows)
            and rows[run_end][1] == anchor_ms
            and rows[run_end][2] == anchor_ms
        ):
            run_end += 1
        width_ms = run_end - index
        if width_ms > _NATIVE_QUANTIZATION_TOLERANCE_MS:
            index = run_end
            continue

        previous_end = rows[index - 1][2] if index else 0
        next_start = (
            min(rows[run_end][1], duration_ms) if run_end < len(rows) else duration_ms
        )
        repaired_start: int | None = None

        # Preserve neighbouring positive spans whenever the timestamp grid left
        # enough room on either side of the point-valued run.
        right_start = max(anchor_ms, previous_end)
        if right_start + width_ms <= next_start:
            repaired_start = right_start

        # A point can legitimately share a quantized boundary with an adjacent
        # word. Borrow only the minimum number of milliseconds needed, leaving
        # the adjacent word with a strictly positive span.
        if repaired_start is None and run_end < len(rows):
            borrowed_start = max(anchor_ms, previous_end)
            borrowed_end = borrowed_start + width_ms
            if borrowed_end <= duration_ms and borrowed_end < rows[run_end][2]:
                rows[run_end][1] = borrowed_end
                repaired_start = borrowed_start
        if repaired_start is None:
            left_end = min(anchor_ms, next_start)
            if previous_end <= left_end - width_ms:
                repaired_start = left_end - width_ms
        if repaired_start is None and index:
            borrowed_end = min(anchor_ms, next_start)
            borrowed_start = borrowed_end - width_ms
            if rows[index - 1][1] < borrowed_start:
                rows[index - 1][2] = borrowed_start
                repaired_start = borrowed_start

        if repaired_start is not None:
            for offset, row_index in enumerate(range(index, run_end)):
                rows[row_index][1] = repaired_start + offset
                rows[row_index][2] = repaired_start + offset + 1
        index = run_end

    return tuple(
        AlignedToken(text, start_ms, end_ms) for text, start_ms, end_ms in rows
    )


def alignment_from_funasr_timestamps(
    task: AlignmentTask,
    timestamps: object,
    *,
    primary_alignment: AlignmentResult | None = None,
) -> AlignmentResult:
    """Build a strict alignment from FunASR's source-token timestamps.

    This is intentionally conservative: the transcript must occur exactly once in
    the timestamp token stream and both ends must coincide with token boundaries.
    """

    evidence: dict[str, object] = {
        "phase": "funasr_timestamp_fallback",
        "segment_id": task.segment.segment_id,
        "timestamps": _json_snapshot(timestamps),
    }
    if primary_alignment is not None:
        evidence["primary_alignment"] = {
            "raw": _json_snapshot(primary_alignment.raw),
            "error": _json_snapshot(primary_alignment.error),
        }
    try:
        if not task.transcript:
            raise _funasr_fallback_error("transcript must be non-empty", evidence)
        if not isinstance(timestamps, list) or not timestamps:
            raise _funasr_fallback_error(
                "timestamps must be a non-empty list", evidence
            )

        token_texts: list[str] = []
        boundaries: dict[int, int] = {0: 0}
        cursor = 0
        for index, row in enumerate(timestamps):
            if not isinstance(row, Mapping):
                raise _funasr_fallback_error(
                    f"timestamp row {index} must be a mapping", evidence
                )
            token = row.get("token")
            if not isinstance(token, str) or not token:
                raise _funasr_fallback_error(
                    f"timestamp row {index} has invalid token text", evidence
                )
            token_texts.append(token)
            cursor += len(token)
            boundaries[cursor] = index + 1

        raw_text = "".join(token_texts)
        text_start = raw_text.find(task.transcript)
        if text_start >= 0:
            if raw_text.find(task.transcript, text_start + 1) >= 0:
                raise _funasr_fallback_error(
                    "transcript occurs more than once in the timestamp token stream",
                    evidence,
                )
            text_end = text_start + len(task.transcript)
            if text_start not in boundaries or text_end not in boundaries:
                raise _funasr_fallback_error(
                    "transcript does not coincide with timestamp token boundaries",
                    evidence,
                )

            selected = timestamps[boundaries[text_start] : boundaries[text_end]]
            rows = [
                (
                    token_texts[index],
                    _milliseconds(row.get("start_time"), "start_time"),
                    _milliseconds(row.get("end_time"), "end_time"),
                )
                for index, row in enumerate(
                    timestamps[boundaries[text_start] : boundaries[text_end]],
                    start=boundaries[text_start],
                )
            ]
        elif "\ufffd" in raw_text:
            try:
                selected, rows, repair_evidence = _replacement_timestamp_rows(
                    task.transcript,
                    timestamps,
                    token_texts,
                )
            except AlignmentValidationError as exc:
                raise _funasr_fallback_error(str(exc), evidence) from exc
            evidence["token_repair"] = repair_evidence
        else:
            raise _funasr_fallback_error(
                "transcript is not present in the timestamp token stream", evidence
            )
        duration_ms = task.segment.decode_end_ms - task.segment.decode_start_ms
        tail_tolerance_ms = 80
        if rows and duration_ms < rows[-1][2] <= duration_ms + tail_tolerance_ms:
            text, start_ms, _ = rows[-1]
            rows[-1] = (text, start_ms, duration_ms)

        tokens = tuple(AlignedToken(*row) for row in rows)
        if "".join(token.text for token in tokens) != task.transcript:
            raise _funasr_fallback_error(
                "selected timestamp tokens do not preserve the transcript", evidence
            )
        validate_tokens(tokens, duration_ms, transcript=task.transcript)
        cues = tuple(
            build_cues(
                tokens,
                segment=task.segment,
                audio_offset_ms=task.audio_offset_ms,
            )
        )
    except AlignmentValidationError:
        raise
    except ValueError as exc:
        raise _funasr_fallback_error(str(exc), evidence) from exc

    evidence["timestamps"] = _json_snapshot(selected)
    return AlignmentResult(
        task.segment.segment_id,
        task.transcript,
        tokens,
        cues,
        evidence,
        None,
    )


def _normalize_member(
    task: AlignmentTask,
    expected_words: list[str],
    decoded: object,
) -> AlignmentResult:
    evidence = {
        "phase": "normalize",
        "segment_id": task.segment.segment_id,
        "expected_words": expected_words,
        "decoded": decoded,
    }
    try:
        if not isinstance(expected_words, list) or any(
            not isinstance(word, str) or not word for word in expected_words
        ):
            raise AlignmentValidationError(
                "processor returned an invalid alignment word list"
            )
        if not isinstance(decoded, list):
            raise AlignmentValidationError("decoder member must be a list")
        if any(not isinstance(item, dict) for item in decoded):
            raise AlignmentValidationError("decoder items must be dictionaries")
        decoded_words = [item.get("text") for item in decoded]
        if decoded_words != expected_words:
            raise AlignmentValidationError(
                "decoded words differ from processor words: "
                f"{decoded_words!r} != {expected_words!r}"
            )

        try:
            restored = restore_transcript_text(task.transcript, expected_words)
        except ValueError as exc:
            raise AlignmentValidationError(str(exc)) from exc

        tokens = tuple(
            AlignedToken(
                text,
                _milliseconds(item.get("start_time"), "start_time"),
                _milliseconds(item.get("end_time"), "end_time"),
            )
            for text, item in zip(restored, decoded)
        )
        duration_ms = task.segment.decode_end_ms - task.segment.decode_start_ms
        tokens = _repair_native_tokens(tokens, duration_ms)
        validate_tokens(tokens, duration_ms, transcript=task.transcript)
        cues = tuple(
            build_cues(
                tokens,
                segment=task.segment,
                audio_offset_ms=task.audio_offset_ms,
            )
        )
    except AlignmentValidationError as exc:
        if exc.raw is None:
            exc.raw = evidence
        raise
    except ValueError as exc:
        raise AlignmentValidationError(str(exc), evidence) from exc

    return AlignmentResult(
        task.segment.segment_id,
        task.transcript,
        tokens,
        cues,
        _json_snapshot(decoded),
        None,
    )


def _align_official_batch(
    aligner: Any,
    tasks: list[AlignmentTask],
) -> list[AlignmentResult]:
    segment_ids = [task.segment.segment_id for task in tasks]
    try:
        members = aligner.align(
            audio=[os.fspath(task.wav_path) for task in tasks],
            text=[task.transcript for task in tasks],
            language=["Japanese"] * len(tasks),
        )
    except ImportError as exc:
        if "nagisa" in str(exc).lower():
            raise MissingAlignmentDependency(
                "Japanese forced alignment requires nagisa; "
                "install it with: pip install nagisa"
            ) from exc
        raise
    except Exception as exc:
        raise AlignmentExecutionError(
            str(exc),
            {
                "phase": "official_align",
                "segment_ids": segment_ids,
                "exception_type": type(exc).__name__,
            },
        ) from exc
    if not isinstance(members, list) or len(members) != len(tasks):
        count = len(members) if isinstance(members, list) else 0
        raise AlignmentValidationError(
            f"official aligner returned {count} results for {len(tasks)} inputs",
            {
                "phase": "official_align",
                "segment_ids": segment_ids,
                "members": _json_snapshot(members),
            },
        )

    normalized: list[AlignmentResult] = []
    for task, member in zip(tasks, members, strict=True):
        try:
            items = list(member)
        except (TypeError, ValueError) as exc:
            raise AlignmentValidationError(
                "official aligner result is not iterable",
                {
                    "phase": "official_align",
                    "segment_id": task.segment.segment_id,
                    "member": _json_snapshot(member),
                },
            ) from exc
        decoded = [
            {
                "text": getattr(item, "text", None),
                "start_time": getattr(item, "start_time", None),
                "end_time": getattr(item, "end_time", None),
            }
            for item in items
        ]
        expected_words = [row["text"] for row in decoded]
        normalized.append(_normalize_member(task, expected_words, decoded))
    return normalized


def _model_dtype(model: Any, torch: Any) -> Any:
    dtype = getattr(model, "dtype", None)
    return dtype if dtype is not None else torch.bfloat16


def _align_batch(
    processor: Any,
    model: Any,
    tasks: list[AlignmentTask],
) -> list[AlignmentResult]:
    if isinstance(processor, _OfficialAlignerBackend):
        if model is not None:
            raise AlignmentValidationError(
                "official aligner backend must not include a legacy model"
            )
        return _align_official_batch(processor.aligner, tasks)

    import torch

    segment_ids = [task.segment.segment_id for task in tasks]
    try:
        inputs, word_lists = _prepare(processor, tasks)
    except MissingAlignmentDependency as exc:
        if exc.raw is None:
            exc.raw = {"phase": "prepare", "segment_ids": segment_ids}
        raise
    except Exception as exc:
        raise AlignmentExecutionError(
            str(exc),
            {
                "phase": "prepare",
                "segment_ids": segment_ids,
                "exception_type": type(exc).__name__,
            },
        ) from exc
    if not isinstance(word_lists, list) or len(word_lists) != len(tasks):
        count = len(word_lists) if isinstance(word_lists, list) else 0
        raise AlignmentValidationError(
            f"processor returned {count} word lists for {len(tasks)} inputs",
            {
                "phase": "prepare",
                "segment_ids": segment_ids,
                "word_lists": word_lists,
            },
        )
    try:
        moved = _move_inputs(inputs, model, torch, _model_dtype(model, torch))
    except AlignmentValidationError as exc:
        if exc.raw is None:
            exc.raw = {
                "phase": "move_inputs",
                "segment_ids": segment_ids,
                "word_lists": word_lists,
            }
        raise
    try:
        with torch.inference_mode():
            outputs = model(**moved)
    except Exception as exc:
        raise AlignmentExecutionError(
            str(exc),
            {
                "phase": "forward",
                "segment_ids": segment_ids,
                "word_lists": word_lists,
                "exception_type": type(exc).__name__,
            },
        ) from exc
    if not hasattr(outputs, "logits"):
        raise AlignmentValidationError(
            "aligner output is missing logits",
            {
                "phase": "forward",
                "segment_ids": segment_ids,
                "word_lists": word_lists,
            },
        )
    try:
        decoded = processor.decode_forced_alignment(
            logits=outputs.logits,
            input_ids=moved["input_ids"],
            word_lists=word_lists,
            timestamp_token_id=model.config.timestamp_token_id,
        )
    except Exception as exc:
        raise AlignmentExecutionError(
            str(exc),
            {
                "phase": "decode",
                "segment_ids": segment_ids,
                "word_lists": word_lists,
                "exception_type": type(exc).__name__,
            },
        ) from exc
    if not isinstance(decoded, list) or len(decoded) != len(tasks):
        count = len(decoded) if isinstance(decoded, list) else 0
        raise AlignmentValidationError(
            f"decoder returned {count} results for {len(tasks)} inputs",
            {
                "phase": "decode",
                "segment_ids": segment_ids,
                "word_lists": word_lists,
                "decoded": decoded,
            },
        )
    return [
        _normalize_member(task, words, member)
        for task, words, member in zip(tasks, word_lists, decoded)
    ]


def _error_result(task: AlignmentTask, exc: Exception) -> AlignmentResult:
    if isinstance(exc, MissingAlignmentDependency):
        error_type = "missing_dependency"
    elif isinstance(exc, AlignmentValidationError):
        error_type = "alignment_validation"
    else:
        error_type = "alignment_exception"
    raw = getattr(exc, "raw", None)
    if raw is None:
        raw = {
            "phase": "exception",
            "segment_id": task.segment.segment_id,
            "exception_type": type(exc).__name__,
        }
    return AlignmentResult(
        task.segment.segment_id,
        task.transcript,
        (),
        (),
        _json_snapshot(raw),
        {"type": error_type, "message": str(exc)},
    )


def _retry_one(
    processor: Any,
    model: Any,
    task: AlignmentTask,
) -> AlignmentResult:
    try:
        return _align_batch(processor, model, [task])[0]
    except Exception as exc:
        return _error_result(task, exc)


def align_with_retries(
    processor: Any,
    model: Any,
    tasks: list[AlignmentTask],
) -> list[AlignmentResult]:
    if not tasks:
        return []
    try:
        return _align_batch(processor, model, tasks)
    except Exception:
        return [_retry_one(processor, model, task) for task in tasks]
