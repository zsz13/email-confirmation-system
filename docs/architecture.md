# Architecture

## Components

| Component | Where | Role |
|-----------|-------|------|
| Angular app | `frontend/src/app/` | The form, the success view and the delivery-status polling |
| NGINX | `frontend/nginx.conf`, `frontend/nginx-backend-locations.conf` | Serves the production build; proxies `/api/`, `/openapi.json`, `/docs` and the admin path to the backend |
| Edge NGINX (production) | `deploy/`, `docker-compose.prod.yml` | TLS, Basic Auth and Let's Encrypt in front of the same services; see [deployment-vps.md](deployment-vps.md) |
| FastAPI app | `backend/app/main.py` | `create_app()`: settings, engine, rate limiters, middleware, routers, admin mount, lifespan |
| API v1 | `backend/app/api/v1/` | `routes/health.py`, `routes/submissions.py` |
| Email service | `backend/app/services/email.py` | Builds the text and HTML bodies, calls the Mailjet Send API v3.1 |
| Delivery status | `backend/app/services/delivery_status.py` | Mailjet status mapping, transition rules, message lookup |
| DeliveryReconciler | `backend/app/services/delivery_reconciler.py` | Background task that moves unfinished rows on |
| Admin panel | `backend/app/admin/` | SQLAdmin model view, session login, dashboard and login templates |
| MySQL | `compose.yaml`, `backend/alembic/` | One `submissions` table, owned by Alembic migrations |

Every I/O step is asynchronous: async route handlers, one `AsyncSession` per request, the
`asyncmy` driver and one shared `httpx.AsyncClient` for Mailjet.

## Request flow: `POST /api/v1/submissions`

