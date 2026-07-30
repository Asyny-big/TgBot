#!/usr/bin/env bash
# =============================================================================
# PostgreSQL backup.
#
#   ./scripts/backup.sh [destination-directory]
#
# Produces a compressed custom-format dump, verifies that it is readable, writes
# a checksum next to it, and deletes dumps older than the retention window.
#
# Custom format (-Fc) rather than plain SQL on purpose: it is compressed, it can
# be restored selectively, and pg_restore can list its contents — which is what
# makes verifying the dump possible without restoring it anywhere.
#
# Suitable for cron; every message goes to stdout/stderr with a UTC timestamp.
# =============================================================================

# shellcheck source=scripts/_common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

BACKUP_DIR="${1:-${BACKUP_DIR:-${REPO_ROOT}/backups}}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

require_env_file
require_compose

POSTGRES_USER="$(env_value POSTGRES_USER)"
POSTGRES_DB="$(env_value POSTGRES_DB)"
[[ -n "${POSTGRES_USER}" ]] || die "POSTGRES_USER is not set in ${ENV_FILE}."
[[ -n "${POSTGRES_DB}" ]]   || die "POSTGRES_DB is not set in ${ENV_FILE}."

mkdir -p "${BACKUP_DIR}"

timestamp="$(date -u '+%Y%m%d-%H%M%SZ')"
target="${BACKUP_DIR}/${POSTGRES_DB}-${timestamp}.dump"
partial="${target}.partial"

# A container that is not running would produce a zero-byte "backup" that looks
# like a success in cron output. Fail loudly instead.
if ! compose ps --status running --services | grep -qx postgres; then
    die "the postgres service is not running — start the stack before backing up."
fi

log "dumping database '${POSTGRES_DB}' to ${target}"

# -T: no TTY, so the binary dump reaches the file unmodified.
if ! compose exec -T postgres \
        pg_dump \
            --username "${POSTGRES_USER}" \
            --dbname "${POSTGRES_DB}" \
            --format=custom \
            --compress=9 \
            --no-owner \
            --no-privileges \
        > "${partial}"; then
    rm -f "${partial}"
    die "pg_dump failed; no backup was written."
fi

[[ -s "${partial}" ]] || { rm -f "${partial}"; die "pg_dump produced an empty file."; }

# Verify before publishing the file under its final name: a truncated dump must
# never end up as the newest thing in the backup directory. pg_restore --list
# parses the archive's table of contents, which is exactly the header a truncated
# or half-written dump fails to provide.
verify_dump() {
    if command -v pg_restore >/dev/null 2>&1; then
        pg_restore --list "$1" >/dev/null 2>&1
        return
    fi
    # No client tools on the host: verify with the ones inside the container.
    # The single quotes are deliberate: ${tmp} must be expanded by the shell
    # inside the container, not by this one.
    # shellcheck disable=SC2016
    compose exec -T postgres sh -c '
        set -e
        tmp="$(mktemp)"
        trap "rm -f \"${tmp}\"" EXIT
        cat > "${tmp}"
        pg_restore --list "${tmp}" > /dev/null
    ' < "$1" >/dev/null 2>&1
}

if ! verify_dump "${partial}"; then
    rm -f "${partial}"
    die "the dump is not readable by pg_restore; it was discarded."
fi

mv -- "${partial}" "${target}"
chmod 600 "${target}"

if command -v sha256sum >/dev/null 2>&1; then
    (cd "${BACKUP_DIR}" && sha256sum "$(basename -- "${target}")" > "$(basename -- "${target}").sha256")
fi

size="$(du -h -- "${target}" | cut -f1)"
log "backup complete: ${target} (${size})"

if [[ "${RETENTION_DAYS}" -gt 0 ]]; then
    deleted=0
    while IFS= read -r -d '' stale; do
        rm -f -- "${stale}" "${stale}.sha256"
        deleted=$((deleted + 1))
    done < <(find "${BACKUP_DIR}" -maxdepth 1 -name "${POSTGRES_DB}-*.dump" \
                  -type f -mtime "+${RETENTION_DAYS}" -print0)
    log "retention: removed ${deleted} dump(s) older than ${RETENTION_DAYS} days"
fi
