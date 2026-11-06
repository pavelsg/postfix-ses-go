"""Send one operator-authorized SES smoke message, or verify saved delivery evidence."""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from email import policy
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import formatdate
from pathlib import Path

SES_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,255}")
REGION = re.compile(r"[a-z]+(?:-[a-z0-9]+)+-[0-9]+")
MARKER = re.compile(r"[a-f0-9]{32}")
DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def mailbox(value: str) -> str:
    """Require one plain ASCII addr-spec suitable for a synthetic pipe invocation."""
    try:
        addr = Address(addr_spec=value)
    # Python 3.9's header parser can raise IndexError for an incomplete addr-spec.
    except (ValueError, HeaderParseError, IndexError):
        addr = None
    if (not value.isascii() or not value.isprintable() or '"' in value or len(value) > 254
            or not addr or not 1 <= len(addr.username) <= 64 or addr.addr_spec != value
            or not all(DNS_LABEL.fullmatch(label) for label in addr.domain.split("."))):
        raise ValueError("use a single plain ASCII mailbox address")
    return value


def send(binary: Path, region: str, sender: str, recipient: str) -> dict:
    """Invoke the binary once and return a receipt only on a confirmed acknowledgement."""
    if not REGION.fullmatch(region):
        raise ValueError("invalid AWS region")
    if os.environ.get("AWS_ENDPOINT_URL_SES"):
        raise ValueError("unset AWS_ENDPOINT_URL_SES before running the live smoke")
    sender, recipient = mailbox(sender), mailbox(recipient)
    marker = secrets.token_hex(16)
    message = EmailMessage(policy=policy.SMTP)
    message["Date"] = formatdate(usegmt=True)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"postfix-ses-go smoke {marker}"
    message["X-Postfix-SES-Smoke"] = marker
    message.set_content(f"Postfix SES smoke message {marker}.")
    print(f"Sending one smoke message with marker {marker}", file=sys.stderr, flush=True)
    result = subprocess.run([str(binary.resolve()), sender, recipient, marker],
                            env={**os.environ, "AWS_REGION": region},
                            input=message.as_bytes(), capture_output=True, timeout=30)
    if result.returncode:
        if result.returncode < 0 or b"acceptance uncertain" in result.stderr:
            raise ValueError("SES acceptance uncertain. Inspect mailbox and events before resending")
        raise ValueError(f"delivery agent exited {result.returncode}. No acceptance confirmed")
    expected = f"postfix-ses-go: queue_id={marker} ses_message_id="
    output = result.stdout.decode("ascii", errors="replace")
    message_id = output.removeprefix(expected).removesuffix("\n")
    if not output.startswith(expected) or not SES_ID.fullmatch(message_id):
        raise ValueError("Unrecognized acknowledgement. Inspect mailbox and events before resending")
    return {"status": "accepted", "marker": marker, "ses_message_id": message_id,
            "region": region, "sender": sender, "recipient": recipient}


def read_object(path: Path) -> dict:
    """Read a single JSON object from operator-supplied local evidence."""
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def verify(receipt_path: Path, message_path: Path, event_path: Path, config_set: str) -> dict:
    """Correlate the mailbox export and a published SES Delivery event to one send."""
    receipt = read_object(receipt_path)
    marker, message_id = receipt.get("marker"), receipt.get("ses_message_id")
    if (receipt.get("status") != "accepted" or not isinstance(marker, str)
            or not MARKER.fullmatch(marker) or not isinstance(message_id, str)
            or not SES_ID.fullmatch(message_id)):
        raise ValueError("invalid acceptance receipt")
    sender, recipient = receipt.get("sender"), receipt.get("recipient")
    if not isinstance(sender, str) or not isinstance(recipient, str):
        raise ValueError("invalid receipt addresses")
    mailbox(sender)
    mailbox(recipient)
    with message_path.open("rb") as source:
        headers = BytesHeaderParser(policy=policy.default).parse(source)
    subject = f"postfix-ses-go smoke {marker}"
    if (headers.get_all("X-Postfix-SES-Smoke") != [marker] or headers.get_all("Subject") != [subject]
            or headers.get_all("To") != [recipient] or headers.get_all("From") != [sender]):
        raise ValueError("mailbox message does not match the smoke marker, sender and recipient")
    # SES stamps a Message-ID on delivery. Correlate its opaque ID as well as
    # the synthetic marker, so saving the outgoing fixture cannot prove receipt.
    ids = headers.get_all("Message-ID", [])
    if len(ids) != 1 or not re.fullmatch(r"<" + re.escape(message_id) + r"(?:@[A-Za-z0-9.-]+)?>", ids[0]):
        raise ValueError("mailbox Message-ID does not match the SES acceptance receipt")

    event = read_object(event_path)
    mail, delivery = event.get("mail"), event.get("delivery")
    if (event.get("eventType") != "Delivery" or not isinstance(mail, dict)
            or not isinstance(delivery, dict) or mail.get("messageId") != message_id
            or mail.get("destination") != [recipient] or delivery.get("recipients") != [recipient]):
        raise ValueError("expected an SES Delivery event for the accepted message ID and recipient")
    if config_set and (not isinstance(mail.get("tags"), dict)
                       or mail["tags"].get("ses:configuration-set") != [config_set]):
        raise ValueError("SES Delivery event did not match the requested configuration set")
    return {"status": "verified", "marker": marker, "ses_message_id": message_id}


def main() -> int:
    """Dispatch the explicit live send or the local verification command."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    live = commands.add_parser("send", help="send once through a packaged binary", allow_abbrev=False)
    live.add_argument("--send-live", required=True, action="store_true", help="authorize one real email")
    live.add_argument("--binary", required=True, type=Path)
    live.add_argument("--sender", required=True, help="verified operator-controlled mailbox")
    live.add_argument("--recipient", required=True, help="operator-controlled receiving mailbox")
    live.add_argument("--region", required=True, help="AWS region of the verified identity")
    offline = commands.add_parser("verify", help="compare files without network access")
    offline.add_argument("--receipt", required=True, type=Path, help="JSON printed by send")
    offline.add_argument("--message", required=True, type=Path, help="raw .eml from the designated mailbox")
    offline.add_argument("--event", required=True, type=Path, help="SES Delivery event JSON (inner SNS Message)")
    offline.add_argument("--configuration-set", default="", help="expected SES configuration-set tag")
    args = parser.parse_args()
    try:
        if args.action == "send":
            result = send(args.binary, args.region, args.sender, args.recipient)
        else:
            result = verify(args.receipt, args.message, args.event, args.configuration_set)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        print("Interrupted. A send in progress may have been accepted. Inspect mailbox and events.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Smoke check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
