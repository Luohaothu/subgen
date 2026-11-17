"""``subgen web``: run the local web console on loopback.

Startup resolves the config path, loads settings, takes the work-root lock,
reconciles leftover records, binds the listener, and then waits on a shutdown
event. Signal handlers only set that event; the main thread performs the
shutdown so the worker and the listener stop in a defined order.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import tomllib
import webbrowser
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..settings import (
    DEFAULT_WORK_ROOT,
    RuntimeSettings,
    SettingsError,
    load_settings,
    resolve_config_path,
)
from . import settings_io
from .app import WebApp
from .jobs import JobStore, WorkRootLockedError
from .runner import JobRunner
from .server import create_server

DEFAULT_PORT = 8765
HOST = "127.0.0.1"


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subgen web",
        description="Run the local subtitle pipeline console on loopback",
    )
    parser.add_argument("--config", type=Path, help="settings file to read and write")
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="open the URL in a browser")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved paths without binding",
    )
    parser.add_argument("--verbose", action="store_true", help="log every request")
    return parser


def fallback_work_root(environ: Mapping[str, str], config_path: Path | None) -> Path:
    """Locate the work root when the settings file cannot be loaded."""
    if config_path is not None:
        try:
            document = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            document = {}
        if isinstance(document, dict):
            paths = document.get("paths")
            if isinstance(paths, dict):
                value = paths.get("work_root")
                if isinstance(value, str):
                    candidate = Path(value).expanduser()
                    if candidate.is_absolute():
                        return candidate
    configured = environ.get("SUBGEN_WORK_ROOT")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
    return DEFAULT_WORK_ROOT


def _print_dry_run(config_file: Path | None, work_root: Path, port: int) -> None:
    jobs_root = work_root / "state" / "web" / "jobs"
    print(f"config: {config_file if config_file is not None else '(none)'}")
    print(f"work root: {work_root}")
    print(f"jobs root: {jobs_root}")
    print(f"listen: http://{HOST}:{port}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    environ = os.environ
    resolved = resolve_config_path(environ, args.config)
    display_path = settings_io.resolve_config_path(environ, args.config)
    settings: RuntimeSettings | None = None
    load_error: str | None = None
    try:
        settings = load_settings(environ=environ, config_path=resolved, missing_ok=True)
    except SettingsError as exc:
        load_error = str(exc)
    if args.dry_run:
        if settings is None:
            print(f"subgen web: settings do not load: {load_error}", file=sys.stderr)
            return 2
        _print_dry_run(resolved, settings.work_root, args.port)
        return 0
    work_root = (
        settings.work_root
        if settings is not None
        else fallback_work_root(environ, resolved)
    )
    store = JobStore(work_root)
    try:
        store.acquire_lock()
    except WorkRootLockedError as exc:
        print(f"subgen web: {exc}", file=sys.stderr, flush=True)
        return 2
    except OSError as exc:
        print(
            f"subgen web: cannot use work root {work_root}: {exc}. "
            "Set SUBGEN_WORK_ROOT or paths.work_root in subgen.toml, "
            "then create the directory.",
            file=sys.stderr,
        )
        return 2
    try:
        store.reconcile()
    except OSError as exc:
        print(f"subgen web: cannot reconcile jobs: {exc}", file=sys.stderr)
        store.release_lock()
        return 1
    runner = JobRunner(store)
    app = WebApp(
        store=store,
        runner=runner,
        environ=environ,
        config_path=display_path,
        bound_work_root=work_root,
        bound_source_root=settings.source_root if settings is not None else None,
    )
    try:
        server = create_server(app, host=HOST, port=args.port, verbose=args.verbose)
    except OSError as exc:
        print(f"subgen web: cannot bind {HOST}:{args.port}: {exc}", file=sys.stderr)
        store.release_lock()
        return 2
    port = int(server.server_address[1])
    url = f"http://{HOST}:{port}"
    print(url, flush=True)
    shutdown = threading.Event()

    def _request_shutdown(signum: int, frame: object) -> None:
        shutdown.set()

    previous: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, _request_shutdown)
    thread = threading.Thread(
        target=server.serve_forever,
        name="subgen-web-http",
        daemon=True,
    )
    thread.start()
    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        shutdown.wait()
    except KeyboardInterrupt:
        shutdown.set()
    finally:
        runner.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
        store.release_lock()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
