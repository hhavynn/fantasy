"""An in-process stand-in for R2's S3 API (GET/PUT/DELETE object, ListObjectsV2) for tests."""

from __future__ import annotations

import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.db.r2 import R2Config


class _Server(ThreadingHTTPServer):
    def server_bind(self):   # skip HTTPServer's getfqdn(): a reverse DNS lookup that can stall for ~30 s on macOS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FakeR2:
    def __init__(self, bucket: str = "fantasy-intel"):
        self.bucket, self.objects, self.requests = bucket, {}, []
        self.fail_next: dict[str, int] = {}    # method -> HTTP status to return once
        self.truncate: set[str] = set()         # keys whose body is cut off mid-stream (IncompleteRead)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _key(self):
                parts = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(parts.path)
                assert path.startswith(f"/{fake.bucket}"), path
                return path[len(fake.bucket) + 2:], urllib.parse.parse_qs(parts.query)

            def _record(self):
                key, query = self._key()
                fake.requests.append((self.command, key, self.headers.get("Authorization", "")))
                status = fake.fail_next.pop(self.command, None)
                if status:
                    self._send(status, b"<Error><Code>InjectedFailure</Code></Error>")
                    return None, None
                return key, query

            def _send(self, status, body=b"", ctype="application/xml"):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                key, query = self._record()
                if key is None:
                    return
                if not key:   # ListObjectsV2
                    prefix = query.get("prefix", [""])[0]
                    items = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in sorted(fake.objects) if k.startswith(prefix))
                    self._send(200, f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{items}'
                                    f'<IsTruncated>false</IsTruncated></ListBucketResult>'.encode())
                elif key in fake.truncate and key in fake.objects:
                    body = fake.objects[key]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body[: len(body) // 2])   # then the connection closes short of it
                elif key in fake.objects:
                    self._send(200, fake.objects[key], "application/octet-stream")
                else:
                    self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")

            def do_PUT(self):
                key, _ = self._record()
                if key is None:
                    return
                fake.objects[key] = self.rfile.read(int(self.headers["Content-Length"]))
                self._send(200)

            def do_DELETE(self):
                key, _ = self._record()
                if key is None:
                    return
                fake.objects.pop(key, None)
                self._send(204)

        self.server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.config = R2Config("acct", "AKIDTEST", "secret-test-key", bucket=bucket,
                               endpoint=f"http://127.0.0.1:{self.server.server_port}")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