1. **Validate.** Pydantic trims `name` (1-100 characters, no control characters) and validates
   `email` (see [Email validation](#email-validation)). Invalid input answers `422`.
2. **Rate limit.** The client IP and the mailbox key are checked in one synchronous step. A
   rejection answers `429` with `Retry-After` and touches neither the database nor Mailjet.
3. **Store.** A row with a new UUIDv7 id and `email_status = pending` is inserted and committed,
   so the attempt is recorded even if the send fails.
4. **Send.** `EmailService` posts the message to Mailjet with HTTP Basic auth. A non-200 answer, a
   per-message status other than `success`, a malformed body or a network error raise
   `EmailDeliveryError`.
5. **Record.** On success the row becomes `accepted` and stores the MessageID from Mailjet's reply; the API
   answers `201 {"detail", "tracking_id"}`. On failure the row becomes `failed` and the API answers
   `502`. If the final commit fails after Mailjet accepted, the row stays `pending`, the API answers
   `503` and the discrepancy is logged with the submission id.

What happens after the `201` (the reconciler, the status endpoint and the page's polling) is
described in [delivery-status.md](delivery-status.md).

## Design decisions

### API versioning

All routes live under `/api/v1`. `backend/app/api/router.py` only mounts versions under `/api`, and
`backend/app/api/v1/` owns the v1 routes. A contract change becomes `/api/v2`: a second package
mounted next to v1 with its own schemas, while v1 and `backend/app/schemas/` stay as they are. Clients
built against v1, such as this frontend (`API_PREFIX = '/api/v1'`), keep working, and both versions
can be served side by side during a migration. Unversioned paths do not exist
(`/api/submissions` is `404`), so no client can come to depend on an unversioned contract.

### Minimal public responses

`POST /api/v1/submissions` answers with exactly two fields: a human-readable `detail` and an opaque
`tracking_id`. It deliberately returns no name, email address, timestamp fields, Mailjet MessageID
or other provider data. It also returns no other database column: the only database value exposed
is the row's key, as the tracking id. The status endpoint returns only `status`.

- The client already has what it submitted; the page shows the address from its own form state.
- Data that is never returned cannot leak through a response, a proxy or browser log, a
  screenshot or a bug report. Nor can it be harvested, or used to correlate submissions.
- A two-field contract is small enough to keep stable across versions.

One piece of information is implicit: a UUIDv7 begins with its creation time in milliseconds, so
the `tracking_id` tells its holder when the submission was made. The submitter already knows that.

### UUIDv7 primary key and tracking id

`submissions.id` is a UUIDv7 (RFC 9562), generated in Python with `uuid.uuid7()`, stored as
`BINARY(16)` through the `UUIDBinary` column type (`backend/app/db/types.py`), and doubling as the
public `tracking_id`.

| | Sequential integer | UUIDv4 | UUIDv7 |
|---|---|---|---|
| Globally unique, generated without a database round trip | no | yes | yes |
| Enumerable by counting (`id + 1`) or revealing row counts | yes | no | no |
| Insert locality in InnoDB's clustered primary key | good | poor (random order, page splits) | good (time-ordered) |
| Carries a timestamp | no | no | yes, milliseconds |

- A 48-bit millisecond timestamp comes first, so new keys land at the end of the index. After it
  come a randomly seeded counter and 32 fresh random bits, so ids are non-trivial to enumerate,
  even from a neighbouring one.
- `BINARY(16)` takes 16 bytes instead of 36 characters. Python code only ever sees `uuid.UUID`.
- At the boundaries it is the canonical string (`019a0cac-19d1-7715-8846-1258380249ff`): the API
  returns that form and the status endpoint accepts only that form. The admin panel shows it, and
  its search also accepts other spellings.
- A tracking id is an identifier, not an authentication token. Whoever holds it can read that
  submission's status, and nothing else. It is not secret and must not be used to authorise
  anything more.

The migration `5b2c9e4d7a10` converted the original integer key in place.

### Email validation

- **Browser (Angular):** required, Angular's email validator, a dotted-domain pattern (the
  built-in validator accepts `user@host`) and at most 254 characters. This is feedback only.
- **API (Pydantic `EmailStr`, the source of truth):** syntax validation by `email-validator`, at
  most 254 characters. It normalises the domain to lower case; the local part is kept as typed.
  Deliverability checks are off, so there is no DNS lookup.
- **No SMTP mailbox probing.** Asking a server whether a mailbox exists (`VRFY`, or `RCPT TO`
  without sending) is unreliable: catch-all domains accept everything, and many servers refuse,
  greylist or penalise probes. The meaningful mailbox-level signal is the confirmation email itself
  and the outcome Mailjet reports: `delivered`, `soft_bounced`, `hard_bounced` or `blocked`.

### Delivery reconciliation inside the backend

The `DeliveryReconciler` is an asyncio task in the backend process, not Celery with Redis or a
separate worker container. The `submissions` table is its queue and all state survives restarts in
MySQL, so a take-home needs nothing extra to run. The trade-off is one backend process; see
[delivery-status.md](delivery-status.md#background-reconciliation).

### SQLAdmin for the admin panel

[SQLAdmin](https://aminalaee.dev/sqladmin/) is mounted on the same FastAPI application and uses the
existing SQLAlchemy model and async engine. There is no second frontend application, no admin API
to maintain and nothing extra to deploy. It is configured as a read-only operational view:

- a dashboard with counts: total, delivered, in progress, bounced/blocked/failed, pending, last
  24 hours;
- a submissions table (UUIDv7 id, created, name, email, status), newest first;
- search by name, email or UUIDv7 id, a status filter over every state, sorting by created, name,
  email and status, pages of 25/50/100, and a details page that also shows the Mailjet MessageID;
- create, edit, delete, export and import disabled (`403`, no buttons), and no custom actions.

Two parts needed code of their own. `id` is `BINARY(16)`, so SQLAdmin's text `LIKE` search is
meaningless on it: a term that parses as a UUID is compared with the key, and anything else
searches name and email. The landing page is replaced by the dashboard, computed in one aggregate
query. The panel is not in the OpenAPI document. Its login and the long demo path are described in
[security.md](security.md#demo-admin-panel).

### Same-origin frontend

In Docker, NGINX serves the Angular build and proxies `/api/`, `/openapi.json`, `/docs` and the
admin path to the backend. The production bundle therefore contains no backend address
(`environment.ts` has an empty base URL), and CORS is not involved. The dev server
(`environment.development.ts`) calls `http://localhost:8000` directly.

### Rate limiting without infrastructure

Sliding windows in process memory: exact for one instance, testable with a fake clock, and no Redis
to run. Limits, keys and trade-offs are in [security.md](security.md#rate-limiting).

## Frontend

Angular 22: standalone components, signals, zoneless change detection, `OnPush`, Reactive Forms
and native CSS, with no UI framework. `SubmissionApi` (`core/services/submission-api.ts`) is the
only place that talks HTTP. It maps every failure to an `ApiError` with a user-facing message: the
backend's `detail` (for a `429` it already states the wait), "server unreachable" or a generic
fallback. The
component therefore never inspects responses. Timers and requests are cancelled when the person
starts over or leaves the page.

## Database migrations

Alembic owns the schema; the application never calls `create_all`. `alembic/env.py` reads only
`DATABASE_URL`, which is why the `migrate` container runs without Mailjet settings. The three
migrations do the following:

- create the table;
- convert the original integer key to UUIDv7 in place;
- introduce the Mailjet delivery states and the MessageID column.

The last two check the live schema before each step, so they are safe to re-run after an
interruption (MySQL commits DDL implicitly), and both can be rolled back. The integration test
runs them down and up again on live data.

```bash
cd backend
uv run alembic upgrade head      # apply
uv run alembic check             # fail if the models and the schema differ
uv run alembic downgrade base    # drop everything the migrations created
```

## Logging and request correlation

The backend logs through one handler (`backend/app/core/logging.py`, standard library only). It
writes one line per event to stdout, and Uvicorn's own server messages go to stderr. Docker
collects both, so `docker compose logs backend` is the only tool needed.

```text
2026-09-22 09:14:02,118 INFO     [8e7692bd…] app.api.v1.routes.submissions: submission created submission_id=01a0c81a-… recipient=d***@mailinator.com
2026-09-22 09:14:02,410 INFO     [8e7692bd…] app.api.v1.routes.submissions: submission timings submission_id=01a0c81a-… db_create_duration_ms=9.8 mailjet_request_duration_ms=283.9 db_status_update_duration_ms=7.6 duration_ms=301.9
2026-09-22 09:14:02,431 INFO     [8e7692bd…] app.request: request completed method=POST path='/api/v1/submissions' status_code=201 duration_ms=312.7
2026-09-22 09:14:06,875 INFO     [-] app.services.delivery_status: delivery_status_updated submission_id=01a0c81a-… previous_status=accepted new_status=delivered final=true source=reconciler provider_duration_ms=164.2
```

- **Request id.** The bracketed value is the client's `X-Request-ID` when it matches
  `[A-Za-z0-9._-]{1,64}`, otherwise a fresh id. It is returned in the `X-Request-ID` header of
  every response and prefixes every line the request produced. Background work logs `-`.
- **Durations.** One `request completed` line per request carries the method, the escaped path,
  the status and `duration_ms`. It replaces Uvicorn's access log, which the image turns off
  (`--no-access-log`), and the health check is logged at `DEBUG`. Each stored submission logs `submission timings`:
  `db_create_duration_ms`, `mailjet_request_duration_ms`, `db_status_update_duration_ms` and the
  total. Reconciler lookups log `provider_duration_ms`. Timings are never returned by the API.
- **Redaction.** Recipients are masked (`d***@mailinator.com`). A Mailjet error is logged as its
  error codes and field names only, never its body. No credential is logged. The details, including
  how the admin path is kept out of the logs, are in
  [security.md](security.md#logging-and-redaction).
- `LOG_LEVEL` sets the level of the `app.*` loggers only. Third-party loggers stay at `WARNING`,
  because the database driver would otherwise trace statements with their parameters.

## Docker

`docker compose up --build` builds and starts four services in dependency order (the chain shown
in the README): `migrate` waits for MySQL to be healthy (`service_healthy`), the backend for the
migrations to have exited successfully (`service_completed_successfully`), and the frontend for the
backend to be healthy.

| Service | Image | Health check | Process user | Published |
|---------|-------|--------------|--------------|-----------|
| `mysql` | official `mysql:8.4` | `mysqladmin ping` over TCP as the application user | the image's own: its entrypoint starts as root to prepare the data directory, then runs `mysqld` as the `mysql` user | `127.0.0.1:3306` |
| `migrate` | `email-confirmation-backend` | none (runs once) | `app` (uid 10001) | no |
| `backend` | `email-confirmation-backend` | `GET /api/v1/health` | `app` (uid 10001) | no (Compose network only) |
| `frontend` | `email-confirmation-frontend` | `GET /healthz` | `nginx` (uid 101), unprivileged NGINX on 8080 | `127.0.0.1:4200` |

- **Images.** Both are multi-stage builds.
  - Backend: `python:3.14-slim-trixie` with `uv sync --frozen --no-dev` from `uv.lock`; the
    runtime stage holds only the virtual environment, `alembic.ini`, `alembic/` and `app/`.
  - Frontend: `node:24-trixie-slim` with `pnpm install --frozen-lockfile` and `pnpm build`; the
    runtime stage is `nginxinc/nginx-unprivileged` with the static files only.
  - `migrate` and `backend` share one image, so schema and code cannot drift apart.
- **No secrets in images.** `backend/.dockerignore` excludes `.env` files, tests and bytecode
  caches. `frontend/.dockerignore` keeps `node_modules`, build output and caches out of the build
  context. Credentials reach the backend at run time through `env_file: backend/.env`.
- **Database account.** The four `MYSQL_*` values in the root `.env` are required. The backend and
  the migrations connect as `MYSQL_USER` through a `DATABASE_URL` that Compose assembles, so
  `MYSQL_PASSWORD` must be URL-safe. How root is kept out of reach is described in
  [security.md](security.md#database-account).
- **Persistence.** Data lives in the named volume `mysql_data`. MySQL stores the credentials in it
  on first start, so changing a `MYSQL_*` value needs `docker compose down -v`.
- **Hardening.** Every service has `no-new-privileges`, and the three application services drop
  all Linux capabilities ([security.md](security.md#http-and-containers)). Nothing is privileged
  and the Docker socket is never mounted.
- **NGINX.** It provides SPA fallback, immutable caching for hashed bundles, no caching for
  `index.html`, gzip, and security headers with a Content Security Policy. The admin location and
  `/docs` have their own headers; overriding `ADMIN_PATH` means updating that location too. Those
  backend locations live in `frontend/nginx-backend-locations.conf`, which the production edge
  proxy includes as well.
- `compose.integration.yaml` is an overlay for the integration test only: it adds a Mailjet stub
  and points the backend at it.

## Repository layout

```
.
├── Makefile, compose.yaml, compose.integration.yaml, .env.example
├── docker-compose.prod.yml, .env.prod.example
├── deploy/             production edge NGINX template, certbot and Basic Auth scripts
├── .github/            CI workflow, CODEOWNERS, Dependabot
├── docs/               architecture, delivery status, testing, security, VPS deployment
├── scripts/            integration-test.sh, e2e-real-email.sh, e2e-recipient.sh, mailjet_stub.py
├── backend/
│   ├── Dockerfile, pyproject.toml, uv.lock, alembic.ini, alembic/, .env.example
│   ├── app/
│   │   ├── main.py            application factory, lifespan, error handlers
│   │   ├── check_delivery.py  python -m app.check_delivery <id>
│   │   ├── admin/             SQLAdmin views, login, templates
│   │   ├── api/               router.py, deps.py, v1/
│   │   ├── core/              config.py, logging.py
│   │   ├── db/, models/, schemas/
│   │   └── services/          email, delivery_status, delivery_reconciler, rate_limit
│   └── tests/
└── frontend/
    ├── Dockerfile, nginx.conf, nginx-headers.conf, nginx-backend-locations.conf, package.json, pnpm-lock.yaml
    └── src/app/            app shell, core/ (API types, service, errors), features/submission/, shared/
```
