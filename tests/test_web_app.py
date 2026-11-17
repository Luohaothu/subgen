from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from subtitle_pipeline.web.app import MAX_BODY_BYTES, Request, WebApp
from subtitle_pipeline.web.jobs import (
    RECORD_SCHEMA,
    JobParams,
    JobRecord,
    JobStore,
    StageRun,
    advance,
)
from subtitle_pipeline.web.runner import RunnerError

CONFIG = """# comment
[paths]
source_root = "{source}"
work_root = "{work}"
model_root = "{model}"
asr_model = "{asr}"

[runtime]
device = "auto"
asr_engine = "auto"

[translation]
endpoint = "http://127.0.0.1:8123/v1"
model = "vendor/model"
revision = "revision-1"
"""


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        self.value += 0.5
        return self.value


class FakeRunner:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.started: list[tuple[JobRecord, dict[str, str]]] = []
        self.cancelled: list[str] = []

    def start(self, record: JobRecord, *, environment: dict[str, str]) -> None:
        self.started.append((record, environment))

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True

    def finalize_dry_run(self, record: JobRecord) -> JobRecord:
        now = self.clock()
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


class AppFixture(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.model = self.root / "model"
        self.model.mkdir()
        self.asr = self.model / "asr"
        self.asr.mkdir()
        self.config = self.root / "subgen.toml"
        self.config.write_text(
            CONFIG.format(
                source=self.source,
                work=self.work,
                model=self.model,
                asr=self.asr,
            ),
            encoding="utf-8",
        )
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<html>index</html>", encoding="utf-8")
        (self.static / "app.js").write_text("console.log('app');", encoding="utf-8")
        (self.static / "app.css").write_text("body {}", encoding="utf-8")
        self.clock = Clock()
        self.store = JobStore(self.work, clock=self.clock)
        self.runner = FakeRunner(self.clock)
        self.app = self.make_app()

    def make_app(
        self,
        *,
        bound_work_root: Path | None = None,
        bound_source_root: Path | None = None,
        config_path: Path | None = None,
    ) -> WebApp:
        return WebApp(
            store=self.store,
            runner=self.runner,
            environ={},
            config_path=self.config if config_path is None else config_path,
            bound_work_root=(self.work if bound_work_root is None else bound_work_root),
            bound_source_root=(
                self.source if bound_source_root is None else bound_source_root
            ),
            static_root=self.static,
            python="python",
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        host: str = "127.0.0.1:8765",
    ):
        request_headers = {"host": host}
        if raw_body is not None:
            payload = raw_body
            request_headers["content-type"] = "application/json"
        elif body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers["content-type"] = "application/json"
        else:
            payload = b""
        request_headers.update(headers or {})
        return self.app.handle(Request(method, path, request_headers, payload))

    @staticmethod
    def payload(response) -> dict:
        return json.loads(response.body)

    def create_dry_run(self, **params: object):
        value = {"params": {"dry_run": True, **params}}
        return self.request("POST", "/api/jobs", body=value)


class GuardTests(AppFixture):
    def test_health_and_meta(self):
        health = self.request("GET", "/api/health")
        self.assertEqual(health.status, 200)
        self.assertEqual(self.payload(health), {"ok": True})
        meta = self.payload(self.request("GET", "/api/meta"))
        self.assertEqual(meta["work_root"], str(self.work))
        self.assertEqual(meta["config_path"], str(self.config))
        self.assertTrue(meta["settings_ok"])
        self.assertIsNone(meta["load_error"])
        self.assertEqual(meta["active_job_id"], None)
        self.assertIsNone(meta["external_activity"])
        self.assertEqual(meta["inventory"]["exists"], False)
        self.assertEqual(meta["jobs_corrupt"], 0)
        self.assertEqual(meta["server"]["url"], "http://127.0.0.1:8765")
        self.assertIn("version", meta["server"])

    def test_index_and_static_whitelist(self):
        index = self.request("GET", "/")
        self.assertEqual(index.status, 200)
        self.assertTrue(index.content_type.startswith("text/html"))
        self.assertEqual(index.body, b"<html>index</html>")
        script = self.request("GET", "/static/app.js")
        self.assertEqual(script.status, 200)
        self.assertTrue(script.content_type.startswith("text/javascript"))
        style = self.request("GET", "/static/app.css")
        self.assertTrue(style.content_type.startswith("text/css"))
        for path in (
            "/static/index.html",
            "/static/missing.js",
            "/static/../index.html",
            "/static/%2e%2e%2findex.html",
            "/static/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path).status, 404)

    def test_host_guard(self):
        for host in ("example.com:8765", "127.0.0.2:8765", ""):
            with self.subTest(host=host):
                response = self.request("GET", "/api/health", host=host)
                self.assertEqual(response.status, 403)
                self.assertEqual(
                    self.payload(response)["error"]["code"],
                    "forbidden_host",
                )
        for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
            with self.subTest(host=host):
                self.assertEqual(
                    self.request("GET", "/api/health", host=host).status,
                    200,
                )

    def test_origin_guard(self):
        response = self.request(
            "POST",
            "/api/jobs",
            body={"params": {"dry_run": True}},
            headers={"origin": "http://evil.test"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.payload(response)["error"]["code"], "forbidden_origin")
        response = self.request(
            "POST",
            "/api/jobs",
            body={"params": {"dry_run": True}},
            headers={"origin": "http://127.0.0.1:8765"},
        )
        self.assertEqual(response.status, 201)

    def test_mutating_requests_require_json(self):
        for method, path in (
            ("POST", "/api/jobs"),
            ("PUT", "/api/settings"),
            ("DELETE", "/api/jobs/20261117T101500-1a2b3c4d"),
        ):
            with self.subTest(method=method):
                response = self.request(
                    method,
                    path,
                    headers={"content-type": "text/plain"},
                )
                self.assertEqual(response.status, 415)
        response = self.request(
            "POST",
            "/api/jobs",
            body={"params": {"dry_run": True}},
            headers={
                "content-type": "application/json; charset=utf-8",
                "origin": "http://127.0.0.1:8765",
            },
        )
        self.assertEqual(response.status, 201)

    def test_body_limit(self):
        response = self.request(
            "POST",
            "/api/jobs",
            raw_body=b"x" * (MAX_BODY_BYTES + 1),
        )
        self.assertEqual(response.status, 413)
        self.assertEqual(self.payload(response)["error"]["code"], "payload_too_large")

    def test_invalid_json(self):
        for raw in (b"{not json", b"[]", b'"text"'):
            with self.subTest(raw=raw):
                response = self.request("POST", "/api/jobs", raw_body=raw)
                self.assertEqual(response.status, 400)
                self.assertEqual(
                    self.payload(response)["error"]["code"], "invalid_json"
                )

    def test_unknown_route_and_method(self):
        missing = self.request("GET", "/api/nope")
        self.assertEqual(missing.status, 404)
        error = self.payload(missing)["error"]
        self.assertEqual(set(error), {"code", "message", "fields", "detail"})
        wrong = self.request(
            "DELETE",
            "/api/health",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(wrong.status, 405)
        self.assertIn("Allow", dict(wrong.headers))
        wrong = self.request("GET", "/api/jobs/x/cancel")
        self.assertEqual(wrong.status, 405)
        missing_job = self.request("GET", "/api/jobs/not-a-job")
        self.assertEqual(missing_job.status, 404)
        traversal = self.request("GET", "/api/jobs/%2e%2e%2fescape")
        self.assertEqual(traversal.status, 404)


class CreateTests(AppFixture):
    def test_create_dry_run_records_every_planned_stage(self):
        response = self.create_dry_run()
        self.assertEqual(response.status, 201)
        view = self.payload(response)
        self.assertEqual(view["state"], "dry_run")
        self.assertEqual(view["current"], -1)
        self.assertIsNone(view["exit_code"])
        self.assertEqual(
            [stage["name"] for stage in view["stages"]],
            [
                "inventory",
                "extract",
                "asr",
                "alignment",
                "translation_primary",
                "translation_review",
                "publish",
            ],
        )
        for stage in view["stages"]:
            self.assertEqual(stage["status"], "skipped")
            self.assertTrue(stage["argv"])
        self.assertEqual(self.runner.started, [])
        stored = self.store.load(view["job_id"])
        self.assertEqual(stored.state, "dry_run")
        self.assertEqual(
            stored.settings_snapshot["paths.source_root"], str(self.source)
        )

    def test_create_with_skip_flags_plans_five_stages(self):
        response = self.create_dry_run(skip_inventory=True, skip_extract=True)
        self.assertEqual(response.status, 201)
        names = [stage["name"] for stage in self.payload(response)["stages"]]
        self.assertEqual(
            names,
            [
                "asr",
                "alignment",
                "translation_primary",
                "translation_review",
                "publish",
            ],
        )

    def test_create_rejects_unknown_and_malformed_fields(self):
        cases = (
            {"params": {}, "nope": 1},
            {"params": {"nope": 1}},
            {"params": {"limit": -1}},
            {"params": {"video_ids": ["A" * 64]}},
            {"params": {}, "client_token": ""},
            {"params": {}, "acknowledge_external": "yes"},
            {},
        )
        for body in cases:
            with self.subTest(body=body):
                response = self.request("POST", "/api/jobs", body=body)
                self.assertEqual(response.status, 422)
                error = self.payload(response)["error"]
                self.assertTrue(error["fields"])

    def test_client_token_deduplicates(self):
        body = {"params": {"dry_run": True}, "client_token": "token-1"}
        first = self.request("POST", "/api/jobs", body=body)
        self.assertEqual(first.status, 201)
        first_view = self.payload(first)
        second = self.request("POST", "/api/jobs", body=body)
        self.assertEqual(second.status, 200)
        second_view = self.payload(second)
        self.assertTrue(second_view["deduplicated"])
        self.assertEqual(second_view["job_id"], first_view["job_id"])
        self.assertEqual(len(self.store.list_records()), 1)

    def test_failed_start_does_not_leave_a_queued_job(self):
        class FailingRunner(FakeRunner):
            def start(self, record, *, environment):
                raise RunnerError("a job is already running")

        self.runner = FailingRunner(self.clock)
        self.app = self.make_app()
        response = self.request("POST", "/api/jobs", body={"params": {}})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.payload(response)["error"]["code"], "job_running")
        records = self.store.list_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].state, "interrupted")
        self.assertEqual(records[0].reason, "start_failed")
        self.assertIsNone(self.store.active_job())

    def test_concurrent_creates_share_the_active_slot(self):
        class BlockingRunner(FakeRunner):
            def __init__(self, clock):
                super().__init__(clock)
                self.entered = threading.Event()
                self.release = threading.Event()

            def start(self, record, *, environment):
                self.entered.set()
                self.release.wait(3)
                super().start(record, environment=environment)

        runner = BlockingRunner(self.clock)
        self.runner = runner
        self.app = self.make_app()
        statuses: list[int] = []

        def create() -> None:
            statuses.append(
                self.request("POST", "/api/jobs", body={"params": {}}).status
            )

        first = threading.Thread(target=create)
        second = threading.Thread(target=create)
        first.start()
        self.assertTrue(runner.entered.wait(2))
        second.start()
        time.sleep(0.05)
        runner.release.set()
        first.join(3)
        second.join(3)
        self.assertEqual(sorted(statuses), [201, 409])
        self.assertEqual(len(self.store.list_records()), 1)

    def test_second_create_conflicts_with_an_active_job(self):
        created = self.request("POST", "/api/jobs", body={"params": {}})
        self.assertEqual(created.status, 201)
        self.assertEqual(len(self.runner.started), 1)
        conflict = self.create_dry_run()
        self.assertEqual(conflict.status, 409)
        self.assertEqual(self.payload(conflict)["error"]["code"], "job_running")

    def test_external_activity_gate(self):
        path = self.store.progress_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "timestamp": 999.0,
                    "stage": "extract",
                    "event": "completed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        response = self.create_dry_run()
        self.assertEqual(response.status, 409)
        error = self.payload(response)["error"]
        self.assertEqual(error["code"], "external_activity")
        acknowledged = self.request(
            "POST",
            "/api/jobs",
            body={
                "params": {"dry_run": True},
                "acknowledge_external": True,
            },
        )
        self.assertEqual(acknowledged.status, 201)

    def test_settings_invalid_blocks_creation(self):
        self.config.write_text("not toml", encoding="utf-8")
        response = self.create_dry_run()
        self.assertEqual(response.status, 409)
        self.assertEqual(self.payload(response)["error"]["code"], "settings_invalid")

    def test_restart_required_after_source_root_change(self):
        app = self.make_app(bound_source_root=self.root / "other-source")
        self.app = app
        response = self.create_dry_run()
        self.assertEqual(response.status, 409)
        self.assertEqual(self.payload(response)["error"]["code"], "restart_required")

    def test_missing_provider_returns_plan_invalid(self):
        self.config.write_text(
            CONFIG.format(
                source=self.source,
                work=self.work,
                model=self.model,
                asr=self.asr,
            )
            .replace('[translation]\nendpoint = "http://127.0.0.1:8123/v1"\n', "")
            .replace('model = "vendor/model"\n', "")
            .replace('revision = "revision-1"\n', ""),
            encoding="utf-8",
        )
        response = self.create_dry_run()
        self.assertEqual(response.status, 422)
        self.assertEqual(self.payload(response)["error"]["code"], "plan_invalid")


class JobRouteTests(AppFixture):
    def test_list_jobs_newest_first_and_corrupt_count(self):
        first = self.payload(self.create_dry_run())
        second = self.payload(self.create_dry_run())
        listing = self.payload(self.request("GET", "/api/jobs"))
        self.assertEqual(
            [job["job_id"] for job in listing["jobs"]],
            [second["job_id"], first["job_id"]],
        )
        self.assertIsNone(listing["active_job_id"])
        summary = listing["jobs"][0]
        self.assertEqual(
            set(summary),
            {
                "job_id",
                "state",
                "created_at",
                "updated_at",
                "ended_at",
                "current_stage",
                "exit_code",
                "params",
                "progress",
            },
        )
        self.assertEqual(summary["progress"]["exact"], True)
        self.assertIsNone(summary["current_stage"])
        corrupt = self.store.jobs_root / "20261117T101500-deadbeef.json"
        corrupt.write_text("{", encoding="utf-8")
        meta = self.payload(self.request("GET", "/api/meta"))
        self.assertEqual(meta["jobs_corrupt"], 1)

    def test_failed_stage_without_counters_reports_no_progress(self):
        succeeded = StageRun(
            name="inventory",
            argv=("python", "-m", "subtitle_pipeline.runtime_cli", "inventory"),
            status="succeeded",
            exit_code=0,
            counters={"count": 2},
        )
        failed = StageRun(
            name="extract",
            argv=("python", "-m", "subtitle_pipeline.runtime_cli", "extract"),
            status="failed",
            exit_code=1,
            error="stage exited with status 1",
        )
        record = JobRecord(
            schema_version=RECORD_SCHEMA,
            job_id="20261117T101500-1a2b3c4d",
            state="failed",
            params=JobParams(),
            stages=(succeeded, failed),
            current=1,
            created_at=1.0,
            updated_at=2.0,
            ended_at=2.0,
            exit_code=1,
            child=None,
            error="extract exited with status 1",
            reason=None,
            client_token=None,
            origin_job_id=None,
            settings_snapshot={},
            work_root=str(self.work),
        )
        self.store.save(record)
        summary = self.payload(self.request("GET", "/api/jobs"))["jobs"][0]
        self.assertEqual(summary["current_stage"], "extract")
        self.assertIsNone(summary["progress"]["done"])
        self.assertIsNone(summary["progress"]["total"])
        self.assertFalse(summary["progress"]["exact"])

    def test_get_job_view_and_missing_job(self):
        created = self.payload(self.create_dry_run())
        response = self.request("GET", f"/api/jobs/{created['job_id']}")
        self.assertEqual(response.status, 200)
        view = self.payload(response)
        self.assertEqual(view["job_id"], created["job_id"])
        self.assertEqual(view["schema_version"], RECORD_SCHEMA)
        stage = view["stages"][0]
        self.assertEqual(
            set(stage),
            {
                "name",
                "argv",
                "status",
                "started_at",
                "ended_at",
                "exit_code",
                "run_id",
                "counters",
                "events",
                "progress_start",
                "progress_end",
                "error",
            },
        )
        missing = self.request("GET", "/api/jobs/20261117T101500-00000000")
        self.assertEqual(missing.status, 404)

    def test_cancel_requests_and_idempotent_terminal(self):
        terminal = self.payload(self.create_dry_run())
        created = self.payload(self.request("POST", "/api/jobs", body={"params": {}}))
        response = self.request(
            "POST", f"/api/jobs/{created['job_id']}/cancel", body={}
        )
        self.assertEqual(response.status, 200)
        self.assertIn(created["job_id"], self.runner.cancelled)
        response = self.request(
            "POST", f"/api/jobs/{terminal['job_id']}/cancel", body={}
        )
        self.assertEqual(response.status, 200)
        self.assertNotIn(terminal["job_id"], self.runner.cancelled)
        missing = self.request(
            "POST", "/api/jobs/20261117T101500-00000000/cancel", body={}
        )
        self.assertEqual(missing.status, 404)

    def test_rerun_copies_params_and_links_the_origin(self):
        original = self.payload(self.create_dry_run(limit=2))
        response = self.request(
            "POST", f"/api/jobs/{original['job_id']}/rerun", body={}
        )
        self.assertEqual(response.status, 201)
        view = self.payload(response)
        self.assertNotEqual(view["job_id"], original["job_id"])
        self.assertEqual(view["origin_job_id"], original["job_id"])
        self.assertEqual(view["params"], original["params"])
        self.assertEqual(view["state"], "dry_run")
        missing = self.request(
            "POST", "/api/jobs/20261117T101500-00000000/rerun", body={}
        )
        self.assertEqual(missing.status, 404)

    def test_delete_rules(self):
        terminal = self.payload(self.create_dry_run())
        response = self.request(
            "DELETE",
            f"/api/jobs/{terminal['job_id']}",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(self.payload(response)["deleted"])
        self.assertFalse(self.store.job_path(terminal["job_id"]).exists())
        active = self.payload(self.request("POST", "/api/jobs", body={"params": {}}))
        response = self.request(
            "DELETE",
            f"/api/jobs/{active['job_id']}",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(self.payload(response)["error"]["code"], "job_not_finished")
        missing = self.request(
            "DELETE",
            "/api/jobs/20261117T101500-00000000",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(missing.status, 404)

    def test_meta_reports_active_job_and_hides_external_activity(self):
        created = self.payload(self.request("POST", "/api/jobs", body={"params": {}}))
        path = self.store.progress_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"timestamp": 999.0, "stage": "extract", "event": "started"})
            + "\n",
            encoding="utf-8",
        )
        meta = self.payload(self.request("GET", "/api/meta"))
        self.assertEqual(meta["active_job_id"], created["job_id"])
        self.assertEqual(meta["active_job"]["job_id"], created["job_id"])
        self.assertIsNone(meta["external_activity"])

    def test_meta_reports_external_activity_without_a_web_job(self):
        path = self.store.progress_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "timestamp": 995.0,
                    "stage": "corpus_asr",
                    "event": "video_completed",
                    "run_id": "run",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        meta = self.payload(self.request("GET", "/api/meta"))
        self.assertEqual(meta["external_activity"]["stage"], "corpus_asr")
        self.assertEqual(meta["external_activity"]["run_id"], "run")
        self.assertTrue(meta["external_activity"]["active"])

    def test_meta_inventory_summary(self):
        inventory = self.work / "state" / "runtime_inventory.json"
        inventory.parent.mkdir(parents=True)
        inventory.write_text(
            json.dumps(
                {
                    "records": [{"video_id": "a"}, {"video_id": "b"}],
                    "schema_version": "inventory-v1",
                }
            ),
            encoding="utf-8",
        )
        meta = self.payload(self.request("GET", "/api/meta"))
        self.assertEqual(
            meta["inventory"],
            {
                "exists": True,
                "count": 2,
                "schema_version": "inventory-v1",
            },
        )


class ProgressAndLogTests(AppFixture):
    def _terminal_record(self) -> JobRecord:
        self.store.progress_path.parent.mkdir(parents=True, exist_ok=True)
        rows = (
            {"timestamp": 1.0, "stage": "inventory", "event": "observed"},
            {
                "timestamp": 2.0,
                "stage": "inventory",
                "event": "completed",
                "count": 2,
            },
        )
        payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        self.store.progress_path.write_text(payload, encoding="utf-8")
        size = len(payload.encode("utf-8"))
        stage = StageRun(
            name="inventory",
            argv=("python", "-m", "subtitle_pipeline.runtime_cli", "inventory"),
            status="succeeded",
            started_at=1.0,
            ended_at=3.0,
            exit_code=0,
            run_id="run-1",
            counters={"count": 2},
            events=({"video_id": "a" * 64, "event": "observed"},),
            progress_start=0,
            progress_end=size,
        )
        record = JobRecord(
            schema_version=RECORD_SCHEMA,
            job_id="20261117T101500-1a2b3c4d",
            state="succeeded",
            params=JobParams(),
            stages=(stage,),
            current=0,
            created_at=1.0,
            updated_at=4.0,
            ended_at=4.0,
            exit_code=0,
            child=None,
            error=None,
            reason=None,
            client_token=None,
            origin_job_id=None,
            settings_snapshot={},
            work_root=str(self.work),
        )
        self.store.save(record)
        log = "header\nstage one\nstage two\n"
        self.store.log_path(record.job_id).write_text(log, encoding="utf-8")
        return record

    def test_progress_endpoint_returns_rows_log_and_offsets(self):
        record = self._terminal_record()
        response = self.request(
            "GET",
            f"/api/jobs/{record.job_id}/progress?since=0&log_since=0",
        )
        self.assertEqual(response.status, 200)
        view = self.payload(response)
        self.assertEqual(view["job"]["state"], "succeeded")
        self.assertEqual(len(view["events"]), 2)
        self.assertEqual(view["next_offset"], record.stages[0].progress_end)
        self.assertEqual(view["log"], "header\nstage one\nstage two\n")
        self.assertEqual(view["next_log_offset"], len("header\nstage one\nstage two\n"))
        self.assertFalse(view["log_reset"])

    def test_progress_endpoint_resets_a_stale_log_offset(self):
        record = self._terminal_record()
        response = self.request(
            "GET",
            f"/api/jobs/{record.job_id}/progress?log_since=99999",
        )
        view = self.payload(response)
        self.assertTrue(view["log_reset"])
        self.assertEqual(view["log"], "header\nstage one\nstage two\n")
        self.assertEqual(view["next_log_offset"], len("header\nstage one\nstage two\n"))

    def test_progress_endpoint_rejects_bad_offsets(self):
        record = self._terminal_record()
        for query in ("since=-1", "since=nope", "log_since=-2"):
            with self.subTest(query=query):
                response = self.request(
                    "GET", f"/api/jobs/{record.job_id}/progress?{query}"
                )
                self.assertEqual(response.status, 422)
                self.assertEqual(
                    self.payload(response)["error"]["code"],
                    "invalid_offset",
                )

    def test_log_endpoint_is_plain_text_from_an_offset(self):
        record = self._terminal_record()
        response = self.request("GET", f"/api/jobs/{record.job_id}/log?since=7")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.content_type.startswith("text/plain"))
        self.assertEqual(response.body, b"stage one\nstage two\n")
        beyond = self.request("GET", f"/api/jobs/{record.job_id}/log?since=99999")
        self.assertEqual(beyond.status, 200)
        self.assertEqual(beyond.body, b"")
        missing = self.request("GET", "/api/jobs/20261117T101500-00000000/log")
        self.assertEqual(missing.status, 404)


