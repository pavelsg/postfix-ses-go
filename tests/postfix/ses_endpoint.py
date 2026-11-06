"""A loopback-only SES peer with one test-selected outcome per complete POST."""

import contextlib
import json
import os
import queue
import ssl
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator

MESSAGE_ID = "ses-test-accepted-001"


@dataclass
class Response:
    """An SES JSON response and optional Smithy error type."""

    status: int
    body: dict[str, str]
    error_code: str = ""


@dataclass
class Request:
    """A captured POST awaiting the outcome selected by the queue test."""

    path: str
    content_type: str
    authorization: str
    body: bytes
    response: queue.Queue[Response | None] = field(default_factory=lambda: queue.Queue(maxsize=1),
                                                   repr=False)

    def accept(self) -> None:
        """Acknowledge this handoff with the fixture message ID."""
        self.response.put_nowait(Response(200, {"MessageId": MESSAGE_ID}))

    def reject(self, status: int, code: str, message: str) -> None:
        """Reject this handoff using an SES HTTP status, error code and message."""
        self.response.put_nowait(Response(status, {"message": message}, code))

    def drop(self) -> None:
        """Close the TLS connection after receiving the full POST, before a reply."""
        self.response.put_nowait(None)


class Endpoint(HTTPServer):
    """Expose complete requests to the queue test on loopback."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: queue.Queue[Request] = queue.Queue()


class Handler(BaseHTTPRequestHandler):
    """Accept SES v2 HTTP POST requests from the isolated Postfix container."""

    server: Endpoint

    def do_POST(self) -> None:
        """Read the complete POST, then wait for this request's scripted outcome."""
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            return
        request = Request(self.path, self.headers.get_content_type(),
                          self.headers.get("Authorization", ""), body)
        self.server.requests.put(request)
        try:
            response = request.response.get(timeout=10)
        except queue.Empty:
            self.send_error(504, "test did not supply an SES outcome")
            return
        if response is None:
            self.close_connection = True
            return
        data = json.dumps(response.body).encode()
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if response.error_code:
            self.send_header("x-amzn-errortype", response.error_code)
        self.end_headers()
        self.wfile.write(data)


@contextlib.contextmanager
def controlled_endpoint() -> Iterator[Endpoint]:
    """Run a locally trusted HTTPS endpoint with disposable signing credentials."""
    cert = Path("/run/ses-test-cert.pem")
    key = Path("/run/ses-test-key.pem")
    subprocess.run([
        "openssl", "req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=SES queue test", "-addext", "subjectAltName=IP:127.0.0.1",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    with Endpoint() as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        os.environ.update({
            "AWS_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "TESTKEY",
            "AWS_SECRET_ACCESS_KEY": "local-test-only",
            "AWS_CA_BUNDLE": str(cert),
            "AWS_ENDPOINT_URL_SES": f"https://127.0.0.1:{server.server_port}",
        })
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()
