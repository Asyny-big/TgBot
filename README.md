# Telegram Digital Shop

Telegram bot that sells digital goods through **deep links only**. There is no
catalog, no menu and no product list: a link such as
`https://t.me/MyShopBot?start=vip1` opens exactly one product card. The buyer
pays with **Telegram Stars** or **CryptoBot (USDT)** and instantly receives the
delivery link. Files are never stored — only the link is.

If the same Telegram user opens the deep link again after a successful payment,
the link is returned immediately, without a second charge.

**Contents** — [Stack](#stack) · [Architecture](#architecture) ·
[Data model](#data-model) · [Bot](#bot) · [Admin API](#admin-api) ·
[Admin panel](#admin-panel) · [Performance](#performance) ·
[Local development](#local-development) ·
[Production deployment](#production-deployment) · [Operations](#operations) ·
[Troubleshooting](#troubleshooting)

## Stack

| Layer | Technology |
| --- | --- |
| Bot | aiogram 3 (webhook in production, long polling locally) |
| API | FastAPI, Pydantic v2, JWT auth |
| Data | PostgreSQL 16, SQLAlchemy 2 (async), Alembic |
| Cache / locks | Redis 7 |
| Admin UI | Vue 3, TypeScript, Vite |
| Runtime | Docker, Docker Compose, Nginx |
| Observability | structlog (JSON logs, request correlation) |

## Architecture

Clean Architecture with an explicit dependency direction — the domain layer
knows nothing about SQLAlchemy, aiogram or HTTP.

```
backend/app/
├── core/            configuration, logging, resources, exceptions
├── domain/          entities, value objects, repository interfaces
├── infrastructure/  SQLAlchemy repositories, Redis, payment gateways
├── services/        use cases (products, purchases, payments, statistics)
├── api/             FastAPI routers, dependencies, error envelope
└── bot/             aiogram handlers, keyboards, middlewares
```

Three processes, one image: `api` (admin API), `bot` (Telegram + payments +
background workers) and `migrations` (one-shot). The edge is a separate image:
nginx with the compiled admin panel baked in.

## Data model

Three tables; money-critical rules are database constraints, not conventions.

| Table | Purpose | Key invariants |
| --- | --- | --- |
| `products` | one digital good per deep-link slug | unique `slug` matching Telegram's payload rules, at least one price set, prices strictly positive |
| `users` | Telegram profile snapshot | `telegram_id` as primary key, case-insensitive username index for admin search |
| `purchases` | one attempt to buy one product | unique `(provider, external_id)`, partial unique `(user_id, product_id)` while paid or delivered, unique Telegram charge id, delivered rows must carry a link and a timestamp |

Prices are independent per rail: `price_stars` (integer XTR) and `price_usdt`
(`NUMERIC(12,2)`). A rail without a price is simply not offered on the card.
Purchases store the amount actually charged, so later price changes never
rewrite history. Every amount is `NUMERIC` and every timestamp is timezone aware.

Statuses: `pending → paid → delivered`, plus `refunded` (Stars refund revokes
access) and `expired` (unpaid invoice). Money that arrives after expiry is still
honoured; a refunded purchase can never be re-paid.

The admin panel's search is served by `pg_trgm` GIN indexes on the columns it
looks through, because no B-tree can answer an unanchored `ILIKE '%term%'`. See
[Performance](#performance) for what that is worth in milliseconds.

## Service layer

Use cases live in `backend/app/services`, wired once in `app/core/container.py`.
Responsibilities are deliberately split:

| Service | Owns | Never does |
| --- | --- | --- |
| `ProductService` | validation, CRUD, deep links | payments |
| `PurchaseService` | purchase lifecycle, payment confirmation, repeat-purchase check | sending messages |
| `DeliveryService` | sending the link, retries, delivery logging, confirming delivery | purchase rules |
| `StatsService` | dashboard aggregates and purchase search | writes |
| `AuthService` | admin login, JWT issue/rotate/revoke | anything else |

The payment flow is explicitly staged, so business rules and transport stay apart:

```
payment confirmed
    → PurchaseService.confirm_payment()      (money recorded, nothing sent)
    → DeliveryService.deliver_purchase()     (send + retry with back-off)
    → PurchaseService.mark_delivered()       (delivery recorded)
```

A failed delivery leaves the purchase `paid`, so the buyer keeps the right to the
link and the next attempt (or the next `/start`) hands it over. No database
transaction is ever held open while Telegram is being awaited.

### Concurrency

Two independent guards, because either one alone can be bypassed:

* **Redis locks with a mandatory TTL** (`REDIS_LOCK_TTL_SECONDS`, default 45s)
  serialise invoice creation, payment confirmation and delivery. Every lock
  expires on its own, so a crashed worker cannot block a buyer, and release is a
  compare-and-delete that cannot free somebody else's lock.
* **Database constraints** make the rules absolute: one invoice per purchase, one
  paid copy per buyer, one Telegram charge recorded once.

Covered by tests against real PostgreSQL and real Redis: double `/start`, ten
replayed payment webhooks, five parallel deliveries, two competing invoices for
one product, and two buyers who must not block each other.

## Bot

One process (`python -m app.bot`) owns everything Telegram: updates, payments and
delivery. It runs long polling locally and an aiohttp webhook server in
production, where nginx forwards two paths to it — `/webhook/telegram` (secret
token checked) and `/webhook/cryptobot` (HMAC signature checked). It also serves
`/internal/health` on the same port for the container healthcheck; nginx never
publishes that path.

`SIGTERM` shuts the process down cleanly: the background workers are cancelled,
the Telegram session and the Crypto Pay client are closed, and the pools are
released.

### Purchase flow

```
/start <slug>          card: photo, title, description, prices, two buttons
                       → nothing is billed, no purchase, no invoice
⭐ or 💎 pressed        pending purchase + provider invoice created
payment notification   confirm_payment → deliver_purchase → mark_delivered
/start <slug> again    already owned → the link is re-sent, never re-charged
```

An invoice exists only because a buyer pressed a payment button. Opening a card
records the visitor's Telegram profile and nothing else.

### Resilience

| Situation | Behaviour |
| --- | --- |
| The same webhook arrives five times | one confirmation, one delivery |
| A webhook never arrives | the reconciliation loop polls Crypto Pay and settles the payment |
| Telegram flood control or an outage | delivery retries with exponential back-off, honouring `retry_after` |
| The buyer blocked the bot | no retries; the purchase stays `paid` and the next `/start` delivers it |
| Two payment buttons pressed at once | one invoice: the second press is refused by the lock |
| Stars and USDT invoices both paid | the database allows exactly one paid copy; the loser is reported, not delivered |
| An invoice is never paid | housekeeping expires it, and it stops being polled |

## Admin API

Every endpoint except `/auth/login` and the health probes requires a bearer
access token. Login is rate limited per client and username; the refresh token
travels in an httpOnly, SameSite=strict cookie and is rotated (and revoked) on
every use.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/auth/login` | exchange credentials for a token pair |
| `POST` | `/auth/refresh` | rotate the refresh token |
| `POST` | `/auth/logout` | revoke the refresh token |
| `GET` | `/auth/me` | current administrator |
| `GET` | `/products` | list with pagination, search and activity filter |
| `POST` | `/products` | create; the response carries the ready-to-copy deep link |
| `GET` | `/products/{id}` | read one |
| `PATCH` | `/products/{id}` | partial update — omitted fields are untouched, `null` clears a nullable one |
| `DELETE` | `/products/{id}` | delete a product that was never sold (`409` otherwise) |
| `GET` | `/purchases` | search by Telegram id, username, product, invoice or charge id |
| `POST` | `/purchases/{id}/verify` | **check a payment manually** |
| `POST` | `/purchases/{id}/resend` | send the purchased link again |
| `GET` | `/stats/overview` | today / week / month / all-time, Stars and USDT split, top products, last sales |

### Manual payment check

For the support case "I paid but got no link". The operation is idempotent and
never invents a payment:

| Purchase state | What the check does | Outcome |
| --- | --- | --- |
| delivered | nothing | `already_delivered` |
| paid, delivery failed | retries delivery | `delivered_now` / `delivery_failed` |
| pending, CryptoBot | asks Crypto Pay for the invoice state | `settled_and_delivered` / `still_unpaid` / `expired_unpaid` |
| pending, Stars | uses the stored Telegram charge id (Stars has no invoice lookup) | `settled_and_delivered` / `no_provider_evidence` |
| refunded | nothing | `refunded` |
| provider unreachable | nothing | `provider_unavailable` — press again later |

Validation failures return `422` with the offending field, its rule and a
message; the submitted values are never echoed back.

## Admin panel

Vue 3 + TypeScript + Pinia, built by Vite into static files that nginx serves.
No UI framework: the whole bundle is ~128 KB (48 KB gzipped).

Screens: **Статистика** (four revenue windows, Stars and USDT split, top
products, latest sales), **Товары** (search, pagination, create/edit, hide,
delete, one-click deep-link copy), **Покупки** (search across every field,
status filter, «Проверить платеж» and «Отправить ссылку» per row).

Built for large data sets: pagination, search and filtering all happen on the
server, search input is debounced, and a superseded request is aborted — so a
page never renders the answer to an older query.

### Types come from the API

`backend/openapi.json` is exported from the running app (`make openapi`), and
`make ui-types` regenerates `src/api/schema.d.ts` from it with
`openapi-typescript`. Every request and response in the panel is typed from that
file, so renaming a field on the server breaks `vue-tsc`, not production. CI
regenerates the types and fails if the committed file differs.

### Session handling

The access token lives in memory only — never in `localStorage`, so an XSS
cannot read it. The refresh token is an httpOnly cookie that JavaScript cannot
touch. On `401` the client refreshes once and replays the request; parallel 401s
share a single refresh, so the rotating refresh token is never spent twice. A
page reload resumes the session from the cookie alone.

The panel ships no inline scripts and no inline styles, which is what lets the
edge send a Content-Security-Policy with neither `unsafe-inline` nor
`unsafe-eval`.

## Performance

Measured on this schema with **200 000 purchases, 2 000 products and 50 000
buyers** (an 88 MB `purchases` table), end to end through nginx and TLS:

| Operation | Time |
| --- | --- |
| Purchase search by invoice id, charge id, Telegram id or username | 27–65 ms |
| Purchase list, first page | 35 ms |
| Purchase list filtered by status | 19 ms |
| Product list and product search | 12–21 ms |
| Dashboard (11 aggregate queries, four time windows) | 290 ms |
| Purchase list at offset 100 000 | 280 ms |

Two things make the search fast, and both were found by measuring rather than by
guessing:

* **Trigram indexes** (`pg_trgm`, migration `0002`). `ILIKE '%term%'` has no
  anchored prefix, so a B-tree is useless for it.
* **The search is a `UNION` of one query per criterion, not one `OR` across
  joined tables.** The obvious spelling — five `OR`ed `ILIKE`s over `purchases`,
  `users` and `products` — forces PostgreSQL to materialise the whole join before
  it can evaluate the disjunction, because a disjunction spanning three tables
  cannot be turned into a bitmap of index lookups. Searching for an exact invoice
  id cost a 126 ms sequential scan; the same search as a `UNION` costs 6 ms,
  because every branch is indexable on its own and the outer query only resolves
  primary keys.

Known characteristics, none of which is a defect to fix but all of which are
worth knowing before the shop is large:

* A search term **shorter than three characters** cannot use a trigram index —
  there is no complete trigram in it — and falls back to a scan (~370 ms at this
  size). Longer terms are indexed.
* A search that **matches nearly every row** is inherently expensive: counting
  200 000 matches took 890 ms. Narrower terms are cheap.
* **Offset pagination** costs time proportional to the offset, so jumping to page
  2 000 is slower than page 1. The panel's page size and the recency ordering
  make this a non-issue in practice.
* The **all-time dashboard totals** are a full scan of the paid rows by
  definition (62 ms here); the today/week/month windows use an index and take
  4–10 ms.

The edge compresses responses: the product list goes from 21 129 to 2 588 bytes,
and the panel's JavaScript bundle from 128 KB to 47 KB.

---

# Local development

```bash
cp .env.example .env      # fill in the bot token, CryptoBot token and secrets
make up                   # postgres + redis + migrations + api + bot + panel
curl localhost:8000/api/v1/health/ready
```

The development stack keeps `TELEGRAM_USE_WEBHOOK=false`, so the bot uses long
polling and no public URL is needed. The panel is available on
`http://localhost:8080`, or run `make ui-dev` for the Vite dev server with hot
reload on `:5173`.

Without Docker:

```bash
make install              # uv sync --frozen
make test-unit            # tests that need no database
make check                # ruff + mypy --strict + pytest
make ui-check             # eslint + vue-tsc + vitest + build
```

`make help` lists every target.

## Migrations

Alembic runs on the async engine. In both stacks a one-shot `migrations` service
applies `alembic upgrade head` before the API and the bot start, so neither can
ever talk to an outdated schema.

```bash
make migrate                        # upgrade head
make migrate-down                   # downgrade -1
make revision m="add coupon codes"  # autogenerate a new revision
make prod-migrate                   # upgrade head against the production stack
```

`alembic check` runs as part of the test suite, so a model change without a
migration fails CI instead of production.

The integration suite needs a disposable PostgreSQL database:

```bash
export TEST_DATABASE_DSN=postgresql+asyncpg://tgshop:tgshop@127.0.0.1:5432/tgshop_test
make test
```

Without that variable the database tests are skipped and the rest still run.

---

# Production deployment

## Server requirements

| Resource | Minimum | Comfortable |
| --- | --- | --- |
| CPU | 2 vCPU | 4 vCPU |
| RAM | 2 GB | 4 GB |
| Disk | 20 GB SSD | 40 GB SSD |
| OS | any Linux with Docker Engine 24+ and the Compose v2 plugin | Ubuntu 24.04 / Debian 12 |

Also required:

* a **domain name** with an `A` (and optionally `AAAA`) record pointing at the
  server — Telegram only delivers webhooks over HTTPS with a valid certificate,
  so a bare IP address will not work;
* inbound **TCP 80 and 443** open. Port 80 is not optional: it carries the ACME
  challenge for certificate issuance and renewal;
* outbound HTTPS to `api.telegram.org` and `pay.crypt.bot`.

The resource limits in `docker-compose.prod.yml` add up to about 3.5 GB and 5
CPUs; on a 2 GB box lower `postgres`'s `shared_buffers` to `128MB` and its memory
limit to `512m`, and set `API_WORKERS=1`.

## Installation

```bash
# Docker Engine + Compose plugin (official convenience script)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker

git clone https://github.com/Asyny-big/TgBot.git /opt/tgshop
cd /opt/tgshop

# GitHub does not carry the executable bit through its web API, so restore it
# once after cloning. (`make` targets invoke the scripts through bash and work
# either way.)
chmod +x scripts/*.sh
```

## Configuring .env

```bash
cp .env.example .env
chmod 600 .env
```

Generate real secrets — never deploy the template values:

```bash
openssl rand -hex 32   # SECURITY_JWT_SECRET
openssl rand -hex 24   # SECURITY_ADMIN_PASSWORD
openssl rand -hex 24   # POSTGRES_PASSWORD
openssl rand -hex 24   # REDIS_PASSWORD
openssl rand -hex 24   # TELEGRAM_WEBHOOK_SECRET
```

Values that must change from the development defaults:

| Variable | Production value | Why |
| --- | --- | --- |
| `APP_ENVIRONMENT` | `production` | disables Swagger and rejects debug mode |
| `APP_DEBUG` | `false` | the app refuses to start otherwise |
| `APP_LOG_FORMAT` | `json` | machine-readable logs |
| `TGSHOP_DOMAIN` | `shop.example.com` | the host nginx serves and the certificate covers |
| `TGSHOP_ACME_EMAIL` | your address | Let's Encrypt expiry warnings |
| `TELEGRAM_USE_WEBHOOK` | `true` | long polling is a development mode |
| `TELEGRAM_WEBHOOK_BASE_URL` | `https://shop.example.com` | must equal `https://` + `TGSHOP_DOMAIN` |
| `SECURITY_COOKIE_SECURE` | `true` | the refresh cookie must never cross plain HTTP |
| `SECURITY_CORS_ORIGINS` | *(empty)* | the panel is same-origin; every entry here grants a domain access to the admin API |
| `REDIS_PASSWORD` | a real secret | the production stack starts Redis with `--requirepass` and refuses to come up without it |
| `API_WORKERS` | `2` | keep `API_WORKERS × (POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW)` below PostgreSQL's `max_connections` |

Then check the whole file, including the cross-component agreements the
application cannot see for itself:

```bash
make preflight
```

It fails on things like a webhook base URL that does not match the domain nginx
serves, a webhook path outside `/webhook/` (nginx would route it to the API), a
changed `APP_API_PREFIX` (which would silently remove the login rate limit), a
leftover `change-me` placeholder, or a world-readable `.env`.

## Setting up the Telegram bot

1. Open [@BotFather](https://t.me/BotFather) → `/newbot`, pick a name and a
   username. Copy the token into `TELEGRAM_BOT_TOKEN` and the username (without
   `@`) into `TELEGRAM_BOT_USERNAME`.
2. `/setdescription` and `/setabouttext` are optional. **Do not** add commands
   with `/setcommands`: this bot has no menu on purpose.
3. Telegram Stars need no provider token and no payment provider — the bot sends
   invoices in the `XTR` currency directly. Nothing to configure in BotFather.
4. Deep links work out of the box: every product's link is
   `https://t.me/<TELEGRAM_BOT_USERNAME>?start=<slug>`, and the panel offers a
   copy button for it.

## Setting up CryptoBot

1. Open [@CryptoBot](https://t.me/CryptoBot) → **Crypto Pay** → **Create App**.
2. Copy the API token into `CRYPTOBOT_API_TOKEN`. Keep
   `CRYPTOBOT_NETWORK=mainnet` for real money (`testnet` talks to
   `testnet-pay.crypt.bot` and settles nothing real).
3. In the app's settings enable **Webhooks** and set the URL to
   `https://<TGSHOP_DOMAIN>/webhook/cryptobot`.
4. Leave `CRYPTOBOT_ASSET=USDT` unless you intend to charge in another asset.

Notifications are authenticated by `HMAC-SHA256(SHA256(api_token), raw_body)`
against the `crypto-pay-api-signature` header, so a forged notification is
rejected before the body is parsed. If a notification is lost anyway, the
reconciliation worker polls Crypto Pay every `BOT_RECONCILIATION_INTERVAL_SECONDS`
and settles the payment without anyone noticing.

## First launch

The order matters: nginx cannot start without the certificate it is configured
to load, and the certificate cannot be issued while nginx holds port 80. So the
first certificate is issued standalone, before the stack comes up.

```bash
# 1. Point DNS at this server first, then confirm the configuration is sane.
make preflight

# 2. Issue the first certificate (add --staging first to rehearse without
#    spending one of the five weekly attempts for this domain).
./scripts/tls-init.sh --staging
./scripts/tls-init.sh

# 3. Build the images and start everything.
make prod-build
make prod-up

# 4. Watch the stack become healthy.
make prod-ps
make prod-logs
```

What happens on `prod-up`: PostgreSQL and Redis start and become healthy →
`migrations` applies `alembic upgrade head` and exits → `api` and `bot` start →
the bot registers its Telegram webhook itself → `nginx` starts once both are
healthy.

Verify:

```bash
curl -fsS https://<TGSHOP_DOMAIN>/api/v1/health/ready     # {"status":"ready",...}
curl -fsS "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

`getWebhookInfo` must show `"url": "https://<TGSHOP_DOMAIN>/webhook/telegram"`
and `"pending_update_count": 0`. A non-empty `last_error_message` there is the
fastest way to spot a certificate or routing mistake.

This check also proves outbound connectivity: the bot registers that webhook
itself on start-up, so a `url` that is set at all means the container reached
`api.telegram.org`. PostgreSQL and Redis sit on a network with no route to the
internet; the API and the bot get theirs from the network they share with nginx.

Then open `https://<TGSHOP_DOMAIN>/`, log in, create a product, copy its deep
link and buy it from another Telegram account.

## Creating the administrator

There is no user table and no registration form: the panel has exactly one
account, defined by the environment and hashed with argon2 in memory on start-up.
A password is therefore never stored anywhere, not even hashed at rest.

```bash
# change the credentials
nano .env                     # SECURITY_ADMIN_USERNAME / SECURITY_ADMIN_PASSWORD
docker compose -f docker-compose.prod.yml up -d --force-recreate api
```

The password must be at least 12 characters; the process refuses to start
otherwise. Changing it does not end sessions that are already open — to force
every session to end, rotate `SECURITY_JWT_SECRET` as well, which invalidates
every issued access and refresh token at once.

## The edge

One nginx container terminates TLS, serves the compiled panel and routes
everything else:

| Path | Goes to | Notes |
| --- | --- | --- |
| `/webhook/…` | `bot:8081` | POST only, **never rate limited** |
| `/api/v1/auth/login` | `api:8000` | 10 requests/minute per IP, burst 5 |
| `/api/…` | `api:8000` | 20 requests/second per IP, burst 40, `no-store` |
| `/assets/…` | static | immutable, cached for a year, gzipped |
| `/index.html`, everything else | static | `no-store`, SPA history fallback |

Deliberate decisions:

* **Webhooks are never rate limited.** Dropping a payment notification costs
  money, and both webhooks are already authenticated cryptographically before
  anything is read. Only the login endpoint and the general API surface are
  throttled, because those are the ones an attacker can actually abuse.
* **`X-Forwarded-For` is overwritten, not appended.** The edge is the trust
  boundary, so a browser sending `X-Forwarded-For: 1.2.3.4` cannot poison the
  client address that the API's login rate limiter keys on.
* **Security headers are included in every location.** nginx's `add_header` does
  not merge: a location that sets its own `Cache-Control` would otherwise drop
  the inherited security headers entirely.
* **Content-Security-Policy without `unsafe-inline` or `unsafe-eval`**, which is
  possible because the panel emits no inline scripts, no inline styles and the
  bundle contains no `eval` or `new Function`.
* **Unknown `Host` gets `444`; unknown SNI gets its TLS handshake refused**, so a
  scanner that found the IP address learns nothing.
* **OCSP stapling is off** on purpose: Let's Encrypt has retired its OCSP
  responders, so enabling it would only produce start-up warnings.

TLS is 1.2 and 1.3 only, with forward-secret cipher suites, session tickets
disabled and HSTS for two years (`includeSubDomains`, preload-eligible).

## Container hardening

| Measure | Where |
| --- | --- |
| No published ports except 80/443 | PostgreSQL, Redis, the API and the bot are unreachable from outside the Docker network |
| `internal: true` data network | PostgreSQL and Redis have no route to the internet at all |
| `cap_drop: ALL` + `no-new-privileges` | every service |
| Read-only root filesystem with a small `tmpfs` for `/tmp` | `api`, `bot`, `migrations` |
| Unprivileged user (uid 1001) | the backend image |
| Size-capped JSON logs (10 MB × 5) | every service |
| CPU and memory limits | every service |
| Healthchecks and `restart: always` | every long-running service |
| No source mounts, no dev dependencies, no `--reload` | the whole production stack |

`migrations` is the one service with `restart: "no"` — it is a one-shot job, and
a job that restarted forever would never satisfy the `service_completed_successfully`
gate that the API and the bot wait on.

---

# Operations

## Updating the project

```bash
cd /opt/tgshop
./scripts/backup.sh                 # always take a dump before an update
git pull
make prod-build                     # rebuild the images
make prod-up                        # recreate what changed
make prod-ps                        # confirm everything is healthy again
```

Migrations are applied automatically: `api` and `bot` depend on the `migrations`
job completing successfully, so a new schema is in place before the new code
serves a request. If a migration fails, `api` and `bot` are simply not started
and the previous containers keep running.

One note about migration `0002`, which builds the search indexes: `CREATE INDEX`
takes a lock that blocks writes to the table while it runs. On a table that
already holds millions of purchases, apply it during a quiet minute. It took 1.6
seconds on 200 000 rows.

To roll back to the previous release:

```bash
git checkout <previous-tag>
make prod-build && make prod-up
```

A rollback across a migration that dropped a column needs the dump:
`alembic downgrade` handles the schema, but only a restore brings the data back.

## Backups

```bash
./scripts/backup.sh                       # writes ./backups/<db>-<UTC>.dump
BACKUP_DIR=/mnt/backups ./scripts/backup.sh
RETENTION_DAYS=30 ./scripts/backup.sh
```

The script produces a compressed custom-format dump, **verifies it is readable
with `pg_restore --list` before publishing it under its final name**, writes a
`.sha256` next to it, and deletes dumps older than the retention window
(14 days by default). It refuses to run if the `postgres` container is not up, so
cron cannot quietly collect zero-byte files.

Schedule it, and send the dumps off the machine — a backup on the same disk is
not a backup:

```cron
# /etc/cron.d/tgshop
17 2 * * *  root  cd /opt/tgshop && ./scripts/backup.sh >> /var/log/tgshop-backup.log 2>&1
30 3 * * *  root  rclone sync /opt/tgshop/backups remote:tgshop-backups
23 4,16 * * *  root  cd /opt/tgshop && ./scripts/renew-certs.sh >> /var/log/tgshop-certs.log 2>&1
```

`renew-certs.sh` runs certbot through the *running* nginx (the ACME challenge is
served from a shared volume, so there is no downtime), and reloads nginx only
when the certificate actually changed. Certbot itself only acts within 30 days of
expiry, so running it twice a day costs nothing.

What is **not** in a database dump, and must be kept separately:

* `.env` — every secret lives there and nowhere else;
* the Redis volume — losing it invalidates nothing important, but it does clear
  the revoked-refresh-token list, so rotate `SECURITY_JWT_SECRET` if it is lost;
* the certificate volume — or simply re-issue with `./scripts/tls-init.sh`.

## Recovering from a failure

### A container keeps restarting

```bash
make prod-ps                                    # who is unhealthy
docker compose -f docker-compose.prod.yml logs --tail 200 api
```

Configuration is validated on start-up, so a bad value shows up as a single
explicit error naming the field, not as a mid-payment failure.

### The database is corrupt or the data is wrong

```bash
ls -l backups/
./scripts/restore.sh backups/tgshop-20260130-021500Z.dump
```

The script verifies the checksum, verifies the archive is readable, stops the API
and the bot so nothing writes into a database that is about to be replaced,
restores **inside a single transaction** (a failure rolls back and leaves the
current data intact), brings the schema to the current head in case the dump
predates a migration, and starts the API and the bot again. Add `--yes` for
unattended recovery.

### Rebuilding the whole server

```bash
git clone https://github.com/Asyny-big/TgBot.git /opt/tgshop && cd /opt/tgshop
chmod +x scripts/*.sh
cp /secure/backup/.env .env && chmod 600 .env
./scripts/tls-init.sh
make prod-build && make prod-up
./scripts/restore.sh /secure/backup/tgshop-latest.dump --yes
```

The bot re-registers its Telegram webhook on start; nothing has to be done on
Telegram's side. Re-check the CryptoBot app's webhook URL only if the domain
changed.

### Payments arrived but links did not

The system is designed so this resolves itself, in this order:

1. delivery retries with back-off inside the same request;
2. the buyer's next `/start` re-delivers, because the purchase is still `paid`;
3. the reconciliation worker settles payments whose notification was lost;
4. and if a buyer still complains, **Покупки → Проверить платеж** re-checks the
   provider and re-runs delivery. It is idempotent: pressing it twice cannot
   charge, duplicate or double-deliver anything.

### Certificate expired

```bash
./scripts/renew-certs.sh                        # normal path
./scripts/tls-init.sh                           # if the certificate is gone entirely
```

`tls-init.sh` needs port 80, so stop nginx first:
`docker compose -f docker-compose.prod.yml stop nginx`.

## Health probes

| Endpoint | Meaning |
| --- | --- |
| `GET /api/v1/health/live` | the API process is up (used by its Docker `HEALTHCHECK`) |
| `GET /api/v1/health/ready` | PostgreSQL and Redis reachable, `503` otherwise |
| `GET /internal/health` on `bot:8081` | the bot process is up (container-internal only) |
| `GET /internal/health` on `127.0.0.1:8081` in nginx | the edge is accepting connections (container-internal only) |

Liveness probes deliberately do not touch PostgreSQL or Redis: restarting a
process cannot fix a database outage, and a probe that failed during one would
turn a dependency blip into a restart loop.

---

# Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `nginx` will not start, "cannot load certificate" | the certificate does not exist yet — run `./scripts/tls-init.sh` before the first `make prod-up` |
| `getWebhookInfo` shows an SSL error | the certificate does not cover `TGSHOP_DOMAIN`, or DNS points elsewhere; re-issue and re-check `TELEGRAM_WEBHOOK_BASE_URL` |
| The bot answers `/start` but never delivers | check `make prod-logs` for `delivery_failed`; a buyer who blocked the bot cannot be messaged until they unblock it |
| CryptoBot payments never settle | the app's webhook URL, or `CRYPTOBOT_API_TOKEN`; reconciliation will settle them within a minute anyway, so a persistent failure means the token is wrong |
| `429` from the panel while clicking around | the edge's API rate limit; it allows 20 requests/second per IP, so this normally means a script, not a person |
| Login always returns `401` with the right password | `SECURITY_ADMIN_PASSWORD` in `.env` is not what the running container was started with — recreate the `api` container |
| `make preflight` complains about `APP_API_PREFIX` | nginx rate limits the literal path `/api/v1/auth/login`; changing the prefix removes that protection |
| Redis refuses to start | `REDIS_PASSWORD` is empty; production requires it |

## Configuration

Every setting is read from the environment and validated on start-up; an invalid
or missing value stops the process instead of failing mid-payment. See
[`.env.example`](.env.example) for the annotated list. Secrets are wrapped in
`SecretStr`, so they never leak into logs or tracebacks.

## Delivery status

| Stage | Scope | Status |
| --- | --- | --- |
| 1 | Skeleton, configuration, logging, Docker, CI | done |
| 2 | Database schema, migrations, repositories | done |
| 3 | Service layer, dependency injection | done |
| 4 | Bot: product card, Stars, CryptoBot, re-delivery | done |
| 5 | Admin API: auth, CRUD, statistics, search, manual payment check | done |
| 6 | Vue 3 admin panel | done |
| 7 | Nginx, HTTPS, production compose, backups, deployment guide | done |
