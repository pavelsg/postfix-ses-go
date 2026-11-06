#!/usr/bin/env bash
# Qualify the native Linux archive and rehearse replacing a known earlier build.
# Usage: bash scripts/qualify.sh vMAJOR.MINOR.PATCH OUTPUT_DIRECTORY
set -euo pipefail

if [[ $# -ne 2 || -z $2 ]]; then
    echo "Usage: bash scripts/qualify.sh vMAJOR.MINOR.PATCH OUTPUT_DIRECTORY" >&2
    exit 2
fi
version=$1
mkdir -p -- "$2"
out_dir=$(cd -- "$2" && pwd)
cd -- "$(dirname -- "$0")/.."
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

bash scripts/package.sh "$version" "$work/candidate"
arch=$(go env GOHOSTARCH)
archive="postfix-ses-go_${version}_linux_${arch}.tar.gz"

# Anchor the checksum to the expected basename as well as the archive bytes.
verify_archive() {
    local dir=$1 file=$2
    (cd "$dir" && sha256sum "$file" | cmp - "$file.sha256")
}
verify_archive "$work/candidate" "$archive"
mkdir "$work/damaged"
cp -- "$work/candidate/$archive" "$work/candidate/$archive.sha256" "$work/damaged/"
printf 'damaged' >> "$work/damaged/$archive"
if verify_archive "$work/damaged" "$archive" >/dev/null; then
    echo "Qualification accepted a damaged archive." >&2
    exit 1
fi

# Before the first public release, use the merged phase 4 source as the
# compatibility baseline. Its package script predates checksum fragments.
baseline_commit=4aa13814c5a8c282f01f1995c74adbec84d3faf8
baseline_version=v0.0.0
if [[ $version == "$baseline_version" ]]; then
    baseline_version=v0.0.1
fi
mkdir "$work/baseline-source"
git archive "$baseline_commit" | tar -xf - -C "$work/baseline-source"
bash "$work/baseline-source/scripts/package.sh" "$baseline_version" "$work/baseline"
baseline_archive="postfix-ses-go_${baseline_version}_linux_${arch}.tar.gz"
(cd "$work/baseline" && sha256sum "$baseline_archive" > "$baseline_archive.sha256")
verify_archive "$work/baseline" "$baseline_archive"

# Run the archived executable; inspect its actual build settings in addition
# to exercising it, since some hosts can transparently emulate another arch.
extract_package() {
    local dir=$1 file=$2 expected=$3
    mkdir "$dir/extracted"
    tar -xzf "$dir/$file" -C "$dir/extracted"
    local binary="$dir/extracted/postfix-ses-go"
    [[ $("$binary" --version) == "postfix-ses-go $expected" ]]
    go version -m "$binary" > "$dir/build-info"
    for setting in GOOS=linux "GOARCH=$arch" CGO_ENABLED=0; do
        grep -Fxq $'\tbuild\t'"$setting" "$dir/build-info"
    done
}
extract_package "$work/candidate" "$archive" "$version"
extract_package "$work/baseline" "$baseline_archive" "$baseline_version"
candidate="$work/candidate/extracted"
diff -r examples "$candidate/examples"
for file in LICENSE README.md .env.example scripts/live-smoke.py; do
    cmp "$file" "$candidate/$file"
done
for notice in aws-sdk-go-v2-{LICENSE,NOTICE} smithy-go-{LICENSE,NOTICE} go-LICENSE; do
    test -s "$candidate/licenses/$notice"
done
bash tests/postfix/test.sh --packaged "$candidate" "$work/baseline/extracted"

verify_archive "$work/candidate" "$archive"
mv -- "$work/candidate/$archive" "$work/candidate/$archive.sha256" "$out_dir/"
printf 'Qualified %s on linux/%s against %s (%s)\n' "$archive" "$arch" "$baseline_commit" "$baseline_version"
