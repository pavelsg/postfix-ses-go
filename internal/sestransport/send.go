// Package sestransport hands one prepared raw message to the SES v2 HTTPS API.
package sestransport

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptrace"
	"net/mail"
	"net/netip"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"

	"github.com/aws/aws-sdk-go-v2/aws"
	awshttp "github.com/aws/aws-sdk-go-v2/aws/transport/http"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/sesv2"
	"github.com/aws/aws-sdk-go-v2/service/sesv2/types"
	"github.com/aws/smithy-go/logging"
	smithyhttp "github.com/aws/smithy-go/transport/http"
)

// These errors are safe to map to diagnostics visible to Postfix and senders.
var (
	ErrConfiguration = errors.New("invalid SES configuration or credentials")
	ErrAddress       = errors.New("unsupported envelope address")
	ErrTemporary     = errors.New("SES handoff deferred")
	ErrUncertain     = errors.New("SES acceptance uncertain")
)

var (
	regionPattern = regexp.MustCompile(`^[a-z]+(-[a-z0-9]+)+-[0-9]+$`)
	domainLabel   = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$`)
	messageID     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._@-]{0,255}$`)
)

// Send uses the Postfix envelope identity and sole recipient for a single signed
// send. The caller must supply a deadline that includes credential lookup.
// Only a usable acknowledgement can complete a queued delivery.
func Send(ctx context.Context, sender, recipient string, raw []byte) (string, error) {
	from, err := mailbox(sender)
	if err != nil {
		return "", err
	}
	to, err := mailbox(recipient)
	if err != nil {
		return "", err
	}
	if len(raw) == 0 {
		return "", ErrTemporary
	}
	if _, ok := ctx.Deadline(); !ok {
		return "", ErrConfiguration
	}
	client, err := newClient(ctx)
	if err != nil {
		return "", err
	}
	// A transport failure before any request headers were written cannot have
	// submitted the message. After that point use the conservative outcome.
	var wroteHeaders atomic.Bool
	trace := &httptrace.ClientTrace{WroteHeaders: func() { wroteHeaders.Store(true) }}
	out, err := client.SendEmail(httptrace.WithClientTrace(ctx, trace), &sesv2.SendEmailInput{
		FromEmailAddress: aws.String(from),
		Destination:      &types.Destination{ToAddresses: []string{to}},
		Content:          &types.EmailContent{Raw: &types.RawMessage{Data: raw}},
	})
	if err != nil {
		var response *smithyhttp.ResponseError
		if errors.As(err, &response) && response.HTTPStatusCode() >= 300 && response.HTTPStatusCode() < 500 {
			return "", ErrTemporary
		}
		if wroteHeaders.Load() {
			return "", ErrUncertain
		}
		return "", ErrTemporary
	}
	id := aws.ToString(out.MessageId)
	if !messageID.MatchString(id) {
		return "", ErrUncertain
	}
	return id, nil
}

// newClient uses the SDK credential chain and CA configuration. The SES client
// receives only the endpoint validated here; global/profile endpoint overrides
// cannot silently reroute delivery.
func newClient(ctx context.Context) (*sesv2.Client, error) {
	region := os.Getenv("AWS_REGION")
	if !regionPattern.MatchString(region) {
		return nil, ErrConfiguration
	}
	var endpoint *string
	if value := os.Getenv("AWS_ENDPOINT_URL_SES"); value != "" {
		if !validEndpoint(value) {
			return nil, ErrConfiguration
		}
		endpoint = aws.String(value)
	}
	cfg, err := config.LoadDefaultConfig(ctx, config.WithRegion(region),
		config.WithHTTPClient(awshttp.NewBuildableClient()), config.WithLogger(logging.Nop{}))
	if err != nil {
		return nil, ErrConfiguration
	}
	// Resolve credentials before entering the send path: failures here have a
	// known outcome and can be fixed by the operator. The SDK caches the result.
	if _, err = cfg.Credentials.Retrieve(ctx); err != nil {
		return nil, ErrConfiguration
	}
	// LoadDefaultConfig preserves our BuildableClient and applies AWS_CA_BUNDLE.
	// A fresh HTTP/1 connection avoids connection-pool and HTTP/2 stream replays.
	tr := cfg.HTTPClient.(*awshttp.BuildableClient).GetTransport()
	tr.Protocols = new(http.Protocols)
	tr.Protocols.SetHTTP1(true)
	tr.DisableKeepAlives = true
	httpClient := &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	return sesv2.NewFromConfig(cfg, func(o *sesv2.Options) {
		o.BaseEndpoint = endpoint
		o.Retryer = aws.NopRetryer{}
		o.HTTPClient = httpClient
	}), nil
}

// validEndpoint requires an HTTPS origin. Request paths are owned by the SES
// serializer, and authentication belongs to the SDK credential chain.
func validEndpoint(value string) bool {
	u, err := url.Parse(value)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil ||
		strings.ContainsAny(value, "?#") || (u.EscapedPath() != "" && u.EscapedPath() != "/") ||
		strings.HasSuffix(u.Host, ":") {
		return false
	}
	host := u.Hostname()
	if strings.HasPrefix(u.Host, "[") {
		ip, err := netip.ParseAddr(host)
		if err != nil || !ip.Is6() || ip.Zone() != "" {
			return false
		}
	} else if !validDomain(strings.TrimSuffix(host, ".")) {
		return false
	}
	if p := u.Port(); p != "" {
		n, err := strconv.ParseUint(p, 10, 16)
		return err == nil && n != 0
	}
	return true
}

// validDomain accepts ASCII DNS labels, including pre-encoded IDNs.
func validDomain(host string) bool {
	if len(host) > 253 {
		return false
	}
	for _, label := range strings.Split(host, ".") {
		if !domainLabel.MatchString(label) {
			return false
		}
	}
	return true
}

// mailbox serializes Postfix's unquoted internal mailbox as a single ASCII
// addr-spec. The last @ separates the domain from a possibly quoted local
// part. net/mail adds the quotes and escapes that the wire form requires.
func mailbox(value string) (string, error) {
	at := strings.LastIndexByte(value, '@')
	if at < 1 || len(value) > 254 {
		return "", ErrAddress
	}
	for i := 0; i < at; i++ {
		if value[i] < ' ' || value[i] > '~' {
			return "", ErrAddress
		}
	}
	if !validDomain(value[at+1:]) {
		return "", ErrAddress
	}
	nameAddr := (&mail.Address{Address: value}).String()
	address := nameAddr[1 : len(nameAddr)-1] // net/mail adds outer angle brackets.
	// SMTP limits apply to the serialized form, including quotes and escapes.
	if len(address) > 254 || strings.LastIndexByte(address, '@') > 64 {
		return "", ErrAddress
	}
	return address, nil
}
