"""Loopback-deployed offline UI gateway. No Node download needed for prebuilt assets."""

import http.client
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(os.environ.get("STATIC_ROOT", "/srv/static")).resolve()
HOP = {
    "connection",
    "transfer-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
}


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def proxy(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 5 * 1024 * 1024:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"host"}}
        conn = http.client.HTTPConnection(os.environ.get("API_HOST", "api"), 8000, timeout=45)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while data := response.read1(65536):
                self.wfile.write(data)
                self.wfile.flush()
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except OSError:
            self.close_connection = True
        finally:
            conn.close()

    def do_GET(self):
        if self.path.startswith(("/api/", "/health/")):
            self.proxy()
            return
        requested = urlsplit(self.path).path.lstrip("/")
        if requested and not (ROOT / requested).is_file():
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        self.proxy()

    def do_PATCH(self):
        self.proxy()

    def do_DELETE(self):
        self.proxy()

    def do_PUT(self):
        self.proxy()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()
