"""Offline end-to-end tests of the smoke CLI using a recording delivery executable."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-smoke.py"
SENDER = "operator@example.test"
RECIPIENT = "inbox@example.test"


class SmokeTest(unittest.TestCase):
    """Exercise opt-in, acknowledgement and evidence correlation at CLI boundaries."""

    def setUp(self) -> None:
        """Create a stand-in that records argv/stdin and returns a chosen acknowledgement."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / "postfix-ses-go"
        self.binary.write_text(f"#!{sys.executable}\n" + """
import json
import os
import sys
from pathlib import Path
root = Path(__file__).parent
with (root / 'calls').open('a') as calls:
    print(json.dumps([sys.argv[1:], os.environ['AWS_REGION'], os.environ.get('POSTFIX_SES_SMOKE_DIAGNOSTICS')]), file=calls)
(root / 'raw').write_bytes(sys.stdin.buffer.read())
mode = os.environ.get('FIXTURE', 'accept')
if mode == 'defer':
    print(os.environ['DIAGNOSTIC'].replace('{marker}', sys.argv[3]), file=sys.stderr)
    sys.exit(75)
if mode == 'uncertain':
    print('4.3.0 postfix-ses-go: SES acceptance uncertain', file=sys.stderr)
    sys.exit(75)
if mode == 'reject':
    print('sensitive fixture diagnostic', file=sys.stderr)
    sys.exit(65)
queue_id = 'wrong' if mode == 'wrong-ack' else sys.argv[3]
print(f'postfix-ses-go: queue_id={queue_id} ses_message_id=example-001')
""")
        self.binary.chmod(0o700)
        self.send = ["send", "--binary", str(self.binary), "--sender", SENDER,
                     "--recipient", RECIPIENT, "--region", "us-east-1"]
        self.verify = ["verify", "--receipt", str(self.root / "receipt"),
                       "--message", str(self.root / "inbox"), "--event", str(self.root / "event")]

    def invoke(self, args: list, **environment: str) -> subprocess.CompletedProcess:
        """Call the actual CLI with controlled settings, without using AWS credentials."""
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              env={"PATH": os.defpath, **environment})

    def test_requires_explicit_authorization_and_valid_target(self) -> None:
        """An inert default, missing flag, custom endpoint or malformed address cannot send."""
        for args, env in [([], {}), (self.send, {}), (self.send + ["--send-li"], {}),
                          (self.send + ["--send-live"], {"AWS_ENDPOINT_URL_SES": "https://localhost"}),
                          (self.send + ["--send-live", "--recipient", "a@test,b@test"], {}),
                          (self.send + ["--send-live", "--recipient", "a@"], {}),
                          (self.send + ["--send-live", "--recipient", ""], {}),
                          (self.send + ["--send-live", "--recipient", "a@b..test"], {}),
                          (self.send + ["--send-live", "--recipient", "f@a/b"], {}),
                          (self.send + ["--send-live", "--sender", "s@x\nBcc: p@x"], {}),
                          (self.send + ["--send-live", "--region", "  "], {})]:
            with self.subTest(args=args, env=env):
                result = self.invoke(args, **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse((self.root / "calls").exists())

    def test_send_and_verify(self) -> None:
        """A single raw message, its API receipt and matching event verify together."""
        result = self.invoke(self.send + ["--send-live"])
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "accepted")
        self.assertRegex(receipt["marker"], r"^[0-9a-f]{32}$")
        self.assertEqual(receipt["ses_message_id"], "example-001")
        calls = (self.root / "calls").read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]), [[SENDER, RECIPIENT, receipt["marker"]], "us-east-1", "1"])
        raw = (self.root / "raw").read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        inbox = BytesParser(policy=policy.SMTP).parsebytes(raw)
        self.assertEqual(inbox.get_all("X-Postfix-SES-Smoke"), [receipt["marker"]])
        self.assertIn(receipt["marker"], inbox.get_content())
        inbox["Message-ID"] = "<example-001@email.amazonses.com>"
        event = {"eventType": "Delivery",
                 "mail": {"messageId": "example-001", "destination": [RECIPIENT],
                          "tags": {"ses:configuration-set": ["test-tracking"]}},
                 "delivery": {"recipients": [RECIPIENT]}}
        (self.root / "receipt").write_text(result.stdout)
        (self.root / "inbox").write_bytes(inbox.as_bytes())
        (self.root / "event").write_text(json.dumps(event))
        result = self.invoke(self.verify + ["--configuration-set", "test-tracking"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "verified")

        bad_events = [dict(event, eventType="Send"), dict(event, delivery={"recipients": ["other@test"]}),
                      dict(event, mail=dict(event["mail"], messageId="different")), {},
                      dict(event, mail={"messageId": "example-001", "destination": [SENDER, RECIPIENT]})]
        for bad in bad_events:
            with self.subTest(bad=bad):
                (self.root / "event").write_text(json.dumps(bad))
                self.assertNotEqual(self.invoke(self.verify).returncode, 0)
        (self.root / "event").write_text(json.dumps(event))
        self.assertNotEqual(self.invoke(self.verify + ["--configuration-set", "other"]).returncode, 0)
        (self.root / "inbox").write_bytes(raw)
        self.assertNotEqual(self.invoke(self.verify).returncode, 0)
        inbox.replace_header("Message-ID", "<unrelated@email.amazonses.com>")
        (self.root / "inbox").write_bytes(inbox.as_bytes())
        self.assertNotEqual(self.invoke(self.verify).returncode, 0)
        inbox.replace_header("Message-ID", "<example-001@email.amazonses.com>")
        inbox.replace_header("X-Postfix-SES-Smoke", "different")
        (self.root / "inbox").write_bytes(inbox.as_bytes())
        self.assertNotEqual(self.invoke(self.verify).returncode, 0)
        self.assertEqual((self.root / "calls").read_text().splitlines(), calls)

    def test_exit_75_reports_only_recognized_diagnostics(self) -> None:
        """Keep the binary's safe deferral reason and suppress unexpected child output."""
        prefix = "4.3.0 postfix-ses-go: "
        configuration = prefix + "SES configuration unavailable"
        uncertain = prefix + "SES acceptance uncertain queue_id={marker}"
        detail = "\npostfix-ses-go-smoke: "
        service = detail + "stage=ses http_status=403 aws_code=AccessDeniedException request_id="
        request_id = "01234567-89ab-cdef-0123-456789abcdef"
        cases = [
            (configuration, True),
            (prefix + "SES throttled request", True),
            (prefix + "SES submission failed", True),
            (prefix + "unsupported envelope address", True),
            (uncertain, True),
            (uncertain + detail + "stage=transport", True),
            (configuration + detail + "stage=setup", True),
            (configuration + service + request_id, True),
            (configuration + detail + "stage=ses http_status=503 aws_code=Unknown request_id=unknown", True),
            (prefix + "sensitive fixture diagnostic", False),
            (prefix + "SES configuration unavailable\nsensitive fixture diagnostic", False),
            (prefix + "SES acceptance uncertain queue_id=sensitive fixture diagnostic", False),
            (prefix + "unknown reason" + service + request_id, False),
            (configuration + service + "sensitive fixture diagnostic", False),
            (configuration + service + request_id + "\ninjected", False),
            (configuration + service.replace("403", "600") + request_id, False),
            (configuration + service.replace("AccessDeniedException", "PrivateToken") + request_id, False),
            (configuration + detail + "stage=setup\r", False),
            (configuration + detail + "stage=setup\n", False),
        ]
        for attempt, (diagnostic, recognized) in enumerate(cases, 1):
            with self.subTest(diagnostic=diagnostic):
                result = self.invoke(self.send + ["--send-live"], FIXTURE="defer", DIAGNOSTIC=diagnostic)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                calls = (self.root / "calls").read_text().splitlines()
                self.assertEqual(len(calls), attempt)
                if recognized:
                    marker = json.loads(calls[-1])[0][-1]
                    self.assertIn("exited 75: " + diagnostic.replace("{marker}", marker), result.stderr)
                    if "{marker}" in diagnostic:
                        self.assertIn("Inspect mailbox and events before resending", result.stderr)
                else:
                    self.assertNotIn("sensitive fixture diagnostic", result.stderr)
                    self.assertNotIn(prefix, result.stderr)
                    self.assertNotIn("postfix-ses-go-smoke:", result.stderr)

    def test_failure_or_lost_ack_never_retried(self) -> None:
        """Failed and ambiguous sends produce no receipt, and leak no provider diagnostic."""
        markers = set()
        for attempt, mode in enumerate(["reject", "uncertain", "wrong-ack"], 1):
            with self.subTest(mode=mode):
                result = self.invoke(self.send + ["--send-live"], FIXTURE=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("sensitive fixture diagnostic", result.stderr)
                if mode == "uncertain":
                    self.assertIn("acceptance uncertain", result.stderr)
                calls = (self.root / "calls").read_text().splitlines()
                self.assertEqual(len(calls), attempt)
                markers.add(json.loads(calls[-1])[0][-1])
        self.assertEqual(len(markers), 3)


if __name__ == "__main__":
    unittest.main()
