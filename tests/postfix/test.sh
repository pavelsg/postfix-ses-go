#!/usr/bin/env bash
# Run one network-isolated Postfix queue per scenario. With --packaged all
# executable and example inputs come from the already-verified release archives.
set -euo pipefail

if [[ ${1-} == --packaged ]]; then
    if [[ $# -lt 3 ]]; then
        echo 'Usage: test.sh --packaged CANDIDATE_DIR BASELINE_DIR [SCENARIO ...]' >&2
        exit 2
    fi
    candidate=$(cd -- "$2" && pwd -P)
    baseline=$(cd -- "$3" && pwd -P)
    shift 3
else
    candidate=
fi

cd -- "$(dirname -- "$0")/../.."
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
docker build --iidfile "$work/image" tests/postfix
image=$(cat "$work/image")
if [[ -n $candidate ]]; then
    cp "$candidate/postfix-ses-go" "$work/postfix-ses-go"
    cp -R "$candidate/examples/postfix" "$work/config"
    cp "$baseline/postfix-ses-go" "$work/baseline"
    cp -R "$baseline/examples/postfix" "$work/baseline-config"
else
    arch=$(docker image inspect "$image" --format '{{.Architecture}}')
    CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -o "$work/postfix-ses-go" ./cmd/postfix-ses-go
    cp -R examples/postfix "$work/config"
fi

if [[ $# -eq 0 ]]; then
    set -- defer handoff throttle configuration permanent unknown lost-ack interrupted local-content routing redirect
    if [[ -n $candidate ]]; then
        set -- "$@" upgrade-rollback
    fi
fi
for scenario in "$@"; do
    mounts=(
        --mount "type=bind,src=$work/postfix-ses-go,dst=/usr/local/libexec/postfix-ses-go.real,readonly"
        --mount "type=bind,src=$work/config,dst=/opt/test/config,readonly"
    )
    case "$scenario" in
        defer|handoff) driver=check_queue.py ;;
        routing) driver=routing_queue.py ;;
        redirect) driver=redirect_queue.py ;;
        upgrade-rollback)
            if [[ -z $candidate ]]; then
                echo 'upgrade-rollback requires --packaged CANDIDATE_DIR BASELINE_DIR' >&2
                exit 2
            fi
            driver=upgrade_queue.py
            # Existing operators keep their baseline configuration through both swaps.
            mounts=(
                --mount "type=bind,src=$work/baseline,dst=/opt/test/baseline,readonly"
                --mount "type=bind,src=$work/postfix-ses-go,dst=/opt/test/candidate,readonly"
                --mount "type=bind,src=$work/baseline-config,dst=/opt/test/config,readonly"
            )
            ;;
        *) driver=failure_queue.py ;;
    esac
    docker run --rm --network none --hostname mx.example.test \
        "${mounts[@]}" "$image" "/opt/test/$driver" "$scenario"
done
