# postfix-ses-go

A Go delivery agent under development for Postfix operators who want to route selected outbound mail through the Amazon SES v2 HTTPS API. Postfix chooses the route and owns the queue. The planned agent submits one envelope recipient per invocation. The [project goals](https://github.com/pavelsg/postfix-ses-go/blob/HEAD/GOAL.md) describe routing, failure handling, and release acceptance criteria.

**Development status.** The pipe interface is implemented. Every delivery attempt currently exits with temporary failure (75). Use the examples on a test mail host until SES submission is implemented.

## Postfix pipe contract

```text
postfix-ses-go --version
postfix-ses-go ENVELOPE_SENDER ENVELOPE_RECIPIENT QUEUE_ID < message.eml
```

`--version` as the only argument prints the build version and exits 0. Delivery takes exactly three positional arguments. Pass the envelope sender even when it is empty (the null reverse path), one nonempty envelope recipient, and the Postfix queue ID as ASCII letters and digits. Envelope addresses are opaque arguments and must be free of control characters. The visible From, To and Cc headers belong to the message.

The reference pipe uses Postfix’s unquoted envelope address form (the `q` flag is omitted). For example, the queue and logs show `"a b"@outside.test`, and the executable receives `a b@outside.test` as a single argument with the space preserved. For a message addressed to two recipients on the same external domain, Postfix invokes the executable twice with the same queue ID and a different recipient argument each time.

Postfix writes the queued headers and body to stdin with CRLF line endings and closes the pipe at the end of the message. The current implementation reads and discards up to 10 MiB, probing one more byte to detect an oversized input. The reference transport sets a 30-second Postfix process time limit. These are initial implementation choices. A later SES request deadline must fit inside the transport limit.

| Phase 1 invocation | Exit | Output |
| --- | --- | --- |
| Standalone version query | 0 | Version on stdout |
| Valid delivery arguments and input within the limit | 75 | `4.3.0 postfix-ses-go: delivery is not implemented` on stderr |
| Invalid arguments, an input read error, or an oversized message | 75 | One `4.3.0` diagnostic on stderr identifying the cause |

The diagnostic can appear in a delivery status notification. It excludes message data and command arguments. Postfix retains a deferred recipient for retry under its normal queue lifetime policy. Future success requires an SES message ID, and a permanent failure requires a definitive message-specific cause.

### Try the transport on a stock Postfix test host

1. Install the Linux binary at `/usr/local/libexec/postfix-ses-go`, owned by root and executable by a dedicated unprivileged `sespipe` account. Create that account with a matching group, a disabled login, and no access to the Postfix queue.
2. Merge the settings from `examples/postfix/main.cf.fragment` and append the service from `examples/postfix/master.cf.fragment`. The service name `sespipe` sets the prefix for the time and one-recipient limits. The pipe uses `null_sender=` so an empty sender reaches the program as one empty argument. The queue-ID macro requires Postfix 2.11 or newer.
3. Install `examples/postfix/ses_senders` as `/etc/postfix/ses_senders`. Each anchored regular expression selects one full envelope sender address, using Postfix's default case-insensitive matching. Escape regex metacharacters in actual addresses, including dots and plus signs. Use a separate row for each opted-in alias. Omit domain-wide and null-sender entries. Run `postfix check`, then `postfix reload`.
4. Submit a test message from a listed sender to an external test recipient. Inspect `postqueue -p` for the queue ID, recipient and temporary failure diagnostic. Remove the test entry and reload Postfix before resuming normal routing.

The sender map selects the default outbound transport. Keep sender authorization and redirect handling in the installation that knows the origin of the message. Preserve existing local and forwarding routes when merging these examples. The examples require the `regexp` backend listed by `postconf -m`. Postfix reads this text map directly.

## Local development

From the repository root with Go 1.25 or later installed, run:

```sh
go test ./...
go vet ./...
go build -o bin/postfix-ses-go ./cmd/postfix-ses-go
./bin/postfix-ses-go --version
```

With Docker available, run `bash tests/postfix/test.sh` to exercise the real Postfix queue in a disposable container. The reference configuration has been exercised on Debian 12 with Postfix 3.7.11. The test uses synthetic messages and requires no AWS credentials or external mail delivery.

## Release workflow

Branch pushes and pull requests run formatting checks, vet, tests, and packaging on native Linux amd64 and arm64 runners. Stable version tags run the same checks and publish both archives and a `SHA256SUMS` file once both builds pass. Each archive includes the executable, the example Postfix configuration, this README, and the license.

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
