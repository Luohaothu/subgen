from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, replace

from subtitle_pipeline.segments import SpeechSegment

MAX_CUE_MS = 8000
SENTENCE_ENDINGS = frozenset("。！？!?")
PHRASE_ENDINGS = frozenset("、，,")
CLOSING_PUNCTUATION = frozenset("」』）)]】》〉〕〗〙〛”’\"'")


@dataclass(frozen=True)
class AlignedToken:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class JapaneseCue:
    cue_id: str
    start_ms: int
    end_ms: int
    text: str

    def with_text(self, text: str) -> JapaneseCue:
        return replace(self, text=text)


def validate_tokens(
    tokens: list[AlignedToken] | tuple[AlignedToken, ...],
    segment_duration_ms: int,
    *,
    transcript: str | None = None,
) -> None:
    if type(segment_duration_ms) is not int or segment_duration_ms <= 0:
        raise ValueError("segment duration must be a positive integer")
    if transcript and not tokens:
        raise ValueError("non-empty transcript requires non-empty alignment tokens")

    previous_end = 0
    for index, token in enumerate(tokens):
        if not isinstance(token, AlignedToken):
            raise ValueError(f"alignment token {index} has an invalid type")
        if type(token.start_ms) is not int or type(token.end_ms) is not int:
            raise ValueError(
                f"alignment token {index} times must be integer milliseconds"
            )
        if not 0 <= token.start_ms < token.end_ms <= segment_duration_ms:
            raise ValueError(
                f"alignment token {index} is outside segment bounds: "
                f"{token.start_ms}-{token.end_ms}"
            )
        if index and token.start_ms < previous_end:
            raise ValueError(f"non-monotonic alignment token at index {index}")
        previous_end = token.end_ms


def _is_gap_text(value: str) -> bool:
    return all(
        char.isspace() or unicodedata.category(char).startswith("P") for char in value
    )


def restore_transcript_text(transcript: str, aligned_words: list[str]) -> list[str]:
    if not isinstance(transcript, str) or not isinstance(aligned_words, list):
        raise ValueError("transcript/alignment mismatch: expected text and a word list")
    if not transcript and not aligned_words:
        return []
    if (
        not transcript
        or not aligned_words
        or any(not isinstance(word, str) or not word for word in aligned_words)
    ):
        raise ValueError("transcript/alignment mismatch: empty text or word")
    if _is_gap_text(transcript[0]):
        raise ValueError("transcript/alignment leading mismatch")

    compact_words = [
        "".join(character for character in word if not _is_gap_text(character))
        for word in aligned_words
    ]
    compact_transcript = "".join(
        character for character in transcript if not _is_gap_text(character)
    )
    normalized_words = [
        unicodedata.normalize("NFKC", word) for word in compact_words if word
    ]
    normalized_transcript = unicodedata.normalize("NFKC", compact_transcript)
    if "".join(normalized_words) != normalized_transcript:
        raise ValueError("transcript/alignment mismatch: lexical text differs")

    restored: list[str] = []
    cursor = 0
    for index, word in enumerate(compact_words):
        start = cursor
        if word:
            remaining = len(word)
            while cursor < len(transcript) and remaining:
                if not _is_gap_text(transcript[cursor]):
                    remaining -= 1
                cursor += 1
            if remaining:
                raise ValueError(f"transcript/alignment mismatch at word {index}")
        else:
            aligned_word = aligned_words[index]
            end = cursor + len(aligned_word)
            if unicodedata.normalize(
                "NFKC", transcript[cursor:end]
            ) != unicodedata.normalize("NFKC", aligned_word):
                raise ValueError(f"transcript/alignment mismatch at word {index}")
            cursor = end
        if index + 1 < len(compact_words):
            if compact_words[index + 1]:
                while cursor < len(transcript) and _is_gap_text(transcript[cursor]):
                    cursor += 1
        else:
            cursor = len(transcript)
        restored.append(transcript[start:cursor])

    if cursor != len(transcript) or "".join(restored) != transcript:
        raise ValueError("transcript/alignment mismatch: text was not preserved")
    return restored


@dataclass(frozen=True)
class _OwnedToken:
    text: str
    start_ms: int
    end_ms: int