class SettingsRouteTests(AppFixture):
    def values(self) -> dict[str, str]:
        view = self.payload(self.request("GET", "/api/settings"))
        return {
            f"{field['section']}.{field['key']}": field["file_value"] or ""
            for field in view["fields"]
        }

    def test_get_and_validate(self):
        view = self.payload(self.request("GET", "/api/settings"))
        self.assertTrue(view["settings_ok"])
        self.assertEqual(len(view["fields"]), 12)
        self.assertTrue(view["base_sha256"])
        response = self.request(
            "POST",
            "/api/settings/validate",
            body={"values": self.values()},
        )
        self.assertEqual(response.status, 200)
        preview = self.payload(response)
        self.assertTrue(preview["valid"])
        self.assertIn("[paths]", preview["preview"])
        self.assertEqual(len(preview["fields"]), 12)

    def test_validate_rejects_invalid_values(self):
        values = self.values()
        values["paths.source_root"] = str(self.root / "missing")
        response = self.request(
            "POST", "/api/settings/validate", body={"values": values}
        )
        self.assertEqual(response.status, 422)
        error = self.payload(response)["error"]
        self.assertEqual(error["code"], "invalid_settings")
        self.assertIn("paths.source_root", error["fields"])

    def test_put_saves_and_reports_a_base_hash_conflict(self):
        before = self.payload(self.request("GET", "/api/settings"))
        values = self.values()
        values["runtime.device"] = "mps"
        response = self.request(
            "PUT",
            "/api/settings",
            body={
                "values": values,
                "base_sha256": before["base_sha256"],
            },
        )
        self.assertEqual(response.status, 200)
        saved = self.payload(response)
        field = next(item for item in saved["fields"] if item["key"] == "device")
        self.assertEqual(field["file_value"], "mps")
        conflict = self.request(
            "PUT",
            "/api/settings",
            body={
                "values": values,
                "base_sha256": before["base_sha256"],
            },
        )
        self.assertEqual(conflict.status, 409)
        self.assertEqual(self.payload(conflict)["error"]["code"], "settings_changed")

    def test_put_refuses_an_unrewritable_layout(self):
        self.config.write_text(
            f'[paths]\nsource_root = "{self.source}"\nwork_root = "{self.work}"\n'
            '\n[runtime]\ndevice = """cpu"""\nasr_engine = "auto"\n',
            encoding="utf-8",
        )
        view = self.payload(self.request("GET", "/api/settings"))
        values = {
            f"{field['section']}.{field['key']}": field["file_value"] or ""
            for field in view["fields"]
        }
        values["runtime.device"] = "cuda"
        response = self.request(
            "PUT",
            "/api/settings",
            body={"values": values, "base_sha256": view["base_sha256"]},
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(self.payload(response)["error"]["code"], "cannot_rewrite")


if __name__ == "__main__":
    unittest.main()
