// Package message prepares the raw MIME bytes supplied by a Postfix pipe.
package message

import (
	"bytes"
	"errors"
)

// ErrInvalidHeaders identifies a structural error in a CRLF-delimited field.
var ErrInvalidHeaders = errors.New("invalid message headers")

// errPipeFormat indicates input whose framing cannot be trusted. A pipe using
// its default LF delimiter can cause this error for every message on the host.
var errPipeFormat = errors.New("invalid pipe message framing")

// Prepare removes top-level blind-copy and SES control fields, including all
// their continuation lines. Input must have CRLF-delimited headers, exactly
// one nonblank From field, and a blank line separating the body. The caller
// bounds the size of raw. The body and retained headers are copied verbatim.
func Prepare(raw []byte) ([]byte, error) {
	prepared := make([]byte, 0, len(raw))
	seenFrom := false
	for offset := 0; ; {
		start := offset
		line, next, ok := headerLine(raw, offset)
		if !ok {
			return nil, errPipeFormat
		}
		if len(line) == 0 {
			if !seenFrom {
				return nil, ErrInvalidHeaders
			}
			return append(prepared, raw[start:]...), nil
		}

		name, _, found := bytes.Cut(line, []byte(":"))
		if !found || len(name) == 0 {
			return nil, ErrInvalidHeaders
		}
		for _, c := range name {
			if c < '!' || c > '~' {
				return nil, ErrInvalidHeaders
			}
		}

		// Treat a field and all its folds as a unit. In particular a stripped
		// field's continuation must never attach to the previous visible field.
		offset = next
		for offset < len(raw) && (raw[offset] == ' ' || raw[offset] == '\t') {
			_, offset, ok = headerLine(raw, offset)
			if !ok {
				return nil, errPipeFormat
			}
		}
		if bytes.EqualFold(name, []byte("From")) {
			value := raw[start+len(name)+1 : offset]
			if seenFrom || len(bytes.Trim(value, " \t\r\n")) == 0 {
				return nil, ErrInvalidHeaders
			}
			seenFrom = true
		}
		if bytes.EqualFold(name, []byte("Bcc")) || bytes.EqualFold(name, []byte("Resent-Bcc")) ||
			(len(name) >= 6 && bytes.EqualFold(name[:6], []byte("X-SES-"))) {
			continue
		}
		prepared = append(prepared, raw[start:offset]...)
	}
}

// headerLine requires CRLF and rejects stray controls so downstream parsers
// cannot interpret part of a retained value as a separate header.
func headerLine(raw []byte, offset int) (line []byte, next int, ok bool) {
	line, _, found := bytes.Cut(raw[offset:], []byte("\r\n"))
	if !found {
		return nil, 0, false
	}
	for _, c := range line {
		if (c < ' ' && c != '\t') || c == 0x7f {
			return nil, 0, false
		}
	}
	return line, offset + len(line) + 2, true
}
