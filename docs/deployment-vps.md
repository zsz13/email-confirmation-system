# Deploying to a VPS

The production stack runs the application on one server with Docker Compose at
**https://email.zsz13.com**, with a Let's Encrypt certificate and HTTP Basic Auth in front of the
whole site. It is defined in [`docker-compose.prod.yml`](../docker-compose.prod.yml) and is
separate from the local stack in `compose.yaml`, which it does not change.

Every command below runs from the repository root on the server. They all start with the same
prefix, so a shell alias saves typing:

```bash
alias dcp='docker compose -f docker-compose.prod.yml --env-file .env.prod'
```

The commands are written out in full here.

## Architecture

```
Internet ── :80 ──▶ nginx ─┬─ /.well-known/acme-challenge/  ─▶ certbot's webroot (no auth)
                           └─ everything else                ─▶ 301 https://email.zsz13.com/…
         ── :443 ─▶ nginx: TLS, Basic Auth on every path
                           ├─ /api/, /openapi.json, /docs, admin path ─▶ backend:8000 ─▶ mysql:3306
                           └─ everything else                         ─▶ frontend:8080 (Angular build)
```

| Service | Role | Published |
|---------|------|-----------|
| `nginx` | Edge proxy ([`deploy/nginx.conf.template`](../deploy/nginx.conf.template)): ACME challenge, HTTPS redirect, TLS, Basic Auth, routing | **80, 443** |
| `frontend` | The Angular build on unprivileged NGINX (the same image as locally) | no |
| `backend` | FastAPI, non-root, with the Mailjet and admin settings from `.env.prod` | no |
| `migrate` | `alembic upgrade head` on every `up`, then exits | no |
| `mysql` | MySQL 8.4; the backend and migrations connect as `MYSQL_USER`, never root | no |
| `basic-auth` | One-shot: writes the bcrypt htpasswd file from `.env.prod` ([`deploy/basic-auth.sh`](../deploy/basic-auth.sh)) | no network |
| `certbot` | Issues and renews the certificate and publishes it to NGINX ([`deploy/certbot.sh`](../deploy/certbot.sh)) | no |

The edge NGINX includes the frontend image's own header file and backend locations
(`frontend/nginx-headers.conf`, `frontend/nginx-backend-locations.conf`). The security headers,
the Swagger UI and admin policies, and the admin panel's unlogged location are therefore the
same as locally. HSTS is added on top. Start-up order: MySQL → migrations → backend → frontend;
`basic-auth` and `certbot` get ready in parallel; NGINX starts last.

Named volumes (prefixed `email-confirmation-prod_`):

