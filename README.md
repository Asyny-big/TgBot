# Telegram Digital Shop

Telegram bot that sells digital goods through **deep links only**. There is no
catalog, no menu and no product list: a link such as
`https://t.me/MyShopBot?start=vip1` opens exactly one product card. The buyer
pays with **Telegram Stars** or **CryptoBot (USDT)** and instantly receives the
delivery link. Files are never stored — only the link is.

If the same Telegram user opens the deep link again after a successful payment,
the link is returned immediately, without a second charge.

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

## Quick start

```bash
cp .env.example .env      # fill in the bot token, CryptoBot token and secrets
make up                   # build and start postgres + redis + api
curl localhost:8000/api/v1/health/ready
```

Local development without Docker:

```bash
make install              # uv sync --frozen
make test-unit            # tests that need no database
make check                # ruff + mypy --strict + pytest
```

`make help` lists every target.

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
token checked) and `/webhook/cryptobot` (HMAC signature checked).

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

## Migrations

Alembic runs on the async engine. In the compose stack a one-shot `migrations`
service applies `alembic upgrade head` before the API container starts.

```bash
make migrate                        # upgrade head
make migrate-down                   # downgrade -1
make revision m="add coupon codes"  # autogenerate a new revision
```

`alembic check` runs as part of the test suite, so a model change without a
migration fails CI instead of production.

The integration suite needs a disposable PostgreSQL database:

```bash
export TEST_DATABASE_DSN=postgresql+asyncpg://tgshop:tgshop@127.0.0.1:5432/tgshop_test
make test
```

Without that variable the database tests are skipped and the rest still run.

## Configuration

Every setting is read from the environment and validated on start-up; an invalid
or missing value stops the process instead of failing mid-payment. See
[`.env.example`](.env.example) for the annotated list. Secrets are wrapped in
`SecretStr`, so they never leak into logs or tracebacks.

## Health probes

| Endpoint | Meaning |
| --- | --- |
| `GET /api/v1/health/live` | process is up (used by Docker `HEALTHCHECK`) |
| `GET /api/v1/health/ready` | PostgreSQL and Redis reachable, `503` otherwise |

## Delivery status

| Stage | Scope | Status |
| --- | --- | --- |
| 1 | Skeleton, configuration, logging, Docker, CI | done |
| 2 | Database schema, migrations, repositories | done |
| 3 | Service layer, dependency injection | done |
| 4 | Bot: product card, Stars, CryptoBot, re-delivery | done |
| 5 | Admin API: auth, CRUD, statistics, search, webhooks | planned |
| 6 | Vue 3 admin panel | planned |
| 7 | Nginx, production compose, deployment guide | planned |
