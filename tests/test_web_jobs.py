from __future__ import annotations

import json
import signal
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from subtitle_pipeline.web import jobs as jobs_module
from subtitle_pipeline.web.jobs import (
    RECORD_SCHEMA,
    ChildHandle,
    JobNotFoundError,
    JobNotTerminalError,
    JobParams,
    JobRecord,
    JobStore,
    ParamsError,
    StageRun,
    WorkRootLockedError,
    advance,
)

JOB_ID = "20261117T101500-1a2b3c4d"


def stage(
    name: str = "inventory",
    status: str = "pending",
    argv: tuple[str, ...] = (
        "python",
        "-m",
        "subtitle_pipeline.runtime_cli",
        "inventory",
    ),
    **changes: object,
) -> StageRun:
    return StageRun(name=name, argv=argv, status=status, **changes)


def record(**changes: object) -> JobRecord:
    base: dict[str, object] = {
        "schema_version": RECORD_SCHEMA,
        "job_id": JOB_ID,
        "state": "queued",
        "params": JobParams(),
        "stages": (stage(),),
        "current": -1,
        "created_at": 1000.0,
        "updated_at": 1000.0,
        "ended_at": None,
        "exit_code": None,
        "child": None,
        "error": None,
        "reason": None,
        "client_token": None,
        "origin_job_id": None,
        "settings_snapshot": {},
        "work_root": "/tmp/work",
    }
    base.update(changes)
    return JobRecord(**base)


def running_record(**changes: object) -> JobRecord:
    handle = ChildHandle(pid=123, pgid=123, stage="inventory")
    base: dict[str, object] = {
        "state": "running",
        "stages": (stage("inventory", "running", started_at=1001.0),),
        "current": 0,
        "child": handle,
    }
    base.update(changes)
    return record(**base)


def succeeded_record(**changes: object) -> JobRecord:
    base: dict[str, object] = {
        "state": "succeeded",
        "stages": (
            stage(
                "inventory",
                "succeeded",
                started_at=1001.0,
                ended_at=1002.0,
                exit_code=0,
                counters={"count": 5},
            ),
        ),
        "current": 0,
        "ended_at": 1002.0,
        "exit_code": 0,
    }
    base.update(changes)
    return record(**base)


