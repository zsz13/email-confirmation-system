# Security notes

These are the controls the code actually implements, with their limits. The setup is a local
take-home and is not presented as production-ready; where a real deployment must change something,
the section says so.

## Secrets

- Real values live only in the two git-ignored files, `.env` and `backend/.env`. The committed
  `.env.example` files hold placeholders and local demo values.
- No image contains a secret. `backend/.dockerignore` excludes `.env` files from the build context,
  and the frontend image holds only the built static files. Credentials reach the backend container
  at run time through `env_file: backend/.env`.
- Mailjet is called only by the backend. The production bundle contains no credentials and no
  backend address, and CI fails if the development API URL appears in it.
- The Mailjet key and secret and the admin password and session secret are `SecretStr` settings,
  which do not render their value even when the settings object is printed. `DATABASE_URL` is a
  plain string, and the application never logs it. A settings error at startup names the problem
  but never quotes the configured values (`hide_input_in_errors`).
- `compose.yaml` contains no credentials of its own; without `.env` the stack refuses to start and
  names the missing variable.

## Database account

- The backend and the migrations connect as the application user (`MYSQL_USER`), never as root.
- MySQL gets an explicit root password, so the image does not generate one and print it into the
  container log. `MYSQL_ROOT_HOST=localhost` makes root unusable over TCP, and the root password
  reaches only the database container.
- The MySQL port is published on `127.0.0.1` only, for the local `uv` workflow. The backend port is
  not published at all.

## Demo admin panel

The panel's path, username (`demo-admin`), password (`demo-admin-2026`) and session secret are
**committed on purpose** so the project can be reviewed without setup. They are public and are not
production credentials.

- **The long path is obscurity, not the security boundary.** A 71-character random path keeps the
  panel out of casual scans and adds a small extra barrier, but it is published in this repository.
  The login is what protects the panel: every admin view redirects to the login form without an
  authenticated session; only that form and SQLAdmin's static assets are public.
