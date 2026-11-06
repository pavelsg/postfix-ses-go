"""Exercise sender opt-in and local delivery on the same stock Postfix instance."""

import json
import subprocess
import sys
from pathlib import Path

from check_queue import CAPTURES, CONFIG, MAIL_LOG, configure, decode_ses_request, wait_for_removal
from ses_endpoint import MESSAGE_ID, Endpoint, controlled_endpoint
from smtp_sink import Sink, smtp_sink

EXTERNAL = "peer@outside.test"
MAILDIR = Path("/home/sender/Maildir/new")


def configure_host(smtp_port: int) -> None:
    """Provide the site's mailboxes, local aliases, and ordinary outbound SMTP relay."""
    for user in ("sender", "unlisted"):
        subprocess.run(["useradd", "--create-home", user], check=True)
    with Path("/etc/aliases").open("ab") as aliases:
        aliases.write(b"\n" + (CONFIG / "mailbox-aliases.fragment").read_bytes())
    subprocess.run(["newaliases"], check=True)
    configure(handoff=True, settings=[
        "mydestination=$myhostname, localhost, example.test",
        "myorigin=example.test",
        "home_mailbox=Maildir/",
        "recipient_delimiter=+",
        "transport_maps=",
        "default_transport=smtp",
        f"relayhost=[127.0.0.1]:{smtp_port}",
    ])


def verify_route(peer: Endpoint, smtp: Sink, sender: str, *, via_ses: bool, local: str = "") -> None:
    """Submit from a mailbox identity, then check every delivery under its queue ID.

    Args:
        peer: Isolated SES endpoint.
        smtp: Loopback relay for the site's default outbound transport.
        sender: Local part of the envelope sender at example.test.
        via_ses: Whether the operator example opts this sender into SES.
        local: Optional local part of a hosted recipient sharing the submission.
    """
    envelope = f"{sender}@example.test"
    recipients = [EXTERNAL]
    if local:
        recipients.append(f"{local}@example.test")
    message_id = f"route-{sender}@fixture.test"
    marker = f"Message-ID: <{message_id}>"
    # Keep the visible, opted-in From identical even for unlisted submissions.
    message = (f"From: Mailbox <sender@example.test>\nTo: {EXTERNAL}\n{marker}\n"
               f"Subject: routing fixture\n\n{message_id}\n").encode()
    before_pipe = set(CAPTURES.glob("*/record.json"))
    before_mail = set(MAILDIR.glob("*"))
    # Both aliases and the plus address are submitted by the same OS mailbox user.
    user = "unlisted" if sender == "unlisted" else "sender"
    subprocess.run(["runuser", "-u", user, "--", "/usr/sbin/sendmail", "-i", "-f", envelope,
                    "--", *recipients], input=message, check=True)

    if via_ses:
        request = peer.requests.get(timeout=15)
        raw = decode_ses_request(request, envelope, EXTERNAL)
        request.accept()
    else:
        delivery = smtp.messages.get(timeout=15)
        assert delivery.sender == envelope and delivery.recipients == (EXTERNAL,), delivery
        raw = delivery.raw
    assert marker.encode() + b"\r\n" in raw, raw

    queue_id = wait_for_removal(message_id)
    lines = [line for line in MAIL_LOG.read_text().splitlines() if f"{queue_id}: to=<" in line]
    assert len(lines) == len(recipients) and all("status=sent" in line for line in lines), lines
    transport = "pipe" if via_ses else "smtp"
    assert any(f"/{transport}[" in line and f": to=<{EXTERNAL}>," in line for line in lines), lines

    records = set(CAPTURES.glob("*/record.json")) - before_pipe
    if via_ses:
        record, = records
        result = json.loads(record.read_text())
        assert result["argv"] == [envelope, EXTERNAL, queue_id] and result["exit"] == 0, result
        assert f"queue_id={queue_id} ses_message_id={MESSAGE_ID}" in result["stdout"], result
    else:
        assert not records, records

    delivered = set(MAILDIR.glob("*")) - before_mail
    if local:
        item, = delivered
        assert marker.encode() + b"\n" in item.read_bytes(), item
        assert any("/local[" in line and "relay=local," in line for line in lines), lines
    else:
        assert not delivered, delivered
    assert smtp.messages.empty() and peer.requests.empty(), f"{queue_id}: unexpected extra delivery"
    print(f"{queue_id}: {envelope} via {transport}; local deliveries={len(delivered)}")


if __name__ == "__main__":
    try:
        with controlled_endpoint() as ses, smtp_sink() as smtp:
            configure_host(smtp.port)
            verify_route(ses, smtp, "sender", via_ses=True, local="sender")
            verify_route(ses, smtp, "billing", via_ses=True, local="billing")
            verify_route(ses, smtp, "support", via_ses=False, local="support")
            verify_route(ses, smtp, "unlisted", via_ses=False)
            verify_route(ses, smtp, "sender+tag", via_ses=False)
    except Exception:
        subprocess.run(["postqueue", "-p"], check=False)
        if MAIL_LOG.exists():
            print(MAIL_LOG.read_text(), file=sys.stderr)
        raise
