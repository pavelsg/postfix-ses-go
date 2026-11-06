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
cp -R examples/postfix "$work/config"

# Each fault gets an isolated queue and a peer with an independent request count.
if [[ $# -eq 0 ]]; then
    set -- defer handoff throttle configuration permanent unknown lost-ack interrupted local-content
fi
for scenario in "$@"; do
    case "$scenario" in
        defer|handoff) driver=check_queue.py ;;
        *) driver=failure_queue.py ;;
    esac
    docker run --rm --network none --hostname mx.example.test \
        --mount "type=bind,src=$work/postfix-ses-go,dst=/usr/local/libexec/postfix-ses-go.real,readonly" \
        --mount "type=bind,src=$work/config,dst=/opt/test/config,readonly" \
        "$image" "/opt/test/$driver" "$scenario"
done
