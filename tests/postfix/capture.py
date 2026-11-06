#!/usr/bin/python3
"""Observe the pipe boundary and relay its real output and exit status to Postfix."""

import json
import os
import signal
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
    # A unique directory keeps simultaneous deliveries independent. Publish the
    # PID before the child can send, and its completed exit record atomically.
    path = Path(tempfile.mkdtemp(dir=CAPTURES))
    (path / "stdin").write_bytes(message)
    with subprocess.Popen([BINARY, *args], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        (path / "pid").write_text(str(child.pid))
        stdout, stderr = child.communicate(message)
    (path / "record.tmp").write_text(json.dumps({
        "argv": args,
        "uid": os.getuid(),
        "exit": child.returncode,
        "stdout": stdout.decode(),
        "stderr": stderr.decode(),
    }))
    (path / "record.tmp").rename(path / "record.json")
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    if child.returncode < 0:
        # pipe(8) must see signal termination, just as it would for a directly
        # invoked binary. Passing a negative return code to exit would wrap it.
        signum = -child.returncode
        sys.stdout.buffer.flush()
        sys.stderr.buffer.flush()
        if signum not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)
    return child.returncode


if __name__ == "__main__":
    sys.exit(main())
