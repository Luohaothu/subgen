from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

VALID_STATUSES = frozenset(
    {"pending", "running", "ok", "retryable_error", "blocked", "no_audio"}
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class FrozenDict(Mapping[str, Any]):
    __slots__ = ("_items", "_values")

    def __init__(self, items: tuple[tuple[str, Any], ...]):
        self._items = items
        self._values = dict(items)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return repr(self._values)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return False
        return dict(self.items()) == dict(other.items())


def _freeze_json(value: object) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("stage payload floats must be finite")
        return value
    if isinstance(value, Mapping):
        rows: list[tuple[str, object]] = []
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("stage payload keys must be strings")
            rows.append((key, _freeze_json(item)))
        return FrozenDict(tuple(sorted(rows)))
    if type(value) in {list, tuple}:
        return tuple(_freeze_json(item) for item in value)
    raise ValueError(f"stage payload is not JSON-compatible: {type(value).__name__}")


def _thaw_json(value: object) -> object:
    if isinstance(value, FrozenDict):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _optional_nonblank(value: object, field: str) -> None:
    if value is not None and (type(value) is not str or not value.strip()):
        raise ValueError(f"{field} must be a nonblank string or None")


def _optional_digest(value: object, field: str) -> None:
    if value is not None and (
        type(value) is not str or _SHA256_RE.fullmatch(value) is None
    ):
        raise ValueError(f"{field} must be lowercase SHA-256 or None")


def _optional_timestamp(value: object, field: str) -> None:
    if value is None:
        return
    if type(value) not in {int, float} or value < 0:
        raise ValueError(f"{field} must be finite nonnegative epoch seconds or None")
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"{field} must be finite nonnegative epoch seconds or None")


def stable_video_id(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/")
    return sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VideoRecord:
    video_id: str
    relative_path: str
    source_bytes: int
    duration_ms: int
    audio_stream_index: int | None
    audio_offset_ms: int | None
    target_relative_path: str
    target_exists: bool
    source_mtime_ns: int | None = None
    source_device: int | None = None
    source_inode: int | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    audio_sample_rate_hz: int | None = None
    audio_duration_ms: int | None = None
    audio_start_ms: int | None = None
    audio_language: str | None = None
    format_start_ms: int | None = None
    video_start_ms: int | None = None
    presentation_origin_ms: int | None = None
    probe_sha256: str | None = None
    inventory_schema_version: str | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.video_id, "video_id"),
            (self.relative_path, "relative_path"),
            (self.target_relative_path, "target_relative_path"),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{field} must be a non-empty string")
        for value, field in (
            (self.source_bytes, "source_bytes"),
            (self.duration_ms, "duration_ms"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{field} must be a nonnegative integer")
        if self.audio_stream_index is not None and (
            type(self.audio_stream_index) is not int or self.audio_stream_index < 0
        ):
            raise ValueError("audio_stream_index must be nonnegative or None")
        if self.audio_offset_ms is not None and type(self.audio_offset_ms) is not int:
            raise ValueError("audio_offset_ms must be an integer or None")
        if type(self.target_exists) is not bool:
            raise ValueError("target_exists must be boolean")
        for field in ("source_mtime_ns", "source_device", "source_inode"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field} must be a nonnegative integer or None")
        for field in ("audio_codec", "audio_language", "inventory_schema_version"):
            value = getattr(self, field)
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError(f"{field} must be a nonblank string or None")
        for field in ("audio_channels", "audio_sample_rate_hz"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{field} must be a positive integer or None")
        if self.audio_duration_ms is not None and (
            type(self.audio_duration_ms) is not int or self.audio_duration_ms < 0
        ):
            raise ValueError("audio_duration_ms must be nonnegative or None")
        for field in (
            "audio_start_ms",
            "format_start_ms",
            "video_start_ms",
            "presentation_origin_ms",
        ):
            value = getattr(self, field)
            if value is not None and type(value) is not int:
                raise ValueError(f"{field} must be an integer or None")
        _optional_digest(self.probe_sha256, "probe_sha256")
        if self.audio_stream_index is None and any(
            getattr(self, field) is not None
            for field in (
                "audio_codec",
                "audio_channels",
                "audio_sample_rate_hz",
                "audio_duration_ms",
                "audio_start_ms",
                "audio_language",
            )
        ):
            raise ValueError("no-audio record contains selected audio evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in (
                "video_id",
                "relative_path",
                "source_bytes",
                "duration_ms",
                "audio_stream_index",
                "audio_offset_ms",
                "target_relative_path",
                "target_exists",
                "source_mtime_ns",
                "source_device",
                "source_inode",
                "audio_codec",
                "audio_channels",
                "audio_sample_rate_hz",
                "audio_duration_ms",
                "audio_start_ms",
                "audio_language",
                "format_start_ms",
                "video_start_ms",
                "presentation_origin_ms",
                "probe_sha256",
                "inventory_schema_version",
            )
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VideoRecord:
        required = {
            "video_id",
            "relative_path",
            "source_bytes",
            "duration_ms",
            "audio_stream_index",
            "audio_offset_ms",
            "target_relative_path",
            "target_exists",
        }
        optional = {
            "source_mtime_ns",
            "source_device",
            "source_inode",
            "audio_codec",
            "audio_channels",
            "audio_sample_rate_hz",
            "audio_duration_ms",
            "audio_start_ms",
            "audio_language",
            "format_start_ms",
            "video_start_ms",
            "presentation_origin_ms",
            "probe_sha256",
            "inventory_schema_version",
        }
        if (
            not isinstance(value, Mapping)
            or not required.issubset(value)
            or not set(value).issubset(required | optional)
        ):
            raise ValueError("invalid video record fields")
        return cls(**dict(value))
