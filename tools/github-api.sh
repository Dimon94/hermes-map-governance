#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------
# [L3][COORD] repo://tools/github-api.sh
# [INPUT]  gh CLI 自有认证；GITHUB_REPOSITORY 显式 repository 坐标。
# [OUTPUT] GitHub REST/GraphQL response body，或不含凭据的认证确认。
# [POS]    仓库治理自动化 seam；不属于 product tracker runtime。
# [TEST]   repo://tools/github-api.test.sh
# [PROTOCOL] 变更时同步 repo://tools/AGENTS.md 与 repo://docs/agents/github-api-operations.md。
# ---------------------------------------------------------------------
# [L5] GITHUB_REPOSITORY  owner/name；默认 Dimon94/hermes-map-governance。
# [L5] GITHUB_GH_BIN      gh 可执行文件；测试可替换为 fake。
# ---------------------------------------------------------------------

REPOSITORY="${GITHUB_REPOSITORY:-Dimon94/hermes-map-governance}"
GH_BIN="${GITHUB_GH_BIN:-gh}"

usage() {
  cat >&2 <<'EOF'
Usage:
  tools/github-api.sh auth-check
  tools/github-api.sh repo METHOD PATH [gh api args...]
  tools/github-api.sh raw METHOD PATH [gh api args...]
  tools/github-api.sh issue NUMBER
  tools/github-api.sh pr NUMBER
  tools/github-api.sh runs
  tools/github-api.sh run RUN_ID
EOF
}

validate_repository() {
  if [[ ! "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    printf 'ERROR: GITHUB_REPOSITORY must use owner/name form\n' >&2
    exit 2
  fi
}

repo_path() {
  local api_path="${1#/}"
  if [[ -z "$api_path" ]]; then
    printf 'repos/%s' "$REPOSITORY"
  else
    printf 'repos/%s/%s' "$REPOSITORY" "$api_path"
  fi
}

api_call() {
  local method="$1"
  local api_path="$2"
  shift 2
  "$GH_BIN" api --method "$method" "$api_path" "$@"
}

main() {
  validate_repository
  local command="${1:-}"
  case "$command" in
    auth-check)
      [[ $# -eq 1 ]] || { usage; exit 2; }
      "$GH_BIN" auth status --hostname github.com >/dev/null
      printf 'ok: GitHub CLI authentication is available\n'
      ;;
    repo)
      [[ $# -ge 3 ]] || { usage; exit 2; }
      api_call "$2" "$(repo_path "$3")" "${@:4}"
      ;;
    raw)
      [[ $# -ge 3 ]] || { usage; exit 2; }
      api_call "$2" "${3#/}" "${@:4}"
      ;;
    issue)
      [[ $# -eq 2 && "$2" =~ ^[1-9][0-9]*$ ]] || { usage; exit 2; }
      api_call GET "$(repo_path "issues/$2")"
      ;;
    pr)
      [[ $# -eq 2 && "$2" =~ ^[1-9][0-9]*$ ]] || { usage; exit 2; }
      api_call GET "$(repo_path "pulls/$2")"
      ;;
    runs)
      [[ $# -eq 1 ]] || { usage; exit 2; }
      api_call GET "$(repo_path 'actions/runs?per_page=100')"
      ;;
    run)
      [[ $# -eq 2 && "$2" =~ ^[1-9][0-9]*$ ]] || { usage; exit 2; }
      api_call GET "$(repo_path "actions/runs/$2")"
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
