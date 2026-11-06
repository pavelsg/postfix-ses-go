package sestransport

import (
	"context"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// attemptContext supplies the bounded attempt required of transport callers.
func attemptContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	t.Cleanup(cancel)
	return ctx
}

// configureTest isolates the standard SDK configuration from the operator's
// files and credentials. No test needs a live AWS resource.
func configureTest(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("HOME", dir)
	t.Setenv("AWS_CONFIG_FILE", filepath.Join(dir, "config"))
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", filepath.Join(dir, "credentials"))
	t.Setenv("AWS_PROFILE", "")
	t.Setenv("AWS_DEFAULT_PROFILE", "")
	t.Setenv("AWS_EC2_METADATA_DISABLED", "true")
	t.Setenv("AWS_REGION", "us-east-1")
	t.Setenv("AWS_ACCESS_KEY_ID", "fixture-key")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "fixture-secret")
	t.Setenv("AWS_SESSION_TOKEN", "")
	t.Setenv("AWS_ENDPOINT_URL_SES", "")
	t.Setenv("AWS_CA_BUNDLE", "")
	t.Setenv("AWS_ENDPOINT_URL", "")
}

// endpoint serves a TLS-only fixture trusted through the same bundle path
// that a Postfix operator can supply.
func endpoint(t *testing.T, handler http.HandlerFunc) string {
	t.Helper()
	s := httptest.NewTLSServer(handler)
	t.Cleanup(s.Close)
	path := filepath.Join(t.TempDir(), "ca.pem")
	cert := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: s.Certificate().Raw})
	if err := os.WriteFile(path, cert, 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_CA_BUNDLE", path)
	t.Setenv("AWS_ENDPOINT_URL_SES", s.URL)
	return s.URL
}

// TestSend checks the signed wire request; destination headers in the supplied
// MIME are independent of the actual SES delivery target.
func TestSend(t *testing.T) {
	configureTest(t)
	const raw = "From: Visible Author <author@example.org>\r\nTo: visible@example.org\r\n\r\npayload"
	var requests atomic.Int32
	endpoint(t, func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Method != "POST" || r.URL.RequestURI() != "/v2/email/outbound-emails" {
			t.Errorf("unexpected route: %s %s", r.Method, r.URL)
		}
		auth := r.Header.Get("Authorization")
		if !strings.HasPrefix(auth, "AWS4-HMAC-SHA256 Credential=fixture-key/") ||
			!strings.Contains(auth, "/us-east-1/ses/aws4_request") || !strings.Contains(auth, "Signature=") {
			t.Errorf("expected signed SES request, got %q", auth)
		}
		var in struct {
			FromEmailAddress string
			Destination      map[string][]string
			Content          struct{ Raw struct{ Data []byte } }
		}
		if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
			t.Error(err)
		}
		if len(in.Destination) != 1 || !slices.Equal(in.Destination["ToAddresses"], []string{`"a b"@example.org`}) ||
			in.FromEmailAddress != "opted@example.org" || string(in.Content.Raw.Data) != raw {
			t.Errorf("unexpected payload: %+v", in)
		}
		fmt.Fprint(w, `{"MessageId":"fixture-001"}`)
	})
	id, err := Send(attemptContext(t), "opted@example.org", "a b@example.org", []byte(raw))
	if err != nil || id != "fixture-001" || requests.Load() != 1 {
		t.Fatalf("Send: id=%q err=%v requests=%d", id, err, requests.Load())
	}
}

