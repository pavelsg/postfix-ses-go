"""Exercise SES rejection, lost acknowledgements and worker loss in a real queue."""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from check_queue import (
    CAPTURES, CONFIG_ERROR, MAIL_LOG, configure, queue_entries, receive_attempt, verify_completed,
)
from ses_endpoint import Endpoint, controlled_endpoint

SENDER = "sender@example.test"
RECIPIENT = "recipient@outside.test"

# The same HTTP 400 and service error code can describe a deployment issue, a
# definitive content failure, or an unfamiliar cause. Only one should bounce.
REJECTIONS = {
    "throttle": (429, "TooManyRequestsException", "Maximum sending rate exceeded",
                 "4.3.0 postfix-ses-go: SES throttled request"),
    "configuration": (400, "MessageRejected",
                      "Email address is not verified. The following identities failed the check"
                      " in region US-EAST-1: sender@example.test", CONFIG_ERROR),
    "permanent": (400, "MessageRejected", "Duplicate header 'Subject'.",
                  "5.6.0 postfix-ses-go: SES rejected message content"),
    "unknown": (400, "MessageRejected", "Unrecognized rejection",
                "4.3.0 postfix-ses-go: SES submission failed"),
}


def verify_process(queue_id: str, code: int, stdout: str, stderr: str, attempts: int) -> None:
    """Verify an actual child result and the exact number of pipe invocations."""
    records = [json.loads(path.read_text()) for path in CAPTURES.glob("*/record.json")]
    assert len(records) == attempts, records
    assert all(row["argv"] == [SENDER, RECIPIENT, queue_id] for row in records), records
    assert sum((row["exit"], row["stdout"], row["stderr"]) == (code, stdout, stderr)
               for row in records) == 1, records


def verify_deferred(queue_id: str, diagnostic: str) -> None:
    """Wait for both the queued recipient's diagnostic and the matching pipe log."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        entry = queue_entries().get(queue_id, {})
        deliveries = [line for line in MAIL_LOG.read_text().splitlines()
                      if f"{queue_id}: to=<{RECIPIENT}>," in line]
        if entry.get("queue_name") == "deferred" and deliveries:
            target, = entry["recipients"]
            assert target["address"] == RECIPIENT and diagnostic in target["delay_reason"], entry
            assert len(deliveries) == 1 and "relay=sespipe," in deliveries[0], deliveries
            assert "status=deferred" in deliveries[0] and diagnostic in deliveries[0], deliveries
            if diagnostic.startswith("postfix-ses-go:"):
                assert "dsn=4.3.0, status=deferred" in deliveries[0], deliveries
            return
        time.sleep(0.1)
    raise AssertionError(f"deferred queue entry or log missing for {queue_id}")


def verify_bounced(queue_id: str, diagnostic: str) -> None:
    """Read Postfix's generated delivery-status notification for the failed recipient."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        entries = queue_entries()
        logs = MAIL_LOG.read_text()
        bounce = re.search(rf"{queue_id}: sender non-delivery notification: ([A-Za-z0-9]+)", logs)
        if bounce and bounce[1] in entries and queue_id not in entries and f"{queue_id}: removed" in logs:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"missing permanent DSN or queue removal for {queue_id}")
    deliveries = [line for line in logs.splitlines() if f"{queue_id}: to=<{RECIPIENT}>," in line]
    assert len(deliveries) == 1 and "relay=sespipe," in deliveries[0], deliveries
    assert "dsn=5.6.0, status=bounced" in deliveries[0] and diagnostic in deliveries[0], deliveries
    assert [r["address"] for r in entries[bounce[1]]["recipients"]] == [SENDER], entries
    body = subprocess.check_output(["postcat", "-bq", bounce[1]], text=True)
    assert f"Final-Recipient: rfc822; {RECIPIENT}" in body, body
    assert "Action: failed" in body and "Status: 5.6.0" in body and diagnostic in body, body
    assert REJECTIONS["permanent"][2] not in body, body


