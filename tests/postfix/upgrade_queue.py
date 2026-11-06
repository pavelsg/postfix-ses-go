"""Prove queued delivery across binary upgrade and rollback on the same mail host."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from check_queue import MAIL_LOG, configure, receive_attempt, verify_completed
from failure_queue import RECIPIENT, SENDER, verify_deferred
from ses_endpoint import Endpoint, controlled_endpoint

INSTALLED = Path("/usr/local/libexec/postfix-ses-go")
BASELINE = Path("/opt/test/baseline")
CANDIDATE = Path("/opt/test/candidate")
THROTTLED = "postfix-ses-go: SES throttled request"


def digest(path: Path) -> str:
    """Return the SHA-256 digest of the executable on disk."""
    with path.open("rb") as executable:
        return hashlib.file_digest(executable, "sha256").hexdigest()


def version(path: Path) -> str:
    """Read the version reported by an extracted or installed Linux executable."""
    return subprocess.check_output([path, "--version"], text=True).strip()


def install(source: Path) -> None:
    """Stage and verify a packaged binary beside the live path, then replace it."""
    with tempfile.NamedTemporaryFile(dir=INSTALLED.parent, delete=False) as stage:
        staged = Path(stage.name)
    try:
        shutil.copyfile(source, staged)
        staged.chmod(0o755)
        assert digest(staged) == digest(source) and version(staged) == version(source)
        os.replace(staged, INSTALLED)
        assert digest(INSTALLED) == digest(source) and version(INSTALLED) == version(source)
    finally:
        staged.unlink(missing_ok=True)


def verify_transition(peer: Endpoint, current: Path, replacement: Path, marker: str) -> None:
    """Defer under the current release, swap in place, then flush only that queue ID."""
    assert digest(INSTALLED) == digest(current)
    message = (f"From: {SENDER}\nTo: {RECIPIENT}\nMessage-ID: <{marker}@example.test>\n"
               f"Subject: {marker}\n\nQueued before {marker}.\n").encode()
    subprocess.run(["sendmail", "-i", "-f", SENDER, "--", RECIPIENT], input=message, check=True)
    first, raw, queue_id = receive_attempt(peer, SENDER, RECIPIENT)
    assert f"Message-ID: <{marker}@example.test>\r\n".encode() in raw, raw
    first.reject(429, "TooManyRequestsException", "Maximum sending rate exceeded")
    verify_deferred(queue_id, THROTTLED)
    assert peer.requests.empty(), "unexpected automatic resend after deferral"

    install(replacement)
    verify_deferred(queue_id, THROTTLED)
    subprocess.run(["postqueue", "-i", queue_id], check=True)
    retry, retry_raw, retry_id = receive_attempt(peer, SENDER, RECIPIENT)
    assert retry_id == queue_id and retry_raw == raw
    retry.accept()
    verify_completed(queue_id, RECIPIENT)
    deliveries = [line for line in MAIL_LOG.read_text().splitlines()
                  if f"{queue_id}: to=<{RECIPIENT}>," in line]
    assert len(deliveries) == 2 and all("relay=sespipe," in line for line in deliveries), deliveries
    assert "status=deferred" in deliveries[0] and "status=sent" in deliveries[1], deliveries
    assert peer.requests.empty(), "unexpected extra request after acceptance"
    print(f"{marker}: {version(current)} -> {version(INSTALLED)}, {queue_id} deferred then sent")


if __name__ == "__main__":
    try:
        assert shutil.which("go") is None and shutil.which("aws") is None
        assert digest(BASELINE) != digest(CANDIDATE) and version(BASELINE) != version(CANDIDATE)
        install(BASELINE)
        with controlled_endpoint() as endpoint:
            configure(handoff=True)
            verify_transition(endpoint, BASELINE, CANDIDATE, "upgrade")
            verify_transition(endpoint, CANDIDATE, BASELINE, "rollback")
    except Exception:
        if MAIL_LOG.exists():
            print(MAIL_LOG.read_text(), file=sys.stderr)
        raise
