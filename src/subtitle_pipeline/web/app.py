"""HTTP request handling, routing, and JSON views for the web console.

The app is transport-neutral: :mod:`subtitle_pipeline.web.server` turns one HTTP
request into a :class:`Request` and writes the :class:`Response`. Every check
that makes the console safe on loopback lives here so it is testable without a
socket.
"""

from __future__ import annotations

import dataclasses
import importlib.resources
import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__, runner_cli
from ..settings import RuntimeSettings, SettingsError, load_settings
from . import progress, settings_io
from .jobs import (
    TERMINAL_STATES,
    CorruptRecordError,
    JobNotFoundError,
    JobNotTerminalError,
    JobParams,
    JobRecord,
    JobStore,
    JobStoreError,
    ParamsError,
    advance,
)
from .runner import RunnerError, UnknownStageError, plan_commands

MAX_BODY_BYTES = 64 * 1024
MAX_PROGRESS_BYTES = progress.MAX_READ_BYTES
MAX_LOG_BYTES = 256 * 1024
LIVE_PROGRESS_BYTES = 4 * 1024 * 1024

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
STATIC_NAMES = frozenset({"app.js", "app.css"})

MAX_BROWSE_ENTRIES = 200
# The frontend owns the labels; these keys and this order are the contract.
PLACE_DIRECTORIES: tuple[tuple[str, str | None], ...] = (
    ("home", None),
    ("movies", "Movies"),
    ("downloads", "Downloads"),
    ("desktop", "Desktop"),
    ("videos", "Videos"),
    ("documents", "Documents"),
)


