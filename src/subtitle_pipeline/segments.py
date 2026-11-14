from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import soundfile as sf


@dataclass(frozen=True)
class SpeechSegment:
    segment_id: str
    index: int
    owner_start_ms: int
    owner_end_ms: int
    decode_start_ms: int
    decode_end_ms: int


def normalize_vad(
    video_id: str,
    raw: list[list[int]],
    duration_ms: int,
    padding_ms: int = 300,
    max_span_ms: int = 30000,
) -> list[SpeechSegment]:
    if not isinstance(video_id, str) or not video_id:
        raise ValueError("video_id must be a non-empty string")
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("duration_ms must be a positive integer")
    if type(padding_ms) is not int or padding_ms < 0:
        raise ValueError("padding_ms must be a non-negative integer")
    if type(max_span_ms) is not int or not 1 <= max_span_ms <= 30000:
        raise ValueError("max_span_ms must be in 1..30000")
    if not isinstance(raw, list):
        raise ValueError("VAD intervals must be a list")

    merged: list[list[int]] = []
    previous_raw_start: int | None = None
    for interval in raw:
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or any(type(value) is not int for value in interval)
        ):
            raise ValueError("each VAD interval must be a two-integer list")
        start, end = interval
        if previous_raw_start is not None and start < previous_raw_start:
            raise ValueError("VAD spans are not ordered")
        previous_raw_start = start
        if end <= start:
            raise ValueError(f"non-positive VAD span: {start}-{end}")
        start = max(0, start)
        end = min(duration_ms, end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    result: list[SpeechSegment] = []
    index = 0
    for start, bounded_end in merged:
        cursor = start
        while cursor < bounded_end:
            owner_end = min(cursor + max_span_ms, bounded_end)
            decode_start = max(0, cursor - padding_ms)
            decode_end = min(duration_ms, owner_end + padding_ms)
            result.append(
                SpeechSegment(
                    f"{video_id}-seg-{index:08d}",
                    index,
                    cursor,
                    owner_end,
                    decode_start,
                    decode_end,
                )
            )
            index += 1
            cursor = owner_end
    return result


def materialize_wav_batch(
    flac: Path,
    segments: list[SpeechSegment],
    out_dir: Path,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    with sf.SoundFile(flac) as src:
        if src.samplerate != 16000 or src.channels != 1:
            raise ValueError(
                f"expected 16k mono FLAC, got {src.samplerate}Hz/{src.channels}ch"
            )
        rounded_audio_end_ms = round(src.frames * 1000 / src.samplerate)
        for segment in segments:
            start = round(segment.decode_start_ms * src.samplerate / 1000)
            end = round(segment.decode_end_ms * src.samplerate / 1000)
            if (
                end > src.frames
                and segment.decode_end_ms == rounded_audio_end_ms
                and start < src.frames
            ):
                end = src.frames
            frames = end - start
            if start < 0 or frames <= 0 or end > src.frames:
                raise ValueError(
                    "decode interval exceeds audio: "
                    f"{segment.decode_start_ms}-{segment.decode_end_ms}ms"
                )
            src.seek(start)
            samples = src.read(frames=frames, dtype="int16")
            if len(samples) != frames:
                raise ValueError(
                    f"short audio read for {segment.segment_id}: "
                    f"expected {frames}, got {len(samples)}"
                )
            path = out_dir / f"{segment.segment_id}.wav"
            sf.write(path, samples, src.samplerate, subtype="PCM_16")
            result[segment.segment_id] = path
    return result