def _owned_tokens(
    tokens: list[AlignedToken] | tuple[AlignedToken, ...],
    segment: SpeechSegment,
    audio_offset_ms: int,
) -> list[_OwnedToken]:
    owned: list[_OwnedToken] = []
    for token in tokens:
        if token.end_ms - token.start_ms > MAX_CUE_MS:
            raise ValueError("individual aligned token exceeds 8,000 ms")

        audio_start = segment.decode_start_ms + token.start_ms
        audio_end = segment.decode_start_ms + token.end_ms
        if audio_end <= segment.owner_start_ms or audio_start >= segment.owner_end_ms:
            continue

        clipped_start = max(audio_start, segment.owner_start_ms)
        clipped_end = min(audio_end, segment.owner_end_ms)
        presentation_start = max(0, clipped_start + audio_offset_ms)
        presentation_end = max(0, clipped_end + audio_offset_ms)
        if presentation_end <= presentation_start:
            continue
        owned.append(_OwnedToken(token.text, presentation_start, presentation_end))
    return owned


def _has_ending(token: _OwnedToken, endings: frozenset[str]) -> bool:
    text = token.text.rstrip()
    while text and text[-1] in CLOSING_PUNCTUATION:
        text = text[:-1].rstrip()
    return bool(text) and text[-1] in endings


def _next_cue_end(tokens: list[_OwnedToken], start: int) -> int:
    cue_start_ms = tokens[start].start_ms
    first_sentence: int | None = None
    last_phrase_in_limit: int | None = None
    last_boundary_in_limit: int | None = None

    for index in range(start, len(tokens)):
        duration = tokens[index].end_ms - cue_start_ms
        if duration <= MAX_CUE_MS:
            last_boundary_in_limit = index
            if _has_ending(tokens[index], SENTENCE_ENDINGS):
                if first_sentence is None:
                    first_sentence = index
            if _has_ending(tokens[index], PHRASE_ENDINGS):
                last_phrase_in_limit = index
            continue

        if first_sentence is not None:
            return first_sentence
        if last_phrase_in_limit is not None:
            return last_phrase_in_limit
        if last_boundary_in_limit is not None:
            return last_boundary_in_limit
        raise ValueError("no aligned boundary exists within 8,000 ms")

    if first_sentence is not None:
        return first_sentence
    if last_boundary_in_limit is None:
        raise ValueError("cannot build a positive-duration cue")
    return last_boundary_in_limit


def build_cues(
    tokens: list[AlignedToken] | tuple[AlignedToken, ...],
    *,
    segment: SpeechSegment,
    audio_offset_ms: int,
) -> list[JapaneseCue]:
    if not isinstance(segment, SpeechSegment):
        raise ValueError("segment must be a SpeechSegment")
    if type(audio_offset_ms) is not int:
        raise ValueError("audio_offset_ms must be an integer")
    duration_ms = segment.decode_end_ms - segment.decode_start_ms
    validate_tokens(tokens, duration_ms)
    owned = _owned_tokens(tokens, segment, audio_offset_ms)

    cues: list[JapaneseCue] = []
    cursor = 0
    while cursor < len(owned):
        end_index = _next_cue_end(owned, cursor)
        selected = owned[cursor : end_index + 1]
        start_ms = selected[0].start_ms
        end_ms = selected[-1].end_ms
        if end_ms <= start_ms or end_ms - start_ms > MAX_CUE_MS:
            raise ValueError("cue duration must be within 1..8,000 ms")
        if cues and start_ms < cues[-1].end_ms:
            raise ValueError("generated cues overlap")
        text = "".join(token.text for token in selected)
        if not text:
            raise ValueError("generated cue text must be non-empty")
        cues.append(
            JapaneseCue(
                f"{segment.segment_id}-cue-{len(cues):04d}",
                start_ms,
                end_ms,
                text,
            )
        )
        cursor = end_index + 1
    return cues


def timestamp_digest(cues: list[JapaneseCue] | tuple[JapaneseCue, ...]) -> str:
    payload = [[cue.cue_id, cue.start_ms, cue.end_ms] for cue in cues]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
