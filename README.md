# postfix-ses-go

A Go delivery agent under development for Postfix operators who want to route selected outbound mail through the Amazon SES v2 HTTPS API. Postfix chooses the route and owns the queue. The agent submits one envelope recipient per invocation. The [project goals](https://github.com/pavelsg/postfix-ses-go/blob/HEAD/GOAL.md) describe routing, failure handling, and release acceptance criteria.

**Development status.** The binary submits raw mail through the SES v2 HTTPS API. Release qualification exercises the packaged Linux binaries with a real Postfix queue, including upgrade and rollback. A separate opt-in smoke test checks live SES acceptance, mailbox delivery, and event tracking using an operator's resources. Test installations with real AWS credentials and an opted-in sender can send real mail.

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

### Routing a mailbox and its sending aliases

The sender map applies to external recipients that use the default transport. Postfix selects the local delivery transport for hosted recipients in `mydestination`. A recipient-specific transport override can take precedence, so check the installation’s existing `transport_maps` when adopting the example. The `regexp` backend must be listed by `postconf -m`.

For a site hosting `example.test` with a local mailbox named `sender`, merge `examples/postfix/mailbox-aliases.fragment` into the local aliases database (typically `/etc/aliases`) and run `newaliases`. It makes `billing` and `support` incoming aliases of the same mailbox. Outbound opt-in is a separate decision. The supplied `ses_senders` lists `sender@example.test` and `billing@example.test`. Each address must be authorized by the installation before submission.

| Envelope sender | Envelope recipients | Delivery with the example maps |
| --- | --- | --- |
| `sender@example.test` or `billing@example.test` | External recipient | One SES handoff for that recipient |
| `support@example.test`, an unlisted mailbox, or `sender+tag@example.test` | External recipient | The site's default outbound route |
| `sender@example.test` | A hosted mailbox and an external recipient in the same message | Postfix delivers the local copy and invokes the SES pipe only for the external recipient |

### Redirecting a local alias over SMTP

The optional `examples/postfix/forwarding/aliases.fragment` configures a hosted `redirect` alias and its companion `owner-redirect`. Merge the entries into the local aliases database, adapt the external target, and point the owner alias to a monitored local mailbox. Run `newaliases`. This policy assumes stock `local(8)` alias expansion with `expand_owner_alias = no`, a locally deliverable owner address at `$myorigin`, and a default outbound SMTP route. Keep that owner address outside the SES sender map.

When Postfix expands `redirect`, it assigns `owner-redirect@$myorigin` as the envelope sender of the forwarded copy. That copy takes the site's ordinary SMTP route and retains the author's From header. Delivery failures go to the monitored owner mailbox. Direct mail from the opted-in sender to the very same external target still uses SES.

This example applies to aliases expanded by `local(8)`. For virtual aliases or panel-managed redirects, apply the forwarding policy where that installation recognizes the redirect. A sender map alone sees only the resulting envelope sender. [Postfix local delivery](https://www.postfix.org/local.8.html) · [Postfix owner-alias settings](https://www.postfix.org/postconf.5.html#expand_owner_alias)

## Local development

From the repository root with Go 1.25 or later installed, run:

```sh
go test ./...
go vet ./...
go build -o bin/postfix-ses-go ./cmd/postfix-ses-go
./bin/postfix-ses-go --version
```

With Docker available, run `bash tests/postfix/test.sh` for the full queue suite, or `bash tests/postfix/test.sh routing redirect` for routing alone. The fixture installs the configuration examples into Debian 12 with Postfix 3.7.11. A loopback SMTP receiver, a controlled SES HTTPS endpoint and local mailboxes show where each recipient's copy went. The containers use disposable credentials and run with external networking disabled.

## Release workflow

### Qualify a Linux release

On native Linux amd64 and arm64, CI builds the versioned archive, verifies its SHA-256 checksum and exercises the extracted binary **and its shipped configuration** on Debian 12 with Postfix 3.7.11. Each runner also checks that changing the archive makes checksum verification fail. CI assembles both qualified archives and their combined `SHA256SUMS` into the downloadable `release-bundle` workflow artifact. Stable release tags publish those qualified bytes to GitHub Releases after both architectures pass.

To reproduce one architecture's qualification, use a native Linux checkout with Go, Docker, and Git history containing the baseline commit:

```sh
bash scripts/qualify.sh v0.0.1 dist
```

Until the first release, the rollback baseline is built separately from the merged phase 4 commit `4aa13814c5a8c282f01f1995c74adbec84d3faf8`. The baseline and candidate get distinct test version labels. The rehearsal defers a recipient, installs the candidate, and lets Postfix retry the same queue ID. It repeats in the other direction with mail queued under the candidate. The controlled endpoint checks the message bytes on each attempt. When a published release becomes the supported upgrade baseline, update the pinned commit and test version in the qualification script.

To publish an approved stable version after meeting the release criteria in `GOAL.md`, tag the reviewed commit and push the tag through `github-pavelsg`. For example:

```sh
git tag -a v0.1.0 -m 'Release v0.1.0'
git push git@github-pavelsg:pavelsg/postfix-ses-go.git refs/tags/v0.1.0
```

### Verify and install an archive

Use a fresh directory and obtain the pinned archive and `SHA256SUMS` from the same trusted release or CI artifact. For the complete two-architecture bundle, run `sha256sum --strict --check SHA256SUMS`. For a single downloaded archive, use the following in a Bash shell on Linux. Set `version` to the version you downloaded.

```bash
set -eu
version=v0.0.1
case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) echo "Unsupported architecture" >&2; exit 1 ;;
esac
archive="postfix-ses-go_${version}_linux_${arch}.tar.gz"
actual=$(sha256sum -- "$archive")
grep -Fx -- "$actual" SHA256SUMS
mkdir "release-$version"
tar -xzf "$archive" -C "release-$version"
"release-$version/postfix-ses-go" --version
```

For a regular-file installation at the reference pipe path, keep the verified previous release in its own directory. Record any affected queue IDs with `postqueue -p`. Run the replacement below as the administrator, from the directory containing the verified release trees:

```bash
set -eu
version=v0.0.1
binary=/usr/local/libexec/postfix-ses-go
test -d /usr/local/libexec
staged=$(mktemp /usr/local/libexec/.postfix-ses-go.XXXXXXXX)
trap 'rm -f -- "$staged"' EXIT
install -o root -g root -m 0755 "release-$version/postfix-ses-go" "$staged"
cmp "release-$version/postfix-ses-go" "$staged"
test "$("$staged" --version)" = "postfix-ses-go $version"
mv -fT -- "$staged" "$binary"
"$binary" --version
```

The same-directory rename activates the binary for the next pipe invocation. A running invocation can finish with its original binary. **To roll back**, repeat the replacement with the verified previous version. Keep the pipe path, Postfix configuration, and queue files in place in both directions. Follow the original queue IDs in the log. Before forcing a retry, inspect any uncertain SES acceptance because a second send can duplicate delivery.

### Opt-in live SES smoke test

Use the verified Linux binary, Python 3.9 or newer, a send-only AWS identity, a verified sender in the chosen region, and a mailbox you control. Configure the SES sending identity's default configuration set to publish Delivery events to a destination you can inspect. The send and evidence-verification steps are separate. The [SES event format](https://docs.aws.amazon.com/ses/latest/dg/event-publishing-retrieving-sns-contents.html) describes the message ID assigned at acceptance and the recipients confirmed by the remote mail server.

From the extracted package directory, substitute your own region and plain ASCII sender and recipient addresses. Leave the custom SES endpoint unset. The following command invokes the packaged binary directly with a unique smoke marker as its queue ID. It sends **one real message** and saves the acceptance receipt:

```sh
python3 scripts/live-smoke.py send --send-live \
    --binary ./postfix-ses-go --region us-east-1 \
    --sender sender@example.test --recipient inbox@example.test > receipt.json
```

On a temporary failure, the smoke driver can report which stage failed. `stage=setup` points to local SDK configuration or credential lookup. `stage=transport` indicates that the SDK has no HTTP response to inspect. For an SES response, look for the HTTP status, AWS error code, and request ID. These fields come from the current attempt and can help diagnose a rejection even without a CloudTrail record. The driver enables this detail only for its child process.

Export the received message from the designated mailbox as `received.eml` and the corresponding SES Delivery event as `delivery.json`. For SNS, save the decoded inner `Message` JSON object. Check those files against the saved receipt offline:

```sh
python3 scripts/live-smoke.py verify --receipt receipt.json \
    --message received.eml --event delivery.json
```

Add `--configuration-set NAME` to the verification command to check the event's configuration-set tag. A receipt records SES acceptance. A passing verification additionally matches the mailbox copy and the Delivery event to the probe. If acceptance was uncertain, investigate the existing attempt before running another send. Routine CI uses test credentials, local endpoints, and offline smoke fixtures.

## License and attribution

Released under the [BSD 2-Clause License](LICENSE). If you use `postfix-ses-go` in a product or hosting service, we’d appreciate a link to this repository in your documentation or credits.
