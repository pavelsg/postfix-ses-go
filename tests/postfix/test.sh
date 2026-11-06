#!/usr/bin/env bash
# Build for the test container's native architecture, then run an isolated queue.
set -euo pipefail

cd -- "$(dirname -- "$0")/../.."
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
docker build --iidfile "$work/image" tests/postfix
image=$(cat "$work/image")
arch=$(docker image inspect "$image" --format '{{.Architecture}}')
CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -o "$work/postfix-ses-go" ./cmd/postfix-ses-go

docker run --rm --network none --hostname mx.example.test \
    --mount "type=bind,src=$work/postfix-ses-go,dst=/usr/local/libexec/postfix-ses-go.real,readonly" \
    --mount "type=bind,src=$PWD/examples/postfix,dst=/opt/test/config,readonly" \
    "$image"
