#!/usr/bin/env bash
set -euo pipefail

if [[ "${GITHUB_API_TEST_FAKE_GH:-}" == "1" ]]; then
  printf '%s\n' "$@"
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

invoke() {
  GITHUB_API_TEST_FAKE_GH=1 \
    GITHUB_GH_BIN="$SCRIPT_DIR/github-api.test.sh" \
    GITHUB_REPOSITORY=acme/widgets \
    "$SCRIPT_DIR/github-api.sh" "$@"
}

assert_line() {
  local output="$1"
  local expected="$2"
  if ! grep -Fqx -- "$expected" <<<"$output"; then
    printf 'FAIL: missing argument: %s\n' "$expected" >&2
    exit 1
  fi
}

ISSUE_OUTPUT="$(invoke issue 42)"
assert_line "$ISSUE_OUTPUT" api
assert_line "$ISSUE_OUTPUT" --method
assert_line "$ISSUE_OUTPUT" GET
assert_line "$ISSUE_OUTPUT" repos/acme/widgets/issues/42

WRITE_OUTPUT="$(invoke repo POST 'issues/42/comments' -f 'body=verified')"
assert_line "$WRITE_OUTPUT" POST
assert_line "$WRITE_OUTPUT" repos/acme/widgets/issues/42/comments
assert_line "$WRITE_OUTPUT" -f
assert_line "$WRITE_OUTPUT" body=verified

if GITHUB_API_TEST_FAKE_GH=1 \
  GITHUB_GH_BIN="$SCRIPT_DIR/github-api.test.sh" \
  GITHUB_REPOSITORY='invalid-coordinate' \
  "$SCRIPT_DIR/github-api.sh" issue 42 >/dev/null 2>&1; then
  printf 'FAIL: invalid repository coordinate must fail closed\n' >&2
  exit 1
fi

printf 'PASS: GitHub API wrapper preserves repository and arguments\n'
