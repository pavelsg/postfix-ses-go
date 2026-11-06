package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"testing/iotest"
	"time"

	"github.com/pavelsg/postfix-ses-go/internal/sestransport"
)

// TestRun verifies the positional argument contract, including the null
// sender. Version queries and malformed invocations must not read the message.
func TestRun(t *testing.T) {
	for _, tc := range []struct {
		name       string
		args       []string
		code       int
		diagnostic string
	}{
		{"version", []string{"--version"}, 0, ""},
		{"no arguments", nil, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"unknown flag", []string{"--help"}, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"version with extra argument", []string{"--version", "private"}, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"extra recipient", []string{"sender", "rcpt1", "rcpt2", "123AB"}, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"empty recipient", []string{"sender", "", "123AB"}, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"empty queue ID", []string{"sender", "rcpt", ""}, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"unsafe queue ID", []string{"sender", "rcpt", "123\nAB"}, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"non-ASCII queue ID", []string{"sender", "rcpt", "12É"}, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"sender control", []string{"send\rer", "rcpt", "123AB"}, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"recipient control", []string{"sender", "rcpt\u0085", "123AB"}, 75, "4.3.0 postfix-ses-go: invalid pipe arguments"},
		{"null sender", []string{"", "rcpt", "4zD5Vj3fK8z"}, 75, "4.3.0 postfix-ses-go: null sender is not eligible for SES"},
		{"opaque mailbox", []string{`"a b"@example.org`, "-alias+tag@example.org", "ABC12"}, 65, "5.6.0 postfix-ses-go: invalid message headers"},
		{"version as recipient", []string{"sender", "--version", "123AB"}, 65, "5.6.0 postfix-ses-go: invalid message headers"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			input := strings.NewReader("Subject: private\r\n\r\nSensitive body\r\n")
			var stdout, stderr bytes.Buffer
			code := run(tc.args, input, &stdout, &stderr, nil)
			wantStdout, wantStderr := "", tc.diagnostic
			if tc.code == 0 {
				wantStdout = "postfix-ses-go dev\n"
			} else {
				wantStderr += "\n"
			}
			if code != tc.code || stdout.String() != wantStdout || stderr.String() != wantStderr {
				t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
			}
			wantRemaining := int(input.Size())
			if tc.code == 65 {
				wantRemaining = 0
			}
			if input.Len() != wantRemaining {
				t.Errorf("remaining input = %d, want %d", input.Len(), wantRemaining)
			}
		})
	}
}

// TestHandoff checks that the prepared bytes and envelope reach the sender exactly
// once and that only a confirmed acknowledgement can complete a delivery.
func TestHandoff(t *testing.T) {
	const raw = "From: Visible <author@example.test>\r\nBcc: private@example.test\r\n\r\nbody"
	const prepared = "From: Visible <author@example.test>\r\n\r\nbody"
	for _, tc := range []struct {
		name       string
		id         string
		err        error
		code       int
		diagnostic string
	}{
		{"accepted", "SES-123", nil, 0, ""},
		{"missing ID", "", nil, 75, "4.3.0 postfix-ses-go: SES acceptance uncertain queue_id=ABC123"},
		{"uncertain", "", sestransport.ErrUncertain, 75, "4.3.0 postfix-ses-go: SES acceptance uncertain queue_id=ABC123"},
		{"invalid address", "", sestransport.ErrAddress, 75, "4.3.0 postfix-ses-go: unsupported envelope address"},
		{"configuration", "", sestransport.ErrConfiguration, 75, "4.3.0 postfix-ses-go: SES configuration unavailable"},
		{"throttle", "", sestransport.ErrThrottled, 75, "4.3.0 postfix-ses-go: SES throttled request"},
		{"message content", "", sestransport.ErrMessage, 65, "5.6.0 postfix-ses-go: SES rejected message content"},
		{"other failure", "", errors.New("private detail"), 75, "4.3.0 postfix-ses-go: SES submission failed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			calls := 0
			send := func(ctx context.Context, sender, recipient string, content []byte) (string, error) {
				calls++
				deadline, ok := ctx.Deadline()
				if !ok || time.Until(deadline) > 20*time.Second || ctx.Err() != nil {
					t.Fatal("missing or expired send deadline")
				}
				if sender != "sender@example.test" || recipient != "private@example.test" || string(content) != prepared {
					t.Fatal("incorrect SES envelope or raw MIME")
				}
				return tc.id, tc.err
			}
			code := run([]string{"sender@example.test", "private@example.test", "ABC123"}, strings.NewReader(raw), &stdout, &stderr, send)
			if calls != 1 {
				t.Fatalf("send calls = %d, want 1", calls)
			}
			if tc.diagnostic == "" {
				if code != 0 || stdout.String() != "postfix-ses-go: queue_id=ABC123 ses_message_id=SES-123\n" || stderr.Len() != 0 {
					t.Fatalf("unexpected success result: %d %q %q", code, stdout.String(), stderr.String())
				}
			} else if code != tc.code || stdout.Len() != 0 || stderr.String() != tc.diagnostic+"\n" {
				t.Fatalf("unexpected failure outcome: %d %q %q", code, stdout.String(), stderr.String())
			}
		})
	}
}

