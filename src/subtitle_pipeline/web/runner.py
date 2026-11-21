"""Stage planning and the worker thread that runs one job.

The worker owns the job record for its whole lifetime. It spawns each stage in
its own session so one signal reaches the stage's descendants, sends the output
straight to the job log file, and attributes progress through the byte window
recorded around the spawn.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Any

from .. import runner_cli
from ..settings import RuntimeSettings
from . import progress
from .jobs import (
    TERMINAL_STATES,
    ChildHandle,
    JobParams,
    JobRecord,
    JobStore,
    JobStoreError,
    StageRun,
    advance,
)

POLL_SECONDS = 0.2
CANCEL_GRACE_SECONDS = 10.0
CANCEL_TERM_SECONDS = 3.0
SHUTDOWN_JOIN_SECONDS = 15.0
STORED_EVENT_LIMIT = progress.STORED_EVENT_LIMIT

_RUNTIME_MODULE = "subtitle_pipeline.runtime_cli"
_RUNTIME_ACTIONS = MappingProxyType({"inventory": "inventory", "extract": "extract"})
_MODULE_STAGES = MappingProxyType(
    {
        "subtitle_pipeline.corpus_asr_cli": "asr",
        "subtitle_pipeline.corpus_alignment_cli": "alignment",
        "subtitle_pipeline.corpus_translation_cli": None,
        "subtitle_pipeline.corpus_publish_cli": "publish",
    }
)
_TRANSLATION_PASSES = MappingProxyType(
    {"primary": "translation_primary", "review": "translation_review"}
)


class UnknownStageError(ValueError):
    """A command built by ``runner_cli`` has no stage id."""


class RunnerError(RuntimeError):
    """The runner is asked to do something its lifecycle cannot do."""


def _translation_stage(argv: Sequence[str]) -> str | None:
    for index, token in enumerate(argv):
        if token == "--pass" and index + 1 < len(argv):
            return _TRANSLATION_PASSES.get(argv[index + 1])
    return None


def stage_of(argv: Sequence[str]) -> str:
    """Classify one ``build_commands`` entry.

    ``runtime_cli`` carries both inventory and extract and the translation
    command carries both passes, so the module name alone is not enough.
    """
    if len(argv) < 3:
        raise UnknownStageError(f"command is too short: {list(argv)!r}")
    module = argv[2]
    if module == _RUNTIME_MODULE:
        action = argv[3] if len(argv) > 3 else ""
        try:
            return _RUNTIME_ACTIONS[action]
        except KeyError as exc:
            raise UnknownStageError(f"unknown runtime command: {action}") from exc
    if module in _MODULE_STAGES and _MODULE_STAGES[module] is None:
        stage = _translation_stage(argv)
        if stage is None:
            raise UnknownStageError(f"unknown translation pass: {list(argv)!r}")
        return stage
    try:
        return _MODULE_STAGES[module]
    except KeyError as exc:
        raise UnknownStageError(f"unknown stage module: {module}") from exc


def plan_commands(
    settings: RuntimeSettings,
    params: JobParams,
    *,
    python: str = sys.executable,
) -> tuple[StageRun, ...]:
    """Build the stage list for one run, without spawning anything."""
    commands = runner_cli.build_commands(
        settings,
        python=python,
        limit=params.limit,
        video_ids=params.video_ids,
        skip_inventory=params.skip_inventory,
        skip_extract=params.skip_extract,
        asr_batch_size=params.asr_batch_size,
        alignment_batch_size=params.alignment_batch_size,
    )
    return tuple(
        StageRun(name=stage_of(command), argv=tuple(command), status="pending")
        for command in commands
    )


class _Reason:
    """The reason the worker records when the cancel flag is set."""

    __slots__ = ("_lock", "_value")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = "cancelled"

    @property
    def value(self) -> str:
        with self._lock:
            return self._value

    @value.setter
    def value(self, value: str) -> None:
        with self._lock:
            self._value = value


class JobRunner:
    """Runs the stages of a queued record in one worker thread."""

    def __init__(
        self,
        store: JobStore,
        *,
        spawn: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._spawn = spawn
        self._clock = clock
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active_job_id: str | None = None
        self._cancel = threading.Event()
        self._reason = _Reason()
        self._live_child: ChildHandle | None = None

    @property
    def active_job_id(self) -> str | None:
        with self._state_lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return None
            return self._active_job_id

    def is_active(self) -> bool:
        return self.active_job_id is not None

    def start(
        self,
        record: JobRecord,
        *,
        environment: Mapping[str, str],
    ) -> None:
        """Start the worker for a queued record."""
        if record.state != "queued":
            raise RunnerError(f"job {record.job_id} is not queued")
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                raise RunnerError("a job is already running")
            self._cancel = threading.Event()
            self._reason = _Reason()
            self._active_job_id = record.job_id
            thread = threading.Thread(
                target=self._work,
                args=(record, dict(environment), self._cancel, self._reason),
                name=f"subgen-web-{record.job_id}",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def cancel(self, job_id: str | None = None, *, reason: str = "cancelled") -> bool:
        """Ask the running job to stop. False when it is not the active job."""
        with self._state_lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return False
            if job_id is not None and job_id != self._active_job_id:
                return False
            self._reason.value = reason
            self._cancel.set()
        return True

    def shutdown(self, timeout: float = SHUTDOWN_JOIN_SECONDS) -> bool:
        """Cancel with the server-stop reason and wait for the worker."""
        self.cancel(reason="server_shutdown")
        return self.wait(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        """Join the worker. True when it is no longer running."""
        with self._state_lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def finalize_dry_run(self, record: JobRecord) -> JobRecord:
        """Turn a queued dry-run record into its terminal ``dry_run`` shape."""
        if record.state != "queued":
            raise RunnerError(f"job {record.job_id} is not queued")
        now = self._clock()
        stages = tuple(
            dataclasses.replace(stage, status="skipped") for stage in record.stages
        )
        return advance(
            record,
            state="dry_run",
            stages=stages,
            current=-1,
            ended_at=now,
            updated_at=now,
        )

    def _work(
        self,
        record: JobRecord,
        environment: Mapping[str, str],
        cancel: threading.Event,
        reason: _Reason,
    ) -> None:
        job_id = record.job_id
        try:
            self._run(record, environment, cancel, reason)
        except BaseException as exc:  # keep the worker's persisted contract
            self._finish_after_crash(job_id, exc)
        finally:
            with self._state_lock:
                self._active_job_id = None
                self._live_child = None

    def _finish_after_crash(self, job_id: str, exc: BaseException) -> None:
        handle = self._live_child
        if handle is not None:
            self._signal_group(handle.pgid, signal.SIGKILL)
        try:
            record = self._store.load(job_id)
        except JobStoreError:
            return
        if record.state in TERMINAL_STATES:
            return
        now = self._clock()
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
                    error=f"{type(exc).__name__}: {exc}",
                )
            for index in range(current + 1, len(stages)):
                stages[index] = dataclasses.replace(stages[index], status="skipped")
            current_index = current
        else:
            stages = [dataclasses.replace(stage, status="skipped") for stage in stages]
            current_index = -1
        try:
            self._store.save(
                advance(
                    record,
                    state="interrupted",
                    stages=tuple(stages),
                    current=current_index,
                    child=None,
                    ended_at=now,
                    exit_code=None,
                    reason="error",
                    error=f"{type(exc).__name__}: {exc}",
                    updated_at=now,
                )
            )
        except (ValueError, OSError):
            return

    def _run(
        self,
        record: JobRecord,
        environment: Mapping[str, str],
        cancel: threading.Event,
        reason: _Reason,
    ) -> None:
        log_path = self._store.log_path(record.job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            log_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "ab", buffering=0) as log:
            log.write(self._job_header(record))
            stages = list(record.stages)
            index = 0
            while index < len(stages):
                if cancel.is_set():
                    self._finish_before_spawn(record, stages, index, reason)
                    return
                stage = stages[index]
                started = self._clock()
                progress_start = progress.file_size(self._store.progress_path)
                log.write(self._stage_boundary(stage, started))
                child = self._spawn_stage(stage, environment, log, record.job_id)
                if child is None:
                    self._finish_spawn_failure(record, stages, index, started)
                    return
                pid = int(getattr(child, "pid"))
                handle = ChildHandle(pid=pid, pgid=pid, stage=stage.name)
                stages[index] = dataclasses.replace(
                    stage,
                    status="running",
                    started_at=started,
                    progress_start=progress_start,
                )
                record = advance(
                    record,
                    state="running",
                    current=index,
                    stages=tuple(stages),
                    child=handle,
                    updated_at=started,
                )
                self._store.save(record)
                self._live_child = handle
                code = self._wait_for_child(child, cancel, handle)
                self._live_child = None
                ended = self._clock()
                progress_end = progress.file_size(self._store.progress_path)
                snapshot = self._stage_snapshot(
                    stage.name, progress_start, progress_end
                )
                terminal = {
                    "ended_at": ended,
                    "exit_code": code,
                    "progress_end": progress_end,
                    **snapshot,
                }
                if cancel.is_set():
                    stages[index] = dataclasses.replace(
                        stages[index], status="cancelled", **terminal
                    )
                    self._finish_cancelled(record, stages, index, code, reason.value)
                    return
                if code == 0:
                    # Do not persist yet: the next spawn and the save happen
                    # together so a stored running record always has a child.
                    stages[index] = dataclasses.replace(
                        stages[index], status="succeeded", **terminal
                    )
                    index += 1
                    continue
                if code == 130:
                    stages[index] = dataclasses.replace(
                        stages[index], status="interrupted", **terminal
                    )
                    self._finish_interrupted(record, stages, index, code, "interrupted")
                    return
                stages[index] = dataclasses.replace(
                    stages[index],
                    status="failed",
                    error=f"stage exited with status {code}",
                    **terminal,
                )
                self._finish_failed(record, stages, index, code)
                return
            now = self._clock()
            self._store.save(
                advance(
                    record,
                    state="succeeded",
                    stages=tuple(stages),
                    current=len(stages) - 1 if stages else -1,
                    child=None,
                    ended_at=now,
                    exit_code=0,
                    updated_at=now,
                )
            )

    def _spawn_stage(
        self,
        stage: StageRun,
        environment: Mapping[str, str],
        log: Any,
        job_id: str,
    ) -> Any | None:
        child_environment = dict(environment)
        child_environment["SUBGEN_JOB_ID"] = job_id
        try:
            return self._spawn(
                list(stage.argv),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=child_environment,
            )
        except OSError:
            return None

    def _wait_for_child(
        self, child: Any, cancel: threading.Event, handle: ChildHandle
    ) -> int:
        while child.poll() is None:
            if cancel.wait(POLL_SECONDS):
                self._cancel_child(child, handle)
                break
        return int(child.wait())

    def _cancel_child(self, child: Any, handle: ChildHandle) -> None:
        self._signal_group(handle.pgid, signal.SIGINT)
        if self._wait_for_exit(child, CANCEL_GRACE_SECONDS):
            return
        self._signal_group(handle.pgid, signal.SIGTERM)
        if self._wait_for_exit(child, CANCEL_TERM_SECONDS):
            return
        self._signal_group(handle.pgid, signal.SIGKILL)

    @staticmethod
    def _signal_group(pgid: int, signum: int) -> None:
        try:
            os.killpg(pgid, signum)
        except OSError:
            pass

    @staticmethod
    def _wait_for_exit(child: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                return True
            time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        return child.poll() is not None

    def _stage_snapshot(self, name: str, start: int, end: int) -> dict[str, object]:
        rows = progress.read_rows(self._store.progress_path, start, end)
        return {
            "run_id": progress.stage_run_id(rows, name),
            "counters": progress.terminal_counters(rows, name),
            "events": progress.video_events(rows, name, STORED_EVENT_LIMIT),
        }

    def _finish_before_spawn(
        self,
        record: JobRecord,
        stages: list[StageRun],
        index: int,
        reason: _Reason,
    ) -> None:
        if reason.value == "server_shutdown" and record.state == "queued":
            now = self._clock()
            stages = [dataclasses.replace(stage, status="skipped") for stage in stages]
            self._store.save(
                advance(
                    record,
                    state="interrupted",
                    stages=tuple(stages),
                    current=-1,
                    child=None,
                    ended_at=now,
                    exit_code=None,
                    reason="server_shutdown",
                    updated_at=now,
                )
            )
            return
        now = self._clock()
        stages[index] = dataclasses.replace(
            stages[index], status="cancelled", ended_at=now
        )
        self._finish_cancelled(record, stages, index, None, reason.value)

    def _finish_cancelled(
        self,
        record: JobRecord,
        stages: list[StageRun],
        index: int,
        code: int | None,
        reason: str,
    ) -> None:
        for later in range(index + 1, len(stages)):
            stages[later] = dataclasses.replace(stages[later], status="skipped")
        now = self._clock()
        self._store.save(
            advance(
                record,
                state="cancelled",
                stages=tuple(stages),
                current=index,
                child=None,
                ended_at=now,
                exit_code=code,
                reason=reason,
                updated_at=now,
            )
        )

    def _finish_interrupted(
        self,
        record: JobRecord,
        stages: list[StageRun],
        index: int,
        code: int | None,
        reason: str,
    ) -> None:
        for later in range(index + 1, len(stages)):
            stages[later] = dataclasses.replace(stages[later], status="skipped")
        now = self._clock()
        self._store.save(
            advance(
                record,
                state="interrupted",
                stages=tuple(stages),
                current=index,
                child=None,
                ended_at=now,
                exit_code=code,
                reason=reason,
                updated_at=now,
            )
        )

    def _finish_failed(
        self,
        record: JobRecord,
        stages: list[StageRun],
        index: int,
        code: int | None,
    ) -> None:
        for later in range(index + 1, len(stages)):
            stages[later] = dataclasses.replace(stages[later], status="skipped")
        now = self._clock()
        self._store.save(
            advance(
                record,
                state="failed",
                stages=tuple(stages),
                current=index,
                child=None,
                ended_at=now,
                exit_code=code,
                error=f"{stages[index].name} exited with status {code}",
                updated_at=now,
            )
        )

    def _finish_spawn_failure(
        self,
        record: JobRecord,
        stages: list[StageRun],
        index: int,
        started: float,
    ) -> None:
        stages[index] = dataclasses.replace(
            stages[index],
            status="failed",
            started_at=started,
            ended_at=self._clock(),
            error="stage could not be started",
        )
        for later in range(index + 1, len(stages)):
            stages[later] = dataclasses.replace(stages[later], status="skipped")
        now = self._clock()
        self._store.save(
            advance(
                record,
                state="failed",
                stages=tuple(stages),
                current=index,
                child=None,
                ended_at=now,
                exit_code=None,
                error=f"{stages[index].name} could not be started",
                updated_at=now,
            )
        )

    @staticmethod
    def _job_header(record: JobRecord) -> bytes:
        created = datetime.fromtimestamp(record.created_at).isoformat(
            timespec="seconds"
        )
        params = json.dumps(
            record.params.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            f"=== subgen web job {record.job_id} ===\n"
            f"created: {created}\n"
            f"work_root: {record.work_root}\n"
            f"params: {params}\n"
        ).encode()

    @staticmethod
    def _stage_boundary(stage: StageRun, started: float) -> bytes:
        stamp = datetime.fromtimestamp(started).isoformat(timespec="seconds")
        return (
            f"=== {stage.name} {stamp} ===\nargv: {shlex.join(stage.argv)}\n"
        ).encode()
