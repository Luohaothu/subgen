from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .align_runner import (
    AlignmentResult,
    AlignmentTask,
    align_with_retries,
    alignment_from_funasr_timestamps,
    create_aligner,
)
from .alignment import (
    AlignedToken,
    JapaneseCue,
    build_cues,
    timestamp_digest,
    validate_tokens,
)
from .asr import trim_abnormal_repetition_edges, trim_corrupted_repetition_edges
from .corpus_asr_cli import verified_asr
from .domain import VideoRecord
from .runtime_cli import (
    RuntimeCliError,
    _append_progress,
    _atomic_write,
    _expected_duration_ms,
    _read_manifest,
    _runtime_config,
    _strict_json_load,
    _verify_audio,
)
from .segments import SpeechSegment, materialize_wav_batch
from .smoke_cli import _nonlexical_alignment_omission

DEFAULT_ALIGNER_MODEL = Path("models/Qwen3-ForcedAligner-0.6B-hf")
ASR_RELATIVE_ROOT = Path("asr/corpus")
SUMMARY_RELATIVE_PATH = Path("state/corpus_alignment_summary.json")
ALIGNMENT_SCHEMA_VERSION = "corpus-alignment-v1"
MIN_DENSE_TRANSCRIPT_CHARS = 64
MAX_TIMESTAMP_FREE_CHARS_PER_SECOND = 12
_MISSING = object()
_REVIEWED_WINDOW_ALIGNMENT_OVERRIDES = {
    "d1d431be669328ac93db8f7d1297d8d31a2f2dcfd1feb46cedbdd37077be72f3-seg-00000436": {
        "transcript": "やばい。",
        "decode_start_ms": 2_534_530,
        "decode_end_ms": 2_551_570,
        "start_ms": 2_000,
        "end_ms": 2_320,
        "window_ms": 4_000,
        "stride_ms": 1_000,
        "consensus_absolute_ms": (
            (2_536_530, 2_536_850),
            (2_536_570, 2_536_890),
            (2_536_530, 2_536_850),
        ),
    },
    "134e1b3ed61f136be211594e4bd67ad557d8efa1a01c62e046ecdb39712fc3ee-seg-00001847": {
        "transcript": "はいはいはいはいはい…",
        "decode_start_ms": 9_388_550,
        "decode_end_ms": 9_402_960,
        "start_ms": 0,
        "end_ms": 1_080,
        "window_ms": 2_000,
        "stride_ms": 250,
        "consensus_absolute_ms": (
            (9_388_550, 9_389_630),
            (9_388_920, 9_390_060),
            (9_389_230, 9_390_070),
        ),
        "evidence": {
            "method": "funasr_nano_overlapping_window_timestamps",
            "complete_window_text": "はい、はい、はい、はい、はい、はい、はい、はい…",
            "selected_prefix_token_span_ms": (0, 1_080),
            "owner_clipped_cue_absolute_ms": (9_388_850, 9_389_630),
            "later_windows_are_left_clipped": True,
        },
    },
    "d4cfd630e718e3ba40e36039aa22dbff56332b3757060759facc8302b20b2280-seg-00001304": {
        "transcript": "ああ、ああ、だめ、あい",
        "corrected_transcript": "ああ、舐め、あい、舐め、あ、あい、くっ…",
        "decode_start_ms": 6_586_740,
        "decode_end_ms": 6_600_270,
        "start_ms": 180,
        "end_ms": 3_540,
        "window_ms": 4_000,
        "stride_ms": 500,
        "consensus_absolute_ms": (
            (6_587_460, 6_587_700),
            (6_587_470, 6_587_710),
            (6_587_480, 6_587_720),
        ),
        "evidence": {
            "method": "funasr_nano_multiscale_overlapping_window_timestamps",
            "window_configurations_ms": ((4_000, 500), (3_000, 500), (2_000, 250)),
            "source_transcript": "ああ、ああ、だめ、あい",
            "selected_complete_window_text": "ああ、舐め、あい、舐め、あ、あい、くっ…",
            "selected_token_span_ms": (180, 3_540),
            "owner_clipped_cue_absolute_ms": (6_587_040, 6_590_280),
            "ambiguous_mid_mora_preserved_as_hiragana": "あい",
        },
    },
    "5c639610e56801ad02cd06395f1cf4c9ff22273bb4d375b248f2641fe3b3403a-seg-00000288": {
        "transcript": "あ、うんうんうん、あ、あ、すご、咥える、うん、うん、あ、あ、あ、あ、あ",
        "corrected_transcript": "ああ、うんうんうんうんうん。ああ、すごく入る……んんっ……",
        "decode_start_ms": 2_002_000,
        "decode_end_ms": 2_011_880,
        "start_ms": 60,
        "end_ms": 6_200,
        "window_ms": 3_000,
        "stride_ms": 500,
        "consensus_absolute_ms": (
            (2_005_440, 2_006_820),
            (2_005_460, 2_006_780),
            (2_005_420, 2_006_860),
        ),
        "evidence": {
            "method": "funasr_nano_overlapping_window_timestamps",
            "source_transcript": "あ、うんうんうん、あ、あ、すご、咥える、うん、うん、あ、あ、あ、あ、あ",
            "selected_window_texts": (
                "ああ、うんうんうんうんうん。",
                "ああ、すごーく入る",
                "あ、すごく入る。",
                "すごく入る…んんっ…",
            ),
            "selected_token_span_ms": (60, 6_200),
            "lexical_consensus_absolute_ms": (2_005_420, 2_006_860),
            "owner_clipped_cue_absolute_ms": (2_002_300, 2_008_200),
            "later_windows_are_nonlexical_vocalization": True,
        },
    },
    "b1263d4bb9c3e4b65e40091710454ef6d06ea1e6927bb76fb5d0cdd0b4a6a2ce-seg-00000253": {
        "transcript": "もう一回、ああ、じゃあ、尻文字してみます。",
        "decode_start_ms": 1_705_720,
        "decode_end_ms": 1_730_140,
        "start_ms": 10_440,
        "end_ms": 13_020,
        "window_ms": 4_000,
        "stride_ms": 500,
        "consensus_absolute_ms": (
            (1_716_160, 1_718_740),
            (1_716_180, 1_718_700),
        ),
        "evidence": {
            "method": "funasr_nano_overlapping_window_timestamps",
            "complete_window_text": "もう一回、ああ、じゃあ、尻文字してみます。",
            "selected_token_span_ms": (10_440, 13_020),
            "owner_clipped_cue_absolute_ms": (1_716_160, 1_718_740),
            "neighboring_exact_window_span_ms": (1_716_180, 1_718_700),
            "later_windows_are_left_clipped": True,
        },
    },
    "1075c66605e6933e5929870e4c34f998a6315f19358b2f25f6801253afc14006-seg-00000731": {
        "transcript": "あ、そう。あ、そうね、あ、そうね",
        "corrected_transcript": "ああ、ごめん、ごめん。ああ、そうね。",
        "decode_start_ms": 2_943_320,
        "decode_end_ms": 2_956_380,
        "start_ms": 3_000,
        "end_ms": 6_750,
        "window_ms": 2_000,
        "stride_ms": 250,
        "consensus_absolute_ms": (
            (2_946_320, 2_947_320),
            (2_949_320, 2_950_070),
        ),
        "evidence": {
            "method": "funasr_nano_multiscale_overlapping_window_timestamps",
            "window_configurations_ms": ((4_000, 500), (3_000, 500), (2_000, 250)),
            "source_transcript": "あ、そう。あ、そうね、あ、そうね",
            "corrected_transcript": "ああ、ごめん、ごめん。ああ、そうね。",
            "gomen_consensus_absolute_ms": (2_946_320, 2_947_320),
            "sou_consensus_absolute_ms": (2_949_320, 2_950_070),
            "selected_token_span_ms": (3_000, 6_750),
            "owner_clipped_cue_absolute_ms": (2_946_320, 2_950_070),
            "unstable_crop_only_candidates_rejected": (
                "あんたね",
                "だめ",
                "なるほどね",
                "きっと",
                "きつい",
            ),
        },
    },
    "51584edadd2729c96b726cebd784f757b9c0c5911e784674274019d459e8b53d-seg-00001366": {
        "transcript": "はい、はい、はい、はい、はい、はい、はい、はい、はい、はい。",
        "corrected_transcript": "ああ、そう。",
        "decode_start_ms": 6_157_750,
        "decode_end_ms": 6_158_910,
        "start_ms": 200,
        "end_ms": 900,
        "window_ms": 1_000,
        "stride_ms": 40,
        "consensus_absolute_ms": (
            (6_157_750, 6_158_750),
            (6_157_790, 6_158_790),
            (6_157_830, 6_158_830),
            (6_157_850, 6_158_650),
            (6_157_950, 6_158_750),
        ),
        "evidence": {
            "method": "funasr_nano_multiscale_overlapping_window_consensus",
            "window_configurations_ms": ((1_000, 40), (800, 100)),
            "source_transcript": "はい、はい、はい、はい、はい、はい、はい、はい、はい、はい。",
            "corrected_transcript": "ああ、そう。",
            "exact_consensus_window_count": 5,
            "cross_scale_exact_window_intersection_absolute_ms": (
                6_157_950,
                6_158_650,
            ),
            "selected_token_span_ms": (200, 900),
            "owner_clipped_cue_absolute_ms": (6_158_050, 6_158_610),
            "original_ten_repetitions_impossible_in_1_160_ms": True,
            "crop_only_candidates_rejected": (
                "ああ、でも。",
                "大丈夫。",
                "はい。",
                "ああ、じゃあもう。",
            ),
        },
    },
    "a6882b2fca91f6d2d5b7f6d6a65d4ffb1cbe7a226ad80e8fabbf36e44b823fe7-seg-00001506": {
        "transcript": "あ、あ、あ、あ、はいはいはいはいはい、あ、あ、あ、あ",
        "corrected_transcript": (
            "ああ、なるほど。ありがとうございます。ありがとうございます。"
            "すごい。よし。うん、うん、うん、うん。はい、はい、はい、はい。"
        ),
        "decode_start_ms": 6_318_840,
        "decode_end_ms": 6_333_530,
        "start_ms": 0,
        "end_ms": 9_500,
        "tokens": (
            ("ああ、なるほど。", 0, 1_750),
            ("ありがとうございます。ありがとうございます。", 1_750, 4_500),
            ("すごい。よし。", 4_500, 5_500),
            ("うん、うん、うん、うん。", 5_500, 7_500),
            ("はい、はい、はい、はい。", 7_500, 9_500),
        ),
        "window_ms": 3_000,
        "stride_ms": 250,
        "consensus_absolute_ms": (
            (6_318_840, 6_322_340),
            (6_319_590, 6_323_340),
            (6_320_590, 6_324_340),
            (6_322_090, 6_326_590),
            (6_324_340, 6_328_340),
        ),
        "evidence": {
            "method": "funasr_nano_multiscale_overlapping_window_consensus",
            "window_configurations_ms": ((4_000, 500), (3_000, 250)),
            "source_transcript": "あ、あ、あ、あ、はいはいはいはいはい、あ、あ、あ、あ",
            "corrected_transcript": (
                "ああ、なるほど。ありがとうございます。ありがとうございます。"
                "すごい。よし。うん、うん、うん、うん。はい、はい、はい、はい。"
            ),
            "selected_token_span_ms": (0, 9_500),
            "owner_clipped_cue_range_absolute_ms": (6_319_140, 6_328_340),
            "stable_lexical_items": (
                "なるほど",
                "ありがとうございます",
                "すごい",
                "よし",
            ),
            "repeated_responses_retained_conservatively": ("うん", "はい"),
            "unstable_crop_only_candidates_rejected": (
                "いいよね",
                "いや、いや",
                "いいかぁ",
                "Very good",
                "あさいかあ",
            ),
            "trailing_region_without_stable_lexical_content_ms": (9_500, 14_690),
        },
    },
}
_ASR_FIELDS = {
    "video_id",
    "relative_path",
    "duration_ms",
    "audio_offset_ms",
    "segment_count",
    "failure_count",
    "empty_omission_count",
    "quality_omission_count",
    "segments",
    "results",
}
_SEGMENT_FIELDS = {
    "segment_id",
    "index",
    "owner_start_ms",
    "owner_end_ms",
    "decode_start_ms",
    "decode_end_ms",
}
_ASR_RESULT_FIELDS = {"segment_id", "text", "raw", "error"}
_ALIGNMENT_FIELDS = {
    "stage",
    "schema_version",
    "video_id",
    "relative_path",
    "duration_ms",
    "audio_offset_ms",
    "input_segment_count",
    "empty_omission_count",
    "quality_omission_count",
    "aligned_segment_count",
    "failure_count",
    "nonlexical_omission_count",
    "cue_count",
    "timestamp_digest",
    "provenance",
    "cues",
    "results",
}
_ALIGNMENT_RESULT_FIELDS = {
    "segment_id",
    "transcript",
    "tokens",
    "cues",
    "raw",
    "error",
}
_TOKEN_FIELDS = {"text", "start_ms", "end_ms"}
_CUE_FIELDS = {"cue_id", "start_ms", "end_ms", "text"}


