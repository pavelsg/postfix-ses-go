// Command postfix-ses-go is the Postfix pipe transport for delivery through SES.
package main

import (
	"fmt"
	"io"
	"os"
)

// Release builds set version using the linker's -X main.version option.
var version = "dev"

// Postfix keeps the recipient queued when a pipe transport returns EX_TEMPFAIL.
const exitTempFail = 75

// main passes the process exit status back to Postfix.
func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run prints the version on request. Until delivery is implemented, every
// other invocation must defer without echoing envelope or message data.
func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 1 && args[0] == "--version" {
		if _, err := fmt.Fprintln(stdout, "postfix-ses-go", version); err != nil {
			return exitTempFail
		}
		return 0
	}
	fmt.Fprintln(stderr, "postfix-ses-go: delivery is not implemented")
	return exitTempFail
}
