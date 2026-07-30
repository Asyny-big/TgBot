# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------- #
# Production edge image: TLS terminator, reverse proxy and static file server in
# one container. Built from the repository root, because it needs both the
# frontend sources and the nginx configuration:
#
#   docker compose -f docker-compose.prod.yml build nginx
#
# The development stack uses frontend/Dockerfile instead — a plain HTTP image
# that serves the panel and proxies /api, with no certificates involved.
# ---------------------------------------------------------------------------- #
FROM node:22-alpine AS builder

WORKDIR /build

# Dependencies first: editing a component must not invalidate the install layer.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/tsconfig.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src
COPY frontend/tests ./tests

# The panel is served from the same origin as the API, so requests stay relative
# and no base URL has to be compiled in.
# `npm run build` type-checks with vue-tsc first: a type error fails the image.
RUN npm run build

# ---------------------------------------------------------------------------- #
# Runtime: nginx, the compiled panel, and nothing else.
# ---------------------------------------------------------------------------- #
FROM nginx:1.27-alpine AS runtime

# The image ships a default server on port 80 that would collide with ours.
RUN rm -f /etc/nginx/conf.d/default.conf

COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/snippets/ /etc/nginx/snippets/
COPY nginx/conf.d/ /etc/nginx/conf.d/
# Rendered by the image entrypoint (envsubst) into /etc/nginx/conf.d/ at start-up.
COPY nginx/templates/ /etc/nginx/templates/

COPY --from=builder /build/dist /usr/share/nginx/html

# Only ${TGSHOP_*} placeholders are substituted, so nginx's own $variables — and
# unrelated environment variables — are left untouched.
ENV NGINX_ENVSUBST_FILTER=^TGSHOP_

# Written to by certbot's webroot challenge; created here so a missing bind
# mount cannot turn the ACME location into a 500.
RUN mkdir -p /var/www/certbot

EXPOSE 80 443

# Probes the loopback-only status listener: it proves nginx is alive without
# depending on certificates, on the Host header, or on an upstream being up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --quiet --spider --timeout=3 http://127.0.0.1:8081/internal/health || exit 1
