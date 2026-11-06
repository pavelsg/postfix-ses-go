# postfix-ses-go

Build a standalone Go delivery agent that lets Postfix send selected outbound mail through the Amazon SES v2 HTTPS API. Postfix chooses the route and owns the queue. For each delivery, the executable reads the queued message, calls SES and tells Postfix whether to mark that recipient as handed off, retry later or report a permanent failure.

The intended users are Postfix operators, whether they configure their servers by hand, through a hosting panel or with configuration management. The project will publish source and versioned binaries on GitHub. The first release uses the standard Postfix pipe interface and runs on demand. Installing the binary should require neither a Go toolchain nor an AWS CLI on the mail host.

## Routing and invocation

Treat the command arguments, stdin format and exit statuses as a public interface. A binary upgrade should work with an existing Postfix configuration. Document a migration path for any incompatible change.

- Document exact-address opt-in using `sender_dependent_default_transport_maps`. A mailbox and each of its sending aliases can select the transport independently. Postfix continues to use its configured routes for unlisted senders and its local delivery transport for hosted recipients. Sender authorization and the sender map belong to the installation.
- Postfix invokes the binary as an unprivileged user, supplies the raw message on stdin and passes the envelope sender, one envelope recipient and the Postfix queue ID as distinct arguments. Set `<transport>_destination_recipient_limit = 1` so each invocation can return an outcome for exactly one recipient. The documented queue-ID macro requires Postfix 2.11 or later. [Postfix pipe interface](https://www.postfix.org/pipe.8.html)
- Keep the envelope and message headers distinct. Use the envelope recipient as the sole SES destination, including for blind copies. Preserve the visible author, display name, To/Cc headers, MIME body and attachments. Prevent disclosure of Bcc headers. The visible From header cannot authorize a different sending identity.
- Document how forwarding affects routing. A redirect can carry an opted-in envelope sender and another author's From header. The reference deployment keeps those redirects on its forwarding route and preserves the author. The distinction must be made where the installation knows the message is a redirect. Operators may use other routing policies.

## Delivery and failure handling

Use the AWS SDK for Go to issue one bounded SES v2 `SendEmail` request with raw MIME content per invocation. Disable automatic SDK retries for that send. Postfix owns subsequent attempts, including after a transport process exits or a host restarts. A failed API attempt must never silently switch the message to direct SMTP. [SES SendEmail API](https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_SendEmail.html)

| Outcome | What Postfix should do |
| --- | --- |
| SES accepts the request and returns a message ID | Mark this recipient as handed off. Log the Postfix queue ID and SES message ID together. Final delivery and later bounces are reported by SES. |
| Throttling, temporary service or network failure, or an operator-fixable credential or configuration problem | Defer delivery with a concise diagnostic so the queued message can be retried. |
| A definitive message-specific error, such as invalid message content | Report a permanent delivery failure for this recipient. |
| A timeout or lost response leaves acceptance uncertain | Defer and log the uncertain outcome with the queue ID. A retry can produce a duplicate if SES accepted the first request. |

Classify failures by their cause. An AWS client-error response can indicate either a bad message or a deployment problem. Document and test the Postfix exit statuses and diagnostics for both. [SES sending errors and ambiguous outcomes](https://docs.aws.amazon.com/ses/latest/dg/troubleshoot-error-messages.html)

## Deployment boundary and assumptions

**Operational assumptions.** The operator provides a verified SES sending identity, the intended AWS region, outbound HTTPS access and a runtime identity with send-only permissions. Existing SES identity defaults can supply the configuration set for tracking. Credential provisioning, DNS, IAM, event handling and mailbox management belong to the operator. The interface must work independently of any particular inventory, project schema or secrets file.

**First-release assumptions.** Use a short-lived executable with Postfix as the sole durable queue. Bound message size and request duration. Keep credentials out of command arguments, and exclude credentials, raw messages and attachments from logs. Publish Linux binaries for the architectures actually tested, with SHA-256 checksums so operators can verify, pin and roll back releases. Document the tested Postfix versions and platforms. Record every new implementation assumption explicitly and distinguish it from a confirmed requirement.

**License decision.** Use the [BSD 2-Clause License](LICENSE) and include it with source and binary distributions. Public project credit is appreciated and optional.

## Acceptance

A release should demonstrate all of the following:

1. A stock Postfix installation can invoke the published binary using documented `main.cf` and `master.cf` examples, with no hosting-specific software.
2. Tests with a real Postfix queue and a controlled SES API endpoint cover opted-in and unlisted senders, independently routed aliases, mixed local and external recipients, Bcc, MIME attachments and the documented forwarding policy.
3. Failure tests verify the Postfix queue outcome as well as the process exit status. Each invocation makes at most one send request, a permanent message error is distinguishable from a recoverable configuration error, and a lost acknowledgement is visible to the operator.
4. Routine tests run without live AWS credentials or external email delivery. A separately invoked live smoke test can establish acceptance by SES and check delivery and tracking with operator-designated resources.
