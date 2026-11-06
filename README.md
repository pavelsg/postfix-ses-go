# postfix-ses-go

A Go delivery agent under development for Postfix operators who want to route selected outbound mail through the Amazon SES v2 HTTPS API. Postfix chooses the route and owns the queue. The agent submits one envelope recipient per invocation. The [project goals](https://github.com/pavelsg/postfix-ses-go/blob/HEAD/GOAL.md) describe routing, failure handling, and release acceptance criteria.

**Development status.** The binary submits raw mail through the SES v2 HTTPS API. Real Postfix queue tests cover handoff, deferral, bounces, and recovery against a controlled endpoint. The full routing suite and live SES qualification are pending. Test installations with real AWS credentials and an opted-in sender can send real mail.

## Postfix pipe contract

```text
postfix-ses-go --version
postfix-ses-go ENVELOPE_SENDER ENVELOPE_RECIPIENT QUEUE_ID < message.eml
```

`--version` as the only argument prints the build version and exits 0. Delivery takes exactly three positional arguments. Pass the envelope sender even when it is empty (the null reverse path), one nonempty envelope recipient, and the Postfix queue ID as ASCII letters and digits. Envelope addresses are opaque arguments and must be free of control characters. The visible From, To and Cc headers belong to the message.

The reference pipe uses Postfix’s unquoted envelope address form (the `q` flag is omitted). For example, the queue and logs show `"a b"@outside.test`, and the executable receives `a b@outside.test` as a single argument with the space preserved. For a message addressed to two recipients on the same external domain, Postfix invokes the executable twice with the same queue ID and a different recipient argument each time.

Postfix supplies CRLF headers and body on stdin. The agent reads at most 10 MiB plus one overflow byte and requires exactly one nonblank top-level From field and a blank line before the body. It strips outer Bcc, Resent-Bcc and X-SES-* headers, including their continuations. Other header bytes and the entire body, including MIME attachments, are passed through as received.

The signed SES request uses the envelope sender as `FromEmailAddress` and the single envelope recipient as its only destination. Visible To/Cc and From headers stay in the raw MIME. The adapter serializes ASCII envelope mailboxes for SES, quoting special characters in the Postfix local part when necessary. A null sender forced onto this transport defers. Keep null senders and redirected mail on the installation’s appropriate routes.

| Outcome | Exit | Postfix action |
| --- | --- | --- |
| Standalone version query | 0 | Print the version |
| SES returns a usable message ID | 0 | Complete this recipient's handoff. The binary writes the queue ID and SES message ID together on stdout. |
| Timeout or lost response after transmission, or unusable acknowledgement | 75 | Defer with an uncertain-acceptance diagnostic and queue ID. A later attempt can duplicate a message SES already accepted. |
| SES throttling, temporary network failure, unverified identity, unavailable credentials, or an unrecognized rejection | 75 | Defer with a `4.3.0` diagnostic. Fix deployment problems before retrying. |
| Invalid pipe arguments, unsupported envelope, stdin read failure, or uncertain header framing | 75 | Defer. Check the pipe configuration and CRLF delimiter. |
| Message exceeds the 10 MiB input limit | 65 | Report a permanent size failure (`5.3.4`) for this recipient, before any send request. |
| Definitive message content error | 65 | Report a permanent content failure (`5.6.0`) for this recipient. Postfix handles the delivery status notification. |

**When the binary bounces a message.** Local checks catch malformed header fields and a missing, empty or repeated From field in CRLF-framed input. For an SES rejection, the binary requires a recognized HTTP 400 input error, an explicit duplicate-header reason, and two occurrences of that field in the submitted outer headers. The recognized fields are Subject, Date, Message-ID, MIME-Version, Content-Type and Content-Transfer-Encoding. For example, two Subject fields plus an SES duplicate-Subject rejection qualify. An unverified identity can also produce HTTP 400 `MessageRejected` and stays queued for the operator. An unfamiliar reason also stays queued. AWS documents the possible causes in its [sending errors guide](https://docs.aws.amazon.com/ses/latest/dg/troubleshoot-error-messages.html).

**Upgrading from phase 2.** The command arguments and stdin contract are the same. Messages that exceed the documented size limit or meet the permanent-error criteria now bounce. Check the mail host’s size policy and the pipe’s CRLF configuration before installing the new binary.

The send uses the AWS SDK for Go v2 with automatic send retries disabled. A 20-second application deadline starts before reading stdin and covers credential lookup and HTTPS submission. Postfix enforces a separate 30-second process limit, including a stalled stdin. Postfix owns subsequent attempts. For an uncertain handoff, use the queue ID to investigate before forcing a retry. A retry after a restart can send a second copy if SES accepted the first request. SES acceptance ends the local handoff. Final delivery and later bounces are handled by SES. The tested Postfix 3.7.11 logs the IDs from stdout on success. Logging successful pipe output requires Postfix 3.0 or newer. Postfix can include command output in delivery status notifications, so diagnostics omit addresses, message contents, and credential details.

### Configure the pipe environment

Set `AWS_REGION` explicitly to the region of a verified sending identity. Supply an AWS runtime identity with permission for `ses:SendEmail` through the SDK credential chain. Ensure the dedicated pipe user can access the identity and the system CA bundle. The executable reads its process environment. The repository’s `.env.example` lists settings.

Postfix filters the environment it passes to external commands. For example, to set a public region for this transport, add the following override immediately below the `pipe` service line in the supplied `master.cf` fragment, before `user=sespipe`. This long-form override requires Postfix 3.0 or newer. If using a shared credentials file, include `AWS_SHARED_CREDENTIALS_FILE=/etc/postfix/ses-credentials` in the same list and restrict file access to the pipe identity and administrators.

```text
  -o { export_environment = TZ LANG AWS_REGION=us-east-1 }
```

By default the SDK resolves the regional SES endpoint. For a controlled endpoint, set `AWS_ENDPOINT_URL_SES` to an absolute HTTPS origin and optionally `AWS_CA_BUNDLE` to a readable PEM trust bundle. Only trusted operators should set these values. The endpoint receives signed requests and complete message contents.

### Try the transport on a stock Postfix test host

1. Install the Linux binary at `/usr/local/libexec/postfix-ses-go`, owned by root and executable by a dedicated unprivileged `sespipe` account. Create that account with a matching group, a disabled login, and no access to the Postfix queue.
2. Merge the settings from `examples/postfix/main.cf.fragment` and append the service from `examples/postfix/master.cf.fragment`. The service name `sespipe` sets the prefix for the time and one-recipient limits. The pipe uses `null_sender=` so an empty sender reaches the program as one empty argument. The queue-ID macro requires Postfix 2.11 or newer.
3. Install `examples/postfix/ses_senders` as `/etc/postfix/ses_senders`. Each anchored regular expression selects one full envelope sender address, using Postfix's default case-insensitive matching. Escape regex metacharacters in actual addresses, including dots and plus signs. Use a separate row for each opted-in alias. Omit domain-wide and null-sender entries. Run `postfix check`, then `postfix reload`.
4. Submit a test message from a listed sender to an operator-controlled recipient. Follow the queue ID in the Postfix log. On the tested Postfix version, a confirmed handoff logs `status=sent` with the SES ID. A deferred recipient remains visible in `postqueue -p`. Remove the test sender entry and reload Postfix to end the trial.

The sender map selects the default outbound transport. Keep sender authorization and redirect handling in the installation that knows the origin of the message. Preserve existing local and forwarding routes when merging these examples. The examples require the `regexp` backend listed by `postconf -m`. Postfix reads this text map directly.

## Local development

From the repository root with Go 1.25 or later installed, run:

```sh
go test ./...
go vet ./...
go build -o bin/postfix-ses-go ./cmd/postfix-ses-go
./bin/postfix-ses-go --version
```

With Docker available, run `bash tests/postfix/test.sh` to exercise the real Postfix queue in a disposable container. The reference configuration has been exercised on Debian 12 with Postfix 3.7.11. The suite checks request counts, pipe exits, per-recipient queue state and Postfix diagnostics. It exercises acceptance, throttling, recoverable and permanent HTTP 400 responses, a lost acknowledgement, and an interrupted pipe. Recovery tests let Postfix schedule a later delivery of the same queue ID. Containers run with external networking disabled and use disposable test credentials.

## Release workflow

Branch pushes and pull requests run formatting checks, vet, tests, and packaging on native Linux amd64 and arm64 runners. Stable version tags run the same checks and publish both archives and a `SHA256SUMS` file once both builds pass. Each archive includes the executable, the Postfix and environment examples, this README, the project license, and dependency notices.

To build a candidate archive locally on a supported Linux host, run:

```sh
bash scripts/package.sh v0.1.0 dist
```

Once the release criteria in `GOAL.md` are met, a maintainer can trigger publication by pushing a stable version tag. Use the `github-pavelsg` SSH alias for the `pavelsg` repository. For example, for a reviewed commit at HEAD:

```sh
git tag -a v0.1.0 -m 'Release v0.1.0'
git push git@github-pavelsg:pavelsg/postfix-ses-go.git refs/tags/v0.1.0
```

To verify a download, place your architecture's archive and `SHA256SUMS` in a directory and run this Linux command there before extracting:

```sh
sha256sum --check --ignore-missing SHA256SUMS
```

Check that the output names your downloaded archive and reports `OK`.

## License and attribution

Released under the [BSD 2-Clause License](LICENSE). If you use `postfix-ses-go` in a product or hosting service, we’d appreciate a link to this repository in your documentation or credits.
