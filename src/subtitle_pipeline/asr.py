from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

LATIN_RE = re.compile(r"[A-Za-z]")
MIN_REPETITION_COUNT = 12
MAX_REPETITION_UNIT = 16
_NONLEXICAL_REPETITION_CHARS = frozenset("あいうえおんっぁぃぅぇぉー")


@dataclass(frozen=True)
class AsrResult:
    segment_id: str
    text: str
    raw: dict[str, Any]
    error: dict[str, Any] | None = None


def normalize_asr_batch(segment_ids: list[str], raw: object) -> list[AsrResult]:
    items = [raw] if isinstance(raw, dict) else raw
    if not isinstance(items, list) or len(items) != len(segment_ids):
        count = len(items) if isinstance(items, list) else 0
        raise ValueError(
            f"model returned {count} results for {len(segment_ids)} inputs"
        )

    rows: list[AsrResult] = []
    for segment_id, item in zip(segment_ids, items):
        if not isinstance(item, dict):
            item = {"text": str(item)}
        text = trim_abnormal_repetition_edges(str(item.get("text", "")).strip())
        rows.append(
            AsrResult(
                segment_id,
                text,
                item,
                None,
            )
        )
    return rows


def _trim_abnormal_repetition_tail_once(text: str) -> str:
    compact: list[str] = []
    source_positions: list[int] = []
    for index, character in enumerate(text):
        if character.isspace() or unicodedata.category(character).startswith("P"):
            continue
        normalized = unicodedata.normalize("NFKC", character)
        compact.extend(normalized)
        source_positions.extend([index] * len(normalized))

    size = len(compact)
    earliest: int | None = None
    for width in range(1, min(MAX_REPETITION_UNIT, size) + 1):
        minimum = width * MIN_REPETITION_COUNT
        for start in range(0, size - minimum + 1):
            unit = compact[start : start + width]
            matched = 0
            while (
                start + matched < size
                and compact[start + matched] == unit[matched % width]
            ):
                matched += 1
            if matched < minimum:
                continue
            remainder = compact[start + matched :]
            short_unit_corruption = len(remainder) <= MAX_REPETITION_UNIT and set(
                remainder
            ).issubset(unit)
            variable_nonlexical_tail = set(unit).issubset(
                _NONLEXICAL_REPETITION_CHARS
            ) and set(compact[start:]).issubset(_NONLEXICAL_REPETITION_CHARS)
            if not remainder or short_unit_corruption or variable_nonlexical_tail:
                earliest = start if earliest is None else min(earliest, start)
                break

    if earliest is None:
        return text
    if earliest == 0:
        return ""
    return text[: source_positions[earliest]].rstrip().rstrip("、，,")


def _trim_corrupted_repetition_tail_once(text: str) -> str:
    compact: list[str] = []
    source_positions: list[int] = []
    for index, character in enumerate(text):
        if character.isspace() or unicodedata.category(character).startswith("P"):
            continue
        normalized = unicodedata.normalize("NFKC", character)
        compact.extend(normalized)
        source_positions.extend([index] * len(normalized))

    size = len(compact)
    earliest: int | None = None
    for width in range(1, min(MAX_REPETITION_UNIT, size) + 1):
        minimum = width * MIN_REPETITION_COUNT
        for start in range(0, size - minimum + 1):
            unit = compact[start : start + width]
            matched = 0
            while (
                start + matched < size
                and compact[start + matched] == unit[matched % width]
            ):
                matched += 1
            if matched < minimum:
                continue
            remainder = compact[start + matched :]
            short_unit_corruption = len(remainder) <= MAX_REPETITION_UNIT and set(
                remainder
            ).issubset(unit)
            variable_nonlexical_tail = set(unit).issubset(
                _NONLEXICAL_REPETITION_CHARS
            ) and set(compact[start:]).issubset(_NONLEXICAL_REPETITION_CHARS)
            if short_unit_corruption or variable_nonlexical_tail:
                earliest = start if earliest is None else min(earliest, start)
                break

    if earliest is None:
        return text
    if earliest == 0:
        return ""
    return text[: source_positions[earliest]].rstrip().rstrip("、，,")


def trim_abnormal_repetition_tail(text: str) -> str:
    """Remove nested decoder-degenerate suffixes while preserving real text."""
    trimmed = text
    while True:
        next_trimmed = _trim_abnormal_repetition_tail_once(trimmed)
        if next_trimmed == trimmed:
            return trimmed
        trimmed = next_trimmed


def trim_abnormal_repetition_edges(text: str) -> str:
    """Trim decoder-degenerate periodic text at either edge of a transcript."""
    trimmed = trim_abnormal_repetition_tail(text.rstrip("\ufffd"))
    compact: list[str] = []
    source_positions: list[int] = []
    for index, character in enumerate(trimmed):
        if character.isspace() or unicodedata.category(character).startswith("P"):
            continue
        normalized = unicodedata.normalize("NFKC", character)
        compact.extend(normalized)
        source_positions.extend([index] * len(normalized))

    longest = 0
    size = len(compact)
    for width in range(1, min(MAX_REPETITION_UNIT, size) + 1):
        unit = compact[:width]
        end = width
        while end < size and compact[end] == unit[end % width]:
            end += 1
        if end < size and end // width >= MIN_REPETITION_COUNT:
            longest = max(longest, end)

    if not longest:
        return trimmed
    return trimmed[source_positions[longest] :].lstrip()


def trim_corrupted_repetition_edges(text: str) -> str:
    """Trim damaged decoder loops when stronger alignment evidence is absent."""
    trimmed = text.rstrip("\ufffd")
    while True:
        next_trimmed = _trim_corrupted_repetition_tail_once(trimmed)
        if next_trimmed == trimmed:
            return trim_abnormal_repetition_edges(trimmed)
        trimmed = next_trimmed


def quality_flags(text: str) -> list[str]:
    flags: list[str] = []
    compact = "".join(text.split())
    if not compact:
        flags.append("empty")

    if trim_abnormal_repetition_edges(text) != text:
        flags.append("abnormal_repetition")

    letters = [char for char in compact if char.isalpha()]
    latin = sum(1 for char in letters if LATIN_RE.fullmatch(char))
    if len(letters) >= 8 and latin / len(letters) >= 0.8:
        flags.append("unexpected_language")
    return flags
