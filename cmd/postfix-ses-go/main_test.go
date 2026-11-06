package main

import (
	"bytes"
	"errors"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestRun verifies that only a standalone version query can succeed.
func TestRun(t *testing.T) {
	for _, tc := range []struct {
		name string
		args []string
		code int
	}{
		{"version", []string{"--version"}, 0},
		{"no arguments", nil, 75},
		{"unknown flag", []string{"--help"}, 75},
		{"version with extra argument", []string{"--version", "private"}, 75},
		{"version as recipient", []string{"sender", "--version", "queue-id"}, 75},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			if got := run(tc.args, &stdout, &stderr); got != tc.code {
				t.Fatalf("exit code = %d, want %d", got, tc.code)
			}
			if tc.code == 0 {
				if stdout.String() != "postfix-ses-go dev\n" || stderr.Len() != 0 {
					t.Fatalf("unexpected version output: stdout %q, stderr %q", stdout.String(), stderr.String())
				}
			} else if stdout.Len() != 0 || stderr.String() != "postfix-ses-go: delivery is not implemented\n" {
				t.Fatalf("unexpected delivery output: stdout %q, stderr %q", stdout.String(), stderr.String())
			}
		})
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
	if string(out) != "postfix-ses-go: delivery is not implemented\n" {
		t.Fatalf("unexpected delivery diagnostic: %q", out)
	}
}
