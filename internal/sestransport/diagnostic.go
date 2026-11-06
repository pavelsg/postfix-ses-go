// Smoke diagnostics for the SES send boundary.
package sestransport

import (
	"errors"
	"fmt"
	"regexp"

	awshttp "github.com/aws/aws-sdk-go-v2/aws/transport/http"
	"github.com/aws/smithy-go"
	smithyhttp "github.com/aws/smithy-go/transport/http"
)

// sendFailure retains only the delivery classification and vetted smoke fields.
// The original SDK error can include signed URLs, addresses, or message data.
type sendFailure struct {
	outcome error
	detail  string
}

// Error exposes the same sender-safe diagnostic as the underlying classification.
func (e *sendFailure) Error() string { return e.outcome.Error() }

// Unwrap preserves the delivery decision made by callers using errors.Is.
func (e *sendFailure) Unwrap() error { return e.outcome }

// SmokeDiagnostic returns the stage and any safe SES response metadata for a
// failed Send call. An empty result means the error has no such diagnostic.
func SmokeDiagnostic(err error) string {
	var failure *sendFailure
	if errors.As(err, &failure) {
		return failure.detail
	}
	return ""
}

var requestIDPattern = regexp.MustCompile(`(?i)^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$`)

// responseFailure copies safe response metadata while the SDK error is in scope.
func responseFailure(outcome, sdkErr error) error {
	var response *awshttp.ResponseError
	if !errors.As(sdkErr, &response) {
		return &sendFailure{outcome, "stage=transport"}
	}
	var api smithy.APIError
	code := ""
	if errors.As(sdkErr, &api) {
		code = api.ErrorCode()
	}
	return newSESFailure(outcome, response.HTTPResponse(), code, response.ServiceRequestID())
}

// newSESFailure accepts only recognized error codes and well-formed request
// IDs. The SDK uses a zero-status response when no HTTP response arrived.
func newSESFailure(outcome error, response *smithyhttp.Response, code, requestID string) error {
	if response == nil || response.Response == nil || response.StatusCode < 100 || response.StatusCode > 599 {
		return &sendFailure{outcome, "stage=transport"}
	}
	switch code {
	case "AccessDenied", "AccessDeniedException", "AccountSuspendedException",
		"BadRequestException", "ExpiredToken", "ExpiredTokenException", "IncompleteSignature",
		"InvalidClientTokenId", "InvalidParameterValue", "InvalidSignatureException", "LimitExceededException",
		"MailFromDomainNotVerifiedException", "MessageRejected", "NotFoundException", "RequestExpired",
		"RequestTimeout", "SendingPausedException", "SignatureDoesNotMatch", "Throttling",
		"ThrottlingException", "TooManyRequestsException", "UnrecognizedClientException":
	default:
		code = "Unknown"
	}
	if !requestIDPattern.MatchString(requestID) {
		requestID = "unknown"
	}
	return &sendFailure{outcome, fmt.Sprintf("stage=ses http_status=%d aws_code=%s request_id=%s",
		response.StatusCode, code, requestID)}
}
