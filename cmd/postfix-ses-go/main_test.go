package main

import (
	"bytes"
	"errors"
	"io"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"testing/iotest"
)

// TestRun verifies the positional argument contract, including the null
// sender. Version queries and malformed invocations must not read the message.
func TestRun(t *testing.T) {
	for _, tc := range []struct {
		name       string
		args       []string
		diagnostic string
	}{
		{"version", []string{"--version"}, ""},
		{"no arguments", nil, "invalid pipe arguments"},
		{"unknown flag", []string{"--help"}, "invalid pipe arguments"},
		{"version with extra argument", []string{"--version", "private"}, "invalid pipe arguments"},
		{"extra recipient", []string{"sender", "rcpt1", "rcpt2", "123AB"}, "invalid pipe arguments"},
		{"empty recipient", []string{"sender", "", "123AB"}, "invalid pipe arguments"},
		{"empty queue ID", []string{"sender", "rcpt", ""}, "invalid pipe arguments"},
		{"unsafe queue ID", []string{"sender", "rcpt", "123\nAB"}, "invalid pipe arguments"},
		{"non-ASCII queue ID", []string{"sender", "rcpt", "12É"}, "invalid pipe arguments"},
		{"sender control", []string{"send\rer", "rcpt", "123AB"}, "invalid pipe arguments"},
		{"recipient control", []string{"sender", "rcpt\u0085", "123AB"}, "invalid pipe arguments"},
		{"null sender", []string{"", "rcpt", "4zD5Vj3fK8z"}, "delivery is not implemented"},
		{"opaque mailbox", []string{`"a b"@example.org`, "-alias+tag@example.org", "ABC12"}, "delivery is not implemented"},
		{"version as recipient", []string{"sender", "--version", "123AB"}, "delivery is not implemented"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			input := strings.NewReader("Subject: private\r\n\r\nSensitive body\r\n")
			var stdout, stderr bytes.Buffer
			code := run(tc.args, input, &stdout, &stderr)
			if tc.diagnostic == "" {
				if code != 0 || stdout.String() != "postfix-ses-go dev\n" || stderr.Len() != 0 {
					t.Errorf("version: exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
				}
			} else if code != 75 || stdout.Len() != 0 || stderr.String() != "4.3.0 postfix-ses-go: "+tc.diagnostic+"\n" {
				t.Errorf("delivery: exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
			}
			wantRemaining := int(input.Size())
			if tc.diagnostic == "delivery is not implemented" {
				wantRemaining = 0
			}
			if input.Len() != wantRemaining {
				t.Errorf("remaining input = %d, want %d", input.Len(), wantRemaining)
			}
		})
	}
}

// TestBoundedInput checks the exact byte limit, one-byte overflow probe, and
// handling of read errors that occur after part of a message was consumed.
func TestBoundedInput(t *testing.T) {
	const limit = 10 << 20
	payload := make([]byte, limit+2)
	for _, tc := range []struct {
		size       int
		remaining  int
		diagnostic string
	}{
		{limit, 0, "delivery is not implemented"},
		{limit + 1, 0, "message exceeds 10 MiB"},
		{limit + 2, 1, "message exceeds 10 MiB"},
	} {
		input := bytes.NewReader(payload[:tc.size])
		got := deliveryDiagnostic([]string{"", "rcpt", "123AB"}, input)
		if got != tc.diagnostic || input.Len() != tc.remaining {
			t.Errorf("size %d: diagnostic %q, remaining bytes %d", tc.size, got, input.Len())
		}
	}

	broken := io.MultiReader(strings.NewReader("Subject: private"), iotest.ErrReader(errors.New("private detail")))
	var stdout, stderr bytes.Buffer
	code := run([]string{"sender", "rcpt", "123AB"}, broken, &stdout, &stderr)
	if code != 75 || stdout.Len() != 0 || stderr.String() != "4.3.0 postfix-ses-go: cannot read message\n" {
		t.Fatalf("read error: exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

// TestExecutable verifies the linker override and the exit code observed by a
// pipe transport when it hands the binary a message for delivery.
func TestExecutable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "postfix-ses-go")
	build := exec.Command("go", "build", "-ldflags", "-X main.version=v0.1.2", "-o", path, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build binary: %v\n%s", err, out)
	}
	out, err := exec.Command(path, "--version").CombinedOutput()
	if err != nil || string(out) != "postfix-ses-go v0.1.2\n" {
		t.Fatalf("release version: got output %q, err %v", out, err)
	}
	delivery := exec.Command(path, "private@example.org", "recipient@example.net", "123AB")
	delivery.Stdin = strings.NewReader("Subject: private\r\n\r\nSensitive body\r\n")
	out, err = delivery.CombinedOutput()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 75 {
		t.Fatalf("delivery: want exit code 75, got %v", err)
	}
	if string(out) != "4.3.0 postfix-ses-go: delivery is not implemented\n" {
		t.Fatalf("unexpected delivery diagnostic: %q", out)
	}
}
