# Testing

## Test layers

Run from the repository root:

| Command | What runs | Docker | Email | In CI |
|---------|-----------|--------|-------|-------|
| `make test` | Backend unit and API tests: pytest on SQLite, a fake email service, `httpx.MockTransport` for the Mailjet clients. Seconds. | no | none | yes |
| `make test-integration` | The real Docker stack: MySQL, Alembic migrations, backend, frontend, admin panel. Checked from outside over HTTP and in the database, with Mailjet replaced by an in-network stub. About a minute with warm caches. | yes | none (stub) | yes |
| `make test-all` | `make test`, then `make test-integration` | yes | never | both layers run as separate jobs |
| `RUN_REAL_EMAIL_E2E=1 make test-e2e` | The real stack with real MySQL and the real Mailjet credentials: one real email, to a generated inbox or `E2E_RECIPIENT_EMAIL`, followed until Mailjet reports a final delivery state | yes | **one real email** | never |

The frontend's Vitest suite is not a Make target: `pnpm test` runs it, and so does CI. `make lint`,
`make format-check`, `make build`, `make docker-up` and `make docker-down` wrap the other tools;
`make help` lists them.

### Why the real-email test is opt-in

- **It has an external side effect.** It sends a real email to a real inbox with real credentials,
  and it counts against the Mailjet quota.
- **It depends on systems outside the repository:** Mailjet, the recipient's provider and
  Mailinator. Its timing and outcome vary, so it cannot be a deterministic gate.
- **CI must not send mail**, and it has no Mailjet credentials.

Both `make test-e2e` and the script refuse without `RUN_REAL_EMAIL_E2E=1` and exit non-zero, so a
refusal is never mistaken for a pass. `make test-all` never reaches it, and `make test` checks both
facts (`backend/tests/test_scripts.py`).

The Makefile only wraps each project's own tools, which can be run directly:

```bash
cd backend
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest

cd ../frontend
pnpm install --frozen-lockfile
pnpm lint
pnpm format:check
pnpm test
pnpm build
```

## Local development

For hot reload, run MySQL in Docker and the two applications on the host. Requires uv, Node.js 24
and pnpm 12, plus both `.env` files from the README's quick start. Stop the full stack first: the
dev server also uses port 4200.

```bash
docker compose up -d --wait mysql              # only the database, on 127.0.0.1:3306

cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --no-access-log   # http://localhost:8000, docs at /docs

cd frontend                                    # second terminal, from the repository root
pnpm install
pnpm start                                     # http://localhost:4200
```

The dev frontend calls the API at `http://localhost:8000`, where CORS allows
`http://localhost:4200`. The admin panel is at the same path on port 8000.

## Unit and API tests

**Backend (pytest).** The application is built on a file-backed SQLite database per test and
exercised through `httpx.AsyncClient`. The email service is replaced through a dependency override,
and the Mailjet clients are tested against `httpx.MockTransport`. No test contacts MySQL or Mailjet,
and none needs real credentials.

| Area | Covered |
|------|---------|
| Submissions API | validation of every field, `accepted` (never `delivered`) with its MessageID, the minimal `201` body, `502` with a `failed` row, `503`, unexpected `500` with CORS headers, versioned paths only |
| Status API | only `{"status"}`, `no-store`, one `404` for malformed, non-v7 and unknown ids, its own rate limit, no Mailjet traffic |
| Mailjet | the exact Send API request, auth failures, per-message errors, malformed bodies, network errors; message lookups (`404`, `429`, `5xx`, refusals, unreadable replies) |
| Delivery states | every Mailjet status and webhook event, the transition rules, the conditional update against a stale copy |
| Reconciler | selection of in-progress rows, batching, failures that do not stop it, no overlapping cycles, clean shutdown, resume after restart |
| Rate limiting | sliding windows with a fake clock, `Retry-After`, Gmail aliasing, concurrency (exactly 5 of 12 concurrent requests pass) |
| Admin panel | login, throttling, cookie flags, demo-secret refusal, search (including by UUIDv7), filter, sorting, paging, details, `403` on every write route |
| Logging | request ids, the `request completed` line, event levels, masking, and that no credential, header, cookie or full address reaches the log |
| Scripts | the `check_delivery` poller with a fake clock, the E2E recipient check, and that the real-email test refuses to run without its opt-in |

**Frontend (Vitest).** The page shell and footer link, validation messages and focus handling,
double-submit protection, the request with trimmed values, the success state with the Spam/Junk
note, status polling and the manual check, `429` handling, and the API error mapping.

MySQL-specific behaviour (the `ENUM` column, `BINARY(16)` keys, the migrations in both directions)
is covered by the integration test, not by SQLite.

