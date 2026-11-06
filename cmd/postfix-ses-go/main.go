// Command postfix-ses-go is the Postfix pipe transport for delivery through SES.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"time"
	"unicode"

	"github.com/pavelsg/postfix-ses-go/internal/message"
	"github.com/pavelsg/postfix-ses-go/internal/sestransport"
)

// Release builds set version using the linker's -X main.version option.
var version = "dev"

const (
	exitTempFail    = 75
	maxMessageBytes = 10 << 20
	// Leave time to return a diagnostic before the reference transport's 30s cutoff.
	attemptTimeout = 20 * time.Second
)

// Both short and long Postfix queue IDs use ASCII letters and digits.
var queueIDPattern = regexp.MustCompile(`^[a-zA-Z0-9]+$`)

// sendEmail returns the accepted SES message ID or a classified transport error.
type sendEmail func(context.Context, string, string, []byte) (string, error)

// main passes the delivery outcome back to Postfix.
func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr, sestransport.Send))
}

// run accepts one Postfix delivery or a standalone version query.
func run(args []string, stdin io.Reader, stdout, stderr io.Writer, send sendEmail) int {
	if len(args) == 1 && args[0] == "--version" {
		if _, err := fmt.Fprintln(stdout, "postfix-ses-go", version); err != nil {
			return exitTempFail
		}
		return 0
	}
	// Only log a queue ID once validated. Command output may appear in a DSN.
	if len(args) != 3 || args[1] == "" || !queueIDPattern.MatchString(args[2]) ||
		strings.ContainsFunc(args[0], unicode.IsControl) || strings.ContainsFunc(args[1], unicode.IsControl) {
		return deferDelivery(stderr, "invalid pipe arguments")
	}

	sender, recipient, queueID := args[0], args[1], args[2]
	if sender == "" {
		return deferDelivery(stderr, "null sender is not eligible for SES")
	}
	ctx, cancel := context.WithTimeout(context.Background(), attemptTimeout)
	defer cancel()
	raw, err := io.ReadAll(io.LimitReader(stdin, maxMessageBytes+1))
	if err != nil {
		return deferDelivery(stderr, "cannot read message")
	}
	if len(raw) > maxMessageBytes {
		return deferDelivery(stderr, "message exceeds 10 MiB")
	}
	raw, err = message.Prepare(raw)
	if err != nil {
		return deferDelivery(stderr, "invalid message headers")
	}

	id, err := send(ctx, sender, recipient, raw)
	switch {
	case err == nil && id != "":
		// Once SES accepted the message, a log write error must not trigger a retry.
		fmt.Fprintf(stdout, "postfix-ses-go: queue_id=%s ses_message_id=%s\n", queueID, id)
		return 0
	case err == nil || errors.Is(err, sestransport.ErrUncertain):
		return deferDelivery(stderr, "SES acceptance uncertain queue_id="+queueID)
	case errors.Is(err, sestransport.ErrConfiguration):
		return deferDelivery(stderr, "SES configuration unavailable")
	case errors.Is(err, sestransport.ErrAddress):
		return deferDelivery(stderr, "unsupported envelope address")
	default:
		return deferDelivery(stderr, "SES submission failed")
	}
}

// deferDelivery pairs the process exit with the enhanced status Postfix reads first.
func deferDelivery(stderr io.Writer, diagnostic string) int {
	fmt.Fprintln(stderr, "4.3.0 postfix-ses-go:", diagnostic)
	return exitTempFail
}
