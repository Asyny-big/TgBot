#!/usr/bin/env bash
# =============================================================================
# Certificate renewal — meant for cron, twice a day.
#
#   ./scripts/renew-certs.sh
#
# Renewal runs through the *running* nginx: certbot writes the challenge file
# into the shared webroot volume, nginx serves it from
# /.well-known/acme-challenge/, and nothing has to stop. certbot only acts when a
# certificate is within 30 days of expiry, so running this often is free.
#
# nginx keeps a certificate open until it is told to re-read it, so the edge is
# reloaded — but only when the file actually changed, to avoid pointless reloads
# in the (usual) case where certbot decided renewal was not due yet.
# =============================================================================

# shellcheck source=scripts/_common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

require_env_file
require_compose

DOMAIN="$(env_value TGSHOP_DOMAIN)"
[[ -n "${DOMAIN}" ]] || die "TGSHOP_DOMAIN is not set in ${ENV_FILE}."

certificate="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"

fingerprint() {
    compose run --rm --entrypoint sh certbot -c \
        "[ -f '${certificate}' ] && sha256sum '${certificate}' | cut -d' ' -f1 || printf 'absent'" \
        2>/dev/null | tr -d '\r\n'
}

before="$(fingerprint)"
log "current certificate fingerprint: ${before}"

log "running certbot renew"
compose run --rm certbot renew \
    --webroot \
    --webroot-path /var/www/certbot \
    --non-interactive \
    --quiet

after="$(fingerprint)"

if [[ "${after}" == "absent" ]]; then
    die "no certificate for ${DOMAIN} — run scripts/tls-init.sh first."
fi

if [[ "${before}" == "${after}" ]]; then
    log "certificate unchanged; nginx was not reloaded"
    exit 0
fi

log "certificate was renewed; reloading nginx"
if compose ps --status running --services | grep -qx nginx; then
    compose exec nginx nginx -s reload
    log "nginx reloaded with the new certificate"
else
    warn "nginx is not running; it will pick up the new certificate when it starts"
fi
