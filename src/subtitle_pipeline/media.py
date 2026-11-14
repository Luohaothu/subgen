from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import av

from .config import AudioSelectionPolicy
from .domain import VideoRecord, stable_video_id


class MediaValidationError(ValueError):
    pass


class MediaProbeError(RuntimeError):
    pass


def _seconds(
    value: object,
    field: str,
    *,
    required: bool = False,
    nonnegative: bool = False,
    positive: bool = False,
) -> float | None:
    if value in (None, "N/A", ""):
        if required:
            raise MediaValidationError(f"{field} is required")
        return None
    if type(value) is bool:
        raise MediaValidationError(f"{field} must be finite seconds")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaValidationError(f"{field} must be finite seconds") from exc
    if not math.isfinite(result):
        raise MediaValidationError(f"{field} must be finite seconds")
    if positive and result <= 0:
        raise MediaValidationError(f"{field} must be finite positive seconds")
    if nonnegative and result < 0:
        raise MediaValidationError(f"{field} must be finite nonnegative seconds")
    return result


def _milliseconds(value: float | None) -> int | None:
    return None if value is None else round(value * 1000)


def _positive_int(value: object, field: str) -> int:
    if type(value) is int:
        result = value
    elif type(value) is str and value.isdecimal():
        result = int(value)
    else:
        raise MediaValidationError(f"{field} must be a positive integer")
    if result <= 0:
        raise MediaValidationError(f"{field} must be a positive integer")
    return result


