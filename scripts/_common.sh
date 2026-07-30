#!/usr/bin/env bash
# shellcheck shell=bash
# =============================================================================
# Helpers shared by the operations scripts. Sourced, never executed directly.
# =============================================================================

set -euo pipefail

# Repository root, regardless of where the script was invoked from.
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT

ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker-compose.prod.yml}"

log()  { printf '%s  %s\n'  "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
warn() { printf '%s  WARN  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
die()  { printf '%s  ERROR %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

# Read one value from the env file.
#
# The file is *parsed*, not sourced: sourcing would execute it, and an unquoted
# value containing spaces (APP_NAME=Telegram Digital Shop) would be interpreted
# as a command. Only the last assignment of a key counts, which is what Docker
# Compose does too.
env_value() {
    local key="$1" line value
    line="$(grep -E "^[[:space:]]*${key}=" "${ENV_FILE}" | tail -n 1 || true)"
    [[ -z "${line}" ]] && return 0
    value="${line#*=}"
    # Strip one layer of matching quotes, mirroring Compose's own parsing.
    if [[ "${value}" == \"*\" && ${#value} -ge 2 ]]; then
        value="${value:1:${#value}-2}"
    elif [[ "${value}" == \'*\' && ${#value} -ge 2 ]]; then
        value="${value:1:${#value}-2}"
    fi
    printf '%s' "${value}"
}

require_env_file() {
    [[ -f "${ENV_FILE}" ]] || die "${ENV_FILE} not found — copy .env.example and fill it in."
}

require_compose() {
    command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH."
    docker compose version >/dev/null 2>&1 || die "the Docker Compose v2 plugin is required."
    [[ -f "${COMPOSE_FILE}" ]] || die "${COMPOSE_FILE} not found."
}

# Run docker compose against the production stack.
compose() {
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

confirm() {
    local prompt="$1" answer
    if [[ "${ASSUME_YES:-0}" == "1" ]]; then
        log "${prompt} — assuming yes (--yes)."
        return 0
    fi
    read -r -p "${prompt} [y/N] " answer
    [[ "${answer}" == "y" || "${answer}" == "Y" ]]
}
