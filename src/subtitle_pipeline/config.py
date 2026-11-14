from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

KNOWN_NO_AUDIO_PATH = (
    "Mgstage-2412-collection (Bonus-work005-007)/T66Y.COM@SIRO-5357.mp4"
)
KNOWN_COLLISION_PATHS = (
    "MIDA-482/hhd800.com@MIDA-482.zh.srt",
    "ipzz-652/4k2.com@ipzz-652.zh.srt",
    "mida00449_4K60fps/mida00449_4K60fps.zh.srt",
    "snos00045_4K60fps/snos00045_4K60fps.zh.srt",
)

_PINNED_REVISION_RE = re.compile(r"[0-9a-f]{40}")


def _nonblank_string(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _positive_int_tuple(value: object, field: str) -> tuple[int, ...]:
    if type(value) is not tuple or not value:
        raise ValueError(f"{field} must be a non-empty tuple")
    if any(type(item) is not int or item <= 0 for item in value):
        raise ValueError(f"{field} must contain positive integers")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must be unique")
    return value


def normalize_relative_path(value: str | PurePosixPath) -> str:
    raw = str(value)
    if not raw or raw in {".", ".."} or "\\" in raw:
        raise ValueError(f"invalid relative POSIX path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != raw:
        raise ValueError(f"invalid relative POSIX path: {raw!r}")
    return raw


@dataclass(frozen=True)
class SchemaVersions:
    inventory: str = "inventory-v1"

    def __post_init__(self) -> None:
        for field, value in asdict(self).items():
            _nonblank_string(value, f"{field} schema version")


@dataclass(frozen=True)
class PromptVersions:
    glossary: str = "glossary-v1"
    primary: str = "primary-v1"
    review: str = "review-v1"
    escalation: str = "escalation-v1"

    def __post_init__(self) -> None:
        for field, value in asdict(self).items():
            _nonblank_string(value, f"{field} prompt version")


@dataclass(frozen=True)
class ModelRevisions:
    vad: str
    asr: str
    aligner: str
    primary_translation: str
    fallback_translation: str
    reviewer_translation: str

    def __post_init__(self) -> None:
        for field, value in asdict(self).items():
            if type(value) is not str or _PINNED_REVISION_RE.fullmatch(value) is None:
                raise ValueError(
                    f"{field} revision must be a full 40-character lowercase hex commit"
                )


@dataclass(frozen=True)
class ModelBinding:
    model_id: str
    local_path: Path
    revision: str

    def __post_init__(self) -> None:
        _nonblank_string(self.model_id, "model ID")
        if (
            type(self.revision) is not str
            or _PINNED_REVISION_RE.fullmatch(self.revision) is None
        ):
            raise ValueError(
                "model revision must be a full 40-character lowercase hex commit"
            )
        if not isinstance(self.local_path, Path) or not self.local_path.is_absolute():
            raise ValueError("model local path must be absolute")


@dataclass(frozen=True)
class AudioSelectionPolicy:
    language_preferences: tuple[str, ...] = ("jpn", "ja")
    prefer_default_disposition: bool = True
    prefer_untagged: bool = True
    fallback_policy: str = "lowest_index"

    def __post_init__(self) -> None:
        if (
            type(self.language_preferences) is not tuple
            or any(
                type(item) is not str
                or not item
                or item.strip() != item
                or item != item.lower()
                for item in self.language_preferences
            )
            or len(set(self.language_preferences)) != len(self.language_preferences)
        ):
            raise ValueError("audio language preferences must be unique lowercase tags")
        if type(self.prefer_default_disposition) is not bool:
            raise ValueError("prefer_default_disposition must be boolean")
        if type(self.prefer_untagged) is not bool:
            raise ValueError("prefer_untagged must be boolean")
        if self.fallback_policy != "lowest_index":
            raise ValueError("audio fallback policy must be lowest_index")


@dataclass(frozen=True)
class ExtractionSettings:
    worker_candidates: tuple[int, ...] = (2, 4)
    sample_rate_hz: int = 16_000
    channels: int = 1
    sample_format: str = "s16"
    codec: str = "flac"
    duration_tolerance_ms: int = 1_000
    extraction_timeout_seconds: int = 14_400
    probe_timeout_seconds: int = 120
    audio_selection: AudioSelectionPolicy = AudioSelectionPolicy()

    def __post_init__(self) -> None:
        _positive_int_tuple(self.worker_candidates, "extraction worker candidates")
        for field in (
            "sample_rate_hz",
            "channels",
            "extraction_timeout_seconds",
            "probe_timeout_seconds",
        ):
            _positive_int(getattr(self, field), field)
        if (
            type(self.duration_tolerance_ms) is not int
            or self.duration_tolerance_ms < 0
        ):
            raise ValueError("duration tolerance must be a nonnegative integer")
        _nonblank_string(self.sample_format, "sample format")
        _nonblank_string(self.codec, "audio codec")
        if type(self.audio_selection) is not AudioSelectionPolicy:
            raise ValueError("audio selection policy must be frozen")


@dataclass(frozen=True)
class VadSettings:
    max_span_ms: int = 30_000
    padding_ms: int = 300
    device: str = "auto"

    def __post_init__(self) -> None:
        _positive_int(self.max_span_ms, "VAD maximum span")
        if type(self.padding_ms) is not int or self.padding_ms < 0:
            raise ValueError("VAD padding must be a nonnegative integer")
        _nonblank_string(self.device, "VAD device")


@dataclass(frozen=True)
class AsrSettings:
    batch_candidates: tuple[int, ...] = (32, 64, 96, 128)
    language: str = "日文"
    dtype: str = "bf16"
    temperature: float = 0.0
    repetition_penalty: float = 1.0
    max_new_tokens: int = 256
    device: str = "auto"
    engine: str = "auto"
    tensor_parallel_size: int = 1
    gpu_memory_utilization: float = 0.90
    max_model_len: int = 4096

    def __post_init__(self) -> None:
        _positive_int_tuple(self.batch_candidates, "ASR batch candidates")
        for field in ("language", "dtype", "device", "engine"):
            _nonblank_string(getattr(self, field), f"ASR {field}")
        if self.engine not in {"auto", "vllm", "funasr"}:
            raise ValueError("ASR engine must be auto, vllm, or funasr")
        if self.dtype != "bf16":
            raise ValueError("FunASR dtype must be bf16")
        if (
            type(self.temperature) is not float
            or not math.isfinite(self.temperature)
            or self.temperature != 0.0
        ):
            raise ValueError("ASR temperature must be exactly 0.0")
        if type(self.repetition_penalty) is not float or self.repetition_penalty != 1.0:
            raise ValueError("ASR repetition penalty must be exactly 1.0")
        _positive_int(self.max_new_tokens, "ASR max_new_tokens")
        _positive_int(self.tensor_parallel_size, "ASR tensor parallel size")
        _positive_int(self.max_model_len, "ASR maximum model length")
        if (
            type(self.gpu_memory_utilization) is not float
            or not 0 < self.gpu_memory_utilization < 1
        ):
            raise ValueError("ASR GPU memory utilization must be between zero and one")


@dataclass(frozen=True)
class AlignmentSettings:
    batch_size: int = 16
    language: str = "Japanese"
    dtype: str = "bfloat16"
    device: str = "auto"
    local_files_only: bool = True

    def __post_init__(self) -> None:
        _positive_int(self.batch_size, "alignment batch size")
        for field in ("language", "dtype", "device"):
            _nonblank_string(getattr(self, field), f"alignment {field}")
        if type(self.local_files_only) is not bool or not self.local_files_only:
            raise ValueError("alignment must use local model files only")


@dataclass(frozen=True)
class TranslationSettings:
    concurrency_candidates: tuple[int, ...] = (1, 2, 4)
    primary_temperature: float = 0.2
    review_temperature: float = 0.1
    top_p: float = 0.8
    seed: int = 20_260_710
    n: int = 1
    max_tokens: int = 4096
    enable_thinking: bool = False
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    tensor_parallel_size: int = 1
    gpu_memory_utilization: float = 0.90
    max_model_len: int = 32_768
    health_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        _positive_int_tuple(
            self.concurrency_candidates, "translation concurrency candidates"
        )
        for field in (
            "primary_temperature",
            "review_temperature",
            "top_p",
            "gpu_memory_utilization",
        ):
            value = getattr(self, field)
            if (
                type(value) is not float
                or not math.isfinite(value)
                or not 0 < value < 1
            ):
                raise ValueError(
                    f"translation {field} must be a finite float between zero and one"
                )
        for field in (
            "seed",
            "n",
            "max_tokens",
            "server_port",
            "tensor_parallel_size",
            "max_model_len",
            "health_timeout_seconds",
        ):
            _positive_int(getattr(self, field), f"translation {field}")
        if (
            self.n != 1
            or type(self.enable_thinking) is not bool
            or self.enable_thinking
        ):
            raise ValueError("translation requires n=1 and thinking disabled")
        _nonblank_string(self.server_host, "translation server host")


@dataclass(frozen=True)
class StageSettings:
    extraction: ExtractionSettings = ExtractionSettings()
    vad: VadSettings = VadSettings()
    asr: AsrSettings = AsrSettings()
    alignment: AlignmentSettings = AlignmentSettings()
    translation: TranslationSettings = TranslationSettings()

    def __init__(
        self,
        extraction: ExtractionSettings = ExtractionSettings(),
        vad: VadSettings = VadSettings(),
        asr: AsrSettings = AsrSettings(),
        alignment: AlignmentSettings = AlignmentSettings(),
        translation: TranslationSettings = TranslationSettings(),
        **unsupported: object,
    ) -> None:
        if unsupported:
            raise ValueError(
                f"unsupported or mutable stage settings: {sorted(unsupported)}"
            )
        object.__setattr__(self, "extraction", extraction)
        object.__setattr__(self, "vad", vad)
        object.__setattr__(self, "asr", asr)
        object.__setattr__(self, "alignment", alignment)
        object.__setattr__(self, "translation", translation)
        self.__post_init__()

    def __post_init__(self) -> None:
        expected = (
            (self.extraction, ExtractionSettings, "extraction"),
            (self.vad, VadSettings, "VAD"),
            (self.asr, AsrSettings, "ASR"),
            (self.alignment, AlignmentSettings, "alignment"),
            (self.translation, TranslationSettings, "translation"),
        )
        for value, expected_type, field in expected:
            if type(value) is not expected_type:
                raise ValueError(
                    f"{field} settings must be frozen {expected_type.__name__}"
                )

    @property
    def extraction_worker_candidates(self) -> tuple[int, ...]:
        return self.extraction.worker_candidates

    @property
    def vad_max_span_ms(self) -> int:
        return self.vad.max_span_ms

    @property
    def vad_padding_ms(self) -> int:
        return self.vad.padding_ms

    @property
    def asr_batch_candidates(self) -> tuple[int, ...]:
        return self.asr.batch_candidates

    @property
    def alignment_batch_size(self) -> int:
        return self.alignment.batch_size

    @property
    def translation_concurrency_candidates(self) -> tuple[int, ...]:
        return self.translation.concurrency_candidates


DEFAULT_OWNED_LOCAL_PREFIXES = (
    "state",
    "audio",
    "segments",
    "asr",
    "align",
    "translate",
    "final",
    "models",
    "logs",
    "scratch",
)

DEFAULT_VIDEO_EXTENSIONS = (
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
)


@dataclass(frozen=True)
class PipelineConfig:
    source_root: Path
    work_root: Path
    owned_local_prefixes: tuple[str, ...]
    schemas: SchemaVersions
    prompts: PromptVersions
    vad_model: ModelBinding
    asr_model: ModelBinding
    aligner_model: ModelBinding
    primary_translation_model: ModelBinding
    fallback_translation_model: ModelBinding
    reviewer_translation_model: ModelBinding
    stage_settings: StageSettings
    video_extensions: tuple[str, ...]
    local_capacity_reserve_bytes: int
    expected_video_count: int
    known_no_audio_paths: tuple[str, ...]
    known_collision_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_root, Path) or not self.source_root.is_absolute():
            raise ValueError("source_root must be an absolute Path")
        if not isinstance(self.work_root, Path) or not self.work_root.is_absolute():
            raise ValueError("work_root must be an absolute Path")
        for root, field in (
            (self.source_root, "source_root"),
            (self.work_root, "work_root"),
        ):
            try:
                resolved = root.resolve(strict=True)
            except OSError as exc:
                raise ValueError(
                    f"{field} must exist and resolve canonically: {exc}"
                ) from exc
            if resolved != root:
                raise ValueError(
                    f"{field} must not contain aliases or symlink ancestors"
                )
        nested = (
            (self.schemas, SchemaVersions, "schemas"),
            (self.prompts, PromptVersions, "prompts"),
            (self.vad_model, ModelBinding, "VAD model"),
            (self.asr_model, ModelBinding, "ASR model"),
            (self.aligner_model, ModelBinding, "aligner model"),
            (self.primary_translation_model, ModelBinding, "primary model"),
            (self.fallback_translation_model, ModelBinding, "fallback model"),
            (self.reviewer_translation_model, ModelBinding, "reviewer model"),
            (self.stage_settings, StageSettings, "stage settings"),
        )
        for value, expected_type, field in nested:
            if type(value) is not expected_type:
                raise ValueError(f"{field} must be frozen {expected_type.__name__}")
        if (
            type(self.owned_local_prefixes) is not tuple
            or not self.owned_local_prefixes
        ):
            raise ValueError("owned local prefix allowlist must not be empty")
        if any(type(item) is not str for item in self.owned_local_prefixes):
            raise ValueError("owned local prefixes must be strings")
        normalized_prefixes = tuple(
            normalize_relative_path(item) for item in self.owned_local_prefixes
        )
        if normalized_prefixes != self.owned_local_prefixes or len(
            set(normalized_prefixes)
        ) != len(normalized_prefixes):
            raise ValueError("owned local prefixes must be normalized and unique")
        prefix_parts = tuple(PurePosixPath(item).parts for item in normalized_prefixes)
        for index, parts in enumerate(prefix_parts):
            if any(
                parts[: len(other)] == other
                for pos, other in enumerate(prefix_parts)
                if pos != index
            ):
                raise ValueError("owned local prefixes must not overlap")
        if (
            type(self.local_capacity_reserve_bytes) is not int
            or self.local_capacity_reserve_bytes < 0
        ):
            raise ValueError("local capacity reserve must be a nonnegative integer")
        if type(self.expected_video_count) is not int or self.expected_video_count < 0:
            raise ValueError("expected video count must be a nonnegative integer")
        if type(self.video_extensions) is not tuple or not self.video_extensions:
            raise ValueError("video extension allowlist must not be empty")
        if any(
            type(item) is not str
            or not item.startswith(".")
            or item != item.lower()
            or "/" in item
            or "\\" in item
            for item in self.video_extensions
        ):
            raise ValueError("video extensions must be lowercase suffixes")
        if len(set(self.video_extensions)) != len(self.video_extensions):
            raise ValueError("video extensions must be unique")
        if (
            type(self.known_no_audio_paths) is not tuple
            or type(self.known_collision_paths) is not tuple
        ):
            raise ValueError("known corpus paths must be immutable tuples")
        if any(
            type(path) is not str
            for path in (*self.known_no_audio_paths, *self.known_collision_paths)
        ):
            raise ValueError("known corpus paths must be strings")
        for path in (*self.known_no_audio_paths, *self.known_collision_paths):
            normalize_relative_path(path)
        if len(set(self.known_no_audio_paths)) != len(self.known_no_audio_paths):
            raise ValueError("known no-audio paths must be unique")
        if len(set(self.known_collision_paths)) != len(self.known_collision_paths):
            raise ValueError("known collision paths must be unique")
        if len(self.known_no_audio_paths) > self.expected_video_count:
            raise ValueError("known no-audio paths exceed expected video count")

    @classmethod
    def for_testing(
        cls,
        *,
        source_root: Path,
        work_root: Path,
        expected_video_count: int,
        known_no_audio_paths: tuple[str, ...] = (),
        known_collision_paths: tuple[str, ...] = (),
    ) -> PipelineConfig:
        model_root = work_root / "models"
        revisions = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40)
        return cls(
            source_root=source_root,
            work_root=work_root,
            owned_local_prefixes=DEFAULT_OWNED_LOCAL_PREFIXES,
            schemas=SchemaVersions(),
            prompts=PromptVersions(),
            vad_model=ModelBinding("test/vad", model_root / "vad", revisions[0]),
            asr_model=ModelBinding("test/asr", model_root / "asr", revisions[1]),
            aligner_model=ModelBinding(
                "test/aligner", model_root / "aligner", revisions[2]
            ),
            primary_translation_model=ModelBinding(
                "test/primary", model_root / "primary", revisions[3]
            ),
            fallback_translation_model=ModelBinding(
                "test/fallback", model_root / "fallback", revisions[4]
            ),
            reviewer_translation_model=ModelBinding(
                "test/reviewer", model_root / "reviewer", revisions[5]
            ),
            stage_settings=StageSettings(),
            video_extensions=DEFAULT_VIDEO_EXTENSIONS,
            local_capacity_reserve_bytes=0,
            expected_video_count=expected_video_count,
            known_no_audio_paths=known_no_audio_paths,
            known_collision_paths=known_collision_paths,
        )

    @classmethod
    def production(
        cls,
        *,
        revisions: ModelRevisions,
        source_root: Path,
        work_root: Path,
        model_root: Path,
        asr_model_path: Path,
        device: str = "auto",
        asr_engine: str = "auto",
    ) -> PipelineConfig:
        if type(revisions) is not ModelRevisions:
            raise ValueError("production requires frozen resolved model revisions")
        return cls(
            source_root=source_root,
            work_root=work_root,
            owned_local_prefixes=DEFAULT_OWNED_LOCAL_PREFIXES,
            schemas=SchemaVersions(),
            prompts=PromptVersions(),
            vad_model=ModelBinding(
                "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                model_root / "fsmn-vad",
                revisions.vad,
            ),
            asr_model=ModelBinding(
                "FunAudioLLM/Fun-ASR-Nano-2512",
                asr_model_path,
                revisions.asr,
            ),
            aligner_model=ModelBinding(
                "Qwen/Qwen3-ForcedAligner-0.6B-hf",
                model_root / "Qwen3-ForcedAligner-0.6B-hf",
                revisions.aligner,
            ),
            primary_translation_model=ModelBinding(
                "Qwen/Qwen3.5-35B-A3B-FP8",
                model_root / "Qwen3.5-35B-A3B-FP8",
                revisions.primary_translation,
            ),
            fallback_translation_model=ModelBinding(
                "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8",
                model_root / "Qwen3-30B-A3B-Instruct-2507-FP8",
                revisions.fallback_translation,
            ),
            reviewer_translation_model=ModelBinding(
                "Qwen/Qwen3.6-27B-FP8",
                model_root / "Qwen3.6-27B-FP8",
                revisions.reviewer_translation,
            ),
            stage_settings=StageSettings(
                vad=VadSettings(device=device),
                asr=AsrSettings(device=device, engine=asr_engine),
                alignment=AlignmentSettings(device=device),
            ),
            video_extensions=DEFAULT_VIDEO_EXTENSIONS,
            local_capacity_reserve_bytes=64 * 1024**3,
            expected_video_count=580,
            known_no_audio_paths=(KNOWN_NO_AUDIO_PATH,),
            known_collision_paths=KNOWN_COLLISION_PATHS,
        )

    def owned_prefix(self, relative_path: str | PurePosixPath) -> str:
        normalized = normalize_relative_path(relative_path)
        parts = PurePosixPath(normalized).parts
        for prefix in self.owned_local_prefixes:
            prefix_parts = PurePosixPath(prefix).parts
            if parts[: len(prefix_parts)] == prefix_parts:
                return prefix
        raise ValueError(f"path is outside task-owned local prefixes: {normalized}")

    def owned_path(self, relative_path: str | PurePosixPath) -> Path:
        normalized = normalize_relative_path(relative_path)
        self.owned_prefix(normalized)
        parts = PurePosixPath(normalized).parts
        return self.work_root.joinpath(*parts)

    def to_canonical_dict(self) -> dict[str, Any]:
        def model(value: ModelBinding) -> dict[str, str]:
            return {
                "model_id": value.model_id,
                "local_path": str(value.local_path),
                "revision": value.revision,
            }

        return {
            "source_root": str(self.source_root),
            "work_root": str(self.work_root),
            "owned_local_prefixes": list(self.owned_local_prefixes),
            "schemas": asdict(self.schemas),
            "prompts": asdict(self.prompts),
            "models": {
                "vad": model(self.vad_model),
                "asr": model(self.asr_model),
                "aligner": model(self.aligner_model),
                "primary_translation": model(self.primary_translation_model),
                "fallback_translation": model(self.fallback_translation_model),
                "reviewer_translation": model(self.reviewer_translation_model),
            },
            "stage_settings": asdict(self.stage_settings),
            "video_extensions": list(self.video_extensions),
            "local_capacity_reserve_bytes": self.local_capacity_reserve_bytes,
            "expected_video_count": self.expected_video_count,
            "known_no_audio_paths": list(self.known_no_audio_paths),
            "known_collision_paths": list(self.known_collision_paths),
        }

    def digest(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()
