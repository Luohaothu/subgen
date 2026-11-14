from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from subtitle_pipeline.translation import (
    HAN_RE,
    KANA_RE,
    MissingTranslationDependency,
    load_simplifier,
    translation_flags,
)

TIMESTAMP_RE = re.compile(
    r"(?P<sh>[0-9]{2}):(?P<sm>[0-5][0-9]):(?P<ss>[0-5][0-9]),(?P<sms>[0-9]{3})"
    r" --> "
    r"(?P<eh>[0-9]{2}):(?P<em>[0-5][0-9]):(?P<es>[0-5][0-9]),(?P<ems>[0-9]{3})"
)
INDEX_RE = re.compile(r"[1-9][0-9]*")
META_BOILERPLATE = frozenset(
    {
        "字幕",
        "中文字幕",
        "翻译",
        "译文",
        "无字幕",
        "暂无字幕",
        "待翻译",
        "暂无翻译",
    }
)
META_TOKENS = (
    "人工智能",
    "简体中文",
    "中文字幕",
    "翻译结果",
    "字幕内容",
    "如下所示",
    "暂无",
    "以下",
    "下面",
    "上述",
    "这是",
    "此处",
    "这里",
    "没有",
    "缺失",
    "可用",
    "视频",
    "中文",
    "字幕",
    "翻译",
    "译文",
    "结果",
    "内容",
    "如下",
    "所示",
    "机器",
    "自动",
    "生成",
    "待",
    "无",
    "本",
    "片",
    "由",
    "是",
    "为",
    "的",
    "组",
    "ai",
    "by",
)
VALIDATION_FLAG_ORDER = (
    "bad_sequence",
    "bad_timestamp",
    "non_positive_duration",
    "overlap",
    "past_video_end",
    "empty_text",
    "kana_remaining",
    "traditional_character",
    "not_simplified_chinese",
)


class SrtParseError(ValueError):
    def __init__(self, code: str, message: str):
        if code not in VALIDATION_FLAG_ORDER:
            raise ValueError(f"invalid SRT parse error code: {code}")
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Cue:
    cue_id: str
    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self) -> None:
        if type(self.cue_id) is not str or not self.cue_id.strip():
            raise ValueError("cue ID must be a non-empty string")
        if (
            type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or self.start_ms < 0
            or self.end_ms < 0
        ):
            raise ValueError("cue timestamps must be non-negative integer milliseconds")
        _validate_cue_text(self.text)

    def with_text(self, text: str) -> Cue:
        return replace(self, text=text)


def _validate_cue_text(text: object) -> None:
    if type(text) is not str or not text.strip():
        raise ValueError("cue text must be a non-empty string")
    if any(
        char == "\ufffd"
        or (char != "\n" and unicodedata.category(char).startswith("C"))
        for char in text
    ):
        raise ValueError("cue text contains a forbidden control/replacement character")
    if any(not line.strip() for line in text.split("\n")):
        raise ValueError("cue text contains an empty line")