class CorpusAlignmentError(RuntimeError):
    pass


class AlignerPool:
    """Lazily load one forced aligner and reuse it for the whole corpus run."""

    def __init__(
        self,
        create: Callable[[], tuple[Any, Any]],
        *,
        model_path: Path | None = None,
    ) -> None:
        self._create = create
        self._value: object = _MISSING
        self.model_path = model_path

    def get(self) -> tuple[Any, Any]:
        if self._value is _MISSING:
            value = self._create()
            if type(value) is not tuple or len(value) != 2:
                raise CorpusAlignmentError(
                    "create_aligner must return a processor/model pair"
                )
            self._value = value
        processor, model = self._value
        return processor, model


@dataclass(frozen=True)
class _AsrInput:
    duration_ms: int
    audio_offset_ms: int
    segments: tuple[SpeechSegment, ...]
    transcripts: Mapping[str, str]
    funasr_timestamps: Mapping[str, object]
    empty_omission_count: int
    quality_omission_count: int
    sha256: str

    @property
    def selected(self) -> tuple[SpeechSegment, ...]:
        return tuple(
            segment
            for segment in self.segments
            if segment.segment_id in self.transcripts
        )


def _json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CorpusAlignmentError(
            f"alignment evidence is not strict JSON: {exc}"
        ) from exc
    return encoded + b"\n"


