# syntax=docker/dockerfile:1

# ---- Build stage: install from the lockfile and produce the Angular production build ----
FROM node:24-trixie-slim AS builder

# Same pnpm version as the packageManager field in package.json.
RUN npm install --global pnpm@12.3.4

WORKDIR /app
ENV CI=true

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

# ---- Runtime stage: static files served by NGINX running as an unprivileged user ----
FROM nginxinc/nginx-unprivileged:1.31 AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx-headers.conf /etc/nginx/snippets/headers.conf
COPY nginx-backend-locations.conf /etc/nginx/snippets/backend-locations.conf
COPY --from=builder /app/dist/frontend/browser /usr/share/nginx/html

# The base image already runs as nginx (uid 101); stated explicitly so it cannot regress.
USER 101

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["curl", "-fsS", "http://127.0.0.1:8080/healthz"]
