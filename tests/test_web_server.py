from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from subtitle_pipeline.web.app import WebApp
from subtitle_pipeline.web.cli import fallback_work_root
from subtitle_pipeline.web.cli import main as web_main
from subtitle_pipeline.web.jobs import (
    RECORD_SCHEMA,
    JobParams,
    JobRecord,
    JobStore,
    StageRun,
)
from subtitle_pipeline.web.runner import JobRunner
from subtitle_pipeline.web.server import create_server

CONFIG = """[paths]
source_root = "{source}"
work_root = "{work}"
model_root = "{model}"

[translation]
endpoint = "http://127.0.0.1:8123/v1"
model = "vendor/model"
revision = "revision-1"
"""


class CliTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.source = self.root / "source"
        self.work = self.root / "work"
        self.model = self.root / "model"
        self.config = self.root / "subgen.toml"
        self.config.write_text(
            CONFIG.format(source=self.source, work=self.work, model=self.model),
            encoding="utf-8",
        )

    def test_dry_run_prints_paths_without_binding(self):
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(output),
        ):
            code = web_main(["--dry-run", "--config", str(self.config), "--port", "0"])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn(f"config: {self.config}", text)
        self.assertIn(f"work root: {self.work}", text)
        self.assertIn(f"jobs root: {self.work / 'state' / 'web' / 'jobs'}", text)
        self.assertIn("listen: http://127.0.0.1:0", text)

    def test_dry_run_with_invalid_settings_exits_2(self):
        self.config.write_text("not toml", encoding="utf-8")
        error = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stderr(error),
        ):
            code = web_main(["--dry-run", "--config", str(self.config)])
        self.assertEqual(code, 2)
        self.assertIn("settings do not load", error.getvalue())

    def test_fallback_work_root_prefers_the_file_then_the_environment(self):
        self.assertEqual(fallback_work_root({}, self.config), self.work)
        other = self.root / "other"
        self.assertEqual(
            fallback_work_root({"SUBGEN_WORK_ROOT": str(other)}, None), other
        )
        invalid = self.root / "invalid.toml"
        invalid.write_text("not toml", encoding="utf-8")
        self.assertEqual(
            fallback_work_root({"SUBGEN_WORK_ROOT": str(other)}, invalid),
            other,
        )


class ServerTests(unittest.TestCase):
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
        self.config = self.root / "subgen.toml"
        self.config.write_text(
            CONFIG.format(source=self.source, work=self.work, model=self.model),
            encoding="utf-8",
        )
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text(
            "<html>console</html>", encoding="utf-8"
        )
        (self.static / "app.js").write_text("// app\n", encoding="utf-8")
        (self.static / "app.css").write_text("body{}\n", encoding="utf-8")
        self.store = JobStore(self.work)
        self.runner = JobRunner(self.store)

    def _app(self) -> WebApp:
        return WebApp(
            store=self.store,
            runner=self.runner,
            environ={},
            config_path=self.config,
            bound_work_root=self.work,
            bound_source_root=self.source,
            static_root=self.static,
        )

    def test_loopback_serves_index_and_health(self):
        server = create_server(self._app(), port=0)
        thread = threading.Thread(
            target=server.serve_forever, name="test-server", daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        self.assertEqual(host, "127.0.0.1")
        connection = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"<html>console</html>")
        self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(response.getheader("Content-Length"), str(len(body)))
        self.assertIsNone(response.getheader("Transfer-Encoding"))
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(response.getheader("Content-Length"), str(len(body)))

    def test_real_subprocess_cancel_kills_the_group(self):
        process = self._start_sleeping_stage()
        group = os.getpgid(process.pid)
        self.addCleanup(self._kill_group, group)
        self.assertTrue(self.runner.wait(10))
        record = self.store.load(self.record.job_id)
        self.assertEqual(record.state, "cancelled")
        self.assertEqual(record.reason, "cancelled")
        self.assertEqual(record.stages[0].status, "cancelled")
        with self.assertRaises(ProcessLookupError):
            os.killpg(group, 0)
        log = self.store.log_path(self.record.job_id).read_text(encoding="utf-8")
        self.assertIn("=== subgen web job", log)
        self.assertIn("argv: ", log)

    def _start_sleeping_stage(self):
        argv = (
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        )
        stage = StageRun(name="inventory", argv=argv, status="pending")
        self.record = JobRecord(
            schema_version=RECORD_SCHEMA,
            job_id="20261117T101500-abcdef01",
            state="queued",
            params=JobParams(),
            stages=(stage,),
            current=-1,
            created_at=1.0,
            updated_at=1.0,
            ended_at=None,
            exit_code=None,
            child=None,
            error=None,
            reason=None,
            client_token=None,
            origin_job_id=None,
            settings_snapshot={},
            work_root=str(self.work),
        )
        self.store.save(self.record)
        self.runner.start(self.record, environment=os.environ.copy())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = self.store.load(self.record.job_id)
            if current.state == "running" and current.child is not None:
                self.assertTrue(self.runner.cancel())
                return current.child
            time.sleep(0.02)
        self.fail("the worker never spawned the stage")

    @staticmethod
    def _kill_group(pgid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
