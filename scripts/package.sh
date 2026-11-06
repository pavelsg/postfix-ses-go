#!/usr/bin/env bash
# Build and smoke-test one native Linux archive and write its SHA-256 sidecar.
# Usage: bash scripts/package.sh vMAJOR.MINOR.PATCH OUTPUT_DIRECTORY
set -euo pipefail

if [[ $# -ne 2 || -z $2 ]]; then
    echo "Usage: bash scripts/package.sh vMAJOR.MINOR.PATCH OUTPUT_DIRECTORY" >&2
    exit 2
fi

version=$1
number='(0|[1-9][0-9]*)'
if [[ ! $version =~ ^v${number}\.${number}\.${number}$ ]]; then
    echo "Expected a stable release tag such as v1.2.3." >&2
    exit 2
fi

if [[ $(go env GOHOSTOS) != linux ]]; then
    echo "Build release archives on a native Linux runner." >&2
    exit 1
fi
arch=$(go env GOHOSTARCH)
case "$arch" in
    amd64|arm64) ;;
    *) echo "Unsupported Linux architecture: $arch" >&2; exit 1 ;;
esac

mkdir -p -- "$2"
out_dir=$(cd -- "$2" && pwd)
repo_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd -- "$repo_dir"

# Stage on the output filesystem so a failed build cannot leave a partial asset.
stage=$(mktemp -d -- "$out_dir/.package.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
binary=postfix-ses-go
CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -trimpath -buildvcs=false \
    -ldflags "-X main.version=$version" -o "$stage/$binary" "./cmd/$binary"

actual_version=$("$stage/$binary" --version)
if [[ $actual_version != "$binary $version" ]]; then
    echo "Unexpected version in release binary: $actual_version" >&2
    exit 1
fi

# An empty required region exercises deferral without using host credentials.
delivery_status=0
diagnostic=$(printf 'From: sender@example.test\r\n\r\nfixture\r\n' |
    AWS_REGION= "$stage/$binary" sender@example.test recipient@example.test ABC123 2>&1) || delivery_status=$?
if [[ $delivery_status -ne 75 ]]; then
    echo "Expected a temporary delivery failure (75), got $delivery_status." >&2
    exit 1
fi
if [[ $diagnostic != '4.3.0 postfix-ses-go: SES configuration unavailable' ]]; then
    echo "Unexpected delivery diagnostic from release binary." >&2
    exit 1
fi

install -m 0644 LICENSE README.md .env.example "$stage/"
install -D -m 0644 scripts/live-smoke.py "$stage/scripts/live-smoke.py"
install -d "$stage/licenses"
aws_sdk=$(go list -m -f '{{.Dir}}' github.com/aws/aws-sdk-go-v2)
smithy=$(go list -m -f '{{.Dir}}' github.com/aws/smithy-go)
install -m 0644 "$aws_sdk/LICENSE.txt" "$stage/licenses/aws-sdk-go-v2-LICENSE"
install -m 0644 "$aws_sdk/NOTICE.txt" "$stage/licenses/aws-sdk-go-v2-NOTICE"
install -m 0644 "$smithy/LICENSE" "$stage/licenses/smithy-go-LICENSE"
install -m 0644 "$smithy/NOTICE" "$stage/licenses/smithy-go-NOTICE"
install -m 0644 "$(go env GOROOT)/LICENSE" "$stage/licenses/go-LICENSE"
install -d "$stage/examples/postfix/forwarding"
install -m 0644 examples/postfix/{main.cf.fragment,master.cf.fragment,ses_senders,mailbox-aliases.fragment} "$stage/examples/postfix/"
install -m 0644 examples/postfix/forwarding/aliases.fragment "$stage/examples/postfix/forwarding/"
archive="${binary}_${version}_linux_${arch}.tar.gz"
# Normalize ownership and timestamps so runner metadata stays out of releases.
tar -C "$stage" --format=ustar --owner=0 --group=0 --mtime=@0 \
    --sort=name -cf - "$binary" LICENSE README.md .env.example examples licenses scripts | gzip -n > "$stage/$archive"
(cd "$stage" && sha256sum "$archive" > "$archive.sha256")
mv -- "$stage/$archive" "$out_dir/$archive"
mv -- "$stage/$archive.sha256" "$out_dir/$archive.sha256"
printf '%s\n' "$out_dir/$archive"
