# postfix-ses-go

A Go delivery agent under development for Postfix operators who want to route selected outbound mail through the Amazon SES v2 HTTPS API. Postfix chooses the route and owns the queue. The planned agent submits one envelope recipient per invocation. The [project goals](https://github.com/pavelsg/postfix-ses-go/blob/HEAD/GOAL.md) describe routing, failure handling, and release acceptance criteria.

**Development status.** The scaffold supports `--version`. Delivery invocations exit with temporary failure (75) so Postfix can keep the recipient queued. SES delivery and real Postfix acceptance tests are pending.

## Local development

From the repository root with Go 1.25 or later installed, run:

```sh
go test ./...
go vet ./...
go build -o bin/postfix-ses-go ./cmd/postfix-ses-go
./bin/postfix-ses-go --version
```

## Release workflow

Branch pushes and pull requests run formatting checks, vet, tests, and packaging on native Linux amd64 and arm64 runners. Stable version tags run the same checks and publish both archives and a `SHA256SUMS` file once both builds pass. Each archive includes the executable, this README, and the license.

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