def verify_local_rejection(endpoint: Endpoint) -> None:
    """Prove a malformed queued message bounces before sending any HTTPS request."""
    message = f"From: {SENDER}\nFrom: second@example.test\nTo: {RECIPIENT}\n\nfixture\n"
    subprocess.run(["sendmail", "-i", "-f", SENDER, "--", RECIPIENT], input=message.encode(), check=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        records = list(CAPTURES.glob("*/record.json"))
        if records:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("expected a local rejection of duplicate From headers")
    record, = records
    queue_id = json.loads(record.read_text())["argv"][2]
    diagnostic = "5.6.0 postfix-ses-go: invalid message headers"
    verify_bounced(queue_id, diagnostic.removeprefix("5.6.0 "))
    verify_process(queue_id, 65, "", diagnostic + "\n", 1)
    assert endpoint.requests.empty(), "malformed message reached the SES endpoint"
    print(f"{queue_id}: duplicate From => bounced, requests=0")


def verify_failure(scenario: str, endpoint: Endpoint) -> None:
    """Inject one failure after the peer has received and recorded the full request."""
    subject = "Subject: first\n" + ("Subject: second\n" if scenario == "permanent" else "")
    message = f"From: {SENDER}\nTo: {RECIPIENT}\n{subject}\nfailure fixture\n".encode()
    subprocess.run(["sendmail", "-i", "-f", SENDER, "--", RECIPIENT], input=message, check=True)
    request, raw, queue_id = receive_attempt(endpoint, SENDER, RECIPIENT)
    if scenario in REJECTIONS:
        status, service_error, detail, diagnostic = REJECTIONS[scenario]
        if scenario == "permanent":
            assert b"Subject: first\r\nSubject: second\r\n" in raw, raw
        request.reject(status, service_error, detail)
    elif scenario == "lost-ack":
        # Model SES having accepted the recorded message: no response is sent.
        diagnostic = f"4.3.0 postfix-ses-go: SES acceptance uncertain queue_id={queue_id}"
        request.drop()
    elif scenario == "interrupted":
        pid_path, = CAPTURES.glob("*/pid")
        pid = int(pid_path.read_text())
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        assert argv == [b"/usr/local/libexec/postfix-ses-go.real", SENDER.encode(),
                        RECIPIENT.encode(), queue_id.encode(), b""], argv
        os.kill(pid, signal.SIGKILL)
        request.drop()
        diagnostic = "signal 9"
    else:
        raise AssertionError(f"unknown fault scenario {scenario}")

    if scenario == "permanent":
        verify_bounced(queue_id, diagnostic.removeprefix("5.6.0 "))
        verify_process(queue_id, 65, "", diagnostic + "\n", 1)
    else:
        verify_deferred(queue_id, diagnostic.removeprefix("4.3.0 "))
        if scenario == "interrupted":
            verify_process(queue_id, -signal.SIGKILL, "", "", 1)
        else:
            verify_process(queue_id, 75, "", diagnostic + "\n", 1)
    assert endpoint.requests.empty(), "more than one request during the first pipe invocation"
    if scenario in REJECTIONS:
        assert REJECTIONS[scenario][2] not in MAIL_LOG.read_text(), "provider text leaked to Postfix"

    if scenario in {"throttle", "lost-ack", "interrupted"}:
        if scenario == "lost-ack":
            subprocess.run(["postfix", "stop"], check=True)
            subprocess.run(["postfix", "start"], check=True)
            verify_deferred(queue_id, diagnostic.removeprefix("4.3.0 "))
        # This is the only deliberate retry. The SES request was observed once
        # for the first pipe invocation and will be observed again for this one.
        subprocess.run(["postqueue", "-i", queue_id], check=True)
        retry, retry_raw, retry_id = receive_attempt(endpoint, SENDER, RECIPIENT)
        assert retry_id == queue_id and retry_raw == raw
        retry.accept()
        accepted = verify_completed(queue_id, RECIPIENT)
        verify_process(queue_id, 0, accepted + "\n", "", 2)
        assert endpoint.requests.empty(), "more than one request during the Postfix retry"
        print(f"{queue_id}: {scenario} => deferred, Postfix retry accepted, requests=2")
    else:
        print(f"{queue_id}: {scenario} => {diagnostic}, requests=1")


if __name__ == "__main__":
    try:
        scenario, = sys.argv[1:]
        with controlled_endpoint() as peer:
            configure(handoff=True, hold_bounces=(scenario in {"permanent", "local-content"}))
            if scenario == "local-content":
                verify_local_rejection(peer)
            else:
                verify_failure(scenario, peer)
    except Exception:
        subprocess.run(["postqueue", "-p"], check=False)
        if MAIL_LOG.exists():
            print(MAIL_LOG.read_text(), file=sys.stderr)
        raise
