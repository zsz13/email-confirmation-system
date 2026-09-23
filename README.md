# Email Confirmation System

A person enters a name and an email address; the backend stores the submission in MySQL and sends
a confirmation email through Mailjet that greets them by name and repeats the exact address it was
sent to. The page then follows the email's delivery state until Mailjet reports a final result.

The application is intentionally small: one form, three endpoints, one table. It is built with
production-minded practices: a versioned API with minimal responses, honest delivery tracking, rate
limiting, redacted and correlated logs, hardened containers, three test layers and CI.

**Angular · FastAPI · MySQL · Mailjet · Docker Compose**

## Quick start

Requirements: Docker with Compose v2, and a Mailjet account (API key, secret key, verified
sender address).

```bash
cp .env.example .env                    # MySQL credentials and ports for Compose (local demo values)
cp backend/.env.example backend/.env    # then set MAILJET_API_KEY, MAILJET_SECRET_KEY, MAILJET_SENDER_EMAIL
docker compose up --build
```

The first start takes a couple of minutes. Then open:

| What | URL |
|------|-----|
| Frontend | http://localhost:4200 |
| API docs (Swagger UI) | http://localhost:4200/docs |
| Health check | http://localhost:4200/api/v1/health |
| Admin panel (demo) | http://localhost:4200/q7Vf2mZ9Kx4Tn8Rb3Lp6Hs1Yw5Dc0Gj4Ua9Pe2Nr7Bk6Xv3Qm8Sf1Zh5Wt0Cy4Ld9Jp2En7 |

