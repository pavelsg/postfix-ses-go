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


def verify_delivery(sender: str, recipient: str) -> None:
    """Submit a fixture and match Postfix's deferred recipient to the pipe capture."""
    message = (b"From: Visible Author <author@example.test>\n"
               b"To: visible@example.test\nSubject: phase one\n"
               + BODY.replace(b"\r\n", b"\n"))
    subprocess.run(["sendmail", "-i", "-f", sender or "<>", "--", recipient],
                   input=message, check=True)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        listing = subprocess.check_output(["postqueue", "-j"], text=True)
        # postlogd can append the DSN just after the deferred queue is visible.
        log_lines = MAIL_LOG.read_text().splitlines() if MAIL_LOG.exists() else []
        for line in listing.splitlines():
            entry = json.loads(line)
            if entry["queue_name"] != "deferred":
                continue
            delivery = f"{entry['queue_id']}: to=<{recipient}>, relay=sespipe,"
            if not any(delivery in log and "dsn=4.3.0, status=deferred" in log
                       for log in log_lines):
                continue
            for target in entry["recipients"]:
                if target["address"] == recipient:
                    verify_capture(sender, recipient, entry, target)
                    return
        time.sleep(0.25)
    raise AssertionError(f"recipient {recipient} did not enter the deferred queue")


def verify_capture(sender: str, recipient: str, entry: dict, target: dict) -> None:
    """Check the same queue ID in argv, the queue record, and the Postfix DSN log."""
    queue_id = entry["queue_id"]
    # postqueue renders the null sender as MAILER-DAEMON even though the
    # master.cf null_sender= setting must pass a zero-length argv element.
    assert entry["sender"] == (sender or "MAILER-DAEMON"), entry
    assert len(entry["recipients"]) == 1, entry
    assert "postfix-ses-go: delivery is not implemented" in target["delay_reason"], target
    matching = []
    for record in CAPTURES.glob("*/record.json"):
        observed = json.loads(record.read_text())
        if observed["argv"] == [sender, recipient, queue_id]:
            matching.append((record.parent, observed))
    assert len(matching) == 1, f"expected one capture for {queue_id}, got {matching}"
    directory, observed = matching[0]
    assert observed["uid"] == pwd.getpwnam("sespipe").pw_uid != 0, observed
    assert observed["exit"] == 75, observed
    assert observed["stderr"] == DIAGNOSTIC + "\n" and observed["stdout"] == "", observed
    wire = (directory / "stdin").read_bytes()
    assert wire.endswith(BODY) and b"\n" not in wire.replace(b"\r\n", b""), wire
    assert b"From: Visible Author <author@example.test>\r\n" in wire, wire
    print(f"{queue_id}: sender={sender or '<>'} recipient={recipient} exit=75 queue=deferred")


if __name__ == "__main__":
    try:
        configure()
        verify_delivery("sender@example.test", "recipient@example.test")
        verify_delivery("", "null-probe@example.test")
    except Exception:
        subprocess.run(["postqueue", "-p"], check=False)
        if MAIL_LOG.exists():
            print(MAIL_LOG.read_text(), file=sys.stderr)
        raise