def _timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    if hours > 99:
        raise ValueError("SRT timestamps cannot exceed 99 hours")
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def render_srt(cues: Iterable[Cue]) -> str:
    ordered = tuple(cues)
    if any(not isinstance(cue, Cue) for cue in ordered):
        raise ValueError("render_srt requires Cue values")
    ids = [cue.cue_id for cue in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("SRT cue IDs must be unique")
    blocks = [
        f"{index}\n{_timestamp(cue.start_ms)} --> {_timestamp(cue.end_ms)}\n"
        f"{cue.text}\n\n"
        for index, cue in enumerate(ordered, 1)
    ]
    return "".join(blocks)


def _decode_srt(value: str | bytes) -> str:
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise SrtParseError("bad_sequence", f"invalid UTF-8: {exc}") from exc
    elif type(value) is str:
        text = value
    else:
        raise SrtParseError("bad_sequence", "SRT input must be str or bytes")
    if text.startswith("\ufeff"):
        raise SrtParseError("bad_sequence", "UTF-8 BOM is not allowed")
    if "\r" in text:
        without_crlf = text.replace("\r\n", "")
        if "\r" in without_crlf:
            raise SrtParseError("bad_sequence", "CR-only line endings are invalid")
        text = text.replace("\r\n", "\n")
    return text


def _parse_timestamp(line: str) -> tuple[int, int]:
    match = TIMESTAMP_RE.fullmatch(line)
    if match is None:
        raise SrtParseError("bad_timestamp", f"malformed SRT timestamp: {line!r}")

    def millis(prefix: str) -> int:
        return (
            int(match.group(prefix + "h")) * 3_600_000
            + int(match.group(prefix + "m")) * 60_000
            + int(match.group(prefix + "s")) * 1000
            + int(match.group(prefix + "ms"))
        )

    return millis("s"), millis("e")


def parse_srt(value: str | bytes) -> tuple[Cue, ...]:
    text = _decode_srt(value)
    if not text:
        raise SrtParseError("bad_sequence", "SRT is empty")
    if text.endswith("\n\n\n"):
        raise SrtParseError("bad_sequence", "SRT has extra trailing blank lines")
    if text.endswith("\n\n"):
        body = text[:-2]
    elif text.endswith("\n"):
        body = text[:-1]
    else:
        body = text
    if not body:
        raise SrtParseError("bad_sequence", "SRT is empty")

    blocks = body.split("\n\n")
    if any(not block for block in blocks):
        raise SrtParseError("bad_sequence", "SRT contains an empty block")
    result: list[Cue] = []
    for expected_index, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 2:
            raise SrtParseError("bad_sequence", "SRT block is incomplete")
        if INDEX_RE.fullmatch(lines[0]) is None or lines[0] != str(expected_index):
            raise SrtParseError(
                "bad_sequence",
                f"SRT index must be contiguous at block {expected_index}",
            )
        start_ms, end_ms = _parse_timestamp(lines[1])
        if len(lines) < 3 or not lines[2:]:
            raise SrtParseError("empty_text", f"SRT cue {expected_index} is empty")
        if any(
            INDEX_RE.fullmatch(lines[index]) is not None
            and TIMESTAMP_RE.fullmatch(lines[index + 1]) is not None
            for index in range(2, len(lines) - 1)
        ):
            raise SrtParseError(
                "bad_sequence",
                f"SRT cue {expected_index} contains an unseparated next block",
            )
        cue_text = "\n".join(lines[2:])
        try:
            result.append(Cue(str(expected_index), start_ms, end_ms, cue_text))
        except ValueError as exc:
            raise SrtParseError(
                "empty_text",
                f"invalid text in SRT cue {expected_index}: {exc}",
            ) from exc
    return tuple(result)


def _ordered_flags(values: set[str]) -> list[str]:
    return [flag for flag in VALIDATION_FLAG_ORDER if flag in values]


def _language_content(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace()
        and not unicodedata.category(char).startswith(("P", "S", "C"))
    )


def validate_srt(
    value: str | bytes,
    video_duration_ms: int,
    *,
    simplifier: Callable[[str], str] | None = None,
) -> list[str]:
    if type(video_duration_ms) is not int or video_duration_ms <= 0:
        raise ValueError("video duration must be a positive integer")
    try:
        cues = parse_srt(value)
    except SrtParseError as exc:
        return [exc.code]

    flags: set[str] = set()
    for previous, current in zip(cues, cues[1:]):
        if current.start_ms < previous.end_ms:
            flags.add("overlap")
    if any(cue.end_ms <= cue.start_ms for cue in cues):
        flags.add("non_positive_duration")
    if any(
        cue.start_ms > video_duration_ms or cue.end_ms > video_duration_ms
        for cue in cues
    ):
        flags.add("past_video_end")
    if any(KANA_RE.search(cue.text) for cue in cues):
        flags.add("kana_remaining")
    if any(not _has_meaningful_chinese(cue.text) for cue in cues):
        flags.add("not_simplified_chinese")

    if simplifier is None:
        simplifier = load_simplifier()
    if not callable(simplifier):
        raise ValueError("simplifier must be callable")
    try:
        if any(simplifier(cue.text) != cue.text for cue in cues):
            flags.add("traditional_character")
    except Exception as exc:
        raise MissingTranslationDependency(
            f"Simplified-Chinese conversion failed: {exc}"
        ) from exc
    return _ordered_flags(flags)


def _cue_timestamp_digest(rows: Iterable[tuple[str, int, int]]) -> str:
    payload = [[cue_id, start_ms, end_ms] for cue_id, start_ms, end_ms in rows]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_candidate(
    value: str | bytes,
    source_cues: Iterable[object],
    video_duration_ms: int,
    *,
    simplifier: Callable[[str], str] | None = None,
) -> list[str]:
    base_flags = validate_srt(
        value,
        video_duration_ms,
        simplifier=simplifier,
    )
    try:
        parsed = parse_srt(value)
    except SrtParseError:
        return base_flags

    source = tuple(source_cues)
    source_rows: list[tuple[str, int, int]] = []
    for cue in source:
        cue_id = getattr(cue, "cue_id", None)
        start_ms = getattr(cue, "start_ms", None)
        end_ms = getattr(cue, "end_ms", None)
        if (
            type(cue_id) is not str
            or not cue_id
            or type(start_ms) is not int
            or type(end_ms) is not int
        ):
            raise ValueError("source cues require IDs and integer timestamps")
        source_rows.append((cue_id, start_ms, end_ms))
    if len(source_rows) != len({cue_id for cue_id, _, _ in source_rows}):
        raise ValueError("source cue IDs must be unique")

    extra: list[str] = []
    if len(parsed) != len(source_rows):
        extra.append("cue_cardinality_mismatch")
    else:
        candidate_rows = [
            (source_rows[index][0], cue.start_ms, cue.end_ms)
            for index, cue in enumerate(parsed)
        ]
        if any(
            candidate[1:] != expected[1:]
            for candidate, expected in zip(candidate_rows, source_rows)
        ):
            extra.append("timestamp_mismatch")
        if _cue_timestamp_digest(candidate_rows) != _cue_timestamp_digest(source_rows):
            extra.append("timestamp_digest_mismatch")
        cue_quality_flags: list[str] = []
        for index, (source_cue, candidate_cue) in enumerate(zip(source, parsed)):
            source_text = getattr(source_cue, "text", None)
            if type(source_text) is not str:
                raise ValueError("source cues require text")
            for flag in translation_flags(
                source_text,
                candidate_cue.text,
                simplifier=simplifier,
                cue_id=source_rows[index][0],
            ):
                if flag not in cue_quality_flags:
                    cue_quality_flags.append(flag)
        source_grounded_flags = {
            "kana_remaining": "japanese_remaining",
            "not_simplified_chinese": "not_simplified_chinese",
            "traditional_character": "traditional_character",
        }
        base_flags = [
            flag
            for flag in base_flags
            if flag not in source_grounded_flags
            or source_grounded_flags[flag] in cue_quality_flags
        ]
        extra.extend(cue_quality_flags)
    return base_flags + [flag for flag in extra if flag not in base_flags]


def _has_meaningful_chinese(text: str) -> bool:
    content = _language_content(text)
    if not HAN_RE.search(text) or content in META_BOILERPLATE:
        return False
    if content in {"是", "无"}:
        return True
    remainder = content
    for token in META_TOKENS:
        remainder = remainder.replace(token, "")
    remainder = "".join(character for character in remainder if not character.isdigit())
    return bool(remainder)
