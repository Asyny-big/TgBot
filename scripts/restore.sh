#!/usr/bin/env bash
# =============================================================================
# PostgreSQL restore — disaster recovery.
#
#   ./scripts/restore.sh backups/tgshop-20260130-021500Z.dump [--yes]
#
# This REPLACES the current database. The sequence matters:
#
#   1. verify the dump is readable before anything is touched;
#   2. stop the api and the bot, so nothing writes while the data is swapped
#      (PostgreSQL stays up: it is the thing being restored into);
#   3. restore inside a single transaction — a failure leaves the old data intact;
#   4. bring the schema to the current head, in case the dump predates a
#      migration that the running images already expect;
#   5. start the api and the bot again.
#
# The bot is stopped rather than left running because an incoming payment during
# a restore would be written into a database that is about to be replaced, and
# the buyer would lose their link.
# =============================================================================

# shellcheck source=scripts/_common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

usage() {
    cat >&2 <<'USAGE'
Usage: ./scripts/restore.sh <dump-file> [--yes]

  <dump-file>  A custom-format dump produced by scripts/backup.sh
  --yes        Do not ask for confirmation (for unattended recovery)
USAGE
    exit 2
}

DUMP=""
for argument in "$@"; do
    case "${argument}" in
        --yes) ASSUME_YES=1 ;;
        -h|--help) usage ;;
        -*) die "unknown option: ${argument}" ;;
        *)
            [[ -n "${DUMP}" ]] && die "only one dump file can be restored at a time."
            DUMP="${argument}"
            ;;
    esac
done
[[ -n "${DUMP}" ]] || usage
[[ -f "${DUMP}" ]] || die "dump file not found: ${DUMP}"

require_env_file
require_compose

POSTGRES_USER="$(env_value POSTGRES_USER)"
POSTGRES_DB="$(env_value POSTGRES_DB)"
[[ -n "${POSTGRES_USER}" ]] || die "POSTGRES_USER is not set in ${ENV_FILE}."
[[ -n "${POSTGRES_DB}" ]]   || die "POSTGRES_DB is not set in ${ENV_FILE}."

if [[ -f "${DUMP}.sha256" ]] && command -v sha256sum >/dev/null 2>&1; then
    log "checking the recorded checksum"
    (cd "$(dirname -- "${DUMP}")" && sha256sum --check --status "$(basename -- "${DUMP}").sha256") \
        || die "checksum mismatch — this dump is corrupt, do not restore it."
fi

if ! compose ps --status running --services | grep -qx postgres; then
    log "starting postgres"
    compose up -d postgres
    compose exec -T postgres sh -c 'until pg_isready -q; do sleep 1; done'
fi

log "verifying the dump can be read"
# The single quotes are deliberate: ${tmp} is expanded inside the container.
# shellcheck disable=SC2016
compose exec -T postgres sh -c '
    set -e
    tmp="$(mktemp)"
    trap "rm -f \"${tmp}\"" EXIT
    cat > "${tmp}"
    pg_restore --list "${tmp}" > /dev/null
' < "${DUMP}" >/dev/null 2>&1 || die "the dump is not a readable PostgreSQL archive."

confirm "Replace the contents of database '${POSTGRES_DB}' with ${DUMP}?" \
    || die "aborted; nothing was changed."

log "stopping the api and the bot"
compose stop api bot

log "restoring ${DUMP}"
# --clean --if-exists drops the existing objects first; --single-transaction makes
# the whole restore atomic, so a mid-way failure rolls back to the current data.
# --exit-on-error is implied by --single-transaction and stated for clarity.
if ! compose exec -T postgres \
        pg_restore \
            --username "${POSTGRES_USER}" \
            --dbname "${POSTGRES_DB}" \
            --clean --if-exists \
            --no-owner --no-privileges \
            --single-transaction \
            --exit-on-error \
        < "${DUMP}"; then
    warn "the restore failed and was rolled back; the previous data is still in place."
    log "starting the api and the bot again"
    compose up -d api bot
    die "restore failed."
fi

log "applying any migrations the dump predates"
compose run --rm migrations alembic upgrade head

log "starting the api and the bot"
compose up -d api bot

log "restore complete — check https://$(env_value TGSHOP_DOMAIN)/ and the bot's deep link"