Admin login: `demo-admin` / `demo-admin-2026`. These are **demo-only credentials**, public on
purpose; see [Demo admin panel](#demo-admin-panel).

To try it without a real inbox, submit `<anything>@mailinator.com` and read the message at
`https://www.mailinator.com/v4/public/inboxes.jsp?to=<anything>`. Those inboxes are public, so
never send anything sensitive to them. Stop the stack with `Ctrl+C` or `docker compose down`
(`-v` also deletes the database volume).

## Key features

- **Store first, then send.** The row is committed before Mailjet is called. A `201` comes only
  once Mailjet accepted the message, and carries just a message and an opaque `tracking_id`.
- **Honest delivery tracking.** A background `DeliveryReconciler` follows each message in Mailjet
  until a final state; the page polls a read-only status endpoint.
- **Read-only admin panel** (SQLAdmin): dashboard, search, status filter, sorting, pagination,
  details.
- **Operable:** rate limits, `key=value` logs with `X-Request-ID` and stage timings, one-command
  Docker startup, three test layers, CI.

## Architecture

```
Browser ──▶ frontend: NGINX (127.0.0.1:4200 → :8080)
              ├─ /                                  Angular production build
              └─ /api/v1, /docs, /openapi.json,  ──▶ backend: FastAPI + Uvicorn (:8000, internal only)
                 admin path                          ├─ MySQL 8.4  (async SQLAlchemy, Alembic)
                                                     ├─ Mailjet Send API v3.1  (POST, in the request)
                                                     └─ DeliveryReconciler ──▶ Mailjet message API (GET)
```

1. `POST /api/v1/submissions`: validate → rate limit → insert `pending` and commit → send through
   Mailjet → store `accepted` with the Mailjet MessageID → `201`. If Mailjet refuses or cannot be
   reached, the row becomes `failed` and the API answers `502`.
2. The **DeliveryReconciler**, an asyncio task started with the application, looks up the
   unfinished rows (`accepted`, `queued`, `retrying`, with a stored MessageID) in Mailjet every 5
   seconds and stores each change.
3. The page polls `GET /api/v1/submissions/{tracking_id}/status` every 5 seconds until a final
   state (for at most 2 minutes), and a button checks on demand. That endpoint only reads MySQL.

Request flow, design decisions, logging and Docker topology: [docs/architecture.md](docs/architecture.md).

## Tech stack

| Layer | Choice |
|-------|--------|
| Frontend | Angular 22 (standalone components, signals, zoneless), TypeScript, Reactive Forms, native CSS |
| Backend | Python 3.14 with uv, FastAPI, Pydantic v2, pydantic-settings, httpx |
| Database | MySQL 8.4, SQLAlchemy 2.0 async (`asyncmy`), Alembic, UUIDv7 keys in `BINARY(16)` |
| Email | Mailjet Send API v3.1 and message API |
| Admin | SQLAdmin on the same FastAPI app and engine, session-cookie login |
| Runtime | Docker Compose, multi-stage images, unprivileged NGINX as static server and reverse proxy |
| Quality | pytest, Ruff, Pyright; ESLint, Prettier, Vitest; GitHub Actions, Dependabot |

## Environment setup

Two git-ignored files, each with a committed example that holds demo values only:

| File | Read by | Holds |
|------|---------|-------|
| `.env` (from `.env.example`) | Docker Compose | `MYSQL_*` credentials the database is created with; optional `FRONTEND_PORT`, `MYSQL_PORT` |
| `backend/.env` (from `backend/.env.example`) | the backend | Mailjet credentials and sender, `DATABASE_URL` for local development, optional settings |

Only the three Mailjet values must be filled in; the optional settings (rate limits, reconcile
interval, log level, CORS, admin overrides) are listed with defaults in
[`backend/.env.example`](backend/.env.example). Under Compose, `DATABASE_URL` is built from the
`MYSQL_*` values. MySQL keeps its credentials in the volume, so after changing one run
`docker compose down -v`.

## Docker

```
mysql (healthy) ──▶ migrate: alembic upgrade head (exits 0) ──▶ backend (healthy) ──▶ frontend (healthy)
```

MySQL keeps its data in the `mysql_data` volume and is used through an application account, never
root. `migrate` applies the Alembic migrations and exits. The backend is reachable only inside the
Compose network. The frontend's NGINX serves the Angular build and proxies the API, the docs and
the admin path on the same origin. Images, health checks, users and hardening are covered in
[docs/architecture.md](docs/architecture.md#docker).

```bash
docker compose up --build -d     # start in the background
docker compose ps -a             # mysql, backend, frontend healthy; migrate exited (0)
docker compose logs -f backend   # application logs
docker compose exec mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u"$MYSQL_USER" "$MYSQL_DATABASE" \
  -e "SELECT BIN_TO_UUID(id) AS id, email_status, created_at FROM submissions ORDER BY id DESC LIMIT 5"'
docker compose down              # stop and keep the data (-v deletes it)
```

For hot reload with uv and pnpm (MySQL still in Docker), see
[docs/testing.md](docs/testing.md#local-development).

## API endpoints

All endpoints live under `/api/v1`; interactive docs at `/docs`. Error bodies are
`{"detail": "<message>"}`, except `422`, whose `detail` is FastAPI's list of validation errors.

| Method and path | Success | Errors |
|-----------------|---------|--------|
| `GET /api/v1/health` | `200 {"status": "ok"}` | |
| `POST /api/v1/submissions` | `201 {"detail": "...", "tracking_id": "<uuidv7>"}` | `422` invalid input, `429` rate limited (`Retry-After`), `502` Mailjet refused or unreachable (row stored as `failed`), `503` database unavailable |
| `GET /api/v1/submissions/{tracking_id}/status` | `200 {"status": "<state>"}`, `Cache-Control: no-store` | `404` malformed, non-UUIDv7 or unknown id (one answer for all), `429`, `503` |

```bash
curl -i -X POST http://localhost:4200/api/v1/submissions \
  -H 'Content-Type: application/json' \
  -d '{"name": "Daniyar", "email": "daniyar-demo@mailinator.com"}'
# 201 {"detail":"Confirmation email accepted for delivery.","tracking_id":"019a0cac-19d1-7715-8846-1258380249ff"}

curl http://localhost:4200/api/v1/submissions/019a0cac-19d1-7715-8846-1258380249ff/status
# 200 {"status":"delivered"}
```

Responses are deliberately minimal: no name, address, timestamp field, Mailjet MessageID or other
row data. The `tracking_id` is the row's UUIDv7 key (it encodes its creation time) and opens only
that submission's status. There is no public listing (`GET /api/v1/submissions` is `405`).

## Delivery-status model

**Mailjet accepted the request** (`accepted`) ≠ **the recipient's mail server accepted the
message** (`delivered`) ≠ **Inbox placement**, which no state can promise.

| State | Meaning | Final |
|-------|---------|-------|
| `pending` | stored; Mailjet not called yet | no |
| `accepted` | Mailjet's Send API accepted the request; where the `POST` ends | no |
| `queued` | Mailjet is still processing or holding the message | no |
| `retrying` | temporary refusal; Mailjet is retrying delivery | no |
| `delivered` | the recipient's mail server accepted the message | yes |
| `soft_bounced` | a temporary-type rejection that Mailjet stopped retrying | yes |
| `hard_bounced` | permanent rejection, for example no such mailbox | yes |
| `blocked` | Mailjet itself refused to send it | yes |
| `failed` | sending itself failed; the API answered `502` | yes |

- `delivered` still does not guarantee the Inbox. The receiving provider decides between Inbox and
  Spam/Junk, and the page tells the person to check both.
- Late or out-of-order reports never reopen a final state, and nothing turns a bounce into a
  delivery.

State details, Mailjet mapping, the reconciler and the tracking UX:
[docs/delivery-status.md](docs/delivery-status.md).

## Demo admin panel

| | |
|---|---|
| URL | http://localhost:4200/q7Vf2mZ9Kx4Tn8Rb3Lp6Hs1Yw5Dc0Gj4Ua9Pe2Nr7Bk6Xv3Qm8Sf1Zh5Wt0Cy4Ld9Jp2En7 (with the local `uv` workflow: the same path on port 8000) |
| Username / password | `demo-admin` / `demo-admin-2026` |

> **Demo-only access.** The path, credentials and session secret are committed on purpose so the
> project can be reviewed without setup; they are public and must never be reused. The long path
> adds only a small barrier; the login is the security boundary. Production overrides
> `ADMIN_PATH`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` and `ADMIN_SESSION_SECRET` and sets
> `ADMIN_COOKIE_SECURE=true` behind HTTPS. The backend refuses that flag while demo secrets remain.

The panel is read-only: a dashboard with summary counts, and a submissions table with search by
name, email or UUIDv7 id, a status filter, sorting, pages of 25/50/100 and a details page. Create,
edit, delete, export and import are disabled (`403`).

## Testing

| Command | What runs | Email |
|---------|-----------|-------|
| `make test` | backend unit and API tests (pytest, SQLite, fake Mailjet); no Docker, seconds | none |
| `make test-integration` | real Docker stack: MySQL, migrations, backend, frontend, admin; HTTP and database checks | Mailjet stub |
| `make test-all` | `make test` + `make test-integration` | never |
| `RUN_REAL_EMAIL_E2E=1 make test-e2e` | real stack and Mailjet; one email followed to its final delivery state | one real email |

The real-email test is opt-in because it has an external side effect and depends on outside
providers; without the flag it refuses and exits non-zero. Frontend tests run with `pnpm test`.
Details: [docs/testing.md](docs/testing.md).

## CI

`.github/workflows/ci.yml` runs on every push and pull request. It needs no secrets and never
sends a real Mailjet email; Actions are pinned to commit SHAs.

| Job | Steps |
|-----|-------|
| Backend | `uv sync --frozen`, Ruff lint and format check, Pyright, pytest |
| Frontend | `pnpm install --frozen-lockfile`, ESLint, Prettier check, Vitest, production build; guards that the bundle holds no development backend URL and the footer links to this repository |
| Docker | `docker compose config` for both Compose files, image builds, the integration test |

`.github/CODEOWNERS` assigns every file to `@zsz13`; `.github/dependabot.yml` checks the pnpm
lockfile, both Dockerfiles, the Compose image and the Actions weekly.

## Security notes

- Real secrets (Mailjet, MySQL) live only in the git-ignored `.env` files, never in the frontend
  bundle or an image. The demo admin defaults are public by design.
- The backend uses an application database account; MySQL root is limited to its container.
- Minimal responses, UUIDv7 tracking ids, a status endpoint that returns only a state
  (`no-store`), and rate limits against abuse, quota burn and tracking-id enumeration.
- Mailjet errors are never passed to clients; logs mask recipients, keep only Mailjet error codes,
  and contain no credentials.
- Read-only admin behind a session login. NGINX keeps no access log for its location, and the
  application logs the path as `<admin>`.
- Application processes run as non-root with all capabilities dropped; NGINX sends security
  headers and a strict CSP. Swagger UI at `/docs` gets its own narrower exception for its CDN
  bundle.

Controls and their limits in detail: [docs/security.md](docs/security.md).

## Design decisions

| Decision | Why |
|----------|-----|
| Versioned API (`/api/v1`) | a future `/api/v2` can change the contract without breaking existing clients |
| Minimal public responses | data that is never returned cannot leak, be harvested or correlate submissions |
| UUIDv7 primary key in `BINARY(16)` | unique without a round trip, not enumerable like integers, time-ordered for index locality (unlike UUIDv4) |
| Email validation plus the confirmation itself | syntax checks in the browser and API; no unreliable SMTP probing, since the real mailbox signal is Mailjet's delivery outcome |
| Reconciler inside the backend | no broker or worker to run; MySQL is the queue, and restarts resume unfinished rows |
| SQLAdmin | a read-only admin on the existing model and engine, with no second frontend application |
| Process-local rate limits | exact for one instance, with no extra infrastructure |

The reasoning behind each: [docs/architecture.md](docs/architecture.md#design-decisions).

## Known limitations

- **Deliverability.** The demo sender is on a public email domain without custom SPF/DKIM/DMARC
  alignment, so messages may land in Spam/Junk or be temporarily rejected. Production needs a
  controlled domain, and even that cannot guarantee the Inbox.
- **`delivered` is not the Inbox.** It means the recipient's mail server accepted the message.
- **Provider delays are external.** Mailjet and the receiving server decide how fast states move.
- **Single process.** The reconciler and rate limiter assume one backend instance. Delivery is
  polled, since Mailjet webhooks need a public HTTPS endpoint.
- **Tracking ids are identifiers, not credentials.** Holding one reveals that submission's status.
- **Public demo admin.** The admin credentials and path are public by design, and the Compose
  setup is local-only (no TLS, loopback ports).
- **Synchronous send.** The email is sent inside the request; a high-volume system would queue it.
  If MySQL fails right after Mailjet accepted, the row can stay `pending` (logged).
- **No resend endpoint.** A `failed` row stays for inspection.