// TestNoReplay exercises SDK retryable errors, redirects, and lost acknowledgements.
func TestNoReplay(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		body   string
		want   error
	}{
		{"throttle", 429, `{"message":"sensitive server text"}`, ErrThrottled},
		{"server error", 503, `{}`, ErrUncertain},
		{"redirect", 307, `{}`, ErrTemporary},
		{"lost response", 0, "", ErrUncertain},
		{"missing ID", 200, `{}`, ErrUncertain},
		{"unsafe ID", 200, `{"MessageId":"bad\nvalue"}`, ErrUncertain},
		{"malformed acknowledgement", 200, `{`, ErrUncertain},
	} {
		t.Run(tc.name, func(t *testing.T) {
			configureTest(t)
			t.Setenv("AWS_MAX_ATTEMPTS", "8")
			var requests atomic.Int32
			endpoint(t, func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				if _, err := io.Copy(io.Discard, r.Body); err != nil {
					t.Error(err)
				}
				if tc.status == 0 {
					conn, _, err := http.NewResponseController(w).Hijack()
					if err != nil {
						t.Error(err)
						return
					}
					conn.Close()
					return
				}
				if tc.status == 307 {
					w.Header().Set("Location", "/unexpected")
				}
				w.WriteHeader(tc.status)
				fmt.Fprint(w, tc.body)
			})
			id, err := Send(attemptContext(t), "sender@example.org", "rcpt@example.org", []byte("raw"))
			if id != "" || !errors.Is(err, tc.want) || requests.Load() != 1 {
				t.Fatalf("Send: id=%q err=%v requests=%d", id, err, requests.Load())
			}
		})
	}
}

// TestRejectedRequest exercises the actual SDK deserializer against a TLS peer.
// Every rejected send has a single request and exposes only a safe sentinel.
func TestRejectedRequest(t *testing.T) {
	const duplicate = "Duplicate header 'Subject'."
	const unverified = "Email address is not verified. The following identities failed the check in region US-EAST-1: private@example.org"
	for _, tc := range []struct {
		name, code, reason string
		status             int
		want               error
	}{
		{"confirmed duplicate", "MessageRejected", duplicate, 400, ErrMessage},
		{"bad input with evidence", "BadRequestException", duplicate, 400, ErrMessage},
		{"identity", "MessageRejected", unverified, 400, ErrConfiguration},
		{"sending paused", "SendingPausedException", "", 400, ErrConfiguration},
		{"unverified domain", "MailFromDomainNotVerifiedException", "", 400, ErrConfiguration},
		{"IAM denied", "AccessDeniedException", "", 403, ErrConfiguration},
		{"throttling", "TooManyRequestsException", "", 429, ErrThrottled},
		{"unfamiliar rejection", "MessageRejected", "unknown reason", 400, ErrTemporary},
		{"unfamiliar code", "UnfamiliarException", duplicate, 400, ErrTemporary},
		{"unfamiliar status", "MessageRejected", duplicate, 409, ErrTemporary},
		{"inconsistent evidence", "MessageRejected", "Duplicate header 'Date'.", 400, ErrTemporary},
		{"repeatable header", "BadRequestException", "Duplicate header 'Received'.", 400, ErrTemporary},
		{"reason containing address", "BadRequestException", duplicate + " private@example.org", 400, ErrTemporary},
		{"server failed after write", "MessageRejected", duplicate, 503, ErrUncertain},
		{"timeout after write", "RequestTimeout", "", 408, ErrUncertain},
	} {
		t.Run(tc.name, func(t *testing.T) {
			configureTest(t)
			var requests atomic.Int32
			endpoint(t, func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				w.Header().Set("X-Amzn-Errortype", tc.code)
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(tc.status)
				if err := json.NewEncoder(w).Encode(map[string]string{"message": tc.reason}); err != nil {
					t.Error(err)
				}
			})
			const raw = "From: a@example.org\r\nSubject: one\r\nsUbJeCt: two\r\n" +
				"Received: first\r\nReceived: second\r\nDate: today\r\n\r\nDate: this is body text"
			id, err := Send(attemptContext(t), "sender@example.org", "rcpt@example.org", []byte(raw))
			if id != "" || err != tc.want || requests.Load() != 1 {
				t.Fatalf("Send: id=%q err=%v requests=%d", id, err, requests.Load())
			}
		})
	}
}

