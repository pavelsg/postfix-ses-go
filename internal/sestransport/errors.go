// Error classification for the SES v2 raw-send boundary.
package sestransport

import (
	"bytes"
	"errors"
	"net/http"
	"net/mail"
	"net/textproto"
	"regexp"
	"strings"

	"github.com/aws/smithy-go"
	smithyhttp "github.com/aws/smithy-go/transport/http"
)

// Recognize the complete reason only. Text appended by the provider can
// describe a different problem or contain sender-controlled mailbox data.
var duplicateHeaderReason = regexp.MustCompile(`(?i)^Duplicate header '([A-Za-z0-9-]+)'\.?$`)

// classifySendError maps only a confirmed client rejection to a known cause.
// With no conclusive rejection, a write may already have delivered the mail.
func classifySendError(err error, wroteHeaders bool, raw []byte) error {
	var response *smithyhttp.ResponseError
	if errors.As(err, &response) {
		status := response.HTTPStatusCode()
		switch {
		case status == http.StatusTooManyRequests:
			return ErrThrottled
		case status == http.StatusUnauthorized || status == http.StatusForbidden:
			return ErrConfiguration
		case status >= 300 && status < 400:
			return ErrTemporary
		case status >= 400 && status < 500 && status != http.StatusRequestTimeout:
			return classifyRejection(status, err, raw)
		}
	}
	if wroteHeaders {
		return ErrUncertain
	}
	return ErrTemporary
}

// classifyRejection combines the service error with the actual message when
// deciding to bounce. Unrecognized causes stay queued for operator review.
func classifyRejection(status int, err error, raw []byte) error {
	var api smithy.APIError
	if !errors.As(err, &api) {
		return ErrTemporary
	}
	switch api.ErrorCode() {
	case "TooManyRequestsException", "ThrottlingException", "Throttling":
		return ErrThrottled
	case "MailFromDomainNotVerifiedException", "SendingPausedException", "AccountSuspendedException",
		"NotFoundException", "AccessDeniedException", "ExpiredTokenException", "ExpiredToken",
		"InvalidClientTokenId", "UnrecognizedClientException", "SignatureDoesNotMatch":
		return ErrConfiguration
	case "MessageRejected", "BadRequestException", "InvalidParameterValue":
		// An unverified sending identity can produce the same MessageRejected
		// code as malformed content, including when the account is sandboxed.
		reason := api.ErrorMessage()
		if strings.HasPrefix(reason, "Email address is not verified.") {
			return ErrConfiguration
		}
		if status == http.StatusBadRequest && confirmsDuplicateHeader(reason, raw) {
			return ErrMessage
		}
	}
	return ErrTemporary
}

// confirmsDuplicateHeader checks the rejected field in the outer header block.
// Repetitions in body text, folds, or attached messages are insufficient.
func confirmsDuplicateHeader(reason string, raw []byte) bool {
	match := duplicateHeaderReason.FindStringSubmatch(reason)
	if match == nil {
		return false
	}
	key := textproto.CanonicalMIMEHeaderKey(match[1])
	// SES documents these fields as singletons. In particular, ordinary
	// messages can legitimately contain multiple Received fields.
	switch key {
	case "Subject", "Date", "Message-Id", "Mime-Version", "Content-Type", "Content-Transfer-Encoding":
		msg, err := mail.ReadMessage(bytes.NewReader(raw))
		return err == nil && len(msg.Header[key]) > 1
	}
	return false
}
