package message

import (
	"bytes"
	"testing"
)

// TestPrepareRawMIME keeps the visible author, routing headers and opaque MIME
// payload byte-for-byte, even when an attachment contains header-like text.
func TestPrepareRawMIME(t *testing.T) {
	const from = "From:\r\n\t=?UTF-8?Q?Ren=C3=A9e?= <author@example.test>\r\n"
	const to = "To: Display Name <visible@example.test>\r\n"
	const cc = "cC: other@example.test,\r\n  another@example.test\r\n"
	const mime = "X-SESLike: keep\r\nX-Something: Bcc: keep\r\n" +
		"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=part\r\n"
	const body = "\r\n--part\r\nContent-Type: message/rfc822\r\n\r\n" +
		"From: original@example.test\r\nBcc: nested@example.test\r\n\r\noriginal\r\n" +
		"--part\r\nContent-Type: application/octet-stream\r\n" +
		"Content-Transfer-Encoding: base64\r\n\r\nAAEC/w==\r\n--part--\r\n"
	const input = "Bcc: first@example.test,\r\n second@example.test\r\n" + from +
		"x-SES-source-ARN: control\r\n\tfolded\r\n" + to +
		"bCC:\tthird@example.test\r\nReSeNt-BcC: fourth@example.test\r\n" + cc +
		"X-SES-Custom-Future-Field: override\r\n" + mime +
		"X-SES-CONFIGURATION-SET: private\r\n" + body
	raw := []byte(input)
	got, err := Prepare(raw)
	if err != nil || string(got) != from+to+cc+mime+body {
		t.Fatalf("Prepare returned %q, %v", got, err)
	}
	if string(raw) != input {
		t.Fatal("Prepare mutated the supplied message")
	}
}

// TestPreparePreservesBody covers an empty body and binary data that would be
// invalid in the outer header block. No body bytes participate in filtering.
func TestPreparePreservesBody(t *testing.T) {
	for _, body := range []string{"", "\x00\xff\nBcc: payload\r", ".\r\n"} {
		raw := []byte("From: Author <a@example.test>\r\n\r\n" + body)
		got, err := Prepare(raw)
		if err != nil || !bytes.Equal(got, raw) {
			t.Errorf("Prepare(%q) = %q, %v", raw, got, err)
		}
	}
}

// TestPrepareRejectsAmbiguousHeaders ensures unsupported or malformed input
// produces no partial message and no input-derived diagnostic.
func TestPrepareRejectsAmbiguousHeaders(t *testing.T) {
	const from = "From: a@example.test\r\n"
	for _, tc := range []struct {
		name string
		raw  string
	}{
		{"empty", ""},
		{"missing separator", from + "Bcc: secret@example.test\r\n"},
		{"missing author", "To: visible@example.test\r\n\r\n"},
		{"empty author", "From: \t\r\n \t\r\n\r\n"},
		{"repeated author", from + "fROM: b@example.test\r\n\r\n"},
		{"pseudo author in body", "Subject: example\r\n\r\n" + from},
		{"orphan continuation", " Subject: example\r\n" + from + "\r\n"},
		{"no colon", from + "bcc hidden@example.test\r\n\r\n"},
		{"space in name", from + "Bcc : secret@example.test\r\n\r\n"},
		{"empty name", from + ": value\r\n\r\n"},
		{"non-ASCII name", from + "Bçc: hidden@example.test\r\n\r\n"},
		{"bare LF", from + "Subject: safe\nBcc: secret@example.test\r\n\r\n"},
		{"bare CR in fold", from + " \rBcc: hidden@example.test\r\n\r\n"},
		{"NUL in value", from + "Subject: \x00\r\n\r\n"},
		{"control in dropped field", from + "Bcc: secret\x7f@example.test\r\n\r\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got, err := Prepare([]byte(tc.raw)); got != nil || err != errHeaders {
				t.Fatalf("got %q, %v; want no bytes and a generic header error", got, err)
			}
		})
	}
}
