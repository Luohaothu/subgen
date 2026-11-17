from __future__ import annotations

import json
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from itertools import product
from pathlib import Path
from unittest import mock

from subtitle_pipeline import runner_cli
from subtitle_pipeline.settings import RuntimeSettings, TranslationProvider
from subtitle_pipeline.web import runner as runner_module
from subtitle_pipeline.web.jobs import (
    JobParams,
    JobRecord,
    JobStore,
    StageRun,
)
from subtitle_pipeline.web.runner import (
    JobRunner,
    RunnerError,
    UnknownStageError,
    plan_commands,
    stage_of,
)

PROVIDER = TranslationProvider(
    "http://127.0.0.1:8123/v1",
    "vendor/model",
    "revision-1",
    review_model="vendor/reviewer",
    review_revision="revision-2",
)


def settings(root: Path) -> RuntimeSettings:
    return RuntimeSettings(
        source_root=root / "source",
        work_root=root / "work",
        model_root=root / "work" / "models",
        asr_model_path=root / "work" / "models" / "asr",
        device="cpu",
        asr_engine="funasr",
        translation=PROVIDER,
    )


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        self.value += 0.25
        return self.value


class FakeProcess:
    def __init__(
        self,
        code: int = 0,
        *,
        exit_after_polls: int | None = 1,
    ) -> None:
        self.pid = FakeProcess.next_pid()
        self.code = code
        self.exit_after_polls = exit_after_polls
        self.polls = 0
        self.returncode: int | None = None

    @classmethod
    def next_pid(cls) -> int:
        with _PID_LOCK:
            cls._pid = getattr(cls, "_pid", 4000) + 1
            return cls._pid

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        self.polls += 1
        if self.exit_after_polls is not None and self.polls >= self.exit_after_polls:
            self.returncode = self.code
        return self.returncode

    def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode


_PID_LOCK = threading.Lock()


class SignalledProcess:
    def __init__(
        self,
        signals: list[int],
        *,
        exit_on: int,
        code: int,
    ) -> None:
        self.pid = FakeProcess.next_pid()
        self.signals = signals
        self.exit_on = exit_on
        self.code = code
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None and self.exit_on in self.signals:
            self.returncode = self.code
        return self.returncode

    def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self.exit_after_wait()
        return self.returncode

    def exit_after_wait(self) -> int:
        return self.code


class SpawnLog:
    def __init__(self, processes: list[object]) -> None:
        self.processes = list(processes)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> object:
        self.calls.append((list(argv), dict(kwargs)))
        return self.processes.pop(0)