// TestBoundedInput checks the exact byte limit, one-byte overflow probe, and
// handling of read errors that occur after part of a message was consumed.
func TestBoundedInput(t *testing.T) {
	const limit = 10 << 20
	payload := make([]byte, limit+2)
	copy(payload, "From: a@b.test\r\n\r\n")
	for _, tc := range []struct {
		size       int
		remaining  int
		code       int
		diagnostic string
	}{
		{limit, 0, 75, "4.3.0 postfix-ses-go: SES configuration unavailable"},
		{limit + 1, 0, 65, "5.3.4 postfix-ses-go: message exceeds 10 MiB"},
		{limit + 2, 1, 65, "5.3.4 postfix-ses-go: message exceeds 10 MiB"},
	} {
		input := bytes.NewReader(payload[:tc.size])
		var stderr bytes.Buffer
		code := run([]string{"sender", "rcpt", "123AB"}, input, io.Discard, &stderr, func(context.Context, string, string, []byte) (string, error) {
			return "", sestransport.ErrConfiguration
		})
		got := stderr.String()
		if code != tc.code || got != tc.diagnostic+"\n" || input.Len() != tc.remaining {
			t.Errorf("size %d: diagnostic %q, remaining bytes %d", tc.size, got, input.Len())
		}
	}

	broken := io.MultiReader(strings.NewReader("Subject: private"), iotest.ErrReader(errors.New("private detail")))
	var stdout, stderr bytes.Buffer
	code := run([]string{"sender", "rcpt", "123AB"}, broken, &stdout, &stderr, nil)
	if code != 75 || stdout.Len() != 0 || stderr.String() != "4.3.0 postfix-ses-go: cannot read message\n" {
		t.Fatalf("read error: exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

// TestPipeFraming defers an LF-only pipe configuration that could affect all mail.
func TestPipeFraming(t *testing.T) {
	var stderr bytes.Buffer
	code := run([]string{"sender", "rcpt", "123AB"}, strings.NewReader("From: a@b.test\n\nbody"), io.Discard, &stderr, nil)
	if code != 75 || stderr.String() != "4.3.0 postfix-ses-go: invalid pipe message framing\n" {
		t.Fatalf("pipe framing: exit %d, stderr %q", code, stderr.String())
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
	for _, tc := range []struct {
		mode   string
		detail string
	}{
		{"", ""},
		{"0", ""},
		{"1", "postfix-ses-go-smoke: stage=setup\n"},
	} {
		delivery := exec.Command(path, "private@example.org", "recipient@example.net", "123AB")
		delivery.Env = []string{"AWS_REGION=", "POSTFIX_SES_SMOKE_DIAGNOSTICS=" + tc.mode}
		delivery.Stdin = strings.NewReader("From: author@example.org\r\nSubject: private\r\n\r\nSensitive body\r\n")
		out, err = delivery.CombinedOutput()
		var exit *exec.ExitError
		if !errors.As(err, &exit) || exit.ExitCode() != 75 {
			t.Fatalf("delivery: want exit code 75, got %v", err)
		}
		if string(out) != "4.3.0 postfix-ses-go: SES configuration unavailable\n"+tc.detail {
			t.Fatalf("smoke diagnostics %q: unexpected output %q", tc.mode, out)
		}
	}
}
