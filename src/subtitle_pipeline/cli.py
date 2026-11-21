"""Single command entry point for the pipeline and its server."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from . import runner_cli, serve_cli
from .web import cli as web_cli


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "serve":
        return serve_cli.main(args[1:])
    if args and args[0] == "web":
        return web_cli.main(args[1:])
    return runner_cli.main(args)
