"""Exercise the documented pipe on a stock Postfix queue in a disposable container."""

import base64
import json
import pwd
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ses_endpoint import Endpoint, MESSAGE_ID, controlled_endpoint

CONFIG = Path("/opt/test/config")
POSTFIX = Path("/etc/postfix")
CAPTURES = Path("/run/sespipe-captures")
MAIL_LOG = Path("/var/log/postfix.log")
CONFIG_ERROR = "4.3.0 postfix-ses-go: SES configuration unavailable"
NULL_ERROR = "4.3.0 postfix-ses-go: null sender is not eligible for SES"
BODY = b"\r\n\r\nfirst line\r\n.dot-prefixed line\r\nlast line\r\n"


def configure(*, handoff: bool) -> None:
    """Apply the operator examples and route just the null-sender probe by recipient."""
    for name in ("main.cf", "master.cf"):
        with (POSTFIX / name).open("ab") as dest:
            dest.write(b"\n" + (CONFIG / (name + ".fragment")).read_bytes())
    shutil.copyfile(CONFIG / "ses_senders", POSTFIX / "ses_senders")
    (POSTFIX / "probe_routes").write_text("null-probe@example.test sespipe:\n")
    subprocess.run(["postmap", str(POSTFIX / "probe_routes")], check=True)
    subprocess.run([
        "postconf", "-e",
        "myhostname=mx.example.test", "mydestination=localhost",
        "inet_interfaces=loopback-only", "inet_protocols=ipv4",
        "maillog_file=" + str(MAIL_LOG), "transport_maps=hash:/etc/postfix/probe_routes",
        "default_transport=error:unexpected test route",
    ], check=True)
    if handoff:
        # Allow this fixture to exercise Bcc removal in the binary itself;
        # stock cleanup normally removes it before the pipe receives stdin.
        subprocess.run(["postconf", "-e", "message_drop_headers=content-length, return-path"],
                       check=True)
        names = " AWS_REGION AWS_ENDPOINT_URL_SES AWS_CA_BUNDLE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY"
        for setting in ("import_environment", "export_environment"):
            defaults = subprocess.check_output(["postconf", "-h", setting], text=True).strip()
            subprocess.run(["postconf", "-e", f"{setting}={defaults}{names}"], check=True)
    CAPTURES.mkdir(mode=0o700)
    shutil.chown(CAPTURES, user="sespipe")
    print("Testing stock Postfix", subprocess.check_output(
        ["postconf", "-h", "mail_version"], text=True).strip())
    subprocess.run(["postfix", "start"], check=True)


def verify_delivery(sender: str, recipients: dict[str, str]) -> None:
    """Submit one message and verify all of its deliveries under a single queue ID.

    Args:
        sender: Envelope sender, or an empty string for the null sender.
        recipients: Submitted address mapped to the expected pipe argument.
    """
    message = (b"From: Visible Author <author@example.test>\n"
               b"To: visible@example.test\nSubject: phase one\n"
               + BODY.replace(b"\r\n", b"\n"))
    subprocess.run(["sendmail", "-i", "-f", sender or "<>", "--", *recipients],
                   input=message, check=True)
    diagnostic = CONFIG_ERROR if sender else NULL_ERROR
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        listing = subprocess.check_output(["postqueue", "-j"], text=True)
        # postlogd can append the DSN just after the deferred queue is visible.
        log_lines = MAIL_LOG.read_text().splitlines() if MAIL_LOG.exists() else []
        for line in listing.splitlines():
            entry = json.loads(line)
            # postqueue renders the null sender as MAILER-DAEMON.
            if entry["queue_name"] != "deferred" or entry["sender"] != (sender or "MAILER-DAEMON"):
                continue
            queue_id = entry["queue_id"]
            targets = entry["recipients"]
            # Postfix quotes local parts in the queue listing and logs. The
            # pipe receives the argv forms checked separately below.
            assert sorted(target["address"] for target in targets) == sorted(recipients), entry
            for target in targets:
                assert diagnostic.removeprefix("4.3.0 ") in target["delay_reason"], target
            deliveries = [f"{queue_id}: to=<{rcpt}>, relay=sespipe," for rcpt in recipients]
            if all(any(delivery in log and "dsn=4.3.0, status=deferred" in log for log in log_lines)
                   for delivery in deliveries):
                verify_captures(sender, queue_id, list(recipients.values()))
                return
        time.sleep(0.25)
    raise AssertionError(f"failed to observe deferred deliveries to {list(recipients)}")


def verify_captures(sender: str, queue_id: str, recipients: list[str]) -> None:
    """Check every invocation for this message, including duplicates or batched argv."""
    actual = []
    for record in CAPTURES.glob("*/record.json"):
        observed = json.loads(record.read_text())
        if observed["argv"][-1:] == [queue_id]:
            actual.append(observed["argv"])
            verify_result(record.parent, observed, CONFIG_ERROR if sender else NULL_ERROR)
    expected = [[sender, recipient, queue_id] for recipient in recipients]
    assert sorted(actual) == sorted(expected), (actual, expected)
    for recipient in recipients:
        print(f"{queue_id}: sender={sender or '<>'} recipient={recipient!r} exit=75 queue=deferred")