def _canonical_probe_bytes(probe: Mapping[str, Any]) -> bytes:
    if not isinstance(probe, Mapping):
        raise MediaValidationError("media probe result must be an object")
    try:
        encoded = json.dumps(
            probe,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MediaValidationError(
            f"media probe result is not canonical JSON: {exc}"
        ) from exc
    return encoded.encode("utf-8")


def probe_sha256(probe: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_probe_bytes(probe)).hexdigest()


def stash_target_path(relative_video: Path) -> Path:
    return relative_video.with_name(relative_video.stem + ".zh.srt")


def presentation_origin_ms(
    format_start: float | None, video_start: float | None
) -> int:
    origin_candidates = [
        value for value in (format_start, video_start) if value is not None
    ]
    origin = min(origin_candidates) if origin_candidates else 0.0
    return round(origin * 1000)


def audio_offset_ms(
    audio_start: float | None,
    format_start: float | None,
    video_start: float | None,
) -> int:
    origin_ms = presentation_origin_ms(format_start, video_start)
    start_ms = round(audio_start * 1000) if audio_start is not None else origin_ms
    return start_ms - origin_ms


def _validated_streams(probe: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if "streams" not in probe:
        raise MediaValidationError("media probe streams field is required")
    streams = probe["streams"]
    if type(streams) is not list:
        raise MediaValidationError("media probe streams must be a list")
    result: list[Mapping[str, Any]] = []
    indices: set[int] = set()
    for stream in streams:
        if not isinstance(stream, Mapping):
            raise MediaValidationError("media probe stream must be an object")
        index = stream.get("index")
        codec_type = stream.get("codec_type")
        if type(index) is not int or index < 0:
            raise MediaValidationError("stream index must be a nonnegative integer")
        if index in indices:
            raise MediaValidationError("media probe stream indices must be unique")
        if type(codec_type) is not str or not codec_type.strip():
            raise MediaValidationError("stream codec_type must be a nonblank string")
        disposition = stream.get("disposition")
        if disposition is not None:
            if not isinstance(disposition, Mapping):
                raise MediaValidationError("stream disposition must be an object")
            default = disposition.get("default", 0)
            if type(default) is not int or default not in {0, 1}:
                raise MediaValidationError(
                    "stream disposition.default must be zero or one"
                )
        _stream_language(stream)
        _seconds(stream.get("start_time"), "stream start_time")
        _seconds(stream.get("duration"), "stream duration", nonnegative=True)
        indices.add(index)
        result.append(stream)
    return result


def _stream_language(stream: Mapping[str, Any]) -> str | None:
    tags = stream.get("tags")
    if tags is None:
        return None
    if not isinstance(tags, Mapping):
        raise MediaValidationError("stream tags must be an object")
    language = tags.get("language")
    if language in (None, ""):
        return None
    if type(language) is not str or not language.strip():
        raise MediaValidationError("audio language tag must be a nonblank string")
    return language.lower()


def _select_audio(
    streams: Sequence[Mapping[str, Any]],
    *,
    policy: AudioSelectionPolicy,
) -> Mapping[str, Any] | None:
    if type(policy) is not AudioSelectionPolicy:
        raise MediaValidationError("audio selection policy must be frozen")
    preference = {tag: index for index, tag in enumerate(policy.language_preferences)}
    candidates = [stream for stream in streams if stream["codec_type"] == "audio"]
    if not candidates:
        return None

    def rank(stream: Mapping[str, Any]) -> tuple[int, int, int, int]:
        language = _stream_language(stream)
        disposition = stream.get("disposition") or {}
        is_default = disposition.get("default", 0) == 1
        if language in preference:
            default_rank = 0 if policy.prefer_default_disposition and is_default else 1
            return (0, preference[language], default_rank, stream["index"])
        if policy.prefer_default_disposition and is_default:
            return (1, 0, 0, stream["index"])
        if policy.prefer_untagged and language in {None, "und"}:
            return (2, 0, 0, stream["index"])
        return (3, 0, 0, stream["index"])

    return min(candidates, key=rank)


def parse_probe(
    relative_path: str,
    source_bytes: int,
    probe: dict,
    target_exists: bool,
    *,
    source_mtime_ns: int | None = None,
    source_device: int | None = None,
    source_inode: int | None = None,
    inventory_schema_version: str | None = None,
    audio_selection_policy: AudioSelectionPolicy = AudioSelectionPolicy(),
) -> VideoRecord:
    if type(probe) is not dict:
        raise MediaValidationError("media probe result must be a dictionary")
    fmt = probe.get("format")
    if not isinstance(fmt, Mapping):
        raise MediaValidationError("media probe format must be an object")
    duration = _seconds(
        fmt.get("duration"), "format duration", required=True, positive=True
    )
    streams = _validated_streams(probe)
    audio = _select_audio(streams, policy=audio_selection_policy)
    videos = [stream for stream in streams if stream["codec_type"] == "video"]
    video = min(videos, key=lambda stream: stream["index"]) if videos else None

    format_start = _seconds(fmt.get("start_time"), "format start_time")
    video_start = _seconds(
        None if video is None else video.get("start_time"), "video start_time"
    )
    origin_ms = presentation_origin_ms(format_start, video_start)
    target = stash_target_path(Path(relative_path))

    audio_index: int | None = None
    offset: int | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    audio_duration: int | None = None
    audio_start_ms: int | None = None
    audio_language: str | None = None
    if audio is not None:
        audio_index = audio["index"]
        codec = audio.get("codec_name")
        if type(codec) is not str or not codec.strip():
            raise MediaValidationError("selected audio codec_name is required")
        audio_codec = codec
        audio_channels = _positive_int(audio.get("channels"), "audio channels")
        audio_sample_rate = _positive_int(audio.get("sample_rate"), "audio sample_rate")
        audio_start = _seconds(audio.get("start_time"), "audio start_time")
        stream_duration = _seconds(
            audio.get("duration"), "audio duration", nonnegative=True
        )
        offset = audio_offset_ms(audio_start, format_start, video_start)
        audio_start_ms = _milliseconds(audio_start)
        audio_duration = _milliseconds(stream_duration)
        audio_language = _stream_language(audio)

    return VideoRecord(
        stable_video_id(relative_path),
        relative_path,
        source_bytes,
        round(duration * 1000),
        audio_index,
        offset,
        target.as_posix(),
        target_exists,
        source_mtime_ns=source_mtime_ns,
        source_device=source_device,
        source_inode=source_inode,
        audio_codec=audio_codec,
        audio_channels=audio_channels,
        audio_sample_rate_hz=audio_sample_rate,
        audio_duration_ms=audio_duration,
        audio_start_ms=audio_start_ms,
        audio_language=audio_language,
        format_start_ms=_milliseconds(format_start),
        video_start_ms=_milliseconds(video_start),
        presentation_origin_ms=origin_ms,
        probe_sha256=probe_sha256(probe),
        inventory_schema_version=inventory_schema_version,
    )


def _decode_process_text(value: object, field: str) -> str:
    if value is None:
        return ""
    if type(value) is str:
        return value
    if type(value) is bytes:
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MediaProbeError(f"media probe {field} is not UTF-8") from exc
    raise MediaProbeError(f"media probe {field} has an invalid type")


def _format_seconds(value: float) -> str:
    return f"{value:.6f}"


def probe_media(source: Path, *, timeout_seconds: float = 120) -> dict:
    """Read the metadata that the inventory needs through libavformat."""
    if not isinstance(source, Path):
        raise MediaProbeError("media source must be a Path")
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise MediaProbeError("media timeout must be finite and positive")
    try:
        with av.open(os.fspath(source), timeout=timeout_seconds) as container:
            if container.duration is None:
                raise MediaProbeError(f"media has no duration: {source}")
            duration = container.duration / 1_000_000
            if not math.isfinite(duration) or duration <= 0:
                raise MediaProbeError(f"media duration is invalid: {source}")
            format_value = {"duration": _format_seconds(duration)}
            if container.start_time is not None:
                format_value["start_time"] = _format_seconds(
                    container.start_time / 1_000_000
                )
            streams: list[dict[str, object]] = []
            for stream in container.streams:
                codec = stream.codec_context
                codec_type = codec.type
                codec_name = codec.name
                if not isinstance(codec_type, str) or not codec_type:
                    raise MediaProbeError(f"stream has no codec type: {source}")
                if not isinstance(codec_name, str) or not codec_name:
                    raise MediaProbeError(f"stream has no codec name: {source}")
                item: dict[str, object] = {
                    "index": stream.index,
                    "codec_name": codec_name,
                    "codec_type": codec_type,
                }
                if codec_type == "audio":
                    channels = codec.channels
                    sample_rate = codec.sample_rate
                    if type(channels) is not int or channels <= 0:
                        raise MediaProbeError(f"audio channels are invalid: {source}")
                    if type(sample_rate) is not int or sample_rate <= 0:
                        raise MediaProbeError(f"audio sample rate is invalid: {source}")
                    item["channels"] = channels
                    item["sample_rate"] = str(sample_rate)
                if stream.start_time is not None:
                    item["start_time"] = _format_seconds(
                        float(stream.start_time * stream.time_base)
                    )
                if stream.duration is not None:
                    item["duration"] = _format_seconds(
                        float(stream.duration * stream.time_base)
                    )
                language = stream.metadata.get("language")
                if isinstance(language, str) and language:
                    item["tags"] = {"language": language}
                item["disposition"] = {"default": int(bool(stream.disposition.default))}
                streams.append(item)
            value = {
                "programs": [],
                "stream_groups": [],
                "format": format_value,
                "streams": streams,
            }
            _validated_streams(value)
            _seconds(
                format_value.get("duration"),
                "format duration",
                required=True,
                positive=True,
            )
            _seconds(format_value.get("start_time"), "format start_time")
            return value
    except MediaProbeError:
        raise
    except (av.error.FFmpegError, OSError, ValueError, TypeError) as exc:
        raise MediaProbeError(f"cannot probe {source}: {exc}") from exc


class MediaExtractionError(RuntimeError):
    pass


def extract_audio(
    source: Path,
    stream_index: int,
    expected_ms: int,
    destination: Path,
    *,
    timeout_seconds: float = 14_400,
) -> None:
    """Decode one audio stream, resample it, and write a trimmed FLAC file."""
    if not isinstance(source, Path) or not isinstance(destination, Path):
        raise MediaExtractionError("source and destination must be Paths")
    if type(stream_index) is not int or stream_index < 0:
        raise MediaExtractionError("audio stream index must be nonnegative")
    if type(expected_ms) is not int or expected_ms <= 0:
        raise MediaExtractionError("expected duration must be a positive integer")
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise MediaExtractionError("media timeout must be finite and positive")
    try:
        with av.open(os.fspath(source), timeout=timeout_seconds) as container:
            stream = next(
                (
                    candidate
                    for candidate in container.streams
                    if candidate.index == stream_index
                ),
                None,
            )
            if stream is None or stream.codec_context.type != "audio":
                raise MediaExtractionError(
                    f"audio stream {stream_index} is missing from {source}"
                )
            graph = av.filter.Graph()
            source_node = graph.add_abuffer(template=stream)
            resample_node = graph.add("aresample", "async=1000:first_pts=0")
            trim_node = graph.add("atrim", f"end={expected_ms / 1000:.6f}")
            sink = graph.add("abuffersink")
            source_node.link_to(resample_node)
            resample_node.link_to(trim_node)
            trim_node.link_to(sink)
            graph.configure()
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            with av.open(os.fspath(destination), "w", format="flac") as output:
                output_stream = output.add_stream("flac", rate=16000, layout="mono")

                def mux(frame: av.AudioFrame) -> None:
                    for resampled in resampler.resample(frame):
                        for packet in output_stream.encode(resampled):
                            output.mux(packet)

                for frame in container.decode(stream):
                    source_node.push(frame)
                source_node.push(None)
                while True:
                    try:
                        mux(sink.pull())
                    except (av.error.BlockingIOError, av.error.EOFError):
                        break
                for resampled in resampler.resample(None):
                    for packet in output_stream.encode(resampled):
                        output.mux(packet)
                for packet in output_stream.encode(None):
                    output.mux(packet)
    except MediaExtractionError:
        raise
    except (av.error.FFmpegError, OSError, ValueError, TypeError) as exc:
        raise MediaExtractionError(f"cannot extract {source}: {exc}") from exc
