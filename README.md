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

## Quick start

```bash
cp .env.example .env      # fill in the bot token, CryptoBot token and secrets
make up                   # build and start postgres + redis + api
curl localhost:8000/api/v1/health/ready
```

Local development without Docker:

```bash
make install              # uv sync --frozen
make check                # ruff + mypy --strict + pytest
```

`make help` lists every target.

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
| 2 | Database schema, migrations, repositories | planned |
| 3 | Service layer, dependency injection | planned |
| 4 | Bot: product card, Stars, CryptoBot, re-delivery | planned |
| 5 | Admin API: auth, CRUD, statistics, search, webhooks | planned |
| 6 | Vue 3 admin panel | planned |
| 7 | Nginx, production compose, deployment guide | planned |