def verify_result(directory: Path, observed: dict, diagnostic: str) -> None:
    """Check the process result and message bytes for a single pipe invocation."""
    assert observed["uid"] == pwd.getpwnam("sespipe").pw_uid != 0, observed
    assert observed["exit"] == 75, observed
    assert observed["stderr"] == diagnostic + "\n" and observed["stdout"] == "", observed
    wire = (directory / "stdin").read_bytes()
    assert wire.endswith(BODY) and b"\n" not in wire.replace(b"\r\n", b""), wire
    assert b"From: Visible Author <author@example.test>\r\n" in wire, wire


def verify_handoff(endpoint: Endpoint) -> None:
    """Match one blind-copy delivery across the pipe, signed SES request and queue."""
    sender = "sender@example.test"
    recipient = "blind@outside.test"
    visible = (b"From: Visible Author <author@example.test>\r\n"
               b"To: visible@example.test\r\nCc: copy@example.test\r\n")
    # These exact bytes must arrive at the binary and disappear from its send.
    private = (b"Bcc: blind@outside.test\r\n"
               b"X-SES-SOURCE-ARN: untrusted\r\n")
    content = (b"Subject: handoff\r\nMIME-Version: 1.0\r\n"
               b'Content-Type: multipart/mixed; boundary="fixture"\r\n\r\n'
               b"--fixture\r\nContent-Type: text/plain\r\n\r\nhello\r\n"
               b"--fixture\r\nContent-Type: application/octet-stream\r\n"
               b'Content-Disposition: attachment; filename="blob.bin"\r\n'
               b"Content-Transfer-Encoding: base64\r\n\r\nAP8KAA==\r\n--fixture--\r\n")
    subprocess.run(["sendmail", "-i", "-f", sender, "--", recipient], check=True,
                   input=(visible + private + content).replace(b"\r\n", b"\n"))

    request = endpoint.requests.get(timeout=15)
    assert request.path == "/v2/email/outbound-emails", request.path
    assert request.content_type == "application/json", request.content_type
    assert re.fullmatch(r"AWS4-HMAC-SHA256 Credential=TESTKEY/\d{8}/us-east-1/ses/aws4_request, ?"
                        r"SignedHeaders=[^,]+, ?Signature=[0-9a-f]{64}", request.authorization)
    payload = json.loads(request.body)
    assert payload["FromEmailAddress"] == sender, payload
    assert payload["Destination"] == {"ToAddresses": [recipient]}, payload
    raw = base64.b64decode(payload["Content"]["Raw"]["Data"], validate=True)

    # The endpoint has the full request but has not acknowledged acceptance.
    listing = subprocess.check_output(["postqueue", "-j"], text=True).splitlines()
    assert len(listing) == 1, listing
    entry = json.loads(listing[0])
    assert entry["queue_name"] == "active" and entry["sender"] == sender, entry
    assert [r["address"] for r in entry["recipients"]] == [recipient], entry
    queue_id = entry["queue_id"]
    endpoint.acknowledge.set()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        logs = MAIL_LOG.read_text().splitlines()
        if (any(f"{queue_id}: removed" in line for line in logs)
                and not subprocess.check_output(["postqueue", "-j"]).strip()):
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"{queue_id} was not removed after the SES acknowledgement")

    accepted = f"postfix-ses-go: queue_id={queue_id} ses_message_id={MESSAGE_ID}"
    assert any(f"{queue_id}: to=<{recipient}>, relay=sespipe," in line
               and "status=sent" in line and accepted in line for line in logs), logs
    records = list(CAPTURES.glob("*/record.json"))
    assert len(records) == 1, records
    observed = json.loads(records[0].read_text())
    assert observed["argv"] == [sender, recipient, queue_id], observed
    assert observed["uid"] == pwd.getpwnam("sespipe").pw_uid != 0, observed
    assert observed["exit"] == 0 and observed["stderr"] == "", observed
    assert observed["stdout"] == accepted + "\n", observed
    wire = (records[0].parent / "stdin").read_bytes()
    assert visible in wire and private in wire, wire
    assert wire.partition(b"\r\n\r\n")[2] == content.partition(b"\r\n\r\n")[2], wire
    assert raw == wire.replace(private, b"", 1), raw
    assert endpoint.requests.empty(), "more than one SES request for this invocation"
    print(f"{queue_id}: recipient={recipient!r} ses_message_id={MESSAGE_ID} requests=1 queue=removed")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["handoff"]:
            with controlled_endpoint() as endpoint:
                configure(handoff=True)
                verify_handoff(endpoint)
        elif sys.argv[1:] == ["defer"]:
            configure(handoff=False)
            verify_delivery("sender@example.test", {
                "recipient@outside.test": "recipient@outside.test",
                '"a b"@outside.test': "a b@outside.test",
            })
            verify_delivery("", {"null-probe@example.test": "null-probe@example.test"})
        else:
            raise SystemExit("expected scenario: defer or handoff")
    except Exception:
        subprocess.run(["postqueue", "-p"], check=False)
        if MAIL_LOG.exists():
            print(MAIL_LOG.read_text(), file=sys.stderr)
        raise
