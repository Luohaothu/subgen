"""Job records, transitions, and persistence for the web console.

One record is one attempt. The worker thread is the only writer of a record
after creation, so reads need no lock. Every write is canonical JSON written to
a temporary file in the same directory and moved with ``os.replace``.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
import signal
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from .progress import read_last_row

JobState = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "dry_run",
]
StageStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "skipped",
]

TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted", "dry_run"}
)
STAGE_TERMINAL = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted", "skipped"}
)
RECORD_SCHEMA = "subgen-web-job-v1"
MAX_JOBS = 200
EXTERNAL_ACTIVITY_SECONDS = 120.0

STAGE_NAMES = (
    "inventory",
    "extract",
    "asr",
    "alignment",
    "translation_primary",
    "translation_review",
    "publish",
)
_JOB_ID_PATTERN = re.compile(r"\d{8}T\d{6}-[0-9a-f]{8}")
_VIDEO_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
_PARAM_KEYS = frozenset(
    {
        "limit",
        "video_ids",
        "skip_inventory",
        "skip_extract",
        "asr_batch_size",
        "alignment_batch_size",
        "dry_run",
    }
)


class JobStoreError(RuntimeError):
    """A job store operation failed."""


class JobNotFoundError(JobStoreError):
    """No record has that id."""


class JobNotTerminalError(JobStoreError):
    """Only a terminal record can be deleted."""


class CorruptRecordError(JobStoreError):
    """A record file cannot be parsed or violates the invariants."""


class WorkRootLockedError(JobStoreError):
    """Another console already holds this work root."""


class ParamsError(ValueError):
    """Job parameters are invalid; ``fields`` names each problem."""

    def __init__(self, fields: Mapping[str, str]) -> None:
        self.fields = dict(fields)
        super().__init__(
            "; ".join(f"{key}: {value}" for key, value in self.fields.items())
        )


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_number(value: object) -> bool:
    return type(value) is int or type(value) is float


@dataclasses.dataclass(frozen=True)
class JobParams:
    """The operator's run scope, validated at the HTTP boundary."""

    limit: int | None = None
    video_ids: tuple[str, ...] = ()
    skip_inventory: bool = False
    skip_extract: bool = False
    asr_batch_size: int = 32
    alignment_batch_size: int = 16
    dry_run: bool = False

    @classmethod
    def parse(cls, value: object) -> JobParams:
        if not isinstance(value, Mapping):
            raise ParamsError({"params": "parameters must be an object"})
        fields: dict[str, str] = {}
        for key in sorted(set(value) - _PARAM_KEYS):
            fields[str(key)] = "unknown parameter"

        limit = value.get("limit")
        if limit is not None and (type(limit) is not int or limit < 0):
            fields["limit"] = "limit must be null or an integer at least zero"

        raw_ids = value.get("video_ids", ())
        video_ids: list[str] = []
        if isinstance(raw_ids, (list, tuple)):
            seen: set[str] = set()
            for item in raw_ids:
                if type(item) is not str or not _VIDEO_ID_PATTERN.fullmatch(item):
                    fields["video_ids"] = (
                        "video ids must be 64-character lowercase hex strings"
                    )
                    break
                if item in seen:
                    fields["video_ids"] = "video ids must be unique"
                    break
                seen.add(item)
                video_ids.append(item)
        else:
            fields["video_ids"] = "video_ids must be an array"

        for key in ("skip_inventory", "skip_extract", "dry_run"):
            if type(value.get(key, False)) is not bool:
                fields[key] = f"{key} must be a boolean"
        for key, default in (
            ("asr_batch_size", 32),
            ("alignment_batch_size", 16),
        ):
            item = value.get(key, default)
            if type(item) is not int or item < 1:
                fields[key] = f"{key} must be an integer at least one"

        if fields:
            raise ParamsError(fields)
        return cls(
            limit=limit,
            video_ids=tuple(video_ids),
            skip_inventory=value.get("skip_inventory", False),
            skip_extract=value.get("skip_extract", False),
            asr_batch_size=value.get("asr_batch_size", 32),
            alignment_batch_size=value.get("alignment_batch_size", 16),
            dry_run=value.get("dry_run", False),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "limit": self.limit,
            "video_ids": list(self.video_ids),
            "skip_inventory": self.skip_inventory,
            "skip_extract": self.skip_extract,
            "asr_batch_size": self.asr_batch_size,
            "alignment_batch_size": self.alignment_batch_size,
            "dry_run": self.dry_run,
        }


