"""Verify forwarding at local alias expansion, with a direct SES control."""

import re
import subprocess
import sys
import time
from email.parser import BytesHeaderParser
from pathlib import Path

from check_queue import (
    CAPTURES, CONFIG, MAIL_LOG, configure, receive_attempt, verify_completed, wait_for_removal,
)
from ses_endpoint import Endpoint, controlled_endpoint
from smtp_sink import smtp_sink

SENDER = "sender@example.test"
REDIRECT = "redirect@example.test"
TARGET = "recipient@outside.test"
OWNER = "owner-redirect@example.test"
AUTHOR = "External Author <author@another.test>"
MAILBOX = Path("/var/mail/forward-bounces")


def submit(sender: str, recipient: str, marker: str) -> None:
    """Inject a queued message with traceable content and independent envelope."""
    content = (f"From: {AUTHOR}\r\nTo: {REDIRECT}\r\nMessage-ID: <{marker}@example.test>\r\n".encode()
               + b"Subject: forward policy\r\n\r\noriginal body\r\n")
    subprocess.run(["sendmail", "-i", "-f", sender, "--", recipient], check=True,
                   input=content.replace(b"\r\n", b"\n"))


def logged(pattern: str) -> re.Match[str]:
    """Wait for postlogd, which can flush a delivery line just after queue removal."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        match = re.search(pattern, MAIL_LOG.read_text())
        if match:
            return match
        time.sleep(0.1)
    raise AssertionError(f"missing Postfix log entry matching {pattern}")


def verify_bounce_owner(owner: str) -> None:
    """Cause a real DSN to the forwarding return path and observe local delivery."""
    failed_recipient = "no-such-recipient@example.test"
    submit(owner, failed_recipient, "owner-dsn-probe")
    original = wait_for_removal("owner-dsn-probe@example.test")
    bounce = logged(rf"{original}: sender non-delivery notification: ([A-Za-z0-9]+)")
    logged(rf"{bounce[1]}: removed")
    report = MAILBOX.read_bytes()
    assert f"Final-Recipient: rfc822; {failed_recipient}".encode() in report, report
    assert b"Action: failed" in report and b"owner-dsn-probe@example.test" in report, report


def verify_redirect(endpoint: Endpoint) -> None:
    """Use the shipped local aliases and prove the two distinct outbound routes."""
    subprocess.run(["useradd", "--system", "--no-create-home", "forward-bounces"], check=True)
    with Path("/etc/aliases").open("ab") as aliases:
        aliases.write(b"\n" + (CONFIG / "forwarding/aliases.fragment").read_bytes())
    subprocess.run(["newaliases"], check=True)

    with smtp_sink() as sink:
        configure(handoff=True, settings=[
            "mydestination=localhost, example.test", "myorigin=example.test",
            "default_transport=smtp", f"relayhost=[127.0.0.1]:{sink.port}",
            "mail_spool_directory=/var/mail", "transport_maps=",
        ])
        submit(SENDER, REDIRECT, "redirect-probe")
        forwarded = sink.messages.get(timeout=15)
        assert (forwarded.sender, forwarded.recipients) == (OWNER, (TARGET,)), forwarded
        headers = BytesHeaderParser().parsebytes(forwarded.raw)
        assert headers.get_all("From") == [AUTHOR] and headers.get_all("To") == [REDIRECT], headers
        assert forwarded.raw.partition(b"\r\n\r\n")[2] == b"original body\r\n", forwarded
        original = wait_for_removal("redirect-probe@example.test")
        child = logged(rf"{original}: to=<{re.escape(REDIRECT)}>, relay=local,.*forwarded as ([A-Za-z0-9]+)")[1]
        assert original != child
        logged(rf"{original}: from=<{re.escape(SENDER)}>")
        logged(rf"{child}: from=<{re.escape(OWNER)}>")
        logged(rf"postfix/smtp\[\d+\]: {child}: to=<{re.escape(TARGET)}>,.*status=sent")
        logged(rf"{child}: removed")
        assert endpoint.requests.empty() and not list(CAPTURES.glob("*/record.json"))
        print(f"{original} => {child}: local redirect -> SMTP; return path {OWNER}")

        # Changing just the envelope recipient bypasses alias expansion: the same
        # original sender and author can reach the identical external target via SES.
        submit(SENDER, TARGET, "direct-probe")
        request, raw, direct = receive_attempt(endpoint, SENDER, TARGET)
        headers = BytesHeaderParser().parsebytes(raw)
        assert headers.get_all("From") == [AUTHOR] and headers["Message-ID"] == "<direct-probe@example.test>", headers
        request.accept()
        verify_completed(direct, TARGET)
        verify_bounce_owner(forwarded.sender)
        assert len(list(CAPTURES.glob("*/record.json"))) == 1
        assert endpoint.requests.empty() and sink.messages.empty()
        print(f"{direct}: direct opt-in -> SES; forwarding DSN delivered to {MAILBOX}")


if __name__ == "__main__":
    try:
        with controlled_endpoint() as peer:
            verify_redirect(peer)
    except Exception:
        subprocess.run(["postqueue", "-p"], check=False)
        if MAIL_LOG.exists():
            print(MAIL_LOG.read_text(), file=sys.stderr)
        raise
