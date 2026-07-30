#!/usr/bin/env bash
# =============================================================================
# Pre-deployment configuration check.
#
#   ./scripts/preflight.sh
#
# Reads .env and reports everything that would break, or quietly weaken, a
# production deployment. The application validates its own settings on start-up,
# but it cannot see the things that live *between* components — that the webhook
# base URL matches the domain nginx serves, that the API prefix matches the path
# the edge rate limits, that a template placeholder was actually replaced.
#
# Exits non-zero when at least one error was found. Warnings do not fail the run.
# =============================================================================

# shellcheck source=scripts/_common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

require_env_file

errors=0
warnings=0

fail()  { printf '  [error] %s\n' "$*"; errors=$((errors + 1)); }
flag()  { printf '  [warn ] %s\n' "$*"; warnings=$((warnings + 1)); }
pass()  { printf '  [ ok  ] %s\n' "$*"; }

expect_equals() {
    local key="$1" expected="$2" reason="$3" actual
    actual="$(env_value "${key}")"
    if [[ "${actual}" == "${expected}" ]]; then
        pass "${key}=${expected}"
    else
        fail "${key} must be '${expected}' (found '${actual}') — ${reason}"
    fi
}

expect_present() {
    local key="$1" value
    value="$(env_value "${key}")"
    if [[ -z "${value}" ]]; then
        fail "${key} is empty"
    else
        pass "${key} is set"
    fi
}

expect_min_length() {
    local key="$1" minimum="$2" value
    value="$(env_value "${key}")"
    if [[ "${#value}" -lt "${minimum}" ]]; then
        fail "${key} must be at least ${minimum} characters (found ${#value})"
    else
        pass "${key} is long enough"
    fi
}

expect_not_placeholder() {
    local key="$1" value
    value="$(env_value "${key}")"
    if [[ "${value}" == *change-me* || "${value}" == *CHANGE-ME* ]]; then
        fail "${key} still contains the template placeholder"
    else
        pass "${key} was changed from the template"
    fi
}

printf '\nFile permissions\n'
if [[ "$(stat -c '%a' "${ENV_FILE}")" =~ ^6?00$ ]]; then
    pass "${ENV_FILE} is not readable by other users"
else
    flag "${ENV_FILE} is world- or group-readable; run: chmod 600 ${ENV_FILE}"
fi

printf '\nEnvironment\n'
expect_equals APP_ENVIRONMENT production "the app hardens itself and disables Swagger only in production"
expect_equals APP_DEBUG false "debug mode leaks internals and is rejected in production anyway"
expect_equals APP_LOG_FORMAT json "console logs are for humans at a terminal, not for a log collector"
expect_equals APP_API_PREFIX /api/v1 \
    "nginx rate limits the exact path /api/v1/auth/login; changing the prefix silently removes that limit"

printf '\nEdge\n'
expect_present TGSHOP_DOMAIN
expect_present TGSHOP_ACME_EMAIL
domain="$(env_value TGSHOP_DOMAIN)"
if [[ "${domain}" == http*://* ]]; then
    fail "TGSHOP_DOMAIN must be a bare host name, without a scheme (found '${domain}')"
    # Keep the checks below readable instead of quoting 'https://https://…' back
    # at whoever has to fix this.
    domain="${domain#http://}"
    domain="${domain#https://}"
fi

printf '\nTelegram\n'
expect_equals TELEGRAM_USE_WEBHOOK true "long polling is a development mode; production is served through nginx"
expect_not_placeholder TELEGRAM_BOT_TOKEN
expect_present TELEGRAM_BOT_USERNAME
expect_min_length TELEGRAM_WEBHOOK_SECRET 16
expect_not_placeholder TELEGRAM_WEBHOOK_SECRET

base_url="$(env_value TELEGRAM_WEBHOOK_BASE_URL)"
if [[ -n "${domain}" && "${base_url}" == "https://${domain}" ]]; then
    pass "TELEGRAM_WEBHOOK_BASE_URL matches TGSHOP_DOMAIN"
else
    fail "TELEGRAM_WEBHOOK_BASE_URL must be exactly 'https://${domain}' (found '${base_url}') — Telegram would post to a host nginx does not serve"
fi

webhook_path="$(env_value TELEGRAM_WEBHOOK_PATH)"
crypto_path="$(env_value CRYPTOBOT_WEBHOOK_PATH)"
for entry in "TELEGRAM_WEBHOOK_PATH=${webhook_path}" "CRYPTOBOT_WEBHOOK_PATH=${crypto_path}"; do
    if [[ "${entry#*=}" == /webhook/* ]]; then
        pass "${entry} is inside the /webhook/ prefix nginx forwards to the bot"
    else
        fail "${entry} is outside /webhook/ — nginx would send it to the admin API instead of the bot"
    fi
done
if [[ -n "${webhook_path}" && "${webhook_path}" == "${crypto_path}" ]]; then
    fail "TELEGRAM_WEBHOOK_PATH and CRYPTOBOT_WEBHOOK_PATH must differ"
fi

printf '\nCryptoBot\n'
expect_present CRYPTOBOT_API_TOKEN
if [[ "$(env_value CRYPTOBOT_NETWORK)" == "testnet" ]]; then
    flag "CRYPTOBOT_NETWORK=testnet — real USDT payments will not work"
else
    pass "CRYPTOBOT_NETWORK=mainnet"
fi

printf '\nDatabase and cache\n'
expect_not_placeholder POSTGRES_PASSWORD
expect_min_length POSTGRES_PASSWORD 16
expect_present REDIS_PASSWORD
expect_min_length REDIS_PASSWORD 16

printf '\nSecurity\n'
expect_min_length SECURITY_JWT_SECRET 32
expect_not_placeholder SECURITY_JWT_SECRET
expect_min_length SECURITY_ADMIN_PASSWORD 12
expect_not_placeholder SECURITY_ADMIN_PASSWORD
expect_equals SECURITY_COOKIE_SECURE true "the refresh cookie must never travel over plain HTTP"

cors="$(env_value SECURITY_CORS_ORIGINS)"
if [[ -z "${cors}" ]]; then
    pass "SECURITY_CORS_ORIGINS is empty — the panel is same-origin, so no cross-origin access is granted"
elif [[ "${cors}" == "https://${domain}" ]]; then
    flag "SECURITY_CORS_ORIGINS is set to the deployment's own origin; it is same-origin and the value can be left empty"
else
    flag "SECURITY_CORS_ORIGINS grants '${cors}' access to the admin API — remove it unless a separate front end really needs it"
fi

printf '\nSummary\n'
printf '  %d error(s), %d warning(s)\n\n' "${errors}" "${warnings}"

if [[ "${errors}" -gt 0 ]]; then
    printf 'Fix the errors above before deploying.\n' >&2
    exit 1
fi
printf 'Configuration looks deployable.\n'