- **Production** overrides `ADMIN_PATH`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` and
  `ADMIN_SESSION_SECRET`, sets `ADMIN_COOKIE_SECURE=true` and serves the panel over HTTPS. The
  backend refuses to start with `ADMIN_COOKIE_SECURE=true` while the demo password or secret is in
  force. While they are in use, the login page shows a warning and the backend logs one at startup.
- **Login.** SQLAdmin's session backend provides a real form rather than HTTP Basic, so there is a
  logout and the password is not sent with every request. Credentials are compared with
  `secrets.compare_digest`. Five failed attempts from one address within a minute lock the login
  (`429` with `Retry-After`) until the window passes.
- **Session.** A signed `admin_session` cookie scoped to the panel's path: `HttpOnly`,
  `SameSite=Strict`, eight hours by default, and `Secure` when `ADMIN_COOKIE_SECURE=true`.
- **Read-only.** Create, edit, delete, export and import answer `403`, and there are no custom
  actions.
- The panel is absent from the OpenAPI document. Its NGINX location has its own headers and keeps
  no access log (see [Logging and redaction](#logging-and-redaction)).

## Rate limiting

Checked after validation and before anything is stored or sent (`backend/app/services/rate_limit.py`):

| Endpoint | Key | Default | Purpose |
|----------|-----|---------|---------|
| `POST /api/v1/submissions` | client IP | 5 per 60 s | one client cannot send mail in bulk or burn the Mailjet quota |
| `POST /api/v1/submissions` | destination mailbox | 3 per 10 min | one address cannot be flooded, and probing an address costs time |
| `GET /api/v1/submissions/{tracking_id}/status` | client IP | 60 per 60 s | polling fits comfortably; sweeping candidate tracking ids does not |
| admin login | client IP, failed attempts only | 5 per 60 s | the shared password cannot be guessed at line speed |

- A rejected request answers `429` with `Retry-After`, and creates no row and no Mailjet call.
  Rejections are not counted, so the wait is accurate. Status reads are counted before the lookup,
  so a miss costs as much as a hit. The API limits are configurable through `RATE_LIMIT_*`.
- **Mailbox key.** Surrounding whitespace is trimmed and the domain lower-cased. For Gmail
  (`gmail.com` and `googlemail.com`, treated as one domain), dots in the local part are ignored,
  `+tag` suffixes are folded and case is ignored, so `John.Smith+demo@gmail.com` and
  `johnsmith@gmail.com` share one budget. Every other domain keeps its local part exactly as
  written: `user+one@example.com` and `user+two@example.com` count separately.
- **The key is used for counting only.** It never changes the address that is stored, mailed and
  shown. That address is the validated input; Pydantic's `EmailStr` lower-cases its domain and
  keeps the local part as typed.
- **Client IP.** NGINX overwrites `X-Forwarded-For` with the connecting address, so a client cannot
  spoof it. Uvicorn trusts the header only because `compose.yaml` sets `FORWARDED_ALLOW_IPS=*` for
  a backend whose port is never published. Locally, every browser arrives from the same Docker
  gateway address, so the IP limit acts as one shared bucket.
- The windows live in process memory: exact for one instance, reset on restart. Several instances
  would need a shared store such as Redis.
- Limiting by destination has inherent side effects. A `429` reveals that someone recently
  submitted that address, and a third party can use up an address's budget for ten minutes.

## Public API data and tracking ids

- **Minimal responses.** `POST /api/v1/submissions` returns only `detail` and `tracking_id`. It
  returns no name, address, timestamp field, Mailjet MessageID or other column of the row; the one
  identifier returned is the row's key, by design. The reasons are in
  [architecture.md](architecture.md#minimal-public-responses). The status endpoint returns only
  `{"status": "<state>"}`, sent with `Cache-Control: no-store`.
- **Tracking ids are identifiers, not credentials.** A `tracking_id` is the row's UUIDv7. It is
  hard to enumerate: after its millisecond timestamp come a counter reseeded at random each
  millisecond and 32 fresh random bits. Its creation time is readable from it. Anyone who holds it
  can read that one submission's status, which contains no personal data, and nothing else.
- The status endpoint accepts only the exact canonical UUIDv7 string. Malformed ids, other UUID
  versions and unknown ids share one `404`, so a response never says which of these was sent.
  Guesses are capped by the per-IP limit.
- There is no listing or search in the public API: `GET /api/v1/submissions` is `405`. Records can
  be browsed only in the authenticated admin panel.
- Showing `hard_bounced` does tell the submitter whether a mailbox exists. Probing that way costs a
  real email per try and meets the per-mailbox limit. A deployment that cares could fold the bounce
  states into one public "not delivered" value.
- Provider failures are never passed through. The client gets a short, fixed message (`502`,
  `503`, `500`), never Mailjet's error text, an exception or a stack trace.

## Logging and redaction

The application never logs MySQL passwords or the credentials in `DATABASE_URL`, the Mailjet key
or secret, the admin password or session secret, `Authorization` headers, cookies, session tokens,
request bodies or Mailjet response bodies.

- Recipient addresses are masked to `d***@example.com`.
- A Mailjet error body is reduced to its error codes and field names
  (`error_codes=mj-0013 related_to=To[0].Email`), because its messages quote the address back.
- The database engine runs with `hide_parameters=True`, so a failed statement carries no submitted
  values into the error log.
- `LOG_LEVEL` applies to the application's loggers only. The loggers of libraries such as the
  database driver, SQLAlchemy and httpx stay at `WARNING`, since the driver would otherwise log
  bound parameters. Uvicorn configures its own server loggers.
- The client-controlled path is escaped and length-capped before it is logged, so a decoded
  newline cannot forge a log line.
- **Admin path.**
  - The application's request and error lines show the panel's mount point as `<admin>`, and
    startup logs only `admin_enabled=true`.
  - The image runs Uvicorn with `--no-access-log`, and so does the local workflow in
    [testing.md](testing.md#local-development). Uvicorn's own access log would print raw paths.
  - NGINX keeps no access log for the admin location, so the panel URL and the names, addresses
    and UUIDs an admin searched for are not recorded there. The admin pages send
    `Referrer-Policy: no-referrer`, so requests they cause elsewhere, such as the browser's
    `/favicon.ico`, do not carry the panel URL into the logged locations either.
  - NGINX's error log is still on for that location. If NGINX itself fails a request, for example
    when the backend is unreachable, its error line can contain the request path.
- Tests check the logs of real submissions, admin logins and every Mailjet failure path, and the
  integration test checks the running containers' logs.

## Request IDs

Every request gets an id: the client's `X-Request-ID` when it matches `[A-Za-z0-9._-]{1,64}`,
otherwise a fresh one. A client-supplied value therefore cannot split a header or a log line. The
id is returned in the `X-Request-ID` header of every response, including errors, and prefixes every
log line of that request. A client-supplied id is used for correlation only.

## HTTP and containers

- **CORS** allows only the configured origins, with `GET` and `POST` and the headers `Content-Type`
  and `X-Request-ID`; `*` is rejected at startup. The Docker setup is same-origin and does not rely
  on CORS.
- **Security headers.** NGINX sends `X-Content-Type-Options`, `X-Frame-Options: DENY`, a strict
  `Referrer-Policy` and a Content Security Policy without inline or third-party scripts. The one
  exception is the Swagger UI page at `/docs`. FastAPI's page loads its bundle from
  `cdn.jsdelivr.net` and starts it with an inline script, so that fixed page has a policy of its own
  that allows exactly that. The backend itself sends no security headers; in the uv workflow on
  port 8000 there is no NGINX in front.
- **Input.** It is validated in the browser and again by the API. User values are HTML-escaped in
  the HTML email, and control characters are rejected in names.
- **Containers.** Every container runs with `no-new-privileges`. The backend, migration and
  frontend processes run as non-root users with all capabilities dropped. The official MySQL image
  keeps its documented behaviour: its entrypoint starts as root, then runs `mysqld` as the `mysql`
  user.

## Email authentication and deliverability

Emails are sent through Mailjet from a demo sender on a public email domain. This project does not
control that domain's DNS, so SPF, DKIM and DMARC cannot be aligned for the demo sender. Receiving
providers may therefore place the message in Spam/Junk or temporarily reject it; the page tells the
person to check Spam/Junk.

In production: send from a custom domain you control, verify it in Mailjet and publish the SPF,
DKIM and DMARC records Mailjet provides. That authenticates the sender and improves placement
considerably, but Inbox placement remains the receiving provider's decision and cannot be
guaranteed.