class JobParamsTests(unittest.TestCase):
    def test_defaults_and_round_trip(self):
        params = JobParams.parse({})
        self.assertEqual(
            params,
            JobParams(
                limit=None,
                video_ids=(),
                skip_inventory=False,
                skip_extract=False,
                asr_batch_size=32,
                alignment_batch_size=16,
                dry_run=False,
            ),
        )
        self.assertEqual(JobParams.parse(params.to_dict()), params)

    def test_legal_values_are_accepted(self):
        video = "a" * 64
        params = JobParams.parse(
            {
                "limit": 0,
                "video_ids": [video],
                "skip_inventory": True,
                "skip_extract": True,
                "asr_batch_size": 4,
                "alignment_batch_size": 2,
                "dry_run": True,
            }
        )
        self.assertEqual(params.limit, 0)
        self.assertEqual(params.video_ids, (video,))
        self.assertTrue(params.dry_run)

    def test_invalid_fields_are_named(self):
        cases = {
            "limit": {"limit": -1},
            "bool_limit": {"limit": True},
            "ids": {"video_ids": ["A" * 64]},
            "duplicate": {"video_ids": ["a" * 64, "a" * 64]},
            "batch": {"asr_batch_size": 0},
            "null_batch": {"asr_batch_size": None},
            "flag": {"skip_inventory": 1},
            "unknown": {"nope": 1},
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(ParamsError) as caught:
                JobParams.parse(value)
            self.assertTrue(caught.exception.fields)

    def test_non_mapping_is_rejected(self):
        with self.assertRaises(ParamsError):
            JobParams.parse(["not", "an", "object"])


class RecordInvariantTests(unittest.TestCase):
    def test_queued_shape(self):
        self.assertEqual(record().state, "queued")
        for changes in (
            {"current": 0},
            {"stages": (stage(status="running"),)},
            {"ended_at": 1001.0},
            {"exit_code": 1},
        ):
            with self.subTest(changes), self.assertRaises(ValueError):
                record(**changes)

    def test_running_shape(self):
        self.assertEqual(running_record().state, "running")
        for changes in (
            {"child": None},
            {"current": -1},
            {"stages": (stage("inventory", "pending"),)},
            {"stages": (stage("inventory", "succeeded"),)},
            {"ended_at": 1001.0},
            {
                "child": ChildHandle(pid=123, pgid=123, stage="extract"),
            },
        ):
            with self.subTest(changes), self.assertRaises(ValueError):
                running_record(**changes)

    def test_running_requires_earlier_terminal_and_later_pending(self):
        two = (
            stage("inventory", "pending"),
            stage("extract", "pending"),
        )
        running = running_record(
            stages=(
                stage("inventory", "succeeded", ended_at=1001.0),
                stage("extract", "running"),
            ),
            current=1,
            child=ChildHandle(pid=123, pgid=123, stage="extract"),
        )
        self.assertEqual(running.current, 1)
        for stages, current in (
            ((stage("inventory", "pending"), stage("extract", "running")), 1),
            (
                (stage("inventory", "succeeded"), stage("extract", "pending")),
                0,
            ),
            (two, 0),
        ):
            child = ChildHandle(pid=123, pgid=123, stage=stages[current].name)
            with (
                self.subTest(stages=stages, current=current),
                self.assertRaises(ValueError),
            ):
                running_record(stages=stages, current=current, child=child)

    def test_succeeded_shape(self):
        self.assertEqual(succeeded_record().state, "succeeded")
        with self.assertRaises(ValueError):
            succeeded_record(stages=(stage("inventory", "failed"),))

    def test_failed_shape(self):
        failed = record(
            state="failed",
            stages=(
                stage("inventory", "succeeded", exit_code=0),
                stage("extract", "failed", exit_code=1),
                stage("asr", "skipped"),
            ),
            current=1,
            ended_at=1002.0,
            exit_code=1,
        )
        self.assertEqual(failed.state, "failed")
        with self.assertRaises(ValueError):
            record(
                state="failed",
                stages=(stage("inventory", "failed"),),
                current=0,
                ended_at=1002.0,
                exit_code=0,
            )
        invalid = (
            {
                "current": 0,
                "stages": (
                    stage("inventory", "failed"),
                    stage("extract", "pending"),
                ),
            },
            {
                "current": 0,
                "stages": (
                    stage("inventory", "succeeded"),
                    stage("extract", "failed"),
                ),
            },
        )
        for changes in invalid:
            with self.subTest(changes), self.assertRaises(ValueError):
                record(
                    state="failed",
                    ended_at=1002.0,
                    exit_code=1,
                    **changes,
                )

    def test_cancelled_and_interrupted_shapes(self):
        for state in ("cancelled", "interrupted"):
            valid = record(
                state=state,
                stages=(
                    stage("inventory", "succeeded"),
                    stage("extract", state, ended_at=1002.0),
                    stage("asr", "skipped"),
                ),
                current=1,
                ended_at=1002.0,
            )
            self.assertEqual(valid.state, state)
            boundary = record(
                state=state,
                stages=(stage("inventory", "skipped"),),
                current=-1,
                ended_at=1002.0,
            )
            self.assertEqual(boundary.current, -1)
            for changes in (
                {"current": 1, "stages": (stage("inventory"), stage("extract"))},
                {"current": -1, "stages": (stage("inventory"),)},
            ):
                with (
                    self.subTest(state=state, changes=changes),
                    self.assertRaises(ValueError),
                ):
                    record(
                        state=state,
                        ended_at=1002.0,
                        **changes,
                    )

    def test_dry_run_shape(self):
        dry = record(
            state="dry_run",
            stages=(stage("inventory", "skipped"),),
            ended_at=1002.0,
        )
        self.assertEqual(dry.state, "dry_run")
        with self.assertRaises(ValueError):
            record(state="dry_run", ended_at=1002.0)
        with self.assertRaises(ValueError):
            record(
                state="dry_run",
                stages=(stage("inventory", "skipped"),),
                ended_at=1002.0,
                exit_code=0,
            )
        with self.assertRaises(ValueError):
            record(
                state="dry_run",
                stages=(stage("inventory", "skipped", argv=()),),
                ended_at=1002.0,
            )

    def test_terminal_requires_ended_at_and_no_child(self):
        with self.assertRaises(ValueError):
            succeeded_record(ended_at=None)
        with self.assertRaises(ValueError):
            succeeded_record(child=ChildHandle(pid=1, pgid=1, stage="inventory"))

    def test_counters_and_events_require_terminal_stage(self):
        with self.assertRaises(ValueError):
            stage("inventory", "running", counters={"count": 1})
        with self.assertRaises(ValueError):
            stage("inventory", "pending", events=({"event": "x"},))
        terminal = stage(
            "inventory",
            "cancelled",
            counters={"count": 1},
            events=({"event": "x"},),
        )
        self.assertEqual(terminal.counters, {"count": 1})

    def test_unknown_names_are_rejected(self):
        with self.assertRaises(ValueError):
            stage("unknown")
        with self.assertRaises(ValueError):
            record(stages=(stage("inventory"), stage("inventory")))

    def test_advance_rejects_terminal_changes(self):
        terminal = succeeded_record()
        with self.assertRaises(ValueError):
            advance(terminal, state="running")
        self.assertIs(advance(terminal), terminal)

    def test_advance_transitions_queued_to_running(self):
        started = record()
        child = ChildHandle(pid=10, pgid=10, stage="inventory")
        running = advance(
            started,
            state="running",
            current=0,
            stages=(stage("inventory", "running", started_at=1001.0),),
            child=child,
            updated_at=1001.0,
        )
        self.assertEqual(running.state, "running")
        self.assertIsNot(running, started)

    def test_advance_transitions_running_to_terminal(self):
        running = running_record()
        done = advance(
            running,
            state="succeeded",
            stages=(stage("inventory", "succeeded", exit_code=0),),
            child=None,
            ended_at=1002.0,
            exit_code=0,
            updated_at=1002.0,
        )
        self.assertEqual(done.state, "succeeded")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.store = JobStore(self.root, clock=lambda: 1000.0)

    def _terminal_record(self, index: int, *, token: str | None = None) -> JobRecord:
        return record(
            job_id=f"20261117T1015{index:02d}-{index:08x}",
            state="succeeded",
            stages=(stage("inventory", "succeeded", exit_code=0),),
            current=0,
            created_at=1000.0 + index,
            updated_at=1000.0 + index,
            ended_at=1000.0 + index,
            exit_code=0,
            client_token=token,
            work_root=str(self.root),
        )

    def test_round_trip_and_canonical_write(self):
        saved = succeeded_record(work_root=str(self.root))
        self.store.save(saved)
        path = self.store.job_path(saved.job_id)
        raw = path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(raw), saved.to_dict())
        self.assertEqual(
            raw,
            (
                json.dumps(
                    saved.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(
            [item.name for item in self.store.jobs_root.iterdir()],
            [path.name],
        )
        self.assertEqual(self.store.load(saved.job_id), saved)

    def test_create_uses_job_id_format_and_queued_shape(self):
        record_out = self.store.create(
            JobParams(),
            stages=(stage(),),
            settings_snapshot={"paths.work_root": str(self.root)},
        )
        self.assertRegex(record_out.job_id, r"\d{8}T\d{6}-[0-9a-f]{8}")
        self.assertEqual(record_out.state, "queued")
        self.assertEqual(record_out.work_root, str(self.root))
        self.assertEqual(self.store.load(record_out.job_id), record_out)

    def test_list_order_and_corrupt_count(self):
        for index in (1, 3, 2):
            self.store.save(self._terminal_record(index))
        (self.store.jobs_root / "20261117T101500-deadbeef.json").write_text(
            "{not json", encoding="utf-8"
        )
        records = self.store.list_records()
        self.assertEqual(
            [item.job_id for item in records],
            [
                "20261117T101503-00000003",
                "20261117T101502-00000002",
                "20261117T101501-00000001",
            ],
        )
        self.assertEqual(self.store.corrupt_count, 1)

    def test_prune_keeps_the_newest_and_the_active_job(self):
        self.store.save(
            record(
                job_id="20261117T101500-aaaaaaaa",
                created_at=999.0,
                updated_at=999.0,
                work_root=str(self.root),
            )
        )
        for index in range(5):
            self.store.save(self._terminal_record(index))
        with mock.patch.object(jobs_module, "MAX_JOBS", 3):
            deleted = self.store.prune(keep_job_id="20261117T101500-aaaaaaaa")
        self.assertEqual(len(deleted), 3)
        remaining = self.store.list_records()
        self.assertLessEqual(len(remaining), 3)
        self.assertIn(
            "20261117T101500-aaaaaaaa",
            [item.job_id for item in remaining],
        )
        self.assertFalse(self.store.job_path(deleted[0]).exists())

    def test_delete_rules(self):
        queued = self.store.create(JobParams(), stages=(stage(),), settings_snapshot={})
        with self.assertRaises(JobNotTerminalError):
            self.store.delete(queued.job_id)
        terminal = self._terminal_record(7)
        self.store.save(terminal)
        log = self.store.log_path(terminal.job_id)
        log.write_text("log", encoding="utf-8")
        self.store.delete(terminal.job_id)
        self.assertFalse(self.store.job_path(terminal.job_id).exists())
        self.assertFalse(log.exists())
        with self.assertRaises(JobNotFoundError):
            self.store.load(terminal.job_id)
        with self.assertRaises(JobNotFoundError):
            self.store.load("../escape")

    def test_find_by_token(self):
        first = self._terminal_record(1, token="alpha")
        self.store.save(first)
        self.assertEqual(self.store.find_by_token("alpha"), first)
        self.assertIsNone(self.store.find_by_token("beta"))

    def test_lock_refuses_a_second_holder(self):
        first = JobStore(self.root).acquire_lock()
        try:
            with self.assertRaises(WorkRootLockedError):
                JobStore(self.root).acquire_lock()
        finally:
            first.release()
        second = JobStore(self.root).acquire_lock()
        second.release()

    def test_latest_progress_and_external_activity(self):
        path = self.store.progress_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"timestamp": 990.0, "stage": "extract"}) + "\n",
            encoding="utf-8",
        )
        activity = self.store.external_activity(now=1000.0)
        self.assertIsNotNone(activity)
        self.assertEqual(activity["stage"], "extract")
        self.assertEqual(activity["age_seconds"], 10.0)
        self.assertIsNone(self.store.external_activity(now=1200.0))
        path.write_text("", encoding="utf-8")
        self.assertIsNone(self.store.external_activity(now=1000.0))


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.store = JobStore(self.root, clock=lambda: 2000.0)

    def test_running_record_kills_a_matching_group_and_interrupts(self):
        child = ChildHandle(pid=111, pgid=111, stage="inventory")
        running = running_record(
            stages=(
                stage("inventory", "running", started_at=1001.0),
                stage("extract", "pending"),
            ),
            current=0,
            child=child,
            work_root=str(self.root),
        )
        self.store.save(running)
        with (
            mock.patch.object(jobs_module.os, "getpgid", return_value=111),
            mock.patch.object(jobs_module.os, "killpg") as kill,
        ):
            self.assertEqual(self.store.reconcile(), 1)
        kill.assert_called_once_with(111, signal.SIGKILL)
        result = self.store.load(running.job_id)
        self.assertEqual(result.state, "interrupted")
        self.assertEqual(result.reason, "restart")
        self.assertIsNone(result.child)
        self.assertEqual(result.ended_at, 2000.0)
        self.assertEqual(result.stages[0].status, "interrupted")
        self.assertEqual(result.stages[1].status, "skipped")

    def test_foreign_group_is_not_killed(self):
        child = ChildHandle(pid=111, pgid=111, stage="inventory")
        running = running_record(child=child, work_root=str(self.root))
        self.store.save(running)
        with (
            mock.patch.object(jobs_module.os, "getpgid", return_value=999),
            mock.patch.object(jobs_module.os, "killpg") as kill,
        ):
            self.store.reconcile()
        kill.assert_not_called()
        self.assertEqual(self.store.load(running.job_id).state, "interrupted")

    def test_missing_process_is_not_killed(self):
        child = ChildHandle(pid=111, pgid=111, stage="inventory")
        running = running_record(child=child, work_root=str(self.root))
        self.store.save(running)
        with (
            mock.patch.object(
                jobs_module.os, "getpgid", side_effect=ProcessLookupError
            ),
            mock.patch.object(jobs_module.os, "killpg") as kill,
        ):
            self.store.reconcile()
        kill.assert_not_called()

    def test_queued_record_becomes_skipped_and_interrupted(self):
        queued = record(
            work_root=str(self.root),
            stages=(stage("inventory"), stage("extract")),
        )
        self.store.save(queued)
        self.assertEqual(self.store.reconcile(), 1)
        result = self.store.load(queued.job_id)
        self.assertEqual(result.state, "interrupted")
        self.assertEqual(result.current, -1)
        self.assertTrue(all(item.status == "skipped" for item in result.stages))


if __name__ == "__main__":
    unittest.main()
