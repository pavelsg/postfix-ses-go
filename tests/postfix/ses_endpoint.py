"""A loopback-only SES peer whose acknowledgement the queue test can delay."""

import contextlib
import json
import os
import queue
import ssl
import subprocess
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator

MESSAGE_ID = "ses-test-accepted-001"


@dataclass
class Request:
    """The request line, authentication header and exact bytes received over TLS."""

    path: str
    content_type: str
    authorization: str
    body: bytes


class Endpoint(HTTPServer):
    """Record sends and wait for the test to release the SES acknowledgement."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: queue.Queue[Request] = queue.Queue()
        self.acknowledge = threading.Event()


class Handler(BaseHTTPRequestHandler):
    """Accept SES v2 HTTP POST requests from the isolated Postfix container."""

    server: Endpoint

    def do_POST(self) -> None:
        """Record the request before returning the controlled SES message ID."""
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.put(Request(self.path, self.headers.get_content_type(),
                                         self.headers.get("Authorization", ""), body))
        if not self.server.acknowledge.wait(timeout=10):
            self.send_error(504, "test did not release acknowledgement")
            return
        response = json.dumps({"MessageId": MESSAGE_ID}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


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
            server.acknowledge.set()
            server.shutdown()
            thread.join()
