"""Progress-log vocabulary and byte-window attribution for the web console.

The stages append rows to ``state/runtime_progress.jsonl``. A row carries its
stage and event but no attempt identity shared across stages, so the runner
records a byte window around each spawn and reads only that window. The tables
here name the events each stage emits and how the console counts them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

MAX_READ_BYTES = 256 * 1024
LAST_ROW_BYTES = 64 * 1024
STORED_EVENT_LIMIT = 40

STAGES = (
    "inventory",
    "extract",
    "asr",
    "alignment",
    "translation_primary",
    "translation_review",
    "publish",
)

STAGE_TERMINAL_EVENTS: dict[str, frozenset[str]] = {
    "inventory": frozenset({"completed", "failed"}),
    "extract": frozenset({"batch_completed", "batch_failed"}),
    "asr": frozenset({"batch_completed", "batch_deferred", "batch_failed"}),
    "alignment": frozenset({"batch_completed", "batch_incomplete", "batch_failed"}),
    "translation_primary": frozenset(
        {"batch_completed", "batch_incomplete", "batch_failed"}
    ),
    "translation_review": frozenset(
        {"batch_completed", "batch_incomplete", "batch_failed"}
    ),
    "publish": frozenset({"batch_completed", "batch_incomplete", "batch_failed"}),
}

STAGE_ALIASES: dict[str, frozenset[str]] = {
    "translation_primary": frozenset({"corpus_translation_primary", "primary"}),
    "translation_review": frozenset({"corpus_translation_review", "review"}),
}

# The stage name each command writes. The design's alias table only covers the
# translation quirk, where per-video rows carry the short pass name.
STAGE_PROGRESS_NAMES: dict[str, str] = {
    "inventory": "inventory",
    "extract": "extract",
    "asr": "corpus_asr",
    "alignment": "corpus_alignment",
    "translation_primary": "corpus_translation_primary",
    "translation_review": "corpus_translation_review",
    "publish": "corpus_publish",
}

_COUNTER_METADATA = frozenset(
    {
        "timestamp",
        "stage",
        "event",
        "run_id",
        "progress",
        "artifact_root",
        "source_root",
    }
)

_EXTRACT_DONE_EVENTS = frozenset({"completed", "skipped_existing", "skipped_no_audio"})
_VERIFIED_DONE_EVENTS = frozenset({"video_completed", "skipped_verified"})
_TRANSLATION_DONE_EVENTS = frozenset({"video_completed", "video_existing"})


def row_stage_names(stage: str) -> frozenset[str]:
    """The ``stage`` values that belong to one stage id."""
    return frozenset({STAGE_PROGRESS_NAMES[stage]}) | STAGE_ALIASES.get(
        stage, frozenset()
    )


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _read_complete(path: Path, start: int, stop: int) -> tuple[list[dict], int]:
    """Read ``[start, stop)`` and keep only complete, JSON-object lines."""
    start = max(0, start)
    stop = max(start, stop)
    try:
        with path.open("rb") as stream:
            stream.seek(start)
            data = stream.read(stop - start)
    except FileNotFoundError:
        return [], start
    boundary = data.rfind(b"\n")
    if boundary < 0:
        return [], start
    consumed = boundary + 1
    rows: list[dict] = []
    for line in data[:consumed].decode("utf-8", "replace").split("\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows, start + consumed


def read_rows(path: Path, start: int, end: int) -> list[dict]:
    """Parse the complete rows in ``[start, end)``.

    A torn trailing line is ignored; the caller re-reads it after the writer
    finishes.
    """
    rows, _ = _read_complete(path, start, end)
    return rows


def read_window(
    path: Path,
    start: int,
    end: int | None = None,
    *,
    max_bytes: int = MAX_READ_BYTES,
) -> tuple[list[dict], int]:
    """Parse rows from ``start`` and return the offset after the last one.

    The read is capped at ``max_bytes`` so a poll cannot copy an unbounded log.
    """
    size = file_size(path)
    limit = size if end is None else min(end, size)
    if start > limit:
        start = limit
    stop = min(limit, start + max_bytes)
    rows, consumed = _read_complete(path, start, stop)
    return rows, consumed


def read_last_row(path: Path) -> dict | None:
    """The newest complete JSON object, or ``None`` for a missing or empty log."""
    size = file_size(path)
    if size == 0:
        return None
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, size - LAST_ROW_BYTES))
            data = stream.read()
    except FileNotFoundError:
        return None
    for line in reversed(data.decode("utf-8", "replace").split("\n")):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def stage_rows(rows: Sequence[Mapping], stage: str) -> list[Mapping]:
    names = row_stage_names(stage)
    return [row for row in rows if row.get("stage") in names]


def stage_run_id(rows: Sequence[Mapping], stage: str) -> str | None:
    """The first run id the stage wrote in this window, if any."""
    for row in stage_rows(rows, stage):
        value = row.get("run_id")
        if isinstance(value, str) and value:
            return value
    return None


def terminal_row(rows: Sequence[Mapping], stage: str) -> Mapping | None:
    """The last row in the window that terminates the stage."""
    events = STAGE_TERMINAL_EVENTS[stage]
    result: Mapping | None = None
    for row in stage_rows(rows, stage):
        if row.get("event") in events:
            result = row
    return result


def terminal_counters(rows: Sequence[Mapping], stage: str) -> dict[str, int] | None:
    """Integer counters from the terminal row, with metadata removed."""
    row = terminal_row(rows, stage)
    if row is None:
        return None
    return {
        key: value
        for key, value in row.items()
        if key not in _COUNTER_METADATA and type(value) is int
    }


def video_events(
    rows: Sequence[Mapping], stage: str, limit: int = STORED_EVENT_LIMIT
) -> tuple[dict, ...]:
    """The newest ``limit`` per-video rows for the stage, newest last."""
    result = [
        dict(row)
        for row in stage_rows(rows, stage)
        if isinstance(row.get("video_id"), str)
    ]
    return tuple(result[-limit:]) if limit > 0 else ()


def is_failure_event(event: object) -> bool:
    if not isinstance(event, str):
        return False
    return (
        "_failed_" in event
        or event == "failed"
        or event.startswith(("deferred_", "video_missing_", "video_invalid_"))
        or event == "video_target_collision"
    )


def _batch_value(rows: Sequence[Mapping], field: str) -> int | None:
    result: int | None = None
    for row in rows:
        if row.get("event") != "batch_started":
            continue
        value = row.get(field)
        if type(value) is int:
            result = value
    return result


def _discovered_count(rows: Sequence[Mapping]) -> int | None:
    result: int | None = None
    for row in rows:
        if row.get("event") == "discovered" and type(row.get("count")) is int:
            result = row["count"]
    return result


def live_counts(rows: Sequence[Mapping], stage: str) -> dict[str, object]:
    """Approximate ``done``/``total``/``failed`` for a running stage."""
    scoped = stage_rows(rows, stage)
    failed = sum(1 for row in scoped if is_failure_event(row.get("event")))
    if stage == "inventory":
        done = sum(1 for row in scoped if row.get("event") == "observed")
        total = _discovered_count(scoped)
    elif stage == "extract":
        done = sum(1 for row in scoped if row.get("event") in _EXTRACT_DONE_EVENTS)
        total = _batch_value(scoped, "selected")
    else:
        if stage in {"translation_primary", "translation_review"}:
            done_events = _TRANSLATION_DONE_EVENTS
        elif stage == "publish":
            done_events = frozenset({"video_completed"})
        else:
            done_events = _VERIFIED_DONE_EVENTS
        done = sum(1 for row in scoped if row.get("event") in done_events)
        total = _batch_value(scoped, "processable")
        if total is None:
            total = _batch_value(scoped, "selected")
    return {"done": done, "total": total, "failed": failed, "exact": False}