@dataclasses.dataclass(frozen=True)
class ChildHandle:
    """The live stage process of a running job."""

    pid: int
    pgid: int
    stage: str

    def __post_init__(self) -> None:
        _require(type(self.pid) is int and self.pid > 0, "child pid must be positive")
        _require(
            type(self.pgid) is int and self.pgid > 0, "child pgid must be positive"
        )
        _require(self.stage in STAGE_NAMES, f"unknown child stage: {self.stage}")

    def to_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "pgid": self.pgid, "stage": self.stage}

    @classmethod
    def from_dict(cls, value: object) -> ChildHandle:
        if not isinstance(value, Mapping):
            raise ValueError("child handle must be an object")
        try:
            return cls(pid=value["pid"], pgid=value["pgid"], stage=value["stage"])
        except KeyError as exc:
            raise ValueError(f"child handle is missing {exc}") from exc


@dataclasses.dataclass(frozen=True)
class StageRun:
    """One stage command and what the runner observed for it."""

    name: str
    argv: tuple[str, ...]
    status: StageStatus
    started_at: float | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    run_id: str | None = None
    counters: Mapping[str, int] | None = None
    events: tuple[Mapping[str, object], ...] = ()
    progress_start: int | None = None
    progress_end: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _require(self.name in STAGE_NAMES, f"unknown stage name: {self.name}")
        _require(
            type(self.argv) is tuple and all(type(item) is str for item in self.argv),
            "stage argv must be a tuple of strings",
        )
        _require(
            self.status
            in {
                "pending",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
                "skipped",
            },
            f"unknown stage status: {self.status}",
        )
        if self.status not in STAGE_TERMINAL:
            _require(
                self.counters is None and not self.events,
                "counters and events only appear on terminal stages",
            )
        if self.counters is not None:
            _require(
                isinstance(self.counters, Mapping)
                and all(
                    type(key) is str and type(value) is int
                    for key, value in self.counters.items()
                ),
                "stage counters must be a mapping of integers",
            )
            object.__setattr__(self, "counters", dict(self.counters))
        _require(
            isinstance(self.events, tuple)
            and all(isinstance(item, Mapping) for item in self.events),
            "stage events must be a tuple of objects",
        )
        object.__setattr__(self, "events", tuple(dict(item) for item in self.events))
        for value, field in (
            (self.started_at, "started_at"),
            (self.ended_at, "ended_at"),
        ):
            if value is not None:
                _require(_is_number(value), f"{field} must be a number")
        _require(
            self.exit_code is None or type(self.exit_code) is int,
            "stage exit code must be an integer",
        )
        _require(
            self.run_id is None or type(self.run_id) is str,
            "stage run id must be a string",
        )
        for value, field in (
            (self.progress_start, "progress_start"),
            (self.progress_end, "progress_end"),
        ):
            if value is not None:
                _require(
                    type(value) is int and value >= 0,
                    f"{field} must be a nonnegative integer",
                )
        _require(
            self.error is None or type(self.error) is str,
            "stage error must be a string",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "run_id": self.run_id,
            "counters": None if self.counters is None else dict(self.counters),
            "events": [dict(item) for item in self.events],
            "progress_start": self.progress_start,
            "progress_end": self.progress_end,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: object) -> StageRun:
        if not isinstance(value, Mapping):
            raise ValueError("stage must be an object")
        try:
            return cls(
                name=value["name"],
                argv=tuple(value["argv"]),
                status=value["status"],
                started_at=value.get("started_at"),
                ended_at=value.get("ended_at"),
                exit_code=value.get("exit_code"),
                run_id=value.get("run_id"),
                counters=value.get("counters"),
                events=tuple(value.get("events") or ()),
                progress_start=value.get("progress_start"),
                progress_end=value.get("progress_end"),
                error=value.get("error"),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"stage is invalid: {exc}") from exc


