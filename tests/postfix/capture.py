#!/usr/bin/python3
"""Observe the pipe boundary and relay its real output and exit status to Postfix."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CAPTURES = Path("/run/sespipe-captures")
BINARY = "/usr/local/libexec/postfix-ses-go.real"


def main() -> int:
    """Record one synthetic delivery, publishing its metadata after the binary exits."""
    args = sys.argv[1:]
    message = sys.stdin.buffer.read()
    result = subprocess.run([BINARY, *args], input=message, capture_output=True)
    # A unique directory keeps simultaneous deliveries independent. Publish the
    # metadata last so the harness cannot see a partially written capture.
    path = Path(tempfile.mkdtemp(dir=CAPTURES))
    (path / "stdin").write_bytes(message)
    (path / "record.tmp").write_text(json.dumps({
        "argv": args,
        "uid": os.getuid(),
        "exit": result.returncode,
        "stdout": result.stdout.decode(),
        "stderr": result.stderr.decode(),
    }))
    (path / "record.tmp").rename(path / "record.json")
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