def wait_until(predicate: object, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


class Fixture(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.clock = Clock()
        self.store = JobStore(self.work, clock=self.clock)
        self.settings = settings(self.root)
        self.progress_path = self.work / "state" / "runtime_progress.jsonl"

    def queued(self, count: int = 1, name: str = "inventory") -> JobRecord:
        stages = tuple(
            stage(
                name if index == 0 else "publish",
                (sys.executable, "-c", "pass"),
            )
            for index in range(count)
        )
        return self.store.create(JobParams(), stages=stages, settings_snapshot={})


def stage(name: str, argv: tuple[str, ...]) -> StageRun:
    return StageRun(name=name, argv=argv, status="pending")


class StageOfTests(Fixture):
    def test_stage_of_covers_every_skip_combination(self):
        base = [
            "inventory",
            "extract",
            "asr",
            "alignment",
            "translation_primary",
            "translation_review",
            "publish",
        ]
        for skip_inventory, skip_extract in product((False, True), repeat=2):
            with self.subTest(skip_inventory=skip_inventory, skip_extract=skip_extract):
                commands = runner_cli.build_commands(
                    self.settings,
                    python="python",
                    skip_inventory=skip_inventory,
                    skip_extract=skip_extract,
                )
                names = [stage_of(command) for command in commands]
                expected = [
                    name
                    for name in base
                    if not (
                        (name == "inventory" and skip_inventory)
                        or (name == "extract" and skip_extract)
                    )
                ]
                self.assertEqual(names, expected)
                self.assertEqual(len(names), len(set(names)))

    def test_stage_of_rejects_unknown_commands(self):
        for argv in (
            ("python", "-m"),
            ("python", "-m", "unknown.module"),
            ("python", "-m", "subtitle_pipeline.runtime_cli", "other"),
            ("python", "-m", "subtitle_pipeline.corpus_translation_cli"),
            (
                "python",
                "-m",
                "subtitle_pipeline.corpus_translation_cli",
                "--pass",
                "other",
            ),
        ):
            with self.subTest(argv=argv), self.assertRaises(UnknownStageError):
                stage_of(argv)

    def test_plan_commands_matches_build_commands(self):
        params = JobParams(
            limit=2,
            video_ids=("a" * 64,),
            skip_extract=True,
            asr_batch_size=4,
            alignment_batch_size=2,
        )
        planned = plan_commands(self.settings, params, python="python")
        commands = runner_cli.build_commands(
            self.settings,
            python="python",
            limit=2,
            video_ids=("a" * 64,),
            skip_inventory=False,
            skip_extract=True,
            asr_batch_size=4,
            alignment_batch_size=2,
        )
        self.assertEqual([list(item.argv) for item in planned], commands)
        self.assertEqual([item.status for item in planned], ["pending"] * 6)
        self.assertEqual(
            [item.name for item in planned],
            [
                "inventory",
                "asr",
                "alignment",
                "translation_primary",
                "translation_review",
                "publish",
            ],
        )


class DryRunTests(Fixture):
    def test_finalize_marks_every_command_skipped(self):
        record = self.queued()
        runner = JobRunner(self.store, spawn=SpawnLog([]), clock=self.clock)
        final = runner.finalize_dry_run(record)
        self.assertEqual(final.state, "dry_run")
        self.assertEqual(final.current, -1)
        self.assertTrue(all(item.status == "skipped" for item in final.stages))
        self.assertTrue(all(item.argv for item in final.stages))
        self.assertIsNone(final.exit_code)
        self.assertIsNotNone(final.ended_at)

    def test_finalize_requires_a_queued_record(self):
        record = self.queued()
        runner = JobRunner(self.store, spawn=SpawnLog([]), clock=self.clock)
        final = runner.finalize_dry_run(record)
        with self.assertRaises(RunnerError):
            runner.finalize_dry_run(final)


class ExecutionTests(Fixture):
    def _runner(self, spawn: object) -> JobRunner:
        return JobRunner(self.store, spawn=spawn, clock=self.clock)

    def test_success_runs_every_stage_and_writes_the_log(self):
        processes = [FakeProcess(0), FakeProcess(0)]
        spawn = SpawnLog(processes)
        record = self.queued(count=2)
        runner = self._runner(spawn)
        runner.start(record, environment={"CUSTOM": "1"})
        self.assertTrue(runner.wait(3))
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.current, 1)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(all(item.status == "succeeded" for item in result.stages))
        self.assertEqual(len(spawn.calls), 2)
        argv, kwargs = spawn.calls[0]
        self.assertEqual(argv, list(record.stages[0].argv))
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdout"].mode, "ab")
        environment = kwargs["env"]
        self.assertEqual(environment["CUSTOM"], "1")
        self.assertEqual(environment["SUBGEN_JOB_ID"], record.job_id)
        log = self.store.log_path(record.job_id).read_text(encoding="utf-8")
        self.assertIn(f"=== subgen web job {record.job_id} ===", log)
        for item in record.stages:
            self.assertIn(f"argv: {shlex.join(item.argv)}", log)
        self.assertIn("=== inventory ", log)
        self.assertIn("=== publish ", log)

    def test_failure_stops_the_loop(self):
        spawn = SpawnLog([FakeProcess(3)])
        record = self.queued(count=2)
        runner = self._runner(spawn)
        runner.start(record, environment={})
        self.assertTrue(runner.wait(3))
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.stages[0].status, "failed")
        self.assertEqual(result.stages[1].status, "skipped")
        self.assertEqual(len(spawn.calls), 1)
        self.assertIn("inventory", result.error or "")

    def test_bare_130_is_interrupted(self):
        spawn = SpawnLog([FakeProcess(130)])
        record = self.queued()
        runner = self._runner(spawn)
        runner.start(record, environment={})
        self.assertTrue(runner.wait(3))
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "interrupted")
        self.assertEqual(result.reason, "interrupted")
        self.assertEqual(result.stages[0].status, "interrupted")
        self.assertEqual(result.exit_code, 130)

    def test_spawn_failure_marks_the_job_failed(self):
        def spawn(argv: list[str], **kwargs: object) -> object:
            raise OSError("no such executable")

        record = self.queued(count=2)
        runner = self._runner(spawn)
        runner.start(record, environment={})
        self.assertTrue(runner.wait(3))
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "failed")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.stages[0].status, "failed")
        self.assertEqual(result.stages[1].status, "skipped")
        self.assertIn("could not be started", result.error or "")

    def test_progress_window_is_attributed(self):
        def spawn(argv: list[str], **kwargs: object) -> object:
            self.progress_path.parent.mkdir(parents=True, exist_ok=True)
            rows = (
                {
                    "timestamp": 1.0,
                    "stage": "inventory",
                    "event": "observed",
                    "run_id": "run-1",
                    "video_id": "a" * 64,
                },
                {
                    "timestamp": 2.0,
                    "stage": "inventory",
                    "event": "completed",
                    "run_id": "run-1",
                    "count": 3,
                    "manifest": "state/runtime_inventory.json",
                },
            )
            self.progress_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            return FakeProcess(0)

        record = self.queued()
        runner = self._runner(spawn)
        runner.start(record, environment={})
        self.assertTrue(runner.wait(3))
        result = self.store.load(record.job_id)
        stored = result.stages[0]
        self.assertEqual(stored.run_id, "run-1")
        self.assertEqual(stored.counters, {"count": 3})
        self.assertEqual(len(stored.events), 1)
        self.assertEqual(stored.events[0]["video_id"], "a" * 64)
        self.assertEqual(stored.progress_start, 0)
        self.assertGreater(stored.progress_end or 0, 0)

    def test_unexpected_failure_interrupts_the_record(self):
        def spawn(argv: list[str], **kwargs: object) -> object:
            raise RuntimeError("boom")

        record = self.queued()
        runner = self._runner(spawn)
        runner.start(record, environment={})
        self.assertTrue(runner.wait(3))
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "interrupted")
        self.assertEqual(result.reason, "error")
        self.assertIn("boom", result.error or "")


