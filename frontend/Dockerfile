# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------- #
# Build stage: compile the admin panel into static files.
# ---------------------------------------------------------------------------- #
FROM node:22-alpine AS builder

WORKDIR /app

# Dependencies first: source edits must not invalidate the install layer.
COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

# `npm run build` runs vue-tsc before vite, so a type error fails the image build.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# ---------------------------------------------------------------------------- #
# Runtime stage: nothing but nginx and the compiled assets.
# ---------------------------------------------------------------------------- #
FROM nginx:1.27-alpine AS runtime

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --spider http://127.0.0.1/ || exit 1