@dataclasses.dataclass(frozen=True)
class JobRecord:
    """One attempt at a pipeline run. The state machine is checked here."""

    schema_version: str
    job_id: str
    state: JobState
    params: JobParams
    stages: tuple[StageRun, ...]
    current: int
    created_at: float
    updated_at: float
    ended_at: float | None
    exit_code: int | None
    child: ChildHandle | None
    error: str | None
    reason: str | None
    client_token: str | None
    origin_job_id: str | None
    settings_snapshot: Mapping[str, str]
    work_root: str

    def __post_init__(self) -> None:
        _require(
            self.schema_version == RECORD_SCHEMA,
            f"unsupported record schema: {self.schema_version}",
        )
        _require(
            type(self.job_id) is str and _JOB_ID_PATTERN.fullmatch(self.job_id),
            f"invalid job id: {self.job_id}",
        )
        _require(
            self.state
            in {
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
                "dry_run",
            },
            f"unknown job state: {self.state}",
        )
        _require(isinstance(self.params, JobParams), "params must be JobParams")
        _require(
            type(self.stages) is tuple
            and all(isinstance(stage, StageRun) for stage in self.stages),
            "stages must be a tuple of StageRun",
        )
        names = [stage.name for stage in self.stages]
        _require(len(names) == len(set(names)), "stage names must be unique")
        _require(type(self.current) is int, "current must be an integer")
        _require(_is_number(self.created_at), "created_at must be a number")
        _require(_is_number(self.updated_at), "updated_at must be a number")
        _require(
            self.ended_at is None or _is_number(self.ended_at),
            "ended_at must be a number",
        )
        _require(
            self.exit_code is None or type(self.exit_code) is int,
            "exit code must be an integer",
        )
        _require(
            self.child is None or isinstance(self.child, ChildHandle),
            "child must be a ChildHandle",
        )
        for value, field in (
            (self.error, "error"),
            (self.reason, "reason"),
            (self.client_token, "client_token"),
            (self.origin_job_id, "origin_job_id"),
        ):
            _require(
                value is None or type(value) is str,
                f"{field} must be a string",
            )
        _require(
            isinstance(self.settings_snapshot, Mapping)
            and all(
                type(key) is str and type(value) is str
                for key, value in self.settings_snapshot.items()
            ),
            "settings snapshot must be a mapping of strings",
        )
        object.__setattr__(self, "settings_snapshot", dict(self.settings_snapshot))
        _require(type(self.work_root) is str and self.work_root, "work root required")

        if self.state in TERMINAL_STATES:
            _require(self.ended_at is not None, "terminal state requires ended_at")
            _require(self.child is None, "terminal state has no child")
        else:
            _require(self.ended_at is None, "non-terminal state has no ended_at")

        if self.state == "queued":
            _require(self.current == -1, "queued job current is -1")
            _require(self.exit_code is None, "queued job has no exit code")
            _require(
                all(stage.status == "pending" for stage in self.stages),
                "queued job stages are pending",
            )
        elif self.state == "running":
            self._check_running()
        elif self.state == "succeeded":
            _require(
                all(stage.status == "succeeded" for stage in self.stages),
                "succeeded job has every stage succeeded",
            )
            _require(
                self.exit_code is None or self.exit_code == 0,
                "succeeded job exit code is zero",
            )
        elif self.state == "failed":
            _require(
                self.exit_code is None or self.exit_code != 0,
                "failed job exit code is nonzero",
            )
            _require(
                0 <= self.current < len(self.stages),
                "failed job has a failing stage",
            )
            _require(
                self.stages[self.current].status == "failed",
                "failed job current stage failed",
            )
            _require(
                all(
                    stage.status == "succeeded" for stage in self.stages[: self.current]
                ),
                "failed job earlier stages succeeded",
            )
            _require(
                all(
                    stage.status == "skipped"
                    for stage in self.stages[self.current + 1 :]
                ),
                "failed job later stages skipped",
            )
        elif self.state in {"cancelled", "interrupted"}:
            if self.current == -1:
                _require(
                    all(stage.status == "skipped" for stage in self.stages),
                    f"{self.state} job without a stage has every stage skipped",
                )
            else:
                _require(
                    0 <= self.current < len(self.stages),
                    f"{self.state} job current is a stage index or -1",
                )
                _require(
                    self.stages[self.current].status == self.state,
                    f"{self.state} job current stage has that status",
                )
                _require(
                    all(
                        stage.status == "succeeded"
                        for stage in self.stages[: self.current]
                    ),
                    f"{self.state} job earlier stages succeeded",
                )
                _require(
                    all(
                        stage.status == "skipped"
                        for stage in self.stages[self.current + 1 :]
                    ),
                    f"{self.state} job later stages skipped",
                )
        else:
            _require(
                all(stage.status == "skipped" and stage.argv for stage in self.stages),
                "dry run has every planned stage skipped",
            )
            _require(self.exit_code is None, "dry run has no exit code")

    def _check_running(self) -> None:
        _require(self.child is not None, "running job has a child")
        _require(0 <= self.current < len(self.stages), "running job current is set")
        stage = self.stages[self.current]
        _require(stage.status == "running", "running job current stage is running")
        _require(
            all(
                earlier.status in STAGE_TERMINAL
                for earlier in self.stages[: self.current]
            ),
            "running job earlier stages are terminal",
        )
        _require(
            all(later.status == "pending" for later in self.stages[self.current + 1 :]),
            "running job later stages are pending",
        )
        _require(self.child.stage == stage.name, "child stage matches current stage")
        _require(self.exit_code is None, "running job has no exit code")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "state": self.state,
            "params": self.params.to_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
            "current": self.current,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "child": None if self.child is None else self.child.to_dict(),
            "error": self.error,
            "reason": self.reason,
            "client_token": self.client_token,
            "origin_job_id": self.origin_job_id,
            "settings_snapshot": dict(self.settings_snapshot),
            "work_root": self.work_root,
        }

    @classmethod
    def from_dict(cls, value: object) -> JobRecord:
        if not isinstance(value, Mapping):
            raise ValueError("job record must be an object")
        try:
            return cls(
                schema_version=value["schema_version"],
                job_id=value["job_id"],
                state=value["state"],
                params=JobParams.parse(value["params"]),
                stages=tuple(StageRun.from_dict(stage) for stage in value["stages"]),
                current=value["current"],
                created_at=value["created_at"],
                updated_at=value["updated_at"],
                ended_at=value.get("ended_at"),
                exit_code=value.get("exit_code"),
                child=(
                    None
                    if value.get("child") is None
                    else ChildHandle.from_dict(value["child"])
                ),
                error=value.get("error"),
                reason=value.get("reason"),
                client_token=value.get("client_token"),
                origin_job_id=value.get("origin_job_id"),
                settings_snapshot=value.get("settings_snapshot") or {},
                work_root=value["work_root"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"job record is invalid: {exc}") from exc


def advance(record: JobRecord, **changes: object) -> JobRecord:
    """Return the next record. Terminal records never change."""
    if record.state in TERMINAL_STATES:
        if changes:
            raise ValueError(f"terminal job {record.job_id} cannot change")
        return record
    changes.setdefault("updated_at", time.time())
    return dataclasses.replace(record, **changes)


class _LockHandle:
    def __init__(self, descriptor: int, path: Path) -> None:
        self._descriptor: int | None = descriptor
        self.path = path

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> _LockHandle:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while storing a job record")
        view = view[written:]


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            part,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(part, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            part.unlink()
        except FileNotFoundError:
            pass


class JobStore:
    """Records and the work-root lock under ``<work_root>/state/web``."""

    def __init__(
        self,
        work_root: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._work_root = Path(work_root)
        self._clock = clock
        self._lock: _LockHandle | None = None
        self._corrupt_count = 0

    @property
    def work_root(self) -> Path:
        return self._work_root

    @property
    def state_root(self) -> Path:
        return self._work_root / "state" / "web"

    @property
    def jobs_root(self) -> Path:
        return self.state_root / "jobs"

    @property
    def lock_path(self) -> Path:
        return self.state_root / "server.lock"

    @property
    def progress_path(self) -> Path:
        return self._work_root / "state" / "runtime_progress.jsonl"

    @property
    def corrupt_count(self) -> int:
        return self._corrupt_count

    def job_path(self, job_id: str) -> Path:
        return self.jobs_root / f"{job_id}.json"

    def log_path(self, job_id: str) -> Path:
        return self.jobs_root / f"{job_id}.log"

    def acquire_lock(self) -> _LockHandle:
        self.state_root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            raise WorkRootLockedError(
                f"another console already holds {self.lock_path}"
            ) from exc
        self._lock = _LockHandle(descriptor, self.lock_path)
        return self._lock

    def release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def save(self, record: JobRecord) -> None:
        payload = (
            json.dumps(
                record.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        _atomic_write(self.job_path(record.job_id), payload)

    def load(self, job_id: str) -> JobRecord:
        if not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id):
            raise JobNotFoundError(f"unknown job: {job_id}")
        path = self.job_path(job_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise JobNotFoundError(f"unknown job: {job_id}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
            return JobRecord.from_dict(value)
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise CorruptRecordError(f"job record is corrupt: {path}: {exc}") from exc

    def list_records(self) -> list[JobRecord]:
        try:
            names = sorted(os.listdir(self.jobs_root))
        except FileNotFoundError:
            self._corrupt_count = 0
            return []
        records: list[JobRecord] = []
        corrupt = 0
        for name in names:
            if not name.endswith(".json"):
                continue
            job_id = name[: -len(".json")]
            try:
                records.append(self.load(job_id))
            except (CorruptRecordError, JobNotFoundError):
                corrupt += 1
        self._corrupt_count = corrupt
        records.sort(
            key=lambda record: (record.created_at, record.job_id), reverse=True
        )
        return records

    def find_by_token(self, token: str) -> JobRecord | None:
        for record in self.list_records():
            if record.client_token == token:
                return record
        return None

    def active_job(self) -> JobRecord | None:
        for record in self.list_records():
            if record.state not in TERMINAL_STATES:
                return record
        return None

    def create(
        self,
        params: JobParams,
        *,
        stages: Sequence[StageRun],
        settings_snapshot: Mapping[str, str],
        client_token: str | None = None,
        origin_job_id: str | None = None,
    ) -> JobRecord:
        now = self._clock()
        stamp = datetime.fromtimestamp(now).strftime("%Y%m%dT%H%M%S")
        job_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        record = JobRecord(
            schema_version=RECORD_SCHEMA,
            job_id=job_id,
            state="queued",
            params=params,
            stages=tuple(stages),
            current=-1,
            created_at=now,
            updated_at=now,
            ended_at=None,
            exit_code=None,
            child=None,
            error=None,
            reason=None,
            client_token=client_token,
            origin_job_id=origin_job_id,
            settings_snapshot=settings_snapshot,
            work_root=str(self._work_root),
        )
        self.save(record)
        self.prune(keep_job_id=record.job_id)
        return record

    def delete(self, job_id: str) -> JobRecord:
        record = self.load(job_id)
        if record.state not in TERMINAL_STATES:
            raise JobNotTerminalError(f"job {job_id} is not terminal")
        try:
            self.job_path(job_id).unlink()
        except FileNotFoundError:
            pass
        try:
            self.log_path(job_id).unlink()
        except FileNotFoundError:
            pass
        return record

    def prune(self, keep_job_id: str | None = None) -> list[str]:
        """Delete the oldest terminal records while the store is over MAX_JOBS."""
        records = self.list_records()
        if len(records) <= MAX_JOBS:
            return []
        terminals = [
            record
            for record in records
            if record.state in TERMINAL_STATES and record.job_id != keep_job_id
        ]
        terminals.sort(key=lambda record: (record.created_at, record.job_id))
        deleted: list[str] = []
        remaining = len(records)
        for record in terminals:
            if remaining <= MAX_JOBS:
                break
            self.delete(record.job_id)
            deleted.append(record.job_id)
            remaining -= 1
        return deleted

    def latest_progress_row(self) -> dict | None:
        return read_last_row(self.progress_path)

    def external_activity(
        self,
        *,
        now: float | None = None,
        window: float = EXTERNAL_ACTIVITY_SECONDS,
    ) -> dict | None:
        row = self.latest_progress_row()
        if row is None:
            return None
        timestamp = row.get("timestamp")
        if not _is_number(timestamp):
            return None
        current = self._clock() if now is None else now
        age = current - float(timestamp)
        if age >= window:
            return None
        return {**row, "active": True, "age_seconds": round(age, 3)}

    def reconcile(self) -> int:
        """Interrupt records a crash left non-terminal, killing leftover groups."""
        reconciled = 0
        for record in self.list_records():
            if record.state in TERMINAL_STATES:
                continue
            now = self._clock()
            if record.child is not None:
                self._kill_leftover(record.child)
            stages = list(record.stages)
            if record.state == "running" and record.current >= 0:
                current = record.current
                if 0 <= current < len(stages):
                    stages[current] = dataclasses.replace(
                        stages[current],
                        status="interrupted",
                        ended_at=now,
                        counters=None,
                        events=(),
                    )
                for index in range(current + 1, len(stages)):
                    stages[index] = dataclasses.replace(stages[index], status="skipped")
            else:
                stages = [
                    dataclasses.replace(stage, status="skipped") for stage in stages
                ]
                record = dataclasses.replace(record, current=-1)
            self.save(
                advance(
                    record,
                    state="interrupted",
                    stages=tuple(stages),
                    child=None,
                    ended_at=now,
                    exit_code=None,
                    reason="restart",
                    updated_at=now,
                )
            )
            reconciled += 1
        return reconciled

    @staticmethod
    def _kill_leftover(child: ChildHandle) -> None:
        try:
            if os.getpgid(child.pid) != child.pgid:
                return
        except OSError:
            return
        try:
            os.killpg(child.pgid, signal.SIGKILL)
        except OSError:
            pass


__all__ = [
    "ChildHandle",
    "CorruptRecordError",
    "EXTERNAL_ACTIVITY_SECONDS",
    "JobNotFoundError",
    "JobNotTerminalError",
    "JobParams",
    "JobRecord",
    "JobState",
    "JobStore",
    "JobStoreError",
    "MAX_JOBS",
    "ParamsError",
    "RECORD_SCHEMA",
    "STAGE_NAMES",
    "STAGE_TERMINAL",
    "StageRun",
    "StageStatus",
    "TERMINAL_STATES",
    "WorkRootLockedError",
    "advance",
]
