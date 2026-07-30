#!/usr/bin/env bash
# =============================================================================
# First certificate issuance.
#
#   ./scripts/tls-init.sh [--staging]
#
# Run this BEFORE the first `docker compose up`. nginx refuses to start without
# the certificate files it is configured to load, and the certificate cannot be
# issued while nginx occupies port 80 — so the first issuance uses certbot's
# standalone mode, which binds port 80 itself for the few seconds of the ACME
# challenge. Renewals afterwards go through the running nginx (see renew-certs.sh)
# and never cause downtime.
#
# --staging uses Let's Encrypt's test environment: unlimited attempts, untrusted
# certificate. Use it to prove DNS and firewall are right before spending one of
# the five weekly attempts for the real domain.
# =============================================================================

# shellcheck source=scripts/_common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

STAGING=0
for argument in "$@"; do
    case "${argument}" in
        --staging) STAGING=1 ;;
        -h|--help) printf 'Usage: ./scripts/tls-init.sh [--staging]\n'; exit 0 ;;
        *) die "unknown option: ${argument}" ;;
    esac
done

require_env_file
require_compose

DOMAIN="$(env_value TGSHOP_DOMAIN)"
EMAIL="$(env_value TGSHOP_ACME_EMAIL)"
[[ -n "${DOMAIN}" ]] || die "TGSHOP_DOMAIN is not set in ${ENV_FILE}."
[[ -n "${EMAIL}" ]]  || die "TGSHOP_ACME_EMAIL is not set in ${ENV_FILE} (used for expiry warnings)."

if compose ps --status running --services | grep -qx nginx; then
    die "nginx is running and holds port 80. Stop it first: docker compose -f ${COMPOSE_FILE} stop nginx"
fi

# A domain that does not resolve to this host produces a confusing ACME error.
if command -v getent >/dev/null 2>&1; then
    resolved="$(getent ahosts "${DOMAIN}" | awk 'NR==1 {print $1}')" || resolved=""
    if [[ -z "${resolved}" ]]; then
        die "${DOMAIN} does not resolve — point the DNS A/AAAA record at this server first."
    fi
    log "${DOMAIN} resolves to ${resolved}"
fi

extra=()
if [[ "${STAGING}" == "1" ]]; then
    extra+=(--staging)
    log "using the Let's Encrypt STAGING environment — the certificate will not be trusted"
fi

log "requesting a certificate for ${DOMAIN}"
compose run --rm --publish 80:80 certbot certonly \
    --standalone \
    --non-interactive \
    --agree-tos \
    --email "${EMAIL}" \
    --domain "${DOMAIN}" \
    --key-type ecdsa \
    --rsa-key-size 3072 \
    "${extra[@]}"

log "certificate stored in the 'letsencrypt' volume; nginx can start now"
log "next: docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} up -d"