## Docker integration test

`make test-integration` builds the images, starts `compose.yaml` plus `compose.integration.yaml`,
and checks the running stack from the outside with `curl` and `mysql`:

1. Health through the NGINX proxy, the Angular page renders, `/docs` serves Swagger UI with a
   policy that lets it load, and `/openapi.json` is proxied.
2. `201` with exactly `detail` and a UUIDv7 `tracking_id`; `422` for invalid input; the fourth
   request for one mailbox is `429` with `Retry-After`; the stub received exactly three messages.
3. `X-Request-ID` is echoed through the proxy and appears in the backend log; a newline in the path
   cannot forge a log line.
4. MySQL holds the rows in a `BINARY(16)` primary key with version-7 ids, each stored as `accepted`
   with a MessageID. The reconciler then moves them `accepted → queued → delivered` on its own, and
   `python -m app.check_delivery` prints `RESULT: DELIVERED`.
5. The status endpoint reads MySQL (a direct `UPDATE` shows at once), causes no Mailjet lookups,
   and answers one `404` for malformed and unknown ids; `GET /api/v1/submissions` is `405`.
6. The migrations run down to the integer key and back up on live data; a second upgrade is a
   no-op and `alembic check` is clean. A row left `accepted` while the backend was stopped is
   delivered after the restart.
7. The admin panel: login, search by name, email and UUIDv7 id against the real column, status
   filter, sorting (including MySQL's `ENUM` order), pagination, details, and `403` for writes.
8. No credential or generated root password in any service log; only masked recipient addresses in
   the backend log; no admin path in the backend or NGINX logs; the backend connects as the
   application user.

It uses its own Compose project and ports (`4300`, `3307`, `8025`), so a stack started with
`docker compose up` is left alone. It prints recent logs on failure and always removes its
containers, network and volume.

### Mailjet stub

`scripts/mailjet_stub.py` (standard library only) stands in for Mailjet inside the Compose network:

- `POST /v3.1/send` accepts any message as `success` and assigns MessageIDs `1..n`.
- `GET /v3/REST/message/{id}` answers `queued` on a message's first lookup and `sent` afterwards,
  so rows visibly move through more than one state.
- `GET /messages` lists the accepted recipients for the test to inspect.

The overlay points the backend at the stub with `MAILJET_SEND_URL` and `MAILJET_MESSAGE_URL`,
replaces the credentials with dummy values and polls every second. No real credential is used and
no email leaves the machine.

## Real Mailjet end-to-end test

`scripts/e2e-real-email.sh` runs the real stack from `compose.yaml` with the Mailjet credentials in
`backend/.env` and sends **one real email**. It refuses to run, and exits non-zero, without the
opt-in:

```bash
RUN_REAL_EMAIL_E2E=1 make test-e2e                                        # unique ecs-e2e-…@mailinator.com inbox
RUN_REAL_EMAIL_E2E=1 E2E_RECIPIENT_EMAIL=user@example.com make test-e2e   # a chosen address
```

A `201` alone is not a pass, since it only proves Mailjet's Send API acceptance. The test checks
the `201` body and the stored MySQL row (UUIDv7 id, MessageID, stored as `accepted` by the
request). It then asks Mailjet for the real outcome: `python -m app.check_delivery` runs inside the
backend container, so the credentials never leave it, and polls every 2 seconds for up to
`E2E_DELIVERY_TIMEOUT_SECONDS` (default 120). Finally it checks that the public status endpoint
reports what MySQL holds.

- Only `DELIVERED` passes. Every other result fails the run: `SOFT_BOUNCED`, `HARD_BOUNCED`,
  `BLOCKED`, `TIMEOUT/PENDING` and `CANNOT_CHECK`.
- `DELIVERED` still means the recipient's server accepted the message, not that it reached the
  Inbox.
- The script uses its own project and ports (`4400`, `3308`) and never prints a credential.

## Manual check with Mailinator

1. `docker compose up --build`, then open http://localhost:4200.
2. Submit a name and an unused `<anything>@mailinator.com` address. The page shows "Accepted for
   delivery" and then follows the status.
3. Open `https://www.mailinator.com/v4/public/inboxes.jsp?to=<anything>`: the message **Email
   Confirmation** says `Hello <name>,` and repeats the exact address.
4. The row moves from `accepted` to, typically, `delivered` within seconds; check it in the admin
   panel or with the `mysql` command in the README.

To see the failure path, put a wrong `MAILJET_SECRET_KEY` in `backend/.env`, run
`docker compose up -d backend` and submit again: `502`, a `failed` row, and the form keeps its
values. To see the rate limit, submit four times to one address within ten minutes.
