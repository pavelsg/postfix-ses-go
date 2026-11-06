"""Exercise the documented pipe on a stock Postfix queue in a disposable container."""

import json
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path("/opt/test/config")
POSTFIX = Path("/etc/postfix")
CAPTURES = Path("/run/sespipe-captures")
MAIL_LOG = Path("/var/log/postfix.log")
DIAGNOSTIC = "4.3.0 postfix-ses-go: delivery is not implemented"
BODY = b"\r\n\r\nfirst line\r\n.dot-prefixed line\r\nlast line\r\n"


def configure() -> None:
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
                assert "postfix-ses-go: delivery is not implemented" in target["delay_reason"], target
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
            verify_result(record.parent, observed)
    expected = [[sender, recipient, queue_id] for recipient in recipients]
    assert sorted(actual) == sorted(expected), (actual, expected)
    for recipient in recipients:
        print(f"{queue_id}: sender={sender or '<>'} recipient={recipient!r} exit=75 queue=deferred")


def verify_result(directory: Path, observed: dict) -> None:
    """Check the process result and message bytes for a single pipe invocation."""
    assert observed["uid"] == pwd.getpwnam("sespipe").pw_uid != 0, observed
    assert observed["exit"] == 75, observed
    assert observed["stderr"] == DIAGNOSTIC + "\n" and observed["stdout"] == "", observed
    wire = (directory / "stdin").read_bytes()
    assert wire.endswith(BODY) and b"\n" not in wire.replace(b"\r\n", b""), wire
    assert b"From: Visible Author <author@example.test>\r\n" in wire, wire


if __name__ == "__main__":
    try:
        configure()
        verify_delivery("sender@example.test", {
            "recipient@outside.test": "recipient@outside.test",
            '"a b"@outside.test': "a b@outside.test",
        })
        verify_delivery("", {"null-probe@example.test": "null-probe@example.test"})
    except Exception:
        subprocess.run(["postqueue", "-p"], check=False)
        if MAIL_LOG.exists():
            print(MAIL_LOG.read_text(), file=sys.stderr)
        raise
