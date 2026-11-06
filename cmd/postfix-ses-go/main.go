// Command postfix-ses-go is the Postfix pipe transport for delivery through SES.
package main

import (
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"unicode"
)

// Release builds set version using the linker's -X main.version option.
var version = "dev"

// Postfix keeps the recipient queued when a pipe transport returns EX_TEMPFAIL.
const exitTempFail = 75

// The limit applies to the complete message as serialized by the Postfix pipe.
const maxMessageBytes = 10 << 20

// Both short and long Postfix queue IDs use ASCII letters and digits.
var queueIDPattern = regexp.MustCompile(`^[a-zA-Z0-9]+$`)

// main passes the process exit status back to Postfix.
func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// run prints the version on request. Until delivery is implemented, every
// other invocation must defer without echoing envelope or message data.
func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 1 && args[0] == "--version" {
		if _, err := fmt.Fprintln(stdout, "postfix-ses-go", version); err != nil {
			return exitTempFail
		}
		return 0
	}
	// Postfix interprets a leading enhanced status before the process exit code.
	fmt.Fprintln(stderr, "4.3.0 postfix-ses-go:", deliveryDiagnostic(args, stdin))
	return exitTempFail
}

// deliveryDiagnostic validates the pipe invocation and consumes a bounded
// message. All outcomes currently defer; diagnostics may be visible to senders.
func deliveryDiagnostic(args []string, stdin io.Reader) string {
	// Postfix supplies the envelope, including a possible null sender. Leave
	// mailbox quoting and spelling intact instead of parsing header addresses.
	if len(args) != 3 || args[1] == "" || !queueIDPattern.MatchString(args[2]) ||
		strings.ContainsFunc(args[0], unicode.IsControl) || strings.ContainsFunc(args[1], unicode.IsControl) {
		return "invalid pipe arguments"
	}

	// One lookahead byte distinguishes an exact-limit message from overflow.
	n, err := io.Copy(io.Discard, io.LimitReader(stdin, maxMessageBytes+1))
	if err != nil {
		return "cannot read message"
	}
	if n > maxMessageBytes {
		return "message exceeds 10 MiB"
	}
	return "delivery is not implemented"
}
