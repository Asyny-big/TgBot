#!/usr/bin/env bash
# =============================================================================
# PostgreSQL restore — disaster recovery.
#
#   ./scripts/restore.sh backups/tgshop-20260130-021500Z.dump [--yes]
#
# This REPLACES the current database. The sequence matters:
#
#   1. verify the dump is readable before anything is touched;
#   2. stop every service that writes to the database — the api and the bot,
#      and Daily Content's daily-bot and daily-worker wherever they run — so
#      nothing writes while the data is swapped (PostgreSQL stays up: it is the
#      thing being restored into);
#   3. restore inside a single transaction — a failure leaves the old data intact;
#   4. bring the schema to the current head, in case the dump predates a
#      migration that the running images already expect;
#   5. start the api and the bot again, and daily-bot and daily-worker if they
#      were running before, then reload nginx: a container started again may
#      come back on another address, and nginx resolved api and bot when it
#      started.
#
# The bot is stopped rather than left running because an incoming payment during
# a restore would be written into a database that is about to be replaced, and
# the buyer would lose their link. Daily Content is stopped for the same reason:
# the worker keeps sending and recording deliveries, and the Daily bot keeps
# recording subscriptions and content requests.
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

# Every service that writes to the database. daily-bot and daily-worker sit
# behind the `daily` profile: `--profile daily` puts them in the model whether or
# not .env enables it, and stopping one that was never deployed does nothing.
DATABASE_WRITERS=(api bot daily-bot daily-worker)

compose_daily() {
    compose --profile daily "$@"
}

# The Daily Content services running now — or restarting, which `restart:
# always` counts as running. Only they are started again after the restore: a
# stack without Daily Content stays without it.
running_daily_services() {
    compose_daily ps --status running --status restarting --services \
        | grep -xE 'daily-bot|daily-worker' || true
}

# Start what the restore stopped; nginx then re-resolves api and bot.
start_again() {
    log "starting ${RESTART[*]}"
    compose_daily up -d "${RESTART[@]}"
    if compose ps --status running --services | grep -qx nginx; then
        log "reloading nginx, so it resolves the restarted containers"
        compose exec -T nginx nginx -s reload \
            || warn "nginx did not reload; run 'make prod-reload' before trusting the edge."
    fi
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

mapfile -t DAILY_RUNNING < <(running_daily_services)
RESTART=(api bot ${DAILY_RUNNING[@]+"${DAILY_RUNNING[@]}"})

log "stopping everything that writes to the database: ${DATABASE_WRITERS[*]}"
compose_daily stop "${DATABASE_WRITERS[@]}"

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
    start_again
    die "restore failed."
fi

log "applying any migrations the dump predates"
# Nothing is started on a schema the images do not expect: on failure the
# services stay stopped, and the message says how to start them once it is fixed.
compose run --rm migrations alembic upgrade head \
    || die "the dump is restored, but 'alembic upgrade head' failed, so ${RESTART[*]} stay stopped. Fix the schema, then start them: docker compose -f docker-compose.prod.yml --profile daily up -d ${RESTART[*]} && make prod-reload"

start_again

log "restore complete — check https://$(env_value TGSHOP_DOMAIN)/ and the bot's deep link"
