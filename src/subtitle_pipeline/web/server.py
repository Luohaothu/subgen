"""HTTP/1.1 adapter for the web console.

Every response carries an exact ``Content-Length`` and no transfer encoding.
The default request log is suppressed unless the console runs verbose.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .app import Request, Response, WebApp, error_response


class WebRequestHandler(BaseHTTPRequestHandler):
    """Translate one HTTP exchange into an app request and a response."""

    protocol_version = "HTTP/1.1"
    server_version = "subgen-web"
    sys_version = ""

    app: WebApp
    verbose = False

    def log_message(self, fmt: str, *args: object) -> None:
        if self.verbose:
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header is not None else 0
        except ValueError:
            self.close_connection = True
            self._send(
                error_response(
                    400, "invalid_content_length", "Content-Length is not a number"
                )
            )
            return
        if length < 0:
            self.close_connection = True
            self._send(
                error_response(
                    400, "invalid_content_length", "Content-Length is negative"
                )
            )
            return
        if length > self.app.max_body:
            self.close_connection = True
            self._send(
                error_response(
                    413,
                    "payload_too_large",
                    f"request body exceeds {self.app.max_body} bytes",
                )
            )
            return
        body = self.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in self.headers.items()}
        request = Request(
            method=self.command,
            path=self.path,
            headers=headers,
            body=body,
        )
        try:
            response = self.app.handle(request)
        except Exception as exc:  # keep the connection answering a bug
            response = error_response(
                500,
                "internal_error",
                "internal server error",
                detail=f"{type(exc).__name__}: {exc}",
            )
        self._send(response)

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command == "HEAD" or not response.body:
            return
        try:
            self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def create_server(
    app: WebApp,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    verbose: bool = False,
) -> ThreadingHTTPServer:
    """Bind a threaded loopback server for one app instance."""

    class Handler(WebRequestHandler):
        pass

    Handler.app = app
    Handler.verbose = verbose
    server: Any = ThreadingHTTPServer((host, port), Handler)
    return server