| Volume | Holds |
|--------|-------|
| `mysql_data` | the database |
| `letsencrypt` | the Let's Encrypt account, certificates and renewal settings |
| `tls` | the certificate and key NGINX serves (copies owned by NGINX's user) |
| `acme_webroot` | HTTP-01 challenge files while a certificate is being issued |
| `basic_auth` | the htpasswd file (a bcrypt hash only) |

## 1. Server prerequisites

- A Linux VPS with a public IPv4 address, **Docker Engine 28 or newer** and the **Compose v2
  plugin** ([install guide](https://docs.docker.com/engine/install/)), and `git`. From Engine 28,
  other machines on the provider's network cannot route to the containers' internal addresses.
- Ports **22** (SSH), **80** and **443** reachable from the internet (next two steps).

## 2. DNS A record

At the DNS provider of `zsz13.com`, create:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| `A` | `email` | the VPS's public IPv4 address | 300 |

Check it before the first deploy, from any machine:

```bash
dig +short email.zsz13.com A      # must print the VPS address
```

Do not add an `AAAA` record unless the server serves IPv6 to Docker. Let's Encrypt prefers
IPv6 when an `AAAA` record exists, and validation fails if that address does not answer.

## 3. Firewall: ports 22, 80, 443

Open exactly these three, both in the provider's firewall (security group) if there is one and
on the host, for example with `ufw`:

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

Port 80 must stay open after the first deploy: Let's Encrypt validates every renewal over it,
and it redirects browsers to HTTPS. Docker writes its own iptables rules for published ports,
bypassing `ufw`. That is why the stack publishes only 80 and 443: MySQL (3306), the backend
(8000) and the frontend (8080) have no published port at all, so there is nothing a firewall
could miss. To check on the server:

```bash
sudo ss -tlnp    # public listeners: sshd on 22, docker-proxy on 80 and 443, nothing else from the stack
```

## 4. Get the code and create `.env.prod`

```bash
git clone https://github.com/zsz13/email-confirmation-system.git
cd email-confirmation-system
cp .env.prod.example .env.prod
chmod 600 .env.prod
```

Edit `.env.prod` and replace every `change-me`. `.env.prod` is ignored by Git and is read only
by Compose; no image contains it. Generate each secret on the server:

```bash
openssl rand -hex 32
```

| Variable | Value |
|----------|-------|
| `DOMAIN` | `email.zsz13.com` |
| `LETSENCRYPT_EMAIL` | a real address you read; certbot registers the Let's Encrypt account with it |
| `MYSQL_ROOT_PASSWORD` | generated; used only by the MySQL image's own bootstrap |
| `MYSQL_DATABASE`, `MYSQL_USER` | keep `email_confirmation` and `email_app` |
| `MYSQL_PASSWORD` | generated, **URL-safe** (hex is): it is substituted into the database URL |
| `MAILJET_API_KEY`, `MAILJET_SECRET_KEY` | from https://app.mailjet.com/account/apikeys |
| `MAILJET_SENDER_EMAIL`, `MAILJET_SENDER_NAME` | a sender address verified in that Mailjet account, and its display name |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_SESSION_SECRET` | the admin panel login and its cookie-signing secret; generated, never the demo values (the backend refuses those in production) |
| `BASIC_AUTH_USERNAME`, `BASIC_AUTH_PASSWORD` | the credentials for the whole site; the password generated |

MySQL stores its credentials in `mysql_data` on the very first start. Changing a `MYSQL_*` value
later has no effect on the existing database. Compose prefers a variable exported in the shell over
the same name in `.env.prod`, so do not export these names in the server's shell.

## 5. First deploy and certificate issuance

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

The first run builds both images, which takes a few minutes. There is no separate issuance step:

1. MySQL starts, the migrations run, then the backend and the frontend start.
2. `basic-auth` writes the htpasswd file and exits.
3. `certbot` finds no certificate yet. It publishes a temporary self-signed one, valid for 7 days,
   so NGINX can start at all, and becomes healthy.
4. NGINX starts on 80 and 443. HTTPS works but browsers warn about the temporary certificate.
5. `certbot` waits until NGINX serves the challenge path, then requests the certificate from
   Let's Encrypt with the HTTP-01 challenge over port 80. Its deploy hook copies the
   certificate to the `tls` volume.
6. Within a minute NGINX notices the new certificate and reloads gracefully. The warning is gone.

Follow it:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f certbot
# certbot.sh: no certificate for email.zsz13.com yet; NGINX starts with a temporary self-signed one
# certbot.sh: waiting for NGINX to serve the challenge path
# certbot.sh: requesting a certificate for email.zsz13.com
# Hook 'deploy-hook' ran with output:
#  certbot.sh: published the certificate for email.zsz13.com
# Successfully received certificate.
```

Then check the result:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod ps -a
#   mysql, backend, frontend, certbot, nginx: Up (healthy); migrate, basic-auth: Exited (0)
docker compose -f docker-compose.prod.yml --env-file .env.prod logs nginx | grep 'certificate changed'

echo | openssl s_client -connect email.zsz13.com:443 -servername email.zsz13.com 2>/dev/null \
  | openssl x509 -noout -issuer -dates                          # issuer: Let's Encrypt
curl -sI http://email.zsz13.com/ | grep -i -E '^(HTTP|location)'  # 301, location: https://email.zsz13.com/
curl -s -o /dev/null -w '%{http_code}\n' https://email.zsz13.com/          # 401
curl -s -o /dev/null -w '%{http_code}\n' -u "<BASIC_AUTH_USERNAME>" https://email.zsz13.com/api/v1/health
                                                                # prompts for the password, then 200
```

Then open https://email.zsz13.com, sign in with the Basic Auth credentials and submit the form.

**If issuance fails**, `certbot` logs `issuance failed … retrying in 1 h` and keeps the temporary
certificate. The usual causes are a missing or wrong `A` record, a closed port 80, or an `AAAA`
record. Fix the cause, then retry at once:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod restart certbot
```

Let's Encrypt allows 5 failed validations per host name per hour, so fix the cause before
retrying. To test the whole path against Let's Encrypt's staging server without touching the
real certificate, run a dry run while the stack is up:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm --entrypoint certbot certbot \
  certonly --dry-run --webroot --webroot-path /var/www/certbot --domain email.zsz13.com \
  --email "<LETSENCRYPT_EMAIL>" --agree-tos --no-eff-email --non-interactive
```

## Basic Auth

- **Everything on HTTPS requires it**: the Angular app, `/api/`, `/docs`, `/openapi.json` and the
  admin path. Without valid credentials NGINX answers `401` and the browser shows its login
  prompt. After one sign-in, the browser sends the credentials with every request, including the
  page's API calls and its status polling.
- **Port 80 never asks for credentials.** It serves only the ACME challenge files and redirects
  everything else to HTTPS, so credentials never travel over plain HTTP.
- **The admin panel has two gates**: Basic Auth, then its own login (`ADMIN_USERNAME` /
  `ADMIN_PASSWORD`) with a `Secure` session cookie.
- **Storage.** On every `up`, the one-shot `basic-auth` container turns `BASIC_AUTH_USERNAME` and
  `BASIC_AUTH_PASSWORD` into a bcrypt hash (cost 10) in the `basic_auth` volume. The password is
  passed on stdin, never as a process argument, and is never printed. The htpasswd file exists
  only in that volume, never in the repository or an image.
- **Refusals.** Compose will not start while either value is missing or empty and names the
  variable. `basic-auth` exits with an error while either is still `change-me`, and NGINX then
  does not start.
- **Changing the credentials**: edit `.env.prod` and run the `up -d` command again. NGINX reads
  the file on each request, so the change applies at once without a restart.
- **Logs.** The access log uses a format without the user name. The `Authorization` header is
  removed before requests reach the frontend or the backend. NGINX's error log records each
  failed attempt with the user name that was tried (never a password); that is the trail of
  guessing attempts.
- **Rate limit.** NGINX checks the bcrypt hash on every request, about 90 ms of CPU at cost 10.
  So HTTPS allows each client address 5 requests a second with bursts of 40, counted before the
  credentials are checked; more gets `429`. A page load stays well inside that, and password
  guessing is slowed to the same pace. Choose a user name that is not guessable either.

## Logs

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod ps -a
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f nginx      # access log and errors
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f backend    # application log, request ids
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail 100 certbot
```

The admin location keeps no NGINX access log, and the backend logs its path as `<admin>`, as
locally. Every container's log is rotated by Docker at 10 MB × 3 files, so logs cannot fill the
disk.

## Update and redeploy

```bash
cd email-confirmation-system
git pull
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.prod restart nginx certbot
docker image prune -f
```

`up -d --build` rebuilds the images, recreates only the containers that changed and runs
`migrate` first, so schema changes apply before the new backend starts. There is a short
interruption while the backend restarts. The edge NGINX and `certbot` read their configuration
from bind-mounted files (`deploy/`, `frontend/nginx-*.conf`), which a rebuild does not replace.
The `restart` makes them pick up changes to those files, and is harmless when there are none.
`.env.prod` changes also apply with `up -d`.

## Certificate renewal

Renewal is automatic and needs no cron job. The `certbot` service runs `certbot renew` twice a
day. Certbot renews when a certificate is due, about 30 days before expiry for Let's Encrypt's
90-day certificates. The deploy hook stored with the certificate publishes the renewed pair to
the `tls` volume, and NGINX reloads gracefully within a minute. NGINX reloads only when the
certificate actually changed, so a renewal that fails leaves the current certificate in place.

```bash
# Certificate names, domains and expiry dates
docker compose -f docker-compose.prod.yml --env-file .env.prod exec certbot certbot certificates

# Rehearse a renewal against the staging server; nothing is replaced
docker compose -f docker-compose.prod.yml --env-file .env.prod exec certbot certbot renew --dry-run

# Renew now, e.g. after a key compromise. Counts against Let's Encrypt's limit of 5 identical
# certificates a week. The deploy hook publishes it, and NGINX reloads within a minute:
docker compose -f docker-compose.prod.yml --env-file .env.prod exec certbot certbot renew --force-renewal
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --since 5m nginx | grep 'certificate changed'

# What browsers are actually served
echo | openssl s_client -connect email.zsz13.com:443 -servername email.zsz13.com 2>/dev/null \
  | openssl x509 -noout -issuer -dates
```

Run these `exec` commands from a terminal. Without one (`exec -T`, cron), `certbot renew` first
waits a random time of up to 8 minutes, as Let's Encrypt asks of unattended clients. If a command
reports that another instance of Certbot is already running, the service's own twice-daily check
is in progress; run it again a few minutes later. Let's
Encrypt no longer sends expiry reminder emails, so `certbot certificates` (or the `openssl`
check) is the way to confirm the expiry date moves forward.

## MySQL persistence

The database lives in the named volume `email-confirmation-prod_mysql_data`. It survives
container restarts, rebuilds, redeploys, `docker compose down` and server reboots. Every
long-running service has `restart: unless-stopped`, so the stack comes back after a reboot. The
certificates live in `letsencrypt` in the same way.

`docker compose -f docker-compose.prod.yml --env-file .env.prod down -v` **deletes the volumes**:
every submission and the certificates, and new certificates are rate-limited. Never run it on
the server unless that is the intent; plain `down` keeps everything.

## Backup and restore

A consistent dump taken as the application user, without stopping anything:

```bash
(umask 077 && docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T mysql \
  sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump --single-transaction --no-tablespaces -u"$MYSQL_USER" "$MYSQL_DATABASE"' \
  | gzip > "backup-$(date +%F-%H%M).sql.gz")
```

The password is read inside the container from its own environment. It never appears on the
command line or in shell history. The dump holds names and email addresses, so `umask 077`
creates it readable by its owner only; copy it off the server. To restore it into the running
stack:

```bash
gunzip -c backup-YYYY-MM-DD-HHMM.sql.gz | docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T mysql \
  sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u"$MYSQL_USER" "$MYSQL_DATABASE"'
```

For a daily backup, put the dump command in a script and schedule it with `cron`.

## Troubleshooting

| Symptom | Where to look |
|---------|---------------|
| Browser warns about the certificate | still the temporary one: `logs certbot` (see *If issuance fails*) |
| `nginx` never starts | `ps -a`: a failed `basic-auth` prints why (empty or `change-me` values); `certbot` must be healthy |
| New certificates are not picked up | `logs nginx` says `Ignoring /docker-entrypoint.d/40-reload-on-certificate-change.sh, not executable`: the checkout lost the file mode. `chmod +x deploy/*.sh`, then `restart nginx` |
| `429 Too Many Requests` | more than 5 requests a second from one address for a while; wait a few seconds |
| `502 Bad Gateway` | the backend or frontend is down or restarting: `ps -a`, `logs backend` |
| Backend restarts in a loop | `logs backend`: a missing Mailjet value, or `ADMIN_*` still set to the demo values |
| Emails are not delivered | `logs backend` and the delivery status on the page; the sender must be verified in Mailjet |