class CancelTests(Fixture):
    def setUp(self):
        super().setUp()
        self._patches = [
            mock.patch.object(runner_module, "POLL_SECONDS", 0.005),
            mock.patch.object(runner_module, "CANCEL_GRACE_SECONDS", 0.0),
            mock.patch.object(runner_module, "CANCEL_TERM_SECONDS", 0.0),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_cancel_ladder_sends_int_term_kill(self):
        signals: list[int] = []
        process = SignalledProcess(signals, exit_on=signal.SIGKILL, code=137)
        spawn = SpawnLog([process])
        record = self.queued()
        runner = JobRunner(self.store, spawn=spawn, clock=self.clock)
        with mock.patch.object(
            runner_module.os,
            "killpg",
            side_effect=lambda pgid, signum: signals.append(signum),
        ):
            runner.start(record, environment={})
            wait_until(lambda: self.store.load(record.job_id).child is not None)
            self.assertTrue(runner.cancel())
            self.assertTrue(runner.wait(3))
        self.assertEqual(signals, [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.reason, "cancelled")
        self.assertEqual(result.exit_code, 137)
        self.assertEqual(result.stages[0].status, "cancelled")
        self.assertIsNone(result.child)

    def test_sigint_exit_130_after_cancel_is_cancelled(self):
        signals: list[int] = []
        process = SignalledProcess(signals, exit_on=signal.SIGINT, code=130)
        spawn = SpawnLog([process])
        record = self.queued()
        runner = JobRunner(self.store, spawn=spawn, clock=self.clock)
        with mock.patch.object(
            runner_module.os,
            "killpg",
            side_effect=lambda pgid, signum: signals.append(signum),
        ):
            runner.start(record, environment={})
            wait_until(lambda: self.store.load(record.job_id).child is not None)
            runner.cancel()
            self.assertTrue(runner.wait(3))
        self.assertEqual(signals, [signal.SIGINT])
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.exit_code, 130)

    def test_cancel_ignores_another_job_id(self):
        signals: list[int] = []
        process = SignalledProcess(signals, exit_on=signal.SIGINT, code=130)
        spawn = SpawnLog([process])
        record = self.queued()
        runner = JobRunner(self.store, spawn=spawn, clock=self.clock)
        with mock.patch.object(
            runner_module.os,
            "killpg",
            side_effect=lambda pgid, signum: signals.append(signum),
        ):
            runner.start(record, environment={})
            wait_until(lambda: self.store.load(record.job_id).child is not None)
            self.assertFalse(runner.cancel("20261117T101500-00000000"))
            self.assertEqual(self.store.load(record.job_id).state, "running")
            self.assertTrue(runner.cancel(record.job_id))
            self.assertTrue(runner.wait(3))
        self.assertEqual(self.store.load(record.job_id).state, "cancelled")

    def test_shutdown_cancels_with_the_server_reason(self):
        signals: list[int] = []
        process = SignalledProcess(signals, exit_on=signal.SIGINT, code=130)
        spawn = SpawnLog([process])
        record = self.queued()
        runner = JobRunner(self.store, spawn=spawn, clock=self.clock)
        with mock.patch.object(
            runner_module.os,
            "killpg",
            side_effect=lambda pgid, signum: signals.append(signum),
        ):
            runner.start(record, environment={})
            wait_until(lambda: self.store.load(record.job_id).child is not None)
            self.assertTrue(runner.shutdown(timeout=3))
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.reason, "server_shutdown")
        self.assertEqual(result.stages[0].status, "cancelled")

    def test_cancel_between_stages_skips_the_next_spawn(self):
        record = self.queued(count=2)
        runner = JobRunner(self.store, spawn=SpawnLog([]), clock=self.clock)
        reason = runner_module._Reason()
        reason.value = "cancelled"
        stages = list(record.stages)
        stages[0] = StageRun(
            name="inventory",
            argv=record.stages[0].argv,
            status="succeeded",
            exit_code=0,
        )
        runner._finish_before_spawn(record, stages, 1, reason)
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.current, 1)
        self.assertEqual(result.stages[0].status, "succeeded")
        self.assertEqual(result.stages[1].status, "cancelled")

    def test_shutdown_before_the_first_spawn_interrupts(self):
        record = self.queued()
        runner = JobRunner(self.store, spawn=SpawnLog([]), clock=self.clock)
        reason = runner_module._Reason()
        reason.value = "server_shutdown"
        runner._finish_before_spawn(record, list(record.stages), 0, reason)
        result = self.store.load(record.job_id)
        self.assertEqual(result.state, "interrupted")
        self.assertEqual(result.reason, "server_shutdown")
        self.assertEqual(result.current, -1)
        self.assertTrue(all(item.status == "skipped" for item in result.stages))


class OneJobAtATimeTests(Fixture):
    def test_start_refuses_while_another_job_runs(self):
        signals: list[int] = []
        first = SignalledProcess(signals, exit_on=signal.SIGINT, code=130)
        runner = JobRunner(self.store, spawn=SpawnLog([first]), clock=self.clock)
        record = self.queued()
        with (
            mock.patch.object(runner_module, "CANCEL_GRACE_SECONDS", 0.0),
            mock.patch.object(runner_module, "CANCEL_TERM_SECONDS", 0.0),
            mock.patch.object(
                runner_module.os,
                "killpg",
                side_effect=lambda pgid, signum: signals.append(signum),
            ),
        ):
            runner.start(record, environment={})
            wait_until(lambda: self.store.load(record.job_id).state == "running")
            with self.assertRaises(RunnerError):
                runner.start(self.queued(), environment={})
            runner.cancel()
            self.assertTrue(runner.wait(3))


if __name__ == "__main__":
    unittest.main()