// TestCancelledSend distinguishes expiration before submission from a cancelled
// wait after the endpoint has received the entire request but has yet to reply.
func TestCancelledSend(t *testing.T) {
	for _, afterWrite := range []bool{false, true} {
		configureTest(t)
		ctx, cancel := context.WithCancel(attemptContext(t))
		var requests atomic.Int32
		endpoint(t, func(w http.ResponseWriter, r *http.Request) {
			requests.Add(1)
			if _, err := io.Copy(io.Discard, r.Body); err != nil {
				t.Error(err)
			}
			cancel()
			<-r.Context().Done()
		})
		want, wantRequests := ErrUncertain, int32(1)
		if !afterWrite {
			cancel()
			want, wantRequests = ErrTemporary, 0
		}
		id, err := Send(ctx, "sender@example.org", "rcpt@example.org", []byte("raw"))
		cancel()
		if id != "" || err != want || requests.Load() != wantRequests {
			t.Fatalf("afterWrite=%v: id=%q err=%v requests=%d", afterWrite, id, err, requests.Load())
		}
	}
}

// TestConfig fixes the endpoint trust boundary even when the SDK environment
// or shared file supplies other endpoint settings.
func TestConfig(t *testing.T) {
	configureTest(t)
	t.Setenv("AWS_ENDPOINT_URL", "http://wrong.example")
	t.Setenv("AWS_ENDPOINT_URL_SESV2", "http://wrong-service.example")
	client, err := newClient(context.Background())
	if err != nil || client.Options().BaseEndpoint != nil {
		t.Fatalf("expected built-in regional endpoint: %v", err)
	}
	// Unset (rather than empty) environment endpoints to exercise resolution
	// from a shared profile independently.
	if err := os.Unsetenv("AWS_ENDPOINT_URL"); err != nil {
		t.Fatal(err)
	}
	if err := os.Unsetenv("AWS_ENDPOINT_URL_SESV2"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(os.Getenv("AWS_CONFIG_FILE"), []byte("[default]\nendpoint_url = http://wrong-profile.example\n"), 0600); err != nil {
		t.Fatal(err)
	}
	client, err = newClient(context.Background())
	if err != nil || client.Options().BaseEndpoint != nil {
		t.Fatalf("shared-profile endpoint override: %v", err)
	}
	for _, invalid := range []string{
		"http://localhost", "//example.test", "https://", "https://admin@example.test",
		"https://example.test/path", "https://example.test/?", "https://example.test/#frag",
		"https://host..invalid", "https://[hostname]", "https://2001:db8::1",
		"https://example.test:0", "https://example.test:65536", "https://example.test:",
	} {
		if validEndpoint(invalid) {
			t.Errorf("accepted invalid endpoint %q", invalid)
		}
	}
	t.Setenv("AWS_ENDPOINT_URL_SES", "http://127.0.0.1")
	if _, err := newClient(context.Background()); err != ErrConfiguration {
		t.Errorf("invalid endpoint: %v", err)
	}
	t.Setenv("AWS_ENDPOINT_URL_SES", "")
	t.Setenv("AWS_REGION", "")
	if _, err := newClient(context.Background()); err != ErrConfiguration {
		t.Errorf("missing AWS_REGION: %v", err)
	}
}

// TestAttemptsRequireDeadline makes a bounded lifetime part of the adapter contract.
func TestAttemptsRequireDeadline(t *testing.T) {
	if _, err := Send(context.Background(), "sender@example.org", "rcpt@example.org", []byte("raw")); err != ErrConfiguration {
		t.Fatalf("unbounded Send returned %v", err)
	}
}

// TestMailbox verifies that special local parts stay a single address on the wire.
func TestMailbox(t *testing.T) {
	for _, tc := range []struct{ input, want string }{
		{"local+tag@example.org", "local+tag@example.org"},
		{"a b@example.org", `"a b"@example.org`},
		{"a@b@example.org", `"a@b"@example.org`},
		{`a"b\c@example.org`, `"a\"b\\c"@example.org`},
		{"a@example.org,b@example.org", `"a@example.org,b"@example.org`},
		{"plain", ""}, {"", ""}, {"@example.org", ""}, {"a@", ""},
		{"é@example.org", ""}, {"x@example.org>", ""}, {"x@y..z", ""},
		{"a\rb@example.org", ""}, {"a@x,y", ""},
		{strings.Repeat(" ", 63) + "@example.org", ""},
	} {
		got, err := mailbox(tc.input)
		if got != tc.want || (err == nil) != (tc.want != "") {
			t.Errorf("mailbox(%q) = %q, %v; want %q", tc.input, got, err, tc.want)
		}
	}
}