class ApiError(Exception):
    """An error the HTTP layer renders through the error envelope."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        fields: Mapping[str, str] | None = None,
        detail: str | None = None,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.fields = dict(fields or {})
        self.detail = detail
        self.headers = headers
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class Request:
    """One HTTP request with lower-case header names and a raw body."""

    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes = b""


@dataclasses.dataclass(frozen=True)
class Response:
    """A status, a body, its content type, and any extra headers."""

    status: int
    body: bytes
    content_type: str
    headers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def json(
        cls,
        status: int,
        payload: object,
        *,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> Response:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return cls(
            status,
            body,
            "application/json; charset=utf-8",
            headers,
        )

    @classmethod
    def text(
        cls,
        status: int,
        text: str,
        content_type: str,
        *,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> Response:
        return cls(status, text.encode("utf-8"), content_type, headers)


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    fields: Mapping[str, str] | None = None,
    detail: str | None = None,
    headers: tuple[tuple[str, str], ...] = (),
) -> Response:
    """Render the stable error envelope with all four keys present."""
    return Response.json(
        status,
        {
            "error": {
                "code": code,
                "message": message,
                "fields": dict(fields or {}),
                "detail": detail,
            }
        },
        headers=headers,
    )


def default_static_root() -> Any:
    """The packaged static directory; tests replace it with a fixture."""
    return importlib.resources.files("subtitle_pipeline.web") / "static"


def _host_name(value: str) -> str:
    if value.startswith("["):
        end = value.find("]")
        return value[1:end].lower() if end > 0 else ""
    if ":" in value:
        return value.rsplit(":", 1)[0].lower()
    return value.lower()


def _body_object(request: Request) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        value = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(
            400, "invalid_json", "request body is not valid JSON", detail=str(exc)
        ) from exc
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_json", "request body must be a JSON object")
    return value


def _check_body_keys(body: Mapping[str, object], allowed: frozenset[str]) -> None:
    unknown = sorted(str(key) for key in set(body) - allowed)
    if unknown:
        raise ApiError(
            422,
            "invalid_params",
            "request body has unknown fields",
            fields={key: "unknown field" for key in unknown},
        )


def _is_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_canonical(path: Path) -> bool:
    try:
        return path.resolve(strict=True) == path
    except (OSError, RuntimeError):
        return False


def _entry_is_directory(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _setup_view(
    settings: RuntimeSettings, environ: Mapping[str, str]
) -> dict[str, object]:
    """The setup checklist, derived only from the loaded settings."""
    provider = settings.translation
    return {
        "source_root": str(settings.source_root),
        "source_exists": _is_directory(settings.source_root),
        "source_canonical": _is_canonical(settings.source_root),
        "translation_configured": provider is not None,
        "api_key_configured": bool(
            provider is not None and environ.get(provider.api_key_env)
        ),
        "model_root": str(settings.model_root),
        "model_root_exists": _is_directory(settings.model_root),
        "asr_model_exists": _is_directory(settings.asr_model_path),
    }


def _places_view() -> list[dict[str, str]]:
    """The home-family directories that exist, in the contract order."""
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return []
    places: list[dict[str, str]] = []
    for key, name in PLACE_DIRECTORIES:
        path = home if name is None else home / name
        if _is_directory(path):
            places.append({"key": key, "path": str(path)})
    return places


class WebApp:
    """Routes one request. No socket, no threads, no global state."""

    def __init__(
        self,
        *,
        store: JobStore,
        runner: Any,
        environ: Mapping[str, str],
        config_path: Path | None,
        bound_work_root: Path | None = None,
        bound_source_root: Path | None = None,
        static_root: Any | None = None,
        python: str = sys.executable,
        max_body: int = MAX_BODY_BYTES,
    ) -> None:
        self._store = store
        self._runner = runner
        self._environ = dict(environ)
        self._config_path = settings_io.resolve_config_path(self._environ, config_path)
        self._bound_work_root = (
            Path(bound_work_root) if bound_work_root is not None else store.work_root
        )
        self._bound_source_root = (
            Path(bound_source_root) if bound_source_root is not None else None
        )
        self._static_root = (
            default_static_root() if static_root is None else static_root
        )
        self._python = python
        self._max_body = max_body
        # Create checks the active slot and starts the worker; serialize it so
        # two simultaneous creates cannot both win the slot.
        self._create_lock = threading.Lock()

    @property
    def store(self) -> JobStore:
        return self._store

    @property
    def runner(self) -> Any:
        return self._runner

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def max_body(self) -> int:
        return self._max_body

    def handle(self, request: Request) -> Response:
        try:
            parsed = urlsplit(request.path)
            path = unquote(parsed.path)
            self._guard(request, path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            return self._route(request, path, query)
        except ApiError as exc:
            return error_response(
                exc.status,
                exc.code,
                exc.message,
                fields=exc.fields,
                detail=exc.detail,
                headers=exc.headers,
            )
        except JobNotFoundError as exc:
            return error_response(404, "job_not_found", str(exc))
        except JobNotTerminalError as exc:
            return error_response(409, "job_not_finished", str(exc))
        except CorruptRecordError as exc:
            return error_response(500, "corrupt_job_record", str(exc))
        except ParamsError as exc:
            return error_response(
                422,
                "invalid_params",
                "job parameters are invalid",
                fields=exc.fields,
            )
        except runner_cli.RunnerCliError as exc:
            return error_response(
                422,
                "plan_invalid",
                "the settings cannot produce a run",
                detail=str(exc),
            )
        except settings_io.DraftInvalid as exc:
            return error_response(
                422,
                "invalid_settings",
                "settings draft is invalid",
                fields=exc.fields,
            )
        except settings_io.BaseHashMismatch as exc:
            return error_response(409, "settings_changed", str(exc))
        except settings_io.SettingsLoadError as exc:
            return error_response(409, "settings_load_error", str(exc))
        except settings_io.RewriteError as exc:
            return error_response(409, "cannot_rewrite", str(exc))
        except settings_io.SettingsIOError as exc:
            return error_response(500, "settings_io_error", str(exc))
        except SettingsError as exc:
            return error_response(409, "settings_invalid", str(exc))
        except RunnerError as exc:
            return error_response(409, "runner_conflict", str(exc))
        except JobStoreError as exc:
            return error_response(500, "job_store_error", str(exc))
        except Exception as exc:  # keep the console answering after a bug
            return error_response(
                500,
                "internal_error",
                "internal server error",
                detail=f"{type(exc).__name__}: {exc}",
            )

    def _guard(self, request: Request, path: str) -> None:
        if len(request.body) > self._max_body:
            raise ApiError(
                413,
                "payload_too_large",
                f"request body exceeds {self._max_body} bytes",
            )
        host = request.headers.get("host", "")
        if _host_name(host) not in LOOPBACK_HOSTS:
            raise ApiError(
                403,
                "forbidden_host",
                "the Host header must name a loopback address",
                detail=host or "missing Host header",
            )
        origin = request.headers.get("origin")
        if origin is not None:
            expected = f"http://{host}".rstrip("/")
            if origin.rstrip("/").lower() != expected.lower():
                raise ApiError(
                    403,
                    "forbidden_origin",
                    "cross-origin requests are not allowed",
                    detail=origin,
                )
        if request.method.upper() in MUTATING_METHODS:
            content_type = (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            if content_type != "application/json":
                raise ApiError(
                    415,
                    "unsupported_media_type",
                    "mutating requests require Content-Type: application/json",
                    detail=request.headers.get("content-type", ""),
                )

    def _route(
        self, request: Request, path: str, query: Mapping[str, list[str]]
    ) -> Response:
        method = request.method.upper()
        if path == "/":
            return self._method(
                method, {"GET"}, "GET", lambda: self._static("index.html")
            )
        if path.startswith("/static/"):
            name = path[len("/static/") :]
            return self._method(
                method,
                {"GET"},
                "GET",
                lambda: self._static_builtin(name),
            )
        if path == "/api/health":
            return self._method(
                method, {"GET"}, "GET", lambda: Response.json(200, {"ok": True})
            )
        if path == "/api/meta":
            return self._method(method, {"GET"}, "GET", lambda: self._meta(request))
        if path == "/api/browse":
            return self._method(method, {"GET"}, "GET", lambda: self._browse(query))
        if path == "/api/jobs":
            if method == "GET":
                return self._list_jobs(query)
            if method == "POST":
                return self._create_route(request)
            raise self._method_not_allowed("GET, POST")
        if path.startswith("/api/jobs/"):
            return self._job_route(request, path, query)
        if path == "/api/settings":
            if method == "GET":
                return Response.json(
                    200,
                    settings_io.describe(self._environ, self._config_path),
                )
            if method == "PUT":
                return self._put_settings(request)
            raise self._method_not_allowed("GET, PUT")
        if path == "/api/settings/validate":
            return self._method(
                method, {"POST"}, "POST", lambda: self._validate_settings(request)
            )
        raise ApiError(404, "not_found", f"no route for {path}")

    def _method(
        self,
        method: str,
        allowed: set[str],
        allow_header: str,
        action: Any,
    ) -> Response:
        if method not in allowed:
            raise self._method_not_allowed(allow_header)
        return action()

    @staticmethod
    def _method_not_allowed(allow: str) -> ApiError:
        return ApiError(
            405,
            "method_not_allowed",
            "method not allowed",
            headers=(("Allow", allow),),
        )

    def _static(self, name: str) -> Response:
        try:
            target = self._static_root / name
            if not target.is_file():
                raise ApiError(404, "not_found", f"missing static asset: {name}")
            data = target.read_bytes()
        except ApiError:
            raise
        except OSError as exc:
            raise ApiError(
                404, "not_found", f"missing static asset: {name}", detail=str(exc)
            ) from exc
        content_type = STATIC_FILES.get(name, "application/octet-stream")
        return Response(200, data, content_type, (("Cache-Control", "no-store"),))

    def _static_builtin(self, name: str) -> Response:
        if name not in STATIC_NAMES:
            raise ApiError(404, "not_found", f"unknown static asset: {name}")
        return self._static(name)

    def _job_route(
        self,
        request: Request,
        path: str,
        query: Mapping[str, list[str]],
    ) -> Response:
        method = request.method.upper()
        tail = path[len("/api/jobs/") :]
        parts = tail.split("/")
        if not parts or not parts[0]:
            raise ApiError(404, "not_found", "missing job id")
        job_id = parts[0]
        if len(parts) == 1:
            if method == "GET":
                return Response.json(200, self._job_view(self._store.load(job_id)))
            if method == "DELETE":
                return self._delete(job_id)
            raise self._method_not_allowed("GET, DELETE")
        if len(parts) != 2 or not parts[1]:
            raise ApiError(404, "not_found", f"no route for {path}")
        action = parts[1]
        if action == "progress":
            return self._method(
                method, {"GET"}, "GET", lambda: self._progress(job_id, query)
            )
        if action == "log":
            return self._method(
                method, {"GET"}, "GET", lambda: self._log(job_id, query)
            )
        if action == "cancel":
            return self._method(method, {"POST"}, "POST", lambda: self._cancel(job_id))
        if action == "rerun":
            return self._method(
                method,
                {"POST"},
                "POST",
                lambda: self._rerun(job_id, request),
            )
        raise ApiError(404, "not_found", f"no route for {path}")

    def _load_settings(self) -> tuple[Any | None, str | None]:
        try:
            settings = load_settings(
                environ=self._environ,
                config_path=self._config_path,
                missing_ok=True,
            )
        except SettingsError as exc:
            return None, str(exc)
        return settings, None

    def _restart_required(self, settings: Any) -> bool:
        if settings.work_root != self._bound_work_root:
            return True
        if (
            self._bound_source_root is not None
            and settings.source_root != self._bound_source_root
        ):
            return True
        return False

    def _meta(self, request: Request) -> Response:
        records = self._store.list_records()
        active = next(
            (record for record in records if record.state not in TERMINAL_STATES),
            None,
        )
        settings, load_error = self._load_settings()
        external = None if active is not None else self._store.external_activity()
        host = request.headers.get("host", "")
        payload = {
            "server": {
                "version": __version__,
                "pid": os.getpid(),
                "url": f"http://{host}",
            },
            "config_path": str(self._config_path),
            "config_exists": self._config_path.is_file(),
            "work_root": str(self._store.work_root),
            "jobs_root": str(self._store.jobs_root),
            "settings_ok": settings is not None,
            "load_error": load_error,
            "setup": (
                None if settings is None else _setup_view(settings, self._environ)
            ),
            "places": _places_view(),
            "inventory": self._inventory_info(),
            "active_job": None if active is None else self._summary(active),
            "active_job_id": None if active is None else active.job_id,
            "external_activity": external,
            "jobs_corrupt": self._store.corrupt_count,
            "warnings": [],
        }
        return Response.json(200, payload)

    def _inventory_info(self) -> dict[str, object]:
        path = self._store.work_root / "state" / "runtime_inventory.json"
        if not path.is_file():
            return {"exists": False, "count": None, "schema_version": None}
        try:
            document = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return {"exists": True, "count": None, "schema_version": None}
        records = document.get("records") if isinstance(document, dict) else None
        return {
            "exists": True,
            "count": len(records) if isinstance(records, list) else None,
            "schema_version": (
                document.get("schema_version") if isinstance(document, dict) else None
            ),
        }

    def _browse(self, query: Mapping[str, list[str]]) -> Response:
        raw = (query.get("path") or [""])[0]
        expanded = Path(raw).expanduser()
        if not raw or not expanded.is_absolute():
            raise ApiError(
                400,
                "invalid_path",
                "browse needs an absolute directory in the path parameter",
                detail=raw or None,
            )
        try:
            target = expanded.resolve(strict=True)
        except ValueError as exc:
            raise ApiError(
                400,
                "invalid_path",
                "the path parameter is not a usable path",
                detail=str(exc),
            ) from exc
        except PermissionError as exc:
            raise ApiError(
                403, "forbidden_path", f"cannot read {raw}", detail=str(exc)
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise ApiError(
                404, "not_a_directory", f"no directory at {raw}", detail=str(exc)
            ) from exc
        if not target.is_dir():
            raise ApiError(404, "not_a_directory", f"not a directory: {target}")
        try:
            with os.scandir(target) as scan:
                names = sorted(
                    (
                        entry.name
                        for entry in scan
                        if not entry.name.startswith(".") and _entry_is_directory(entry)
                    ),
                    key=str.casefold,
                )
        except PermissionError as exc:
            raise ApiError(
                403, "forbidden_path", f"cannot read {target}", detail=str(exc)
            ) from exc
        more = len(names) > MAX_BROWSE_ENTRIES
        parent = target.parent
        return Response.json(
            200,
            {
                "path": str(target),
                "parent": None if parent == target else str(parent),
                "entries": [
                    {"name": name, "path": str(target / name)}
                    for name in names[:MAX_BROWSE_ENTRIES]
                ],
                "truncated": more,
            },
        )

    def _list_jobs(self, query: Mapping[str, list[str]]) -> Response:
        records = self._store.list_records()
        active = next(
            (record for record in records if record.state not in TERMINAL_STATES),
            None,
        )
        limit = None
        if "limit" in query:
            limit = _offset(query, "limit", 0)
        selected = records if limit is None else records[:limit]
        return Response.json(
            200,
            {
                "jobs": [self._summary(record) for record in selected],
                "active_job_id": None if active is None else active.job_id,
            },
        )

    def _summary(self, record: JobRecord) -> dict[str, object]:
        stage = (
            record.stages[record.current]
            if 0 <= record.current < len(record.stages)
            else None
        )
        return {
            "job_id": record.job_id,
            "state": record.state,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "ended_at": record.ended_at,
            "current_stage": None if stage is None else stage.name,
            "exit_code": record.exit_code,
            "params": record.params.to_dict(),
            "progress": self._progress_view(record, stage),
        }

    def _progress_view(self, record: JobRecord, stage: Any | None) -> dict[str, object]:
        if record.state == "running" and stage is not None:
            start = stage.progress_start or 0
            end = progress.file_size(self._store.progress_path)
            rows = progress.read_rows(
                self._store.progress_path,
                start,
                min(end, start + LIVE_PROGRESS_BYTES),
            )
            return progress.live_counts(rows, stage.name)
        if record.state == "dry_run":
            return {"done": 0, "total": None, "failed": 0, "exact": True}
        if not 0 <= record.current < len(record.stages):
            return {"done": None, "total": None, "failed": None, "exact": False}
        counters = record.stages[record.current].counters
        if counters is None:
            return {"done": None, "total": None, "failed": None, "exact": False}
        done = counters.get("completed")
        if done is None:
            done = counters.get("count", 0)
        total = counters.get("selected")
        if total is None:
            total = counters.get("count")
        return {
            "done": done,
            "total": total,
            "failed": counters.get("failed", 0),
            "exact": True,
        }

    def _job_view(self, record: JobRecord) -> dict[str, object]:
        return {
            "schema_version": record.schema_version,
            "job_id": record.job_id,
            "state": record.state,
            "params": record.params.to_dict(),
            "current": record.current,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "ended_at": record.ended_at,
            "exit_code": record.exit_code,
            "child": (None if record.child is None else record.child.to_dict()),
            "error": record.error,
            "reason": record.reason,
            "client_token": record.client_token,
            "origin_job_id": record.origin_job_id,
            "settings_snapshot": dict(record.settings_snapshot),
            "work_root": record.work_root,
            "stages": [
                {
                    "name": stage.name,
                    "argv": list(stage.argv),
                    "status": stage.status,
                    "started_at": stage.started_at,
                    "ended_at": stage.ended_at,
                    "exit_code": stage.exit_code,
                    "run_id": stage.run_id,
                    "counters": (
                        None if stage.counters is None else dict(stage.counters)
                    ),
                    "events": [dict(event) for event in stage.events],
                    "progress_start": stage.progress_start,
                    "progress_end": stage.progress_end,
                    "error": stage.error,
                }
                for stage in record.stages
            ],
        }

    def _create_route(self, request: Request) -> Response:
        body = _body_object(request)
        _check_body_keys(
            body, frozenset({"params", "client_token", "acknowledge_external"})
        )
        params = JobParams.parse(body.get("params"))
        client_token = body.get("client_token")
        if client_token is not None and (
            not isinstance(client_token, str) or not client_token
        ):
            raise ApiError(
                422,
                "invalid_params",
                "client_token must be a non-empty string",
                fields={"client_token": "must be a non-empty string"},
            )
        acknowledge = body.get("acknowledge_external", False)
        if type(acknowledge) is not bool:
            raise ApiError(
                422,
                "invalid_params",
                "acknowledge_external must be a boolean",
                fields={"acknowledge_external": "must be a boolean"},
            )
        status, payload = self._create_job(
            params,
            client_token=client_token,
            acknowledge_external=acknowledge,
            origin_job_id=None,
        )
        return Response.json(status, payload)

    def _create_job(
        self,
        params: JobParams,
        *,
        client_token: str | None,
        acknowledge_external: bool,
        origin_job_id: str | None,
    ) -> tuple[int, dict[str, object]]:
        with self._create_lock:
            return self._create_job_locked(
                params,
                client_token=client_token,
                acknowledge_external=acknowledge_external,
                origin_job_id=origin_job_id,
            )

    def _create_job_locked(
        self,
        params: JobParams,
        *,
        client_token: str | None,
        acknowledge_external: bool,
        origin_job_id: str | None,
    ) -> tuple[int, dict[str, object]]:
        if client_token is not None:
            existing = self._store.find_by_token(client_token)
            if existing is not None:
                view = self._job_view(existing)
                view["deduplicated"] = True
                return 200, view
        settings, load_error = self._load_settings()
        if settings is None:
            raise ApiError(
                409,
                "settings_invalid",
                "the pipeline settings do not load",
                detail=load_error,
            )
        if self._restart_required(settings):
            raise ApiError(
                409,
                "restart_required",
                "settings changed the source or work root; restart the console",
            )
        active = self._store.active_job()
        if active is not None:
            raise ApiError(
                409,
                "job_running",
                "another job is already active",
                detail=active.job_id,
            )
        if not acknowledge_external:
            external = self._store.external_activity()
            if external is not None:
                raise ApiError(
                    409,
                    "external_activity",
                    "recent progress rows come from outside this console",
                    detail=(
                        f"{external.get('stage')}/{external.get('event')} "
                        f"at {external.get('timestamp')}"
                    ),
                )
        try:
            stages = plan_commands(settings, params, python=self._python)
        except UnknownStageError as exc:
            raise ApiError(
                500,
                "unknown_stage",
                "the runner cannot classify a command",
                detail=str(exc),
            ) from exc
        record = self._store.create(
            params,
            stages=stages,
            settings_snapshot=settings_io.snapshot(settings),
            client_token=client_token,
            origin_job_id=origin_job_id,
        )
        if params.dry_run:
            record = self._runner.finalize_dry_run(record)
            self._store.save(record)
            return 201, self._job_view(record)
        environment = runner_cli.settings_environment(settings)
        try:
            self._runner.start(record, environment=environment)
        except RunnerError as exc:
            now = time.time()
            stages = tuple(
                dataclasses.replace(stage, status="skipped") for stage in record.stages
            )
            self._store.save(
                advance(
                    record,
                    state="interrupted",
                    stages=stages,
                    current=-1,
                    child=None,
                    ended_at=now,
                    exit_code=None,
                    reason="start_failed",
                    error=str(exc),
                    updated_at=now,
                )
            )
            raise ApiError(409, "job_running", str(exc)) from exc
        try:
            record = self._store.load(record.job_id)
        except JobStoreError:
            pass
        return 201, self._job_view(record)

    def _progress(self, job_id: str, query: Mapping[str, list[str]]) -> Response:
        record = self._store.load(job_id)
        stage = (
            record.stages[record.current]
            if 0 <= record.current < len(record.stages)
            else None
        )
        default_since = stage.progress_start if stage is not None else None
        since = _offset(query, "since", 0 if default_since is None else default_since)
        rows, next_offset = progress.read_window(
            self._store.progress_path, since, max_bytes=MAX_PROGRESS_BYTES
        )
        log_since = _offset(query, "log_since", 0)
        log, next_log_offset, log_reset = self._log_chunk(job_id, log_since)
        return Response.json(
            200,
            {
                "job": self._job_view(record),
                "events": rows,
                "next_offset": next_offset,
                "log": log,
                "next_log_offset": next_log_offset,
                "log_reset": log_reset,
            },
        )

    def _log(self, job_id: str, query: Mapping[str, list[str]]) -> Response:
        self._store.load(job_id)
        since = _offset(query, "since", 0)
        path = self._store.log_path(job_id)
        try:
            with path.open("rb") as stream:
                stream.seek(since)
                data = stream.read(MAX_LOG_BYTES)
        except FileNotFoundError:
            data = b""
        return Response.text(
            200, data.decode("utf-8", "replace"), "text/plain; charset=utf-8"
        )

    def _log_chunk(self, job_id: str, offset: int) -> tuple[str, int, bool]:
        path = self._store.log_path(job_id)
        size = progress.file_size(path)
        reset = offset > size
        if reset:
            offset = 0
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(MAX_LOG_BYTES)
        except FileNotFoundError:
            return "", offset, reset
        return (
            data.decode("utf-8", "replace"),
            offset + len(data),
            reset,
        )

    def _cancel(self, job_id: str) -> Response:
        record = self._store.load(job_id)
        if record.state not in TERMINAL_STATES:
            self._runner.cancel(record.job_id)
            record = self._store.load(job_id)
        return Response.json(200, self._job_view(record))

    def _rerun(self, job_id: str, request: Request) -> Response:
        original = self._store.load(job_id)
        body = _body_object(request)
        _check_body_keys(body, frozenset({"client_token", "acknowledge_external"}))
        client_token = body.get("client_token")
        if client_token is not None and (
            not isinstance(client_token, str) or not client_token
        ):
            raise ApiError(
                422,
                "invalid_params",
                "client_token must be a non-empty string",
                fields={"client_token": "must be a non-empty string"},
            )
        acknowledge = body.get("acknowledge_external", False)
        if type(acknowledge) is not bool:
            raise ApiError(
                422,
                "invalid_params",
                "acknowledge_external must be a boolean",
                fields={"acknowledge_external": "must be a boolean"},
            )
        status, payload = self._create_job(
            original.params,
            client_token=client_token,
            acknowledge_external=acknowledge,
            origin_job_id=original.job_id,
        )
        return Response.json(status, payload)

    def _delete(self, job_id: str) -> Response:
        self._store.delete(job_id)
        return Response.json(200, {"deleted": True, "job_id": job_id})

    def _validate_settings(self, request: Request) -> Response:
        body = _body_object(request)
        _check_body_keys(body, frozenset({"values"}))
        result = settings_io.validate_draft(
            body.get("values"),
            environ=self._environ,
            config_path=self._config_path,
        )
        if not result.valid:
            raise ApiError(
                422,
                "invalid_settings",
                "settings draft is invalid",
                fields=result.errors,
            )
        return Response.json(
            200,
            {
                "valid": True,
                "preview": result.preview,
                "fields": list(result.fields),
                "warnings": list(result.warnings),
            },
        )

    def _put_settings(self, request: Request) -> Response:
        body = _body_object(request)
        _check_body_keys(body, frozenset({"values", "base_sha256", "force"}))
        base_sha256 = body.get("base_sha256")
        if not isinstance(base_sha256, str) or not base_sha256:
            raise ApiError(
                422,
                "invalid_params",
                "base_sha256 is required",
                fields={"base_sha256": "must be a non-empty string"},
            )
        force = body.get("force", False)
        if type(force) is not bool:
            raise ApiError(
                422,
                "invalid_params",
                "force must be a boolean",
                fields={"force": "must be a boolean"},
            )
        settings_io.save(
            body.get("values"),
            environ=self._environ,
            config_path=self._config_path,
            expected_sha256=base_sha256,
            force=force,
        )
        return Response.json(
            200, settings_io.describe(self._environ, self._config_path)
        )


def _offset(query: Mapping[str, list[str]], name: str, default: int) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ApiError(
            422,
            "invalid_offset",
            f"{name} must be a nonnegative integer",
            fields={name: "must be a nonnegative integer"},
        ) from exc
    if value < 0:
        raise ApiError(
            422,
            "invalid_offset",
            f"{name} must be a nonnegative integer",
            fields={name: "must be a nonnegative integer"},
        )
    return value
