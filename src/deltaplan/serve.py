"""Serving one page on localhost, for `deltaplan ui`.

The page is a plan rendered as HTML — a whole document with nothing to fetch —
so this is the smallest server that can hand it over: one document, one address,
the loopback interface, and no way to ask it for anything else. It reads nothing
from disk while it runs and takes no input, which is the only reason a server
belongs in a tool like this at all.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def page_server(page: str, port: int = 0) -> ThreadingHTTPServer:
    """A bound server that answers every GET with `page`, on the loopback address.

    Port 0 lets the operating system pick a free one; read it back from
    `server.server_address[1]`. The caller serves it and closes it.
    """
    body = page.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        """One document, and nothing else — not even a directory listing."""

        server_version = "deltaplan"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
            if self.path not in ("/", "/index.html"):
                self.send_error(404, "deltaplan serves one page")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Quiet: the terminal is showing a plan, not a request log."""

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
