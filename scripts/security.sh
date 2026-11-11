#!/bin/sh
# Security checks, run by the pre-push hook and by CI (.github/workflows/security.yml).
#
#   scripts/security.sh               every check, secrets over the whole history
#   scripts/security.sh <git range>   secrets only over that range (pre-push)
#
# Each tool is fetched on the fly (uvx, or a container when no binary is on
# PATH), so none of them weighs on the project's dependencies. Every finding
# is blocking; a false positive gets a `nosemgrep: <rule>` (or the tool's own
# marker) on its line, with the reason beside it.
set -eu

cd "$(dirname "$0")/.."
RANGE="${1:-}"
failed=""

# A tool from its binary, else from its image (podman, else docker). The
# checkout is mounted at its own path, and so is the repository's git
# directory, which a worktree's .git points to from outside the checkout.
container() {
	image=$1
	shift
	engine=$(command -v podman || command -v docker || true)
	if [ -z "$engine" ]; then
		echo "security: neither podman nor docker to run $image" >&2
		return 1
	fi
	common=$(cd "$(git rev-parse --git-common-dir)" && pwd)
	"$engine" run --rm -i -v "$PWD:$PWD:ro,z" -v "$common:$common:ro,z" -w "$PWD" \
		-e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
		"$image" "$@"
}

run() {
	name=$1
	shift
	echo "--> $name"
	if ! "$@"; then
		failed="$failed $name"
	fi
}

# Known vulnerabilities in the locked dependencies (OSV / PyPI advisories).
pip_audit() {
	requirements=$(mktemp)
	trap 'rm -f "$requirements"' EXIT
	uv export --frozen --format requirements-txt --no-emit-project --quiet >"$requirements"
	uvx --quiet pip-audit --strict --disable-pip --progress-spinner off -r "$requirements"
}

# Secrets committed, in the pushed range or the whole history.
gitleaks_scan() {
	if [ -n "$RANGE" ]; then set -- --log-opts="$RANGE"; else set --; fi
	if command -v gitleaks >/dev/null; then
		gitleaks git --no-banner --redact "$@" .
	else
		container ghcr.io/gitleaks/gitleaks:v8.30.1 git --no-banner --redact "$@" .
	fi
}

# Injection, XSS, Flask misconfiguration, secrets: Python and our own JS
# (static/lib is vendored, excluded in .semgrepignore), plus our own rules
# in .semgrep/ for the sinks the registry does not follow.
semgrep_scan() {
	uvx --quiet semgrep scan --metrics=off --quiet --error \
		--config p/python --config p/flask --config p/owasp-top-ten \
		--config p/javascript --config p/secrets --config .semgrep
}

# The Containerfile (.hadolint.yaml holds the reasoned exceptions).
hadolint_scan() {
	if command -v hadolint >/dev/null; then
		hadolint Containerfile
	else
		container docker.io/hadolint/hadolint:v2.15.1 hadolint Containerfile
	fi
}

# GitHub workflows: unpinned actions, injection through expressions, tokens.
zizmor_scan() {
	uvx --quiet zizmor --offline .github/workflows
}

run gitleaks gitleaks_scan
run pip-audit pip_audit
run semgrep semgrep_scan
run hadolint hadolint_scan
run zizmor zizmor_scan

if [ -n "$failed" ]; then
	echo "security: failed:$failed" >&2
	exit 1
fi
echo "security: ok"