def _atomic_json(path: Path, value: object) -> None:
    _atomic_write(path, _json_bytes(value))


def _sha256_file(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CorpusAlignmentError(f"cannot hash local artifact {path}: {exc}") from exc
    return digest.hexdigest()


def _empty_asr_omission(row: object) -> bool:
    return (
        isinstance(row, dict)
        and row.get("text") == ""
        and row.get("error") == {"type": "quality_gate", "flags": ["empty"]}
    )


def _quality_asr_omission(row: object) -> bool:
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


def _load_asr(path: Path, record: VideoRecord, tolerance_ms: int) -> _AsrInput:
    expected_duration_ms = _expected_duration_ms(record)
    if not verified_asr(
        path,
        record.video_id,
        expected_duration_ms,
        tolerance_ms=tolerance_ms,
    ):
        raise CorpusAlignmentError(f"ASR artifact is incomplete or invalid: {path}")
    try:
        value = _strict_json_load(path)
    except (OSError, RuntimeCliError, TypeError, ValueError) as exc:
        raise CorpusAlignmentError(f"cannot read ASR artifact {path}: {exc}") from exc
    if not isinstance(value, dict) or set(value) != _ASR_FIELDS:
        raise CorpusAlignmentError("ASR artifact fields differ from the corpus schema")
    duration_ms = value["duration_ms"]
    audio_offset_ms = value["audio_offset_ms"]
    raw_segments = value["segments"]
    raw_results = value["results"]
    if (
        value["relative_path"] != record.relative_path
        or type(duration_ms) is not int
        or duration_ms <= 0
        or type(audio_offset_ms) is not int
        or audio_offset_ms != record.audio_offset_ms
        or type(raw_segments) is not list
        or type(raw_results) is not list
    ):
        raise CorpusAlignmentError("ASR artifact provenance differs from inventory")

    segments: list[SpeechSegment] = []
    previous_owner_end = 0
    for index, row in enumerate(raw_segments):
        if not isinstance(row, dict) or set(row) != _SEGMENT_FIELDS:
            raise CorpusAlignmentError(f"ASR segment {index} has invalid fields")
        if (
            any(
                type(row[field]) is not int
                for field in _SEGMENT_FIELDS - {"segment_id"}
            )
            or type(row["segment_id"]) is not str
        ):
            raise CorpusAlignmentError(f"ASR segment {index} has invalid types")
        segment = SpeechSegment(**row)
        if (
            segment.index != index
            or segment.segment_id != f"{record.video_id}-seg-{index:08d}"
            or not 0
            <= segment.decode_start_ms
            <= segment.owner_start_ms
            < segment.owner_end_ms
            <= segment.decode_end_ms
            <= duration_ms
            or (index and segment.owner_start_ms < previous_owner_end)
        ):
            raise CorpusAlignmentError(f"ASR segment {index} violates timeline order")
        previous_owner_end = segment.owner_end_ms
        segments.append(segment)

    transcripts: dict[str, str] = {}
    funasr_timestamps: dict[str, object] = {}
    empty_count = 0
    quality_count = 0
    runtime_quality_count = 0
    for index, (segment, row) in enumerate(zip(segments, raw_results, strict=True)):
        if not isinstance(row, dict) or set(row) != _ASR_RESULT_FIELDS:
            raise CorpusAlignmentError(f"ASR result {index} has invalid fields")
        text = row["text"]
        if (
            row["segment_id"] != segment.segment_id
            or type(text) is not str
            or not isinstance(row["raw"], dict)
        ):
            raise CorpusAlignmentError(f"ASR result {index} is invalid")
        if _empty_asr_omission(row):
            empty_count += 1
        elif _quality_asr_omission(row):
            quality_count += 1
        elif text:
            if row["error"] is not None:
                raise CorpusAlignmentError(f"ASR result {index} has text and an error")
            raw_timestamps = row["raw"].get("timestamps")
            sanitized = trim_abnormal_repetition_edges(text)
            segment_duration_ms = segment.decode_end_ms - segment.decode_start_ms
            timestamp_free_dense = (
                (not isinstance(raw_timestamps, list) or not raw_timestamps)
                and len(sanitized) >= MIN_DENSE_TRANSCRIPT_CHARS
                and len(sanitized) * 1000
                > segment_duration_ms * MAX_TIMESTAMP_FREE_CHARS_PER_SECOND
            )
            if timestamp_free_dense:
                sanitized = trim_corrupted_repetition_edges(text)
            if sanitized:
                transcripts[segment.segment_id] = sanitized
                funasr_timestamps[segment.segment_id] = raw_timestamps
            else:
                runtime_quality_count += 1
        else:
            raise CorpusAlignmentError(
                f"ASR result {index} is neither text nor an accepted empty omission"
            )
    if empty_count != value["empty_omission_count"]:
        raise CorpusAlignmentError("ASR empty omission count differs")
    if quality_count != value["quality_omission_count"]:
        raise CorpusAlignmentError("ASR quality omission count differs")
    return _AsrInput(
        duration_ms=duration_ms,
        audio_offset_ms=audio_offset_ms,
        segments=tuple(segments),
        transcripts=transcripts,
        funasr_timestamps=funasr_timestamps,
        empty_omission_count=empty_count,
        quality_omission_count=quality_count + runtime_quality_count,
        sha256=_sha256_file(path),
    )


def _audio_evidence(path: Path) -> dict[str, int]:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CorpusAlignmentError(f"cannot stat local audio {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CorpusAlignmentError(f"local audio is not a regular file: {path}")
    return {"audio_bytes": metadata.st_size, "audio_mtime_ns": metadata.st_mtime_ns}


def _provenance(
    config: Any,
    record: VideoRecord,
    asr_input: _AsrInput,
    flac: Path,
    model_pool: AlignerPool,
) -> dict[str, object]:
    model_path = model_pool.model_path or config.aligner_model.local_path
    return {
        "asr_sha256": asr_input.sha256,
        **_audio_evidence(flac),
        "source_bytes": record.source_bytes,
        "source_mtime_ns": record.source_mtime_ns,
        "probe_sha256": record.probe_sha256,
        "aligner_model_id": config.aligner_model.model_id,
        "aligner_model_revision": config.aligner_model.revision,
        "aligner_model_path": str(model_path),
    }


def _reviewed_alignment_transcript(segment_id: str, transcript: str) -> str:
    override = _REVIEWED_WINDOW_ALIGNMENT_OVERRIDES.get(segment_id)
    if override is None or override["transcript"] != transcript:
        return transcript
    corrected = override.get("corrected_transcript")
    return corrected if isinstance(corrected, str) else transcript


def _reviewed_window_alignment_override(
    task: AlignmentTask,
    primary: AlignmentResult,
) -> AlignmentResult | None:
    override = _REVIEWED_WINDOW_ALIGNMENT_OVERRIDES.get(task.segment.segment_id)
    if override is None:
        return None
    expected = (
        override["transcript"],
        override["decode_start_ms"],
        override["decode_end_ms"],
    )
    actual = (
        task.transcript,
        task.segment.decode_start_ms,
        task.segment.decode_end_ms,
    )
    if actual != expected:
        raise CorpusAlignmentError(
            "reviewed alignment override provenance differs from the current task"
        )
    aligned_transcript = _reviewed_alignment_transcript(
        task.segment.segment_id,
        task.transcript,
    )
    reviewed_tokens = override.get("tokens")
    if reviewed_tokens is None:
        tokens = (
            AlignedToken(
                aligned_transcript,
                override["start_ms"],
                override["end_ms"],
            ),
        )
    else:
        tokens = tuple(
            AlignedToken(text, start, end) for text, start, end in reviewed_tokens
        )
        if "".join(token.text for token in tokens) != aligned_transcript:
            raise CorpusAlignmentError(
                "reviewed alignment token text differs from corrected transcript"
            )
    duration_ms = task.segment.decode_end_ms - task.segment.decode_start_ms
    validate_tokens(tokens, duration_ms, transcript=aligned_transcript)
    cues = tuple(
        build_cues(
            tokens,
            segment=task.segment,
            audio_offset_ms=task.audio_offset_ms,
        )
    )
    return AlignmentResult(
        task.segment.segment_id,
        aligned_transcript,
        tokens,
        cues,
        {
            "phase": "reviewed_overlapping_window_consensus",
            "window_ms": override["window_ms"],
            "stride_ms": override["stride_ms"],
            "consensus_absolute_ms": override["consensus_absolute_ms"],
            "evidence": override.get("evidence"),
            "primary_alignment": {
                "raw": primary.raw,
                "error": primary.error,
            },
        },
        None,
    )


def _decode_token(row: object, index: int) -> AlignedToken:
    if not isinstance(row, dict) or set(row) != _TOKEN_FIELDS:
        raise ValueError(f"alignment token {index} has invalid fields")
    token = AlignedToken(row["text"], row["start_ms"], row["end_ms"])
    if (
        type(token.text) is not str
        or not token.text
        or type(token.start_ms) is not int
        or type(token.end_ms) is not int
        or not 0 <= token.start_ms < token.end_ms
    ):
        raise ValueError(f"alignment token {index} is invalid")
    return token


def _decode_cue(row: object, index: int) -> JapaneseCue:
    if not isinstance(row, dict) or set(row) != _CUE_FIELDS:
        raise ValueError(f"alignment cue {index} has invalid fields")
    cue = JapaneseCue(row["cue_id"], row["start_ms"], row["end_ms"], row["text"])
    if (
        type(cue.cue_id) is not str
        or not cue.cue_id
        or type(cue.start_ms) is not int
        or type(cue.end_ms) is not int
        or not 0 <= cue.start_ms < cue.end_ms
        or type(cue.text) is not str
        or not cue.text.strip()
        or "\ufffd" in cue.text
    ):
        raise ValueError(f"alignment cue {index} is invalid")
    return cue


def verified_alignment(
    path: Path,
    video_id: str,
    asr_sha256: str,
    *,
    expected_provenance: Mapping[str, object] | None = None,
    expected_segments: Sequence[SpeechSegment] | None = None,
    expected_transcripts: Mapping[str, str] | None = None,
    expected_input_segment_count: int | None = None,
    expected_empty_omission_count: int | None = None,
    expected_quality_omission_count: int | None = None,
    expected_duration_ms: int | None = None,
    expected_audio_offset_ms: int | None = None,
    expected_relative_path: str | None = None,
) -> bool:
    """Strictly verify a successful, ordered alignment artifact for resume."""

    try:
        value = _strict_json_load(path)
        if not isinstance(value, dict) or set(value) != _ALIGNMENT_FIELDS:
            return False
        provenance = value["provenance"]
        if (
            value["stage"] != "corpus_alignment"
            or value["schema_version"] != ALIGNMENT_SCHEMA_VERSION
            or value["video_id"] != video_id
            or type(value["relative_path"]) is not str
            or not value["relative_path"]
            or (
                expected_relative_path is not None
                and value["relative_path"] != expected_relative_path
            )
            or not isinstance(provenance, dict)
            or provenance.get("asr_sha256") != asr_sha256
            or (
                expected_provenance is not None
                and provenance != dict(expected_provenance)
            )
            or value["failure_count"] != 0
        ):
            return False
        for field in (
            "duration_ms",
            "audio_offset_ms",
            "input_segment_count",
            "empty_omission_count",
            "quality_omission_count",
            "aligned_segment_count",
            "failure_count",
            "nonlexical_omission_count",
            "cue_count",
        ):
            if type(value[field]) is not int:
                return False
        if (
            value["duration_ms"] <= 0
            or value["input_segment_count"] < 0
            or value["empty_omission_count"] < 0
            or value["quality_omission_count"] < 0
            or value["aligned_segment_count"] < 0
            or value["nonlexical_omission_count"] < 0
            or value["cue_count"] < 0
            or (
                expected_input_segment_count is not None
                and value["input_segment_count"] != expected_input_segment_count
            )
            or (
                expected_empty_omission_count is not None
                and value["empty_omission_count"] != expected_empty_omission_count
            )
            or (
                expected_quality_omission_count is not None
                and value["quality_omission_count"] != expected_quality_omission_count
            )
            or (
                expected_duration_ms is not None
                and value["duration_ms"] != expected_duration_ms
            )
            or (
                expected_audio_offset_ms is not None
                and value["audio_offset_ms"] != expected_audio_offset_ms
            )
        ):
            return False
        rows = value["results"]
        top_cue_rows = value["cues"]
        if (
            type(rows) is not list
            or type(top_cue_rows) is not list
            or len(rows) != value["aligned_segment_count"]
            or len(top_cue_rows) != value["cue_count"]
        ):
            return False
        expected_by_id = (
            {segment.segment_id: segment for segment in expected_segments}
            if expected_segments is not None
            else {}
        )
        expected_ids = (
            [segment.segment_id for segment in expected_segments]
            if expected_segments is not None
            else None
        )
        result_ids: list[str] = []
        nested_cues: list[JapaneseCue] = []
        nonlexical_count = 0
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != _ALIGNMENT_RESULT_FIELDS:
                return False
            segment_id = row["segment_id"]
            transcript = row["transcript"]
            if (
                type(segment_id) is not str
                or not segment_id.startswith(f"{video_id}-seg-")
                or type(transcript) is not str
                or not transcript
                or type(row["tokens"]) is not list
                or type(row["cues"]) is not list
            ):
                return False
            if expected_transcripts is not None:
                source_transcript = expected_transcripts.get(segment_id)
                if (
                    not isinstance(source_transcript, str)
                    or _reviewed_alignment_transcript(segment_id, source_transcript)
                    != transcript
                ):
                    return False
            result_ids.append(segment_id)
            tokens = [
                _decode_token(token, token_index)
                for token_index, token in enumerate(row["tokens"])
            ]
            cues = [
                _decode_cue(cue, cue_index) for cue_index, cue in enumerate(row["cues"])
            ]
            error = row["error"]
            if error is None:
                if not tokens or "".join(token.text for token in tokens) != transcript:
                    return False
                if any(
                    current.start_ms < previous.end_ms
                    for previous, current in zip(tokens, tokens[1:])
                ):
                    return False
                if segment_id in expected_by_id:
                    duration = (
                        expected_by_id[segment_id].decode_end_ms
                        - expected_by_id[segment_id].decode_start_ms
                    )
                    validate_tokens(tokens, duration, transcript=transcript)
            elif _nonlexical_alignment_omission(row):
                if tokens or cues:
                    return False
                nonlexical_count += 1
            else:
                return False
            if any(
                not cue.cue_id.startswith(f"{segment_id}-cue-") for cue in cues
            ) or any(
                current.start_ms < previous.end_ms
                for previous, current in zip(cues, cues[1:])
            ):
                return False
            nested_cues.extend(cues)
        if (
            len(result_ids) != len(set(result_ids))
            or (expected_ids is not None and result_ids != expected_ids)
            or nonlexical_count != value["nonlexical_omission_count"]
        ):
            return False
        ordered_cues = sorted(
            nested_cues, key=lambda cue: (cue.start_ms, cue.end_ms, cue.cue_id)
        )
        top_cues = [
            _decode_cue(cue, cue_index) for cue_index, cue in enumerate(top_cue_rows)
        ]
        if (
            top_cues != ordered_cues
            or len({cue.cue_id for cue in top_cues}) != len(top_cues)
            or any(
                current.start_ms < previous.end_ms
                for previous, current in zip(top_cues, top_cues[1:])
            )
            or value["timestamp_digest"] != timestamp_digest(top_cues)
        ):
            return False
        return True
    except (OSError, RuntimeCliError, TypeError, ValueError):
        return False


def _validate_batch_results(
    results: object,
    tasks: Sequence[AlignmentTask],
) -> list[AlignmentResult]:
    if type(results) is not list or len(results) != len(tasks):
        raise CorpusAlignmentError(
            f"aligner returned an invalid batch cardinality for {len(tasks)} tasks"
        )
    checked: list[AlignmentResult] = []
    for index, (result, task) in enumerate(zip(results, tasks, strict=True)):
        if (
            not isinstance(result, AlignmentResult)
            or result.segment_id != task.segment.segment_id
            or result.transcript != task.transcript
        ):
            raise CorpusAlignmentError(f"aligner result {index} is out of order")
        checked.append(result)
    return checked


def _process_record(
    config: Any,
    record: VideoRecord,
    *,
    batch_size: int,
    model_pool: AlignerPool,
    run_id: str,
) -> str:
    if type(batch_size) is not int or batch_size <= 0:
        raise CorpusAlignmentError("alignment batch size must be positive")
    output_dir = config.work_root / ASR_RELATIVE_ROOT / record.video_id
    asr_path = output_dir / "asr.json"
    flac = config.work_root / "audio" / f"{record.video_id}.flac"
    if not os.path.lexists(asr_path):
        return "missing_asr"
    if not os.path.lexists(flac):
        return "missing_audio"
    tolerance_ms = config.stage_settings.extraction.duration_tolerance_ms
    _verify_audio(flac, record, tolerance_ms)
    asr_input = _load_asr(asr_path, record, tolerance_ms)
    provenance = _provenance(config, record, asr_input, flac, model_pool)
    final_path = output_dir / "alignment.json"
    if verified_alignment(
        final_path,
        record.video_id,
        asr_input.sha256,
        expected_provenance=provenance,
        expected_segments=asr_input.selected,
        expected_transcripts=asr_input.transcripts,
        expected_input_segment_count=len(asr_input.segments),
        expected_empty_omission_count=asr_input.empty_omission_count,
        expected_quality_omission_count=asr_input.quality_omission_count,
        expected_duration_ms=asr_input.duration_ms,
        expected_audio_offset_ms=asr_input.audio_offset_ms,
        expected_relative_path=record.relative_path,
    ):
        _append_progress(
            config,
            {
                "stage": "corpus_alignment",
                "event": "skipped_verified",
                "run_id": run_id,
                "video_id": record.video_id,
                "relative_path": record.relative_path,
            },
        )
        return "existing"

    _append_progress(
        config,
        {
            "stage": "corpus_alignment",
            "event": "video_started",
            "run_id": run_id,
            "video_id": record.video_id,
            "relative_path": record.relative_path,
            "input_segment_count": len(asr_input.segments),
            "alignable_segment_count": len(asr_input.selected),
        },
    )
    completed: list[AlignmentResult] = []
    scratch = config.work_root / "scratch/corpus-alignment" / run_id / record.video_id
    try:
        for offset in range(0, len(asr_input.selected), batch_size):
            batch = list(asr_input.selected[offset : offset + batch_size])
            batch_dir = scratch / f"batch-{offset:08d}"
            try:
                wavs = materialize_wav_batch(flac, batch, batch_dir)
                processor, model = model_pool.get()
                tasks = [
                    AlignmentTask(
                        segment,
                        wavs[segment.segment_id],
                        asr_input.transcripts[segment.segment_id],
                        asr_input.audio_offset_ms,
                    )
                    for segment in batch
                ]
                primary_results = _validate_batch_results(
                    align_with_retries(processor, model, tasks), tasks
                )
                results: list[AlignmentResult] = []
                for task, primary in zip(tasks, primary_results, strict=True):
                    if primary.error is None:
                        results.append(primary)
                        continue
                    try:
                        recovered = alignment_from_funasr_timestamps(
                            task,
                            asr_input.funasr_timestamps.get(task.segment.segment_id),
                            primary_alignment=primary,
                        )
                    except ValueError:
                        recovered = (
                            _reviewed_window_alignment_override(task, primary)
                            or primary
                        )
                    results.append(recovered)
                completed.extend(results)
            finally:
                shutil.rmtree(batch_dir, ignore_errors=True)
            _append_progress(
                config,
                {
                    "stage": "corpus_alignment",
                    "event": "alignment_batch_completed",
                    "run_id": run_id,
                    "video_id": record.video_id,
                    "completed": len(completed),
                    "pending": len(asr_input.selected),
                    "batch_size": len(batch),
                },
            )
            print(
                f"ALIGN {record.video_id} {len(completed)}/{len(asr_input.selected)}",
                flush=True,
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    encoded_results = [asdict(result) for result in completed]
    nonlexical_omissions = [
        row for row in encoded_results if _nonlexical_alignment_omission(row)
    ]
    failures = [
        row
        for row in encoded_results
        if row.get("error") is not None and row not in nonlexical_omissions
    ]
    cues = [cue for result in completed for cue in result.cues]
    cues.sort(key=lambda cue: (cue.start_ms, cue.end_ms, cue.cue_id))
    if any(
        current.start_ms < previous.end_ms for previous, current in zip(cues, cues[1:])
    ):
        raise CorpusAlignmentError("aligned cues overlap across segments")
    payload = {
        "stage": "corpus_alignment",
        "schema_version": ALIGNMENT_SCHEMA_VERSION,
        "video_id": record.video_id,
        "relative_path": record.relative_path,
        "duration_ms": asr_input.duration_ms,
        "audio_offset_ms": asr_input.audio_offset_ms,
        "input_segment_count": len(asr_input.segments),
        "empty_omission_count": asr_input.empty_omission_count,
        "quality_omission_count": asr_input.quality_omission_count,
        "aligned_segment_count": len(completed),
        "failure_count": len(failures),
        "nonlexical_omission_count": len(nonlexical_omissions),
        "cue_count": len(cues),
        "timestamp_digest": timestamp_digest(cues),
        "provenance": provenance,
        "cues": [asdict(cue) for cue in cues],
        "results": encoded_results,
    }
    _atomic_json(final_path, payload)
    if not failures and not verified_alignment(
        final_path,
        record.video_id,
        asr_input.sha256,
        expected_provenance=provenance,
        expected_segments=asr_input.selected,
        expected_transcripts=asr_input.transcripts,
        expected_input_segment_count=len(asr_input.segments),
        expected_empty_omission_count=asr_input.empty_omission_count,
        expected_quality_omission_count=asr_input.quality_omission_count,
        expected_duration_ms=asr_input.duration_ms,
        expected_audio_offset_ms=asr_input.audio_offset_ms,
        expected_relative_path=record.relative_path,
    ):
        raise CorpusAlignmentError(
            f"new alignment artifact failed strict verification: {final_path}"
        )
    event = "video_completed" if not failures else "video_failed_quality"
    _append_progress(
        config,
        {
            "stage": "corpus_alignment",
            "event": event,
            "run_id": run_id,
            "video_id": record.video_id,
            "relative_path": record.relative_path,
            "aligned_segment_count": len(completed),
            "failure_count": len(failures),
            "nonlexical_omission_count": len(nonlexical_omissions),
            "cue_count": len(cues),
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
        raise CorpusAlignmentError("--video-id values must be unique")
    by_id = {record.video_id: record for record in records}
    unknown = tuple(video_id for video_id in requested if video_id not in by_id)
    if unknown:
        raise CorpusAlignmentError(f"unknown --video-id values: {unknown!r}")
    requested_set = set(requested)
    selected = tuple(
        record
        for record in records
        if not requested_set or record.video_id in requested_set
    )
    return selected if limit is None else selected[:limit]


def _model_candidate(config: Any, model_dir: Path) -> Path:
    expanded = model_dir.expanduser()
    return expanded if expanded.is_absolute() else config.work_root / expanded


def _load_aligner(model_path: Path, device: str) -> tuple[Any, Any]:
    try:
        resolved = model_path.resolve(strict=True)
    except OSError as exc:
        raise CorpusAlignmentError(
            f"cannot resolve aligner model {model_path}: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise CorpusAlignmentError(f"aligner model is not a directory: {resolved}")
    return create_aligner(resolved, device=device)


def run_corpus(
    *,
    batch_size: int,
    model_dir: Path,
    limit: int | None,
    video_ids: Sequence[str],
) -> dict[str, object]:
    if type(batch_size) is not int or batch_size <= 0:
        raise CorpusAlignmentError("alignment batch size must be positive")
    config = _runtime_config()
    snapshot = _read_manifest(config)
    selected = _select_records(
        snapshot.records,
        limit=limit,
        video_ids=video_ids,
    )
    run_id = uuid.uuid4().hex
    model_path = _model_candidate(config, model_dir)
    model_pool = AlignerPool(
        lambda: _load_aligner(model_path, config.stage_settings.alignment.device),
        model_path=model_path,
    )
    counts = {
        "completed": 0,
        "existing": 0,
        "no_audio": 0,
        "target_exists": 0,
        "missing_audio": 0,
        "missing_asr": 0,
        "failed": 0,
    }
    processable: list[VideoRecord] = []
    skipped: list[tuple[VideoRecord, str]] = []
    for record in selected:
        if record.target_exists:
            counts["target_exists"] += 1
            continue
        if record.audio_stream_index is None:
            counts["no_audio"] += 1
            skipped.append((record, "skipped_no_audio"))
        else:
            processable.append(record)
    _append_progress(
        config,
        {
            "stage": "corpus_alignment",
            "event": "batch_started",
            "run_id": run_id,
            "selected": len(selected),
            "processable": len(processable),
            "batch_size": batch_size,
            "snapshot_digest": snapshot.snapshot_digest,
        },
    )
    for record, event in skipped:
        _append_progress(
            config,
            {
                "stage": "corpus_alignment",
                "event": event,
                "run_id": run_id,
                "video_id": record.video_id,
                "relative_path": record.relative_path,
            },
        )
    for index, record in enumerate(processable, 1):
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
            _append_progress(
                config,
                {
                    "stage": "corpus_alignment",
                    "event": "video_failed_exception",
                    "run_id": run_id,
                    "video_id": record.video_id,
                    "relative_path": record.relative_path,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            print(
                f"corpus alignment FAILED {record.video_id}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
        counts[status] += 1
        if status.startswith("missing_"):
            _append_progress(
                config,
                {
                    "stage": "corpus_alignment",
                    "event": f"deferred_{status}",
                    "run_id": run_id,
                    "video_id": record.video_id,
                    "relative_path": record.relative_path,
                },
            )
        print(
            f"corpus alignment {status} {index}/{len(processable)} {record.video_id}",
            flush=True,
        )

    incomplete = counts["missing_audio"] + counts["missing_asr"]
    summary: dict[str, object] = {
        "stage": "corpus_alignment",
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
        prog="python -m subtitle_pipeline.corpus_alignment_cli",
        description="Resumable local full-corpus Qwen3 forced alignment runtime",
    )
    parser.add_argument("--batch-size", type=_positive_int, default=16)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_ALIGNER_MODEL)
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
            batch_size=args.batch_size,
            model_dir=args.model_dir,
            limit=args.limit,
            video_ids=args.video_ids,
        )
    except KeyboardInterrupt:
        print("corpus alignment interrupted", file=sys.stderr, flush=True)
        return 130
    except BaseException as exc:
        print(
            f"corpus alignment failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    incomplete = summary["missing_audio"] + summary["missing_asr"]
    return 1 if summary["failed"] or incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
